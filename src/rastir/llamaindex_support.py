"""LlamaIndex integration for Rastir.

Provides ``llamaindex_agent`` — a single decorator that instruments a
LlamaIndex agent workflow.  The decorator:

  1. Scans function arguments for LlamaIndex agent objects
  2. Wraps the agent's LLM for per-call tracing
  3. Wraps existing tools (local or MCP — doesn't matter) for
     per-invocation tracing
  4. Creates an ``@agent`` span around the entire run

MCP tools are handled natively by LlamaIndex via ``llama-index-tools-mcp``
(``McpToolSpec.to_tool_list_async()``).  By the time the agent is created,
MCP tools are already regular ``FunctionTool`` objects — Rastir wraps them
for observability like any other tool.

Usage::

    from rastir import configure, llamaindex_agent

    configure(service="my-app", push_url="http://localhost:8080")

    @llamaindex_agent(agent_name="qa_agent")
    def run(agent):
        return agent.chat("Hello")

No LlamaIndex import is performed at module scope — detection uses
class-name / module inspection only.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any, Callable, TypeVar

from rastir.wrapper import wrap

logger = logging.getLogger("rastir")

F = TypeVar("F", bound=Callable[..., Any])


# ---------------------------------------------------------------------------
# Agent detection helpers
# ---------------------------------------------------------------------------

# LlamaIndex agent class names we recognise
_LI_AGENT_CLASS_NAMES = frozenset({
    "ReActAgent",
    "OpenAIAgent",
    "FunctionAgent",
    "FunctionCallingAgent",
    "StructuredPlannerAgent",
    "AgentRunner",
    "BaseAgent",
})


def _is_llamaindex_agent(obj: Any) -> bool:
    """True if ``obj`` looks like a LlamaIndex agent instance."""
    cls = type(obj)
    module = getattr(cls, "__module__", "") or ""
    name = cls.__name__

    # Direct match on known agent classes
    if name in _LI_AGENT_CLASS_NAMES and "llama_index" in module:
        return True

    # Walk MRO to catch subclasses of BaseAgent
    for base in cls.__mro__:
        base_mod = getattr(base, "__module__", "") or ""
        if base.__name__ in _LI_AGENT_CLASS_NAMES and "llama_index" in base_mod:
            return True

    return False


def _get_agent_tools(agent: Any) -> list:
    """Extract tools from a LlamaIndex agent."""
    # ReActAgent / FunctionCallingAgent keep tools in _tools or tools
    tools = getattr(agent, "_tools", None)
    if tools is None:
        tools = getattr(agent, "tools", None)
    return list(tools) if tools else []


def _set_agent_tools(agent: Any, tools: list) -> None:
    """Set tools on a LlamaIndex agent."""
    if hasattr(agent, "_tools"):
        agent._tools = tools
    elif hasattr(agent, "tools"):
        agent.tools = tools


# ---------------------------------------------------------------------------
# llamaindex_agent decorator
# ---------------------------------------------------------------------------

def llamaindex_agent(
    func: F | None = None,
    *,
    agent_name: str | None = None,
) -> F | Callable[[F], F]:
    """Decorator that instruments a LlamaIndex agent call.

    Wraps agent LLMs and tools for per-call observability and creates
    an ``@agent`` span around execution.  MCP tools are handled
    natively by LlamaIndex (``McpToolSpec.to_tool_list_async()``) and
    are wrapped the same as any local tool.

    Args:
        agent_name: Name for the outer agent span.  Defaults to the
            function name.

    Usage::

        @llamaindex_agent(agent_name="research")
        def run(agent):
            return agent.chat("What is 2+2?")

        @llamaindex_agent
        async def run(agent):
            return await agent.arun("List files in /tmp")
    """

    def decorator(fn: F) -> F:
        resolved_name = agent_name or fn.__name__

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return _llamaindex_agent_impl(fn, resolved_name, args, kwargs)

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            return await _async_llamaindex_agent_impl(
                fn, resolved_name, args, kwargs
            )

        if asyncio.iscoroutinefunction(fn):
            return async_wrapper  # type: ignore[return-value]
        return wrapper  # type: ignore[return-value]

    if func is not None:
        return decorator(func)
    return decorator  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------

def _llamaindex_agent_impl(
    fn: Callable,
    agent_name: str,
    args: tuple,
    kwargs: dict,
) -> Any:
    """Sync implementation of llamaindex_agent."""
    from rastir.context import (
        end_span, start_span, set_current_agent, reset_current_agent,
    )
    from rastir.queue import enqueue_span
    from rastir.spans import SpanStatus, SpanType

    span, token = start_span(agent_name, SpanType.AGENT)
    span.set_attribute("agent_name", agent_name)
    agent_token = set_current_agent(agent_name)

    originals: dict[int, dict[str, Any]] = {}

    try:
        for obj in (*args, *kwargs.values()):
            if _is_llamaindex_agent(obj):
                _wrap_agent_internals(obj, originals)

        result = fn(*args, **kwargs)
        span.finish(SpanStatus.OK)
        return result
    except BaseException as exc:
        span.record_error(exc)
        span.finish(SpanStatus.ERROR)
        raise
    finally:
        _restore_originals(originals)
        reset_current_agent(agent_token)
        end_span(token)
        enqueue_span(span)


async def _async_llamaindex_agent_impl(
    fn: Callable,
    agent_name: str,
    args: tuple,
    kwargs: dict,
) -> Any:
    """Async implementation of llamaindex_agent."""
    from rastir.context import (
        end_span, start_span, set_current_agent, reset_current_agent,
    )
    from rastir.queue import enqueue_span
    from rastir.spans import SpanStatus, SpanType

    span, token = start_span(agent_name, SpanType.AGENT)
    span.set_attribute("agent_name", agent_name)
    agent_token = set_current_agent(agent_name)

    originals: dict[int, dict[str, Any]] = {}

    try:
        for obj in (*args, *kwargs.values()):
            if _is_llamaindex_agent(obj):
                _wrap_agent_internals(obj, originals)

        result = await fn(*args, **kwargs)
        span.finish(SpanStatus.OK)
        return result
    except BaseException as exc:
        span.record_error(exc)
        span.finish(SpanStatus.ERROR)
        raise
    finally:
        _restore_originals(originals)
        reset_current_agent(agent_token)
        end_span(token)
        enqueue_span(span)


# ---------------------------------------------------------------------------
# Internal wrapping / restore
# ---------------------------------------------------------------------------

def _wrap_agent_internals(
    agent: Any,
    originals: dict[int, dict[str, Any]],
) -> None:
    """Wrap LLM and tools on a LlamaIndex agent.

    Stores original values for cleanup in ``originals``.
    """
    agent_id = id(agent)
    originals[agent_id] = {"_agent_ref": agent}

    # --- Wrap LLM ---
    llm = getattr(agent, "_llm", None) or getattr(agent, "llm", None)
    if llm is not None and not getattr(llm, "_rastir_wrapped", False):
        originals[agent_id]["llm"] = llm
        originals[agent_id]["llm_attr"] = (
            "_llm" if hasattr(agent, "_llm") else "llm"
        )
        wrapped_llm = wrap(
            llm,
            name=f"llamaindex.{type(agent).__name__}.llm",
            span_type="llm",
            include=["chat", "complete", "achat", "acomplete",
                     "stream_chat", "stream_complete",
                     "astream_chat", "astream_complete"],
        )
        setattr(agent, originals[agent_id]["llm_attr"], wrapped_llm)

    # --- Wrap existing tools (local or MCP — all are FunctionTool) ---
    existing_tools = _get_agent_tools(agent)
    originals[agent_id]["tools"] = existing_tools
    wrapped_tools = []
    for t in existing_tools:
        if not getattr(t, "_rastir_wrapped", False):
            tool_name = getattr(t, "metadata", None)
            if tool_name and hasattr(tool_name, "name"):
                tool_name = tool_name.name
            else:
                tool_name = getattr(t, "name", None) or "tool"
            wrapped_tools.append(
                wrap(t, name=tool_name, span_type="tool",
                     include=["call", "__call__"])
            )
        else:
            wrapped_tools.append(t)

    _set_agent_tools(agent, wrapped_tools)


def _restore_originals(originals: dict[int, dict[str, Any]]) -> None:
    """Restore original LLMs and tools on agents after execution."""
    for _agent_id, saved in originals.items():
        ag = saved.get("_agent_ref")
        if ag is None:
            continue
        if "llm" in saved:
            attr = saved.get("llm_attr", "llm")
            setattr(ag, attr, saved["llm"])
        if "tools" in saved:
            _set_agent_tools(ag, saved["tools"])
