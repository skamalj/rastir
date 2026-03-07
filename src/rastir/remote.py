"""Remote MCP distributed tracing for Rastir.

Uses W3C ``traceparent`` HTTP headers for trace propagation across MCP
tool boundaries.

Client side
-----------
**Framework users (LangGraph / CrewAI / LlamaIndex):**
  No client-side code needed — the framework decorators
  (``@langgraph_agent``, ``@crew_kickoff``, ``@llamaindex_agent``)
  auto-discover MCP client objects and mutate their ``headers`` dict
  with the current ``traceparent`` before each tool call.

**Standalone users:**
  ``wrap_mcp(session, http_client=client)`` wraps a raw MCP
  ``ClientSession``.  The proxy intercepts ``call_tool()`` and sets
  the ``traceparent`` header on the provided ``httpx.AsyncClient``.

Server side
-----------
``RastirMCPMiddleware`` (ASGI middleware) reads ``traceparent`` from
the incoming HTTP request and stores it in a ContextVar.

``@mcp_endpoint`` wraps a tool function to create a server span
linked to the client trace context.

Trace topology::

    Agent Span
    └── Tool Client Span  (span_type="tool")

    mcpserver:<tool_name> (span_type="tool", independent trace)

W3C traceparent format::

    traceparent: 00-<32-char-trace-id>-<16-char-span-id>-01
"""

from __future__ import annotations

import asyncio
import functools
import logging
from contextvars import ContextVar
from typing import Any, Callable, TypeVar

from rastir.context import (
    end_span,
    get_current_agent,
    get_current_model,
    get_current_provider,
    get_current_span,
    start_span,
)
import rastir.queue as _queue
from rastir.spans import SpanRecord, SpanStatus, SpanType

logger = logging.getLogger("rastir")

F = TypeVar("F", bound=Callable[..., Any])

# ContextVar set by RastirMCPMiddleware for server-side extraction
_incoming_trace_context: ContextVar[dict[str, str] | None] = ContextVar(
    "_incoming_trace_context", default=None,
)

# Marker to prevent double-wrapping
_WRAPPED_MARKER = "_rastir_mcp_wrapped"


# ---------------------------------------------------------------------------
# W3C traceparent helpers
# ---------------------------------------------------------------------------

def _format_traceparent(trace_id: str, span_id: str) -> str:
    """Format a W3C ``traceparent`` header value.

    Args:
        trace_id: 32-character hex trace ID.
        span_id: 16-character hex span ID (parent span).

    Returns:
        ``"00-<trace_id>-<span_id>-01"``
    """
    tid = trace_id.replace("-", "").ljust(32, "0")[:32]
    sid = span_id.replace("-", "")[:16].ljust(16, "0")
    return f"00-{tid}-{sid}-01"


def _parse_traceparent(value: str) -> tuple[str, str] | None:
    """Parse a W3C ``traceparent`` header value.

    Returns:
        ``(trace_id, parent_span_id)`` or ``None`` on invalid input.
    """
    if not value:
        return None
    parts = value.strip().split("-")
    if len(parts) < 4:
        return None
    trace_id = parts[1]
    parent_id = parts[2]
    if len(trace_id) != 32 or len(parent_id) != 16:
        return None
    return trace_id, parent_id


def traceparent_headers() -> dict[str, str]:
    """Return a ``{"traceparent": "..."}`` dict from the current span.

    Useful for manual header injection when not using a framework
    decorator.  Returns an empty dict if no active span exists.
    """
    span = get_current_span()
    if span is None:
        return {}
    return {"traceparent": _format_traceparent(span.trace_id, span.span_id)}


# ---------------------------------------------------------------------------
# Framework MCP client discovery helpers
# ---------------------------------------------------------------------------

def _is_mcp_multi_client(obj: Any) -> bool:
    """True if *obj* is a LangGraph ``MultiServerMCPClient``."""
    cls = type(obj)
    module = getattr(cls, "__module__", "") or ""
    return cls.__name__ == "MultiServerMCPClient" and "mcp" in module


def _is_crewai_mcp_server(obj: Any) -> bool:
    """True if *obj* is a CrewAI ``MCPServerHTTP`` or ``MCPServerSSE``."""
    cls = type(obj)
    module = getattr(cls, "__module__", "") or ""
    return cls.__name__ in ("MCPServerHTTP", "MCPServerSSE") and "crewai" in module


def _is_llamaindex_mcp_client(obj: Any) -> bool:
    """True if *obj* is a LlamaIndex ``BasicMCPClient``."""
    cls = type(obj)
    module = getattr(cls, "__module__", "") or ""
    return cls.__name__ == "BasicMCPClient" and "llama_index" in module


