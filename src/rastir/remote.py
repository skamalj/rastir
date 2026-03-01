"""Remote tool distributed tracing for Rastir.

Simple approach: trace context is passed as extra fields in the tool
**arguments** dict (``rastir_trace_id``, ``rastir_span_id``).

Client side
-----------
``@trace_remote_tools`` wraps ``session.call_tool()`` to:
  1. Create a client span (``remote="true"``).
  2. Inject ``rastir_trace_id`` / ``rastir_span_id`` into the arguments.

Server side
-----------
``@mcp_endpoint`` wraps a tool function to:
  1. Pop ``rastir_trace_id`` / ``rastir_span_id`` from kwargs.
  2. Create a server span (``remote="false"``) linked to the client.

If the server does **not** use ``@mcp_endpoint``, the extra fields are
silently ignored by FastMCP's Pydantic validation (unknown fields are
stripped before the function is called).

Helper
------
``mcp_to_langchain_tools(session)`` converts MCP tools into LangChain
``StructuredTool`` instances with proper ``args_schema`` and automatic
trace injection.

Trace topology::

    Agent Span
    └── Tool Client Span  (span_type="tool", remote="true")
          └── Tool Server Span (span_type="tool", remote="false")
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
from typing import Any, Callable, TypeVar

from rastir.context import (
    end_span,
    get_current_agent,
    get_current_model,
    get_current_provider,
    start_span,
)
import rastir.queue as _queue
from rastir.spans import SpanRecord, SpanStatus, SpanType

logger = logging.getLogger("rastir")

F = TypeVar("F", bound=Callable[..., Any])

# Keys injected into tool arguments
TRACE_ID_KEY = "rastir_trace_id"
SPAN_ID_KEY = "rastir_span_id"


# ---------------------------------------------------------------------------
# @trace_remote_tools  — client side
# ---------------------------------------------------------------------------

def trace_remote_tools(func: F) -> F:
    """Wrap an MCP session-returning function to auto-inject trace context.

    Decorates a function that returns an MCP ``ClientSession`` (or a tuple
    containing one).  The decorator monkey-patches ``session.call_tool()``
    so every tool invocation:

    1. Creates a client-side tool span (``remote="true"``).
    2. Injects ``rastir_trace_id`` and ``rastir_span_id`` into the
       tool *arguments* dict.
    3. Forwards the call to the original ``session.call_tool()``.
    4. Records errors and finishes the span.

    Usage::

        @trace_remote_tools
        async def get_session():
            ...
            return session          # or (tools, session)
    """

    def _wrap_session(session: Any) -> Any:
        """Monkey-patch call_tool on an MCP ClientSession."""
        cls_name = type(session).__name__
        module = getattr(type(session), "__module__", "") or ""
        if cls_name != "ClientSession" or "mcp" not in module:
            return session

        original_call_tool = session.call_tool

        @functools.wraps(original_call_tool)
        async def _traced_call_tool(
            name: str,
            arguments: dict[str, Any] | None = None,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            # 1. Create client-side tool span
            span, token = start_span(name, SpanType.TOOL)
            span.set_attribute("tool_name", name)
            span.set_attribute("remote", "true")
            agent_name = get_current_agent()
            if agent_name:
                span.set_attribute("agent", agent_name)
            ctx_model = get_current_model()
            if ctx_model:
                span.set_attribute("model", ctx_model)
            ctx_provider = get_current_provider()
            if ctx_provider:
                span.set_attribute("provider", ctx_provider)

            try:
                # 2. Inject trace context into arguments
                trace_id = span.trace_id.replace("-", "").ljust(32, "0")[:32]
                span_id = span.span_id.replace("-", "").ljust(16, "0")[:16]
                merged_args = dict(arguments) if arguments else {}
                merged_args[TRACE_ID_KEY] = trace_id
                merged_args[SPAN_ID_KEY] = span_id

                # 3. Invoke original call_tool
                result = await original_call_tool(
                    name, merged_args, *args, **kwargs
                )
                span.finish(SpanStatus.OK)
                return result
            except BaseException as exc:
                span.record_error(exc)
                span.finish(SpanStatus.ERROR)
                raise
            finally:
                end_span(token)
                _queue.enqueue_span(span)

        session.call_tool = _traced_call_tool
        return session

    def _find_and_wrap_sessions(value: Any) -> Any:
        """Recursively find MCP ClientSession objects and wrap them."""
        cls_name = type(value).__name__
        module = getattr(type(value), "__module__", "") or ""
        if cls_name == "ClientSession" and "mcp" in module:
            return _wrap_session(value)

        if isinstance(value, (tuple, list)):
            wrapped = [_find_and_wrap_sessions(item) for item in value]
            return type(value)(wrapped)

        return value

    if asyncio.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            result = await func(*args, **kwargs)
            return _find_and_wrap_sessions(result)

        return async_wrapper  # type: ignore[return-value]

    else:

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            result = func(*args, **kwargs)
            return _find_and_wrap_sessions(result)

        return sync_wrapper  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# @mcp_endpoint  — server side
# ---------------------------------------------------------------------------

def mcp_endpoint(func: F) -> F:
    """Create a server-side span from trace context in tool arguments.

    Placed **under** ``@mcp.tool()`` so that it wraps the actual function.
    The wrapper adds ``rastir_trace_id`` and ``rastir_span_id`` as
    optional keyword parameters.  When called, it pops them from kwargs,
    creates a child span, and delegates to the original function.

    The original function signature is **unchanged** — it does not need
    to accept any ``rastir_*`` parameters.

    Usage::

        @mcp.tool()
        @mcp_endpoint
        async def search(query: str) -> str:
            ...

    Span attributes:
        - span_type = "tool"
        - remote = "false"
        - tool_name = function name
    """
    tool_name = func.__name__

    if asyncio.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            # Pop trace fields (injected by client) before forwarding
            trace_id = kwargs.pop(TRACE_ID_KEY, None)
            span_id = kwargs.pop(SPAN_ID_KEY, None)

            # Create server-side span
            span, token = start_span(tool_name, SpanType.TOOL)
            if trace_id:
                span.trace_id = trace_id
            if span_id:
                span.parent_id = span_id
            span.set_attribute("tool_name", tool_name)
            span.set_attribute("remote", "false")

            agent_name = get_current_agent()
            if agent_name:
                span.set_attribute("agent", agent_name)

            try:
                result = await func(*args, **kwargs)
                span.finish(SpanStatus.OK)
                return result
            except BaseException as exc:
                span.record_error(exc)
                span.finish(SpanStatus.ERROR)
                raise
            finally:
                end_span(token)
                _queue.enqueue_span(span)

        # Extend the wrapper's signature to include rastir_* params
        # so FastMCP's Pydantic validation passes them through.
        _extend_signature(async_wrapper, func)
        return async_wrapper  # type: ignore[return-value]

    else:

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            trace_id = kwargs.pop(TRACE_ID_KEY, None)
            span_id = kwargs.pop(SPAN_ID_KEY, None)

            span, token = start_span(tool_name, SpanType.TOOL)
            if trace_id:
                span.trace_id = trace_id
            if span_id:
                span.parent_id = span_id
            span.set_attribute("tool_name", tool_name)
            span.set_attribute("remote", "false")

            agent_name = get_current_agent()
            if agent_name:
                span.set_attribute("agent", agent_name)

            try:
                result = func(*args, **kwargs)
                span.finish(SpanStatus.OK)
                return result
            except BaseException as exc:
                span.record_error(exc)
                span.finish(SpanStatus.ERROR)
                raise
            finally:
                end_span(token)
                _queue.enqueue_span(span)

        _extend_signature(sync_wrapper, func)
        return sync_wrapper  # type: ignore[return-value]


def _extend_signature(wrapper: Callable, original: Callable) -> None:
    """Add ``rastir_trace_id`` and ``rastir_span_id`` optional params
    to *wrapper*'s ``__signature__`` so FastMCP's introspection sees them.
    """
    sig = inspect.signature(original)
    extra_params = [
        inspect.Parameter(
            TRACE_ID_KEY,
            inspect.Parameter.KEYWORD_ONLY,
            default=None,
            annotation=str | None,
        ),
        inspect.Parameter(
            SPAN_ID_KEY,
            inspect.Parameter.KEYWORD_ONLY,
            default=None,
            annotation=str | None,
        ),
    ]
    params = list(sig.parameters.values())
    wrapper.__signature__ = sig.replace(parameters=params + extra_params)


# ---------------------------------------------------------------------------
# MCP → LangChain tool bridge
# ---------------------------------------------------------------------------

# JSON-schema type → Python type mapping
_JSON_TYPE_MAP: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
}


async def mcp_to_langchain_tools(
    session: Any,
    *,
    trace: bool = True,
) -> list[Any]:
    """Convert MCP tools to LangChain StructuredTool instances.

    Handles all the bridging boilerplate:
    1. Fetches tool list from the MCP server via ``session.list_tools()``.
    2. Wraps the session with ``@trace_remote_tools`` for automatic
       trace injection into tool arguments (unless *trace=False*).
    3. Builds a Pydantic ``args_schema`` from each tool's ``inputSchema``,
       filtering out ``rastir_*`` internal keys.
    4. Returns a list of ``StructuredTool`` ready for
       use with ``create_react_agent`` or any LangChain agent.

    Args:
        session: An initialised MCP ``ClientSession``.
        trace: If *True* (default), wrap the session with
            ``@trace_remote_tools`` so every ``call_tool`` injects
            ``rastir_trace_id`` / ``rastir_span_id`` into arguments.

    Returns:
        A list of ``StructuredTool`` instances, one per MCP tool.

    Example::

        from rastir import mcp_to_langchain_tools

        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await mcp_to_langchain_tools(session)
            agent = create_react_agent(llm, tools)
    """
    try:
        from langchain_core.tools import StructuredTool
        from pydantic import create_model
    except ImportError as exc:
        raise ImportError(
            "mcp_to_langchain_tools requires langchain-core. "
            "Install it with: pip install langchain-core"
        ) from exc

    # Optionally wrap for trace propagation
    if trace:
        @trace_remote_tools
        def _wrap():
            return session
        wrapped = _wrap()
    else:
        wrapped = session

    # Fetch available tools from the MCP server
    tools_response = await session.list_tools()

    lc_tools: list[StructuredTool] = []
    for mcp_tool in tools_response.tools:
        tool_name = mcp_tool.name
        tool_desc = mcp_tool.description or tool_name

        # Build Pydantic model from the MCP tool's JSON inputSchema,
        # filtering out any rastir_* internal fields
        props = (mcp_tool.inputSchema or {}).get("properties", {})
        required_set = set(
            (mcp_tool.inputSchema or {}).get("required", [])
        )
        field_defs: dict[str, Any] = {}
        for fname, fschema in props.items():
            if fname.startswith("rastir_"):
                continue  # skip internal trace fields
            ftype = _JSON_TYPE_MAP.get(
                fschema.get("type", "string"), str
            )
            default = ... if fname in required_set else None
            field_defs[fname] = (ftype, default)

        args_model = create_model(f"{tool_name}_args", **field_defs)

        # Build the async closure that calls the (possibly traced) session
        async def _call_mcp(
            _tn: str = tool_name,
            **kwargs: Any,
        ) -> str:
            result = await wrapped.call_tool(_tn, kwargs)
            if result.content:
                return " ".join(
                    getattr(c, "text", str(c)) for c in result.content
                )
            return str(result)

        lc_tool = StructuredTool.from_function(
            coroutine=_call_mcp,
            name=tool_name,
            description=tool_desc,
            args_schema=args_model,
        )
        lc_tools.append(lc_tool)

    return lc_tools
