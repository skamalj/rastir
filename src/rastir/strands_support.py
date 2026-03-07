"""AWS Strands Agents integration for Rastir.

Provides ``strands_agent`` — a single decorator that instruments a
Strands Agent call.  The decorator:

  1. Scans function arguments for Strands ``Agent`` objects
  2. Wraps the agent's ``model.stream`` for LLM span tracing
  3. Wraps each tool's ``stream`` method for tool span tracing
  4. Creates an ``@agent`` span around the entire execution

Strands agents are invoked via ``agent(prompt)`` (sync) or
``agent.invoke_async(prompt)`` (async), using ``model.stream()``
for LLM calls and ``tool.stream()`` for tool invocations.

Usage::

    from rastir import configure, strands_agent

    configure(service="my-app", push_url="http://localhost:8080")

    @strands_agent(agent_name="my_strands_agent")
    def run(agent, prompt):
        return agent(prompt)

No Strands import is performed at module scope — detection uses
class-name / module inspection only.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any, Callable, TypeVar

from rastir.remote import discover_mcp_client, inject_traceparent_into_mcp_clients
from rastir.wrapper import wrap

logger = logging.getLogger("rastir")

F = TypeVar("F", bound=Callable[..., Any])


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------

def _is_strands_agent(obj: Any) -> bool:
    """True if *obj* looks like a Strands ``Agent``."""
    cls = type(obj)
    for base in cls.__mro__:
        module = getattr(base, "__module__", "") or ""
        if base.__name__ in ("Agent", "AgentBase") and "strands" in module:
            return True
    return False


def _get_model_name(agent: Any) -> str:
    """Extract the model name from a Strands agent."""
    model = getattr(agent, "model", None)
    if model is None:
        return "unknown"
    if isinstance(model, str):
        return model
    # Check for model_id (BedrockModel) or model_name
    for attr in ("model_id", "model_name", "model"):
        val = getattr(model, attr, None)
        if val and isinstance(val, str):
            return val
    return type(model).__name__


# ---------------------------------------------------------------------------
# strands_agent decorator
# ---------------------------------------------------------------------------

def strands_agent(
    func: F | None = None,
    *,
    agent_name: str | None = None,
) -> F | Callable[[F], F]:
    """Decorator that instruments a Strands Agent call.

    Wraps the agent's model and tools for per-call observability, and
    creates an ``@agent`` span around execution.

    Args:
        agent_name: Name for the outer agent span.  Defaults to the
            function name.

    Usage::

        @strands_agent(agent_name="researcher")
        def run(agent, prompt):
            return agent(prompt)
    """

    def decorator(fn: F) -> F:
        resolved_name = agent_name or fn.__name__

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return _strands_agent_impl(fn, resolved_name, args, kwargs)

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            return await _async_strands_agent_impl(fn, resolved_name, args, kwargs)

        if asyncio.iscoroutinefunction(fn):
            return async_wrapper  # type: ignore[return-value]
        return wrapper  # type: ignore[return-value]

    if func is not None:
        return decorator(func)
    return decorator  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------

def _strands_agent_impl(
    fn: Callable,
    agent_name: str,
    args: tuple,
    kwargs: dict,
) -> Any:
    """Sync implementation of strands_agent."""
    from rastir.context import (
        end_span, start_span, set_current_agent, reset_current_agent,
    )
    from rastir.queue import enqueue_span
    from rastir.spans import SpanStatus, SpanType

    span, token = start_span(agent_name, SpanType.AGENT)
    span.set_attribute("agent_name", agent_name)
    agent_token = set_current_agent(agent_name)

    originals: dict[int, dict[str, Any]] = {}
    mcp_clients: list[Any] = []

    try:
        for obj in (*args, *kwargs.values()):
            if _is_strands_agent(obj):
                _wrap_strands_internals(obj, originals)
            mc = discover_mcp_client(obj)
            if mc is not None:
                mcp_clients.append(mc)

        _walk_func_for_mcp_clients(fn, mcp_clients)
        inject_traceparent_into_mcp_clients(mcp_clients)

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


async def _async_strands_agent_impl(
    fn: Callable,
    agent_name: str,
    args: tuple,
    kwargs: dict,
) -> Any:
    """Async implementation of strands_agent."""
    from rastir.context import (
        end_span, start_span, set_current_agent, reset_current_agent,
    )
    from rastir.queue import enqueue_span
    from rastir.spans import SpanStatus, SpanType

    span, token = start_span(agent_name, SpanType.AGENT)
    span.set_attribute("agent_name", agent_name)
    agent_token = set_current_agent(agent_name)

    originals: dict[int, dict[str, Any]] = {}
    mcp_clients: list[Any] = []

    try:
        for obj in (*args, *kwargs.values()):
            if _is_strands_agent(obj):
                _wrap_strands_internals(obj, originals)
            mc = discover_mcp_client(obj)
            if mc is not None:
                mcp_clients.append(mc)

        _walk_func_for_mcp_clients(fn, mcp_clients)
        inject_traceparent_into_mcp_clients(mcp_clients)

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
# Internal wrapping
# ---------------------------------------------------------------------------

def _wrap_strands_internals(
    agent: Any,
    originals: dict[int, dict[str, Any]],
) -> None:
    """Wrap model and tools on a Strands agent for observability."""
    agent_id = id(agent)
    if agent_id in originals:
        return
    originals[agent_id] = {"_agent_ref": agent}

    # --- Wrap model (LLM tracing) ---
    model = getattr(agent, "model", None)
    if model is not None and not getattr(model, "_rastir_wrapped", False):
        originals[agent_id]["model"] = model
        model_name = _get_model_name(agent)
        agent.model = wrap(
            model,
            name=f"strands.{model_name}",
            span_type="llm",
            include=["stream"],
        )

    # --- Wrap tools (tool tracing) ---
    # Tools live in agent.tool_registry.registry: dict[str, AgentTool]
    registry = getattr(getattr(agent, "tool_registry", None), "registry", None)
    if registry:
        from rastir.wrapper import _make_sync_wrapper
        from rastir.spans import SpanType

        patched_tools: list[tuple[Any, str, Any]] = []  # (tool, attr_name, orig)
        for tool_name, tool in registry.items():
            if getattr(tool, "_rastir_tool_patched", False):
                continue
            # Patch tool.stream in-place via __dict__ to bypass ABC
            original_stream = tool.stream
            span_name = f"strands.tool.{tool_name}"
            wrapped_stream = _make_sync_wrapper(
                original_stream, span_name, SpanType.TOOL, wrapped_obj=tool,
            )
            tool.__dict__["stream"] = wrapped_stream
            tool.__dict__["_rastir_tool_patched"] = True
            patched_tools.append((tool, "stream", original_stream))
        originals[agent_id]["_patched_tools"] = patched_tools


def _restore_originals(originals: dict[int, dict[str, Any]]) -> None:
    """Restore original model and tools on agents after execution."""
    for _id, saved in originals.items():
        ag = saved.get("_agent_ref")
        if ag is None:
            continue
        if "model" in saved:
            ag.model = saved["model"]
        # Restore patched tool methods
        for tool, attr_name, _orig in saved.get("_patched_tools", []):
            tool.__dict__.pop(attr_name, None)
            tool.__dict__.pop("_rastir_tool_patched", None)


def _walk_func_for_mcp_clients(
    func: Any, mcp_clients: list[Any],
) -> None:
    """Walk a function's closures and globals for MCP client objects."""
    seen: set[int] = set()

    closure = getattr(func, "__closure__", None)
    if closure:
        for cell in closure:
            try:
                val = cell.cell_contents
            except ValueError:
                continue
            if id(val) not in seen:
                seen.add(id(val))
                mc = discover_mcp_client(val)
                if mc is not None:
                    mcp_clients.append(mc)

    code = getattr(func, "__code__", None)
    func_globals = getattr(func, "__globals__", None)
    if code is not None and func_globals is not None:
        for varname in code.co_names:
            val = func_globals.get(varname)
            if val is None or id(val) in seen:
                continue
            seen.add(id(val))
            mc = discover_mcp_client(val)
            if mc is not None:
                mcp_clients.append(mc)