def inject_traceparent_into_mcp_clients(mcp_clients: list[Any]) -> None:
    """Mutate headers on discovered MCP client objects.

    Called by framework decorators just before each invocation to set
    the ``traceparent`` header from the current active span.

    Supports:
    - LangGraph ``MultiServerMCPClient``: sets ``traceparent`` on
      each connection's headers dict.
    - CrewAI ``MCPServerHTTP`` / ``MCPServerSSE``: mutates
      ``server.headers``.
    - LlamaIndex ``BasicMCPClient``: mutates ``client.headers``.
    """
    span = get_current_span()
    if span is None:
        return
    tp = _format_traceparent(span.trace_id, span.span_id)

    for obj in mcp_clients:
        try:
            if _is_mcp_multi_client(obj):
                connections = getattr(obj, "connections", None)
                if isinstance(connections, dict):
                    for _name, conn in connections.items():
                        if isinstance(conn, dict):
                            hdrs = conn.get("headers")
                            if hdrs is None:
                                hdrs = {}
                                conn["headers"] = hdrs
                            hdrs["traceparent"] = tp

            elif _is_crewai_mcp_server(obj):
                hdrs = getattr(obj, "headers", None)
                if hdrs is None:
                    obj.headers = {"traceparent": tp}
                else:
                    hdrs["traceparent"] = tp

            elif _is_llamaindex_mcp_client(obj):
                hdrs = getattr(obj, "headers", None)
                if hdrs is None:
                    obj.headers = {"traceparent": tp}
                else:
                    hdrs["traceparent"] = tp
                # Also update the httpx client headers — the AsyncClient
                # copies headers at construction time, so mutating the
                # dict above doesn't affect already-created clients.
                http_client = getattr(obj, "http_client", None)
                if http_client is not None:
                    http_client.headers["traceparent"] = tp

        except Exception:
            logger.debug("Failed to inject traceparent into %r", obj,
                         exc_info=True)


def discover_mcp_client(obj: Any) -> Any | None:
    """Check if *obj* is a known MCP client. Returns it, or None."""
    if _is_mcp_multi_client(obj) or _is_crewai_mcp_server(obj) or _is_llamaindex_mcp_client(obj):
        return obj
    return None


# ---------------------------------------------------------------------------
# wrap_mcp(session, http_client=...) — standalone client side proxy
# ---------------------------------------------------------------------------

def wrap_mcp(session: Any, *, http_client: Any = None) -> Any:
    """Wrap an MCP ``ClientSession`` for automatic trace propagation.

    For standalone usage (not via framework decorators).  The proxy
    intercepts ``call_tool()`` to create a client-side span and
    optionally set the ``traceparent`` header on *http_client*.

    Args:
        session: An initialised MCP ``ClientSession``.
        http_client: Optional ``httpx.AsyncClient`` used by the MCP
            transport.  If provided, ``traceparent`` header is set
            on it before each ``call_tool()``.

    Returns:
        A ``_TracedMCPSession`` proxy.
    """
    if isinstance(session, _TracedMCPSession):
        logger.debug("Session %r already wrapped, returning as-is", session)
        return session

    return _TracedMCPSession(session, http_client=http_client)


class _TracedMCPSession:
    """Transparent proxy around an MCP ClientSession.

    Intercepts only ``call_tool()`` to create a client span and
    set the ``traceparent`` HTTP header.  All other attribute accesses
    delegate to the underlying session via ``__getattr__``.
    """

    __slots__ = ("_session", "_http_client")

    def __init__(self, session: Any, *, http_client: Any = None) -> None:
        object.__setattr__(self, "_session", session)
        object.__setattr__(self, "_http_client", http_client)

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
        """Intercept call_tool to inject traceparent header."""
        session = object.__getattribute__(self, "_session")
        http_client = object.__getattribute__(self, "_http_client")

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
            # 2. Set traceparent header on http_client (if provided)
            if http_client is not None:
                tp = _format_traceparent(span.trace_id, span.span_id)
                http_client.headers["traceparent"] = tp

            # 3. Invoke original call_tool
            result = await session.call_tool(
                name, arguments, *args, **kwargs
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
# RastirMCPMiddleware — ASGI middleware for server-side header reading
# ---------------------------------------------------------------------------

class RastirMCPMiddleware:
    """ASGI middleware that reads ``traceparent`` from incoming requests.

    Stores the parsed trace context in a ContextVar so that
    ``@mcp_endpoint`` can create linked server spans.

    Usage::

        from starlette.applications import Starlette
        from rastir.remote import RastirMCPMiddleware

        app = Starlette(...)
        app = RastirMCPMiddleware(app)
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] in ("http", "websocket"):
            headers = dict(scope.get("headers", []))
            # ASGI headers are bytes
            tp_value = headers.get(b"traceparent", b"").decode("utf-8", errors="replace")
            parsed = _parse_traceparent(tp_value)
            if parsed:
                ctx = {"trace_id": parsed[0], "parent_id": parsed[1]}
                tok = _incoming_trace_context.set(ctx)
                try:
                    await self.app(scope, receive, send)
                finally:
                    _incoming_trace_context.reset(tok)
                return

        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# @mcp_endpoint  — server side
# ---------------------------------------------------------------------------

def mcp_endpoint(func: F) -> F:
    """Create a server-side span for an MCP tool function.

    Placed **under** ``@mcp.tool()`` so that it wraps the actual function.
    Reads trace context from ``_incoming_trace_context`` ContextVar
    (set by ``RastirMCPMiddleware``) and links the span to the client
    trace.  Span is named ``mcpserver:<function_name>``.

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
    span_name = f"mcpserver:{tool_name}"

    if asyncio.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            # Read trace context from ContextVar (set by middleware)
            ctx = _incoming_trace_context.get()

            # Create server-side span, linked to client trace if available
            span, token = start_span(span_name, SpanType.TOOL)
            if ctx:
                span.trace_id = ctx["trace_id"]
                span.parent_id = ctx["parent_id"]
                span._reanchor()
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

        return async_wrapper  # type: ignore[return-value]

    else:

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            # Read trace context from ContextVar (set by middleware)
            ctx = _incoming_trace_context.get()

            # Create server-side span, linked to client trace if available
            span, token = start_span(span_name, SpanType.TOOL)
            if ctx:
                span.trace_id = ctx["trace_id"]
                span.parent_id = ctx["parent_id"]
                span._reanchor()
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

        return sync_wrapper  # type: ignore[return-value]
