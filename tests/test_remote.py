"""Unit tests for rastir.remote — argument-based trace propagation.

Tests cover:
- wrap_mcp: session proxy, call_tool interception, rastir_* injection
- @mcp_endpoint: rastir_* extraction, span linking, signature extension
- End-to-end: client→server trace_id propagation via arguments
"""

from __future__ import annotations

import asyncio
import inspect
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from rastir.remote import (
    TRACE_ID_KEY,
    SPAN_ID_KEY,
    mcp_endpoint,
    wrap_mcp,
)
from rastir.spans import SpanRecord, SpanType


class TestWrapMcp(unittest.TestCase):
    """Test wrap_mcp() session proxy."""

    def _make_session(self):
        """Create a mock MCP ClientSession."""
        session = MagicMock()
        type(session).__name__ = "ClientSession"
        type(session).__module__ = "mcp.client.session"
        session.call_tool = AsyncMock(return_value="result")
        session.list_tools = AsyncMock(return_value=MagicMock(tools=[]))
        session.initialize = AsyncMock()
        return session

    def test_returns_proxy(self):
        """wrap_mcp should return a proxy, not the original session."""
        session = self._make_session()
        wrapped = wrap_mcp(session)
        assert wrapped is not session

    def test_proxy_delegates_list_tools(self):
        """list_tools should pass through to the real session."""
        session = self._make_session()
        wrapped = wrap_mcp(session)
        result = asyncio.get_event_loop().run_until_complete(
            wrapped.list_tools()
        )
        session.list_tools.assert_called_once()

    def test_proxy_delegates_initialize(self):
        """initialize should pass through to the real session."""
        session = self._make_session()
        wrapped = wrap_mcp(session)
        asyncio.get_event_loop().run_until_complete(wrapped.initialize())
        session.initialize.assert_called_once()

    def test_proxy_delegates_arbitrary_attrs(self):
        """Arbitrary attributes should delegate to the real session."""
        session = self._make_session()
        session.some_attr = "hello"
        wrapped = wrap_mcp(session)
        assert wrapped.some_attr == "hello"

    def test_prevents_double_wrapping(self):
        """Wrapping an already-wrapped session should return it as-is."""
        session = self._make_session()
        wrapped = wrap_mcp(session)
        double = wrap_mcp(wrapped)
        assert double is wrapped

    @patch("rastir.queue.enqueue_span")
    def test_call_tool_creates_span(self, mock_enqueue):
        """Wrapped call_tool should create a tool span with remote=true."""
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
    def test_injects_rastir_keys_into_arguments(self, mock_enqueue):
        """Should inject rastir_trace_id / rastir_span_id into arguments."""
        session = self._make_session()

        captured_args = {}
        async def fake_call_tool(name, arguments=None, *a, **kw):
            captured_args.update(arguments or {})
            return "ok"
        session.call_tool = fake_call_tool

        wrapped = wrap_mcp(session)
        asyncio.get_event_loop().run_until_complete(
            wrapped.call_tool("search", {"query": "hi"})
        )

        assert TRACE_ID_KEY in captured_args
        assert SPAN_ID_KEY in captured_args
        assert len(captured_args[TRACE_ID_KEY]) == 32
        assert len(captured_args[SPAN_ID_KEY]) == 16
        # Original args preserved
        assert captured_args["query"] == "hi"

    @patch("rastir.queue.enqueue_span")
    def test_preserves_original_arguments(self, mock_enqueue):
        """Original arguments dict should not be mutated."""
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

        # Original dict should not be mutated
        assert TRACE_ID_KEY not in original_args
        # But captured should have it
        assert TRACE_ID_KEY in captured

    @patch("rastir.queue.enqueue_span")
    def test_call_tool_error_propagation(self, mock_enqueue):
        """Errors should mark span as ERROR and re-raise."""
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
    def test_call_tool_none_arguments(self, mock_enqueue):
        """call_tool with None arguments should still inject trace IDs."""
        session = self._make_session()

        captured_args = {}
        async def fake(name, arguments=None, *a, **kw):
            captured_args.update(arguments or {})
            return "ok"
        session.call_tool = fake

        wrapped = wrap_mcp(session)
        asyncio.get_event_loop().run_until_complete(
            wrapped.call_tool("ping", None)
        )

        assert TRACE_ID_KEY in captured_args
        assert SPAN_ID_KEY in captured_args

    def test_repr(self):
        """Proxy repr should identify itself as rastir.wrap_mcp."""
        session = self._make_session()
        wrapped = wrap_mcp(session)
        assert "rastir.wrap_mcp" in repr(wrapped)


