"""Unit tests for rastir.remote — W3C traceparent header-based trace propagation.

Tests cover:
- _format_traceparent / _parse_traceparent: W3C header formatting/parsing
- traceparent_headers(): convenience helper
- wrap_mcp: session proxy, call_tool interception, traceparent on httpx client
- @mcp_endpoint: reads _incoming_trace_context, creates linked server span
- RastirMCPMiddleware: ASGI middleware reads traceparent header
- inject_traceparent_into_mcp_clients: framework MCP header injection
- discover_mcp_client: MCP client detection
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from rastir.remote import (
    RastirMCPMiddleware,
    _format_traceparent,
    _incoming_trace_context,
    _parse_traceparent,
    discover_mcp_client,
    inject_traceparent_into_mcp_clients,
    mcp_endpoint,
    traceparent_headers,
    wrap_mcp,
)
from rastir.spans import SpanRecord, SpanType


# ---------------------------------------------------------------------------
# traceparent formatting / parsing
# ---------------------------------------------------------------------------

class TestFormatTraceparent(unittest.TestCase):

    def test_basic_format(self):
        tp = _format_traceparent("a" * 32, "b" * 16)
        assert tp == f"00-{'a' * 32}-{'b' * 16}-01"

    def test_pads_short_ids(self):
        tp = _format_traceparent("abc", "def")
        parts = tp.split("-")
        assert len(parts[1]) == 32
        assert len(parts[2]) == 16

    def test_strips_dashes(self):
        tp = _format_traceparent("a-b-c-d", "e-f")
        assert "-" not in tp.split("-")[1]  # trace_id part


class TestParseTraceparent(unittest.TestCase):

    def test_valid_traceparent(self):
        tp = f"00-{'a' * 32}-{'b' * 16}-01"
        result = _parse_traceparent(tp)
        assert result == ("a" * 32, "b" * 16)

    def test_empty_returns_none(self):
        assert _parse_traceparent("") is None

    def test_invalid_format_returns_none(self):
        assert _parse_traceparent("invalid") is None

    def test_wrong_lengths_returns_none(self):
        assert _parse_traceparent("00-short-short-01") is None


class TestTraceparentHeaders(unittest.TestCase):

    def test_no_span_returns_empty(self):
        result = traceparent_headers()
        assert result == {}

    @patch("rastir.remote.get_current_span")
    def test_with_active_span(self, mock_span):
        span = SpanRecord("test", SpanType.TOOL)
        mock_span.return_value = span
        result = traceparent_headers()
        assert "traceparent" in result
        parsed = _parse_traceparent(result["traceparent"])
        assert parsed is not None


# ---------------------------------------------------------------------------
# wrap_mcp
# ---------------------------------------------------------------------------

class TestWrapMcp(unittest.TestCase):

    def _make_session(self):
        session = MagicMock()
        type(session).__name__ = "ClientSession"
        type(session).__module__ = "mcp.client.session"
        session.call_tool = AsyncMock(return_value="result")
        session.list_tools = AsyncMock(return_value=MagicMock(tools=[]))
        session.initialize = AsyncMock()
        return session

    def test_returns_proxy(self):
        session = self._make_session()
        wrapped = wrap_mcp(session)
        assert wrapped is not session

    def test_proxy_delegates_list_tools(self):
        session = self._make_session()
        wrapped = wrap_mcp(session)
        asyncio.get_event_loop().run_until_complete(wrapped.list_tools())
        session.list_tools.assert_called_once()

    def test_prevents_double_wrapping(self):
        session = self._make_session()
        wrapped = wrap_mcp(session)
        double = wrap_mcp(wrapped)
        assert double is wrapped

    @patch("rastir.queue.enqueue_span")
    def test_call_tool_creates_span(self, mock_enqueue):
        session = self._make_session()
        wrapped = wrap_mcp(session)
        asyncio.get_event_loop().run_until_complete(
            wrapped.call_tool("search", {"query": "test"})
        )
        span = mock_enqueue.call_args[0][0]
        assert span.span_type == SpanType.TOOL
        assert span.attributes["tool_name"] == "search"
        assert span.attributes["remote"] == "true"
        assert span.status.value == "OK"

    @patch("rastir.queue.enqueue_span")
    def test_call_tool_sets_traceparent_on_http_client(self, mock_enqueue):
        """When http_client is provided, traceparent should be set on it."""
        session = self._make_session()
        http_client = MagicMock()
        http_client.headers = {}

        wrapped = wrap_mcp(session, http_client=http_client)
        asyncio.get_event_loop().run_until_complete(
            wrapped.call_tool("search", {"query": "hi"})
        )

        assert "traceparent" in http_client.headers
        parsed = _parse_traceparent(http_client.headers["traceparent"])
        assert parsed is not None
        assert len(parsed[0]) == 32
        assert len(parsed[1]) == 16

    @patch("rastir.queue.enqueue_span")
    def test_call_tool_no_http_client(self, mock_enqueue):
        """Without http_client, call_tool should still create a span."""
        session = self._make_session()
        wrapped = wrap_mcp(session)
        asyncio.get_event_loop().run_until_complete(
            wrapped.call_tool("search", {"query": "hi"})
        )
        span = mock_enqueue.call_args[0][0]
        assert span.span_type == SpanType.TOOL

    @patch("rastir.queue.enqueue_span")
    def test_call_tool_error_propagation(self, mock_enqueue):
        session = self._make_session()
        session.call_tool = AsyncMock(side_effect=RuntimeError("down"))
        wrapped = wrap_mcp(session)
        with self.assertRaises(RuntimeError):
            asyncio.get_event_loop().run_until_complete(
                wrapped.call_tool("search", {"q": "hi"})
            )
        span = mock_enqueue.call_args[0][0]
        assert span.status.value == "ERROR"

    @patch("rastir.queue.enqueue_span")
    def test_does_not_mutate_arguments(self, mock_enqueue):
        """Arguments dict should NOT be modified (no more arg injection)."""
        session = self._make_session()
        captured = {}
        async def fake(name, arguments=None, *a, **kw):
            captured.update(arguments or {})
            return "ok"
        session.call_tool = fake

        wrapped = wrap_mcp(session)
        original_args = {"city": "Tokyo"}
        asyncio.get_event_loop().run_until_complete(
            wrapped.call_tool("weather", original_args)
        )
        # No rastir_* keys injected into arguments
        assert "rastir_trace_id" not in captured
        assert "rastir_span_id" not in captured

    def test_repr(self):
        session = self._make_session()
        wrapped = wrap_mcp(session)
        assert "rastir.wrap_mcp" in repr(wrapped)


# ---------------------------------------------------------------------------
# RastirMCPMiddleware
# ---------------------------------------------------------------------------

class TestRastirMCPMiddleware(unittest.TestCase):

    def test_reads_traceparent_header(self):
        """Middleware should parse traceparent and store in ContextVar."""
        trace_id = "a" * 32
        span_id = "b" * 16
        tp = f"00-{trace_id}-{span_id}-01"

        captured_ctx = {}
        async def app(scope, receive, send):
            ctx = _incoming_trace_context.get()
            if ctx:
                captured_ctx.update(ctx)

        middleware = RastirMCPMiddleware(app)

        scope = {
            "type": "http",
            "headers": [(b"traceparent", tp.encode())],
        }

        asyncio.get_event_loop().run_until_complete(
            middleware(scope, None, None)
        )
        assert captured_ctx["trace_id"] == trace_id
        assert captured_ctx["parent_id"] == span_id

    def test_no_traceparent_header(self):
        """Without traceparent, ContextVar should remain None."""
        captured_ctx = {"called": False}
        async def app(scope, receive, send):
            ctx = _incoming_trace_context.get()
            captured_ctx["called"] = True
            captured_ctx["ctx"] = ctx

        middleware = RastirMCPMiddleware(app)
        scope = {"type": "http", "headers": []}
        asyncio.get_event_loop().run_until_complete(
            middleware(scope, None, None)
        )
        assert captured_ctx["ctx"] is None

    def test_non_http_scope(self):
        """Non-HTTP scopes should pass through."""
        called = {"value": False}
        async def app(scope, receive, send):
            called["value"] = True

        middleware = RastirMCPMiddleware(app)
        scope = {"type": "lifespan"}
        asyncio.get_event_loop().run_until_complete(
            middleware(scope, None, None)
        )
        assert called["value"] is True


# ---------------------------------------------------------------------------
# @mcp_endpoint
# ---------------------------------------------------------------------------

class TestMcpEndpoint(unittest.TestCase):

    @patch("rastir.queue.enqueue_span")
    def test_creates_tool_span(self, mock_enqueue):
        @mcp_endpoint
        async def search(query: str) -> str:
            return f"results for {query}"

        result = asyncio.get_event_loop().run_until_complete(
            search(query="hello")
        )
        assert result == "results for hello"
        span = mock_enqueue.call_args[0][0]
        assert span.span_type == SpanType.TOOL
        assert span.attributes["tool_name"] == "search"
        assert span.attributes["remote"] == "false"

    @patch("rastir.queue.enqueue_span")
    def test_reads_trace_context_from_contextvar(self, mock_enqueue):
        """Should link span to incoming trace context from middleware."""
        trace_id = "c" * 32
        parent_id = "d" * 16

        @mcp_endpoint
        async def search(query: str) -> str:
            return "ok"

        async def run():
            tok = _incoming_trace_context.set({
                "trace_id": trace_id,
                "parent_id": parent_id,
            })
            try:
                return await search(query="hi")
            finally:
                _incoming_trace_context.reset(tok)

        asyncio.get_event_loop().run_until_complete(run())
        span = mock_enqueue.call_args[0][0]
        assert span.trace_id == trace_id
        assert span.parent_id == parent_id
        assert span.name == "mcpserver:search"

    @patch("rastir.queue.enqueue_span")
    def test_no_trace_context_creates_root(self, mock_enqueue):
        @mcp_endpoint
        async def search(query: str) -> str:
            return "ok"

        asyncio.get_event_loop().run_until_complete(search(query="hi"))
        span = mock_enqueue.call_args[0][0]
        assert span.trace_id is not None
        assert len(span.trace_id) > 0

    @patch("rastir.queue.enqueue_span")
    def test_error_propagation(self, mock_enqueue):
        @mcp_endpoint
        async def failing(x: int) -> int:
            raise ValueError("bad")

        with self.assertRaises(ValueError):
            asyncio.get_event_loop().run_until_complete(failing(x=1))
        span = mock_enqueue.call_args[0][0]
        assert span.status.value == "ERROR"

    @patch("rastir.queue.enqueue_span")
    def test_sync_function(self, mock_enqueue):
        @mcp_endpoint
        def sync_tool(x: int) -> int:
            return x * 2

        result = sync_tool(x=5)
        assert result == 10
        span = mock_enqueue.call_args[0][0]
        assert span.attributes["tool_name"] == "sync_tool"
        assert span.attributes["remote"] == "false"


# ---------------------------------------------------------------------------
# inject_traceparent_into_mcp_clients / discover_mcp_client
# ---------------------------------------------------------------------------

class TestInjectTraceparent(unittest.TestCase):

    def _make_langgraph_client(self):
        """Mock a LangGraph MultiServerMCPClient."""
        client = MagicMock()
        type(client).__name__ = "MultiServerMCPClient"
        type(client).__module__ = "langchain_mcp_adapters.client"
        client.connections = {
            "weather": {"url": "http://localhost:9000/mcp", "headers": {}},
            "search": {"url": "http://localhost:9001/mcp", "headers": {}},
        }
        return client

    def _make_crewai_server(self):
        """Mock a CrewAI MCPServerHTTP."""
        srv = MagicMock()
        type(srv).__name__ = "MCPServerHTTP"
        type(srv).__module__ = "crewai.mcp.config"
        srv.headers = {}
        return srv

    def _make_llamaindex_client(self):
        """Mock a LlamaIndex BasicMCPClient."""
        client = MagicMock()
        type(client).__name__ = "BasicMCPClient"
        type(client).__module__ = "llama_index.tools.mcp"
        client.headers = {}
        return client

    @patch("rastir.remote.get_current_span")
    def test_inject_langgraph(self, mock_span):
        span = SpanRecord("test", SpanType.AGENT)
        mock_span.return_value = span

        client = self._make_langgraph_client()
        inject_traceparent_into_mcp_clients([client])

        for name in ("weather", "search"):
            assert "traceparent" in client.connections[name]["headers"]
            parsed = _parse_traceparent(
                client.connections[name]["headers"]["traceparent"]
            )
            assert parsed is not None

    @patch("rastir.remote.get_current_span")
    def test_inject_crewai(self, mock_span):
        span = SpanRecord("test", SpanType.AGENT)
        mock_span.return_value = span

        srv = self._make_crewai_server()
        inject_traceparent_into_mcp_clients([srv])

        assert "traceparent" in srv.headers

    @patch("rastir.remote.get_current_span")
    def test_inject_llamaindex(self, mock_span):
        span = SpanRecord("test", SpanType.AGENT)
        mock_span.return_value = span

        client = self._make_llamaindex_client()
        inject_traceparent_into_mcp_clients([client])

        assert "traceparent" in client.headers

    def test_discover_mcp_client_langgraph(self):
        client = self._make_langgraph_client()
        assert discover_mcp_client(client) is client

    def test_discover_mcp_client_crewai(self):
        srv = self._make_crewai_server()
        assert discover_mcp_client(srv) is srv

    def test_discover_mcp_client_llamaindex(self):
        client = self._make_llamaindex_client()
        assert discover_mcp_client(client) is client

    def test_discover_mcp_client_unknown(self):
        obj = MagicMock()
        type(obj).__name__ = "SomeOtherThing"
        assert discover_mcp_client(obj) is None


# ---------------------------------------------------------------------------
# End-to-end: middleware → mcp_endpoint linking
# ---------------------------------------------------------------------------

class TestEndToEndHeaderTracing(unittest.TestCase):

    @patch("rastir.queue.enqueue_span")
    def test_middleware_to_endpoint_linking(self, mock_enqueue):
        """Simulate: client sets traceparent → middleware reads → endpoint links."""
        trace_id = "e" * 32
        span_id = "f" * 16
        tp = f"00-{trace_id}-{span_id}-01"

        @mcp_endpoint
        async def server_search(query: str) -> str:
            return "results"

        async def app(scope, receive, send):
            await server_search(query="test")

        middleware = RastirMCPMiddleware(app)

        scope = {
            "type": "http",
            "headers": [(b"traceparent", tp.encode())],
        }

        asyncio.get_event_loop().run_until_complete(
            middleware(scope, None, None)
        )

        span = mock_enqueue.call_args[0][0]
        assert span.trace_id == trace_id
        assert span.parent_id == span_id
        assert span.attributes["remote"] == "false"
        assert span.name == "mcpserver:server_search"


if __name__ == "__main__":
    unittest.main()
