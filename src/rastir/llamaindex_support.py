"""LlamaIndex integration for Rastir.

Provides ``llamaindex_agent`` — a single decorator that instruments a
LlamaIndex agent workflow.  The decorator:

  1. Scans function arguments for LlamaIndex agent objects
  2. Wraps the agent's LLM for per-call tracing
  3. Wraps existing tools for per-invocation tracing
  4. Optionally injects MCP tools (converted to LlamaIndex
     ``FunctionTool`` objects) into the agent
  5. Creates an ``@agent`` span around the entire run

Usage::

    from rastir import configure, llamaindex_agent, wrap_mcp

    configure(service="my-app", push_url="http://localhost:8080")

    # Without MCP:
    @llamaindex_agent(agent_name="qa_agent")
    def run(agent):
        return agent.chat("Hello")

    # With MCP:
    session = wrap_mcp(mcp_session)

    @llamaindex_agent(agent_name="qa_agent", mcp=session)
    async def run(agent):
        return agent.chat("What files are in /tmp?")

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

# MCP JSON Schema type → Python type mapping
_JSON_TYPE_MAP: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


# ---------------------------------------------------------------------------
# MCP-to-LlamaIndex tool bridge
# ---------------------------------------------------------------------------

def _build_llamaindex_tools_from_mcp(session: Any, tools: list) -> list:
    """Convert MCP tool descriptors into LlamaIndex ``FunctionTool`` objects.

    Each generated tool calls ``session.call_tool(name, args)`` — which,
    if the session is a ``wrap_mcp`` proxy, automatically injects trace
    context.

    Args:
        session: An MCP ``ClientSession`` (ideally wrapped with
            ``wrap_mcp``).
        tools: List of MCP ``Tool`` objects from ``session.list_tools()``.

    Returns:
        List of ``FunctionTool`` instances ready for agent use.
    """
    try:
        from llama_index.core.tools import FunctionTool
    except ImportError:
        logger.warning(
            "llama-index-core not installed — cannot convert MCP tools "
            "to LlamaIndex tools"
        )
        return []

    li_tools: list[Any] = []

    for mcp_tool in tools:
        tool_name = getattr(mcp_tool, "name", None) or str(mcp_tool)
        tool_desc = getattr(mcp_tool, "description", None) or tool_name

        def _make_fn(sess: Any, tname: str) -> Callable:
            """Build a sync callable that invokes the MCP tool."""

            def fn(**kwargs: Any) -> str:
                coro = sess.call_tool(tname, kwargs)
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None

                if loop and loop.is_running():
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor(1) as pool:
                        result = pool.submit(asyncio.run, coro).result()
                else:
                    result = asyncio.run(coro)

                # Extract text content from MCP CallToolResult
                if hasattr(result, "content"):
                    parts = []
                    for item in result.content:
                        text = getattr(item, "text", None)
                        if text is not None:
                            parts.append(text)
                    return "\n".join(parts) if parts else str(result)
                return str(result)

            fn.__name__ = tname
            fn.__doc__ = tool_desc
            return fn

        async def _make_async_fn(sess: Any, tname: str) -> Callable:
            """Build an async callable that invokes the MCP tool."""

            async def fn(**kwargs: Any) -> str:
                result = await sess.call_tool(tname, kwargs)
                if hasattr(result, "content"):
                    parts = []
                    for item in result.content:
                        text = getattr(item, "text", None)
                        if text is not None:
                            parts.append(text)
                    return "\n".join(parts) if parts else str(result)
                return str(result)

            fn.__name__ = tname
            fn.__doc__ = tool_desc
            return fn

        li_tools.append(FunctionTool.from_defaults(
            fn=_make_fn(session, tool_name),
            name=tool_name,
            description=tool_desc,
        ))

    return li_tools


# ---------------------------------------------------------------------------
# Agent detection helpers
# ---------------------------------------------------------------------------

# LlamaIndex agent class names we recognise
_LI_AGENT_CLASS_NAMES = frozenset({
    "ReActAgent",
    "OpenAIAgent",
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
    mcp: Any | None = None,
) -> F | Callable[[F], F]:
    """Decorator that instruments a LlamaIndex agent call.

    Wraps agent LLMs and tools for per-call observability, optionally
    injects MCP tools, and creates an ``@agent`` span around execution.

    Args:
        agent_name: Name for the outer agent span.  Defaults to the
            function name.
        mcp: MCP session(s) to convert into LlamaIndex tools and
            inject into agents.  Accepts:

            - A single session → tools injected into **all** agents
            - A list of sessions → tools from all sessions merged
            - A dict mapping agent class name (str) to session

    Usage::

        @llamaindex_agent(agent_name="research")
        def run(agent):
            return agent.chat("What is 2+2?")

        @llamaindex_agent(agent_name="research", mcp=wrapped_session)
        async def run(agent):
            return agent.chat("List files in /tmp")
    """

    def decorator(fn: F) -> F:
        resolved_name = agent_name or fn.__name__

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return _llamaindex_agent_impl(
                fn, resolved_name, mcp, args, kwargs
            )

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            return await _async_llamaindex_agent_impl(
                fn, resolved_name, mcp, args, kwargs
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
    mcp: Any | None,
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
                _wrap_agent_internals(obj, mcp, originals)

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
    mcp: Any | None,
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
                _wrap_agent_internals(obj, mcp, originals)

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

def _resolve_mcp_tools(
    agent: Any,
    mcp: Any | None,
    cache: dict[int, list],
) -> list:
    """Resolve MCP tools for a specific LlamaIndex agent.

    Handles single session, list-of-sessions, and name→session dict.
    """
    if mcp is None:
        return []

    sessions: list[Any] = []

    if isinstance(mcp, dict):
        # {"AgentClassName": session} or {"agent_name": session}
        cls_name = type(agent).__name__
        session = mcp.get(cls_name)
        if session is not None:
            sessions = [session]
    elif isinstance(mcp, (list, tuple)):
        sessions = list(mcp)
    else:
        sessions = [mcp]

    all_tools: list[Any] = []
    for session in sessions:
        sid = id(session)
        if sid not in cache:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop and loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(1) as pool:
                    tools_result = pool.submit(
                        asyncio.run, session.list_tools()
                    ).result()
            else:
                tools_result = asyncio.run(session.list_tools())

            raw_tools = getattr(tools_result, "tools", tools_result)
            if not isinstance(raw_tools, (list, tuple)):
                raw_tools = list(raw_tools) if raw_tools else []

            cache[sid] = _build_llamaindex_tools_from_mcp(session, raw_tools)

        all_tools.extend(cache[sid])

    return all_tools


def _wrap_agent_internals(
    agent: Any,
    mcp: Any | None,
    originals: dict[int, dict[str, Any]],
) -> None:
    """Wrap LLM and tools on a LlamaIndex agent.

    Also injects MCP tools if ``mcp`` is provided.
    Stores original values for cleanup in ``originals``.
    """
    mcp_tools_cache: dict[int, list] = {}
    agent_id = id(agent)
    originals[agent_id] = {"_agent_ref": agent}

    # --- Wrap LLM ---
    llm = getattr(agent, "_llm", None) or getattr(agent, "llm", None)
    if llm is not None and not getattr(llm, "_rastir_wrapped", False):
        originals[agent_id]["llm"] = llm
        originals[agent_id]["llm_attr"] = "_llm" if hasattr(agent, "_llm") else "llm"
        wrapped_llm = wrap(
            llm,
            name=f"llamaindex.{type(agent).__name__}.llm",
            span_type="llm",
            include=["chat", "complete", "achat", "acomplete",
                     "stream_chat", "stream_complete",
                     "astream_chat", "astream_complete"],
        )
        setattr(agent, originals[agent_id]["llm_attr"], wrapped_llm)

    # --- Wrap existing tools ---
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
                wrap(t, name=tool_name, span_type="tool", include=["call", "__call__"])
            )
        else:
            wrapped_tools.append(t)

    # --- Inject MCP tools ---
    mcp_tools = _resolve_mcp_tools(agent, mcp, mcp_tools_cache)
    wrapped_tools.extend(mcp_tools)

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