class TestMcpEndpoint(unittest.TestCase):
    """Test @mcp_endpoint decorator."""

    @patch("rastir.queue.enqueue_span")
    def test_creates_tool_span(self, mock_enqueue):
        """Should create a tool span with remote=false."""
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
    def test_pops_trace_fields(self, mock_enqueue):
        """Should pop rastir_* fields and link the span."""
        @mcp_endpoint
        async def search(query: str) -> str:
            return "ok"

        asyncio.get_event_loop().run_until_complete(
            search(
                query="hi",
                rastir_trace_id="aabb" * 8,
                rastir_span_id="1122" * 4,
            )
        )

        span = mock_enqueue.call_args[0][0]
        assert span.trace_id == "aabb" * 8
        assert span.parent_id == "1122" * 4

    @patch("rastir.queue.enqueue_span")
    def test_no_trace_fields_creates_root(self, mock_enqueue):
        """Without rastir_* fields, should create a root span."""
        @mcp_endpoint
        async def search(query: str) -> str:
            return "ok"

        asyncio.get_event_loop().run_until_complete(search(query="hi"))

        span = mock_enqueue.call_args[0][0]
        assert span.trace_id is not None
        assert len(span.trace_id) > 0

    @patch("rastir.queue.enqueue_span")
    def test_trace_fields_not_passed_to_original(self, mock_enqueue):
        """Original function should NOT receive rastir_* kwargs."""
        received_kwargs = {}

        @mcp_endpoint
        async def search(query: str) -> str:
            return "ok"

        # Should not raise — rastir_* popped before call
        asyncio.get_event_loop().run_until_complete(
            search(
                query="hi",
                rastir_trace_id="a" * 32,
                rastir_span_id="b" * 16,
            )
        )

    @patch("rastir.queue.enqueue_span")
    def test_error_propagation(self, mock_enqueue):
        """Errors should mark span as ERROR and re-raise."""
        @mcp_endpoint
        async def failing(x: int) -> int:
            raise ValueError("bad")

        with self.assertRaises(ValueError):
            asyncio.get_event_loop().run_until_complete(failing(x=1))

        span = mock_enqueue.call_args[0][0]
        assert span.status.value == "ERROR"

    @patch("rastir.queue.enqueue_span")
    def test_sync_function(self, mock_enqueue):
        """Should work with sync functions."""
        @mcp_endpoint
        def sync_tool(x: int) -> int:
            return x * 2

        result = sync_tool(x=5)
        assert result == 10

        span = mock_enqueue.call_args[0][0]
        assert span.attributes["tool_name"] == "sync_tool"
        assert span.attributes["remote"] == "false"

    def test_signature_extended(self):
        """Wrapper signature should include rastir_* optional params."""
        @mcp_endpoint
        async def search(query: str) -> str:
            return "ok"

        sig = inspect.signature(search)
        assert TRACE_ID_KEY in sig.parameters
        assert SPAN_ID_KEY in sig.parameters
        # Both should have default=None
        assert sig.parameters[TRACE_ID_KEY].default is None
        assert sig.parameters[SPAN_ID_KEY].default is None


class TestEndToEndTracing(unittest.TestCase):
    """Test client→server trace propagation via arguments."""

    @patch("rastir.queue.enqueue_span")
    def test_trace_id_propagation(self, mock_enqueue):
        """Client and server spans should share the same trace_id."""

        # Server-side tool
        @mcp_endpoint
        async def server_search(query: str) -> str:
            return "results"

        # Client-side session mock
        session = MagicMock()
        session.call_tool = AsyncMock()

        async def fake_call_tool(name, arguments=None, *a, **kw):
            # Simulate server receiving arguments with rastir_* fields
            return await server_search(**arguments)

        session.call_tool = fake_call_tool

        async def run():
            wrapped = wrap_mcp(session)
            return await wrapped.call_tool("search", {"query": "test"})

        asyncio.get_event_loop().run_until_complete(run())

        # 2 spans: server (inner, enqueued first) + client (outer)
        assert mock_enqueue.call_count == 2
        spans = [c[0][0] for c in mock_enqueue.call_args_list]
        server_span = spans[0]
        client_span = spans[1]

        # Same trace_id
        client_trace = client_span.trace_id.replace("-", "").ljust(32, "0")[:32]
        assert server_span.trace_id == client_trace

        # Server parent == client span_id
        client_sid = client_span.span_id.replace("-", "").ljust(16, "0")[:16]
        assert server_span.parent_id == client_sid

        # Correct remote flags
        assert client_span.attributes["remote"] == "true"
        assert server_span.attributes["remote"] == "false"


if __name__ == "__main__":
    unittest.main()
