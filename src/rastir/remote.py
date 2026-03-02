"""Remote tool distributed tracing for Rastir.

Simple approach: trace context is passed as extra fields in the tool
**arguments** dict (``rastir_trace_id``, ``rastir_span_id``).

Client side
-----------
``wrap_mcp(session)`` returns a proxy around an MCP ``ClientSession``.
The proxy intercepts ``call_tool()`` to:
  1. Create a client span (``remote="true"``).
  2. Inject ``rastir_trace_id`` / ``rastir_span_id`` into the arguments.
All other methods delegate transparently to the real session.

Server side
-----------
``@mcp_endpoint`` wraps a tool function to:
  1. Pop ``rastir_trace_id`` / ``rastir_span_id`` from kwargs.
  2. Create a server span (``remote="false"``) linked to the client.

If the server does **not** use ``@mcp_endpoint``, the extra fields are
silently ignored by FastMCP's Pydantic validation (unknown fields are
stripped before the function is called).

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

# Marker to prevent double-wrapping
_WRAPPED_MARKER = "_rastir_mcp_wrapped"


# ---------------------------------------------------------------------------
# wrap_mcp(session) — client side proxy
# ---------------------------------------------------------------------------

def wrap_mcp(session: Any) -> Any:
    """Wrap an MCP ``ClientSession`` for automatic trace propagation.

    Returns a proxy object that delegates all methods to the real session.
    Only ``call_tool()`` is intercepted: each invocation creates a
    client-side tool span and injects ``rastir_trace_id`` /
    ``rastir_span_id`` into the tool arguments dict.  No other methods
    are modified — ``list_tools()``, ``initialize()``, etc. pass through
    unchanged.

    Usage::

        from rastir import wrap_mcp

        session = wrap_mcp(raw_session)
        tools = await session.list_tools()      # proxied, unchanged
        # Pass tools to any framework (LangChain, CrewAI, etc.)
        # When the framework calls session.call_tool(), trace IDs are
        # injected automatically.

    Args:
        session: An initialised MCP ``ClientSession``.

    Returns:
        A ``_TracedMCPSession`` proxy.

    Raises:
        TypeError: If the session is already wrapped.
    """
    if isinstance(session, _TracedMCPSession):
        logger.debug("Session %r already wrapped, returning as-is", session)
        return session

    return _TracedMCPSession(session)


class _TracedMCPSession:
    """Transparent proxy around an MCP ClientSession.

    Intercepts only ``call_tool()`` to inject trace context into tool
    arguments.  All other attribute accesses delegate to the underlying
    session via ``__getattr__``.
    """

    __slots__ = ("_session",)

    def __init__(self, session: Any) -> None:
        object.__setattr__(self, "_session", session)

    # -- Proxy plumbing ------------------------------------------------

    @property
    def __class__(self) -> type:
        """Preserve isinstance() by delegating __class__."""
        return type(object.__getattribute__(self, "_session"))

    def __getattr__(self, attr: str) -> Any:
        return getattr(object.__getattribute__(self, "_session"), attr)

    def __setattr__(self, attr: str, value: Any) -> None:
        setattr(object.__getattribute__(self, "_session"), attr, value)

    def __delattr__(self, attr: str) -> None:
        delattr(object.__getattribute__(self, "_session"), attr)

    def __repr__(self) -> str:
        session = object.__getattribute__(self, "_session")
        return f"<rastir.wrap_mcp: {session!r}>"

    @property
    def _rastir_mcp_wrapped(self) -> bool:
        return True

    # -- Traced call_tool ----------------------------------------------

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Intercept call_tool to inject trace context."""
        session = object.__getattribute__(self, "_session")

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
            result = await session.call_tool(
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
