"""Unit tests for rastir.remote — argument-based trace propagation.

Tests cover:
- @trace_remote_tools: session wrapping, rastir_* injection into arguments
- @mcp_endpoint: rastir_* extraction, span linking, signature extension
- mcp_to_langchain_tools: schema building, rastir_* filtering
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
    mcp_to_langchain_tools,
    trace_remote_tools,
)
from rastir.spans import SpanRecord, SpanType


class TestTraceRemoteTools(unittest.TestCase):
    """Test @trace_remote_tools decorator."""

    def test_wraps_client_session(self):
        """Session's call_tool should be replaced by traced version."""
        session = MagicMock()
        type(session).__name__ = "ClientSession"
        type(session).__module__ = "mcp.client.session"
        original = AsyncMock(return_value="result")
        session.call_tool = original

        @trace_remote_tools
        def get_session():
            return session

        result = get_session()
        assert result.call_tool is not original

    def test_wraps_session_in_tuple(self):
        """Session inside a tuple should also be wrapped."""
        session = MagicMock()
        type(session).__name__ = "ClientSession"
        type(session).__module__ = "mcp.client.session"
        original = AsyncMock()
        session.call_tool = original

        @trace_remote_tools
        def get_resources():
            return (["tool1"], session)

        result = get_resources()
        assert isinstance(result, tuple)
        assert result[1].call_tool is not original

    def test_non_session_passthrough(self):
        """Non-session return values pass through unchanged."""
        @trace_remote_tools
        def get_data():
            return {"key": "value"}

        assert get_data() == {"key": "value"}

    def test_async_function_support(self):
        """Async functions should be supported."""
        session = MagicMock()
        type(session).__name__ = "ClientSession"
        type(session).__module__ = "mcp.client.session"
        session.call_tool = AsyncMock()

        @trace_remote_tools
        async def get_session():
            return session

        result = asyncio.get_event_loop().run_until_complete(get_session())
        assert not isinstance(result.call_tool, AsyncMock)

    @patch("rastir.queue.enqueue_span")
    def test_call_tool_creates_span(self, mock_enqueue):
        """Wrapped call_tool should create a tool span with remote=true."""
        session = MagicMock()
        type(session).__name__ = "ClientSession"
        type(session).__module__ = "mcp.client.session"
        session.call_tool = AsyncMock(return_value="ok")

        @trace_remote_tools
        def get_session():
            return session

        wrapped = get_session()
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
        session = MagicMock()
        type(session).__name__ = "ClientSession"
        type(session).__module__ = "mcp.client.session"

        captured_args = {}
        async def fake_call_tool(name, arguments=None, *a, **kw):
            captured_args.update(arguments or {})
            return "ok"
        session.call_tool = fake_call_tool

        @trace_remote_tools
        def get_session():
            return session

        wrapped = get_session()
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
        """Original arguments should not be mutated."""
        session = MagicMock()
        type(session).__name__ = "ClientSession"
        type(session).__module__ = "mcp.client.session"

        captured = {}
        async def fake(name, arguments=None, *a, **kw):
            captured.update(arguments or {})
            return "ok"
        session.call_tool = fake

        @trace_remote_tools
        def get_session():
            return session

        original_args = {"city": "Tokyo"}
        wrapped = get_session()
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
        session = MagicMock()
        type(session).__name__ = "ClientSession"
        type(session).__module__ = "mcp.client.session"
        session.call_tool = AsyncMock(side_effect=RuntimeError("down"))

        @trace_remote_tools
        def get_session():
            return session

        wrapped = get_session()
        with self.assertRaises(RuntimeError):
            asyncio.get_event_loop().run_until_complete(
                wrapped.call_tool("search", {"q": "hi"})
            )

        span = mock_enqueue.call_args[0][0]
        assert span.status.value == "ERROR"


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
        type(session).__name__ = "ClientSession"
        type(session).__module__ = "mcp.client.session"

        async def fake_call_tool(name, arguments=None, *a, **kw):
            # Simulate server receiving arguments with rastir_* fields
            return await server_search(**arguments)

        session.call_tool = fake_call_tool

        @trace_remote_tools
        async def get_session():
            return session

        async def run():
            s = await get_session()
            return await s.call_tool("search", {"query": "test"})

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


class TestMcpToLangchainTools(unittest.TestCase):
    """Tests for mcp_to_langchain_tools helper."""

    @patch("rastir.queue.enqueue_span")
    def test_returns_structured_tools(self, mock_enqueue):
        """Should return a StructuredTool for each MCP tool."""
        from langchain_core.tools import StructuredTool

        tool1 = MagicMock()
        tool1.name = "weather"
        tool1.description = "Get weather"
        tool1.inputSchema = {
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        }
        tool2 = MagicMock()
        tool2.name = "calc"
        tool2.description = "Calculator"
        tool2.inputSchema = {
            "properties": {"expr": {"type": "string"}},
            "required": ["expr"],
        }

        resp = MagicMock()
        resp.tools = [tool1, tool2]
        session = AsyncMock()
        session.list_tools = AsyncMock(return_value=resp)

        tools = asyncio.get_event_loop().run_until_complete(
            mcp_to_langchain_tools(session)
        )

        assert len(tools) == 2
        assert all(isinstance(t, StructuredTool) for t in tools)
        assert tools[0].name == "weather"
        assert tools[1].name == "calc"

    @patch("rastir.queue.enqueue_span")
    def test_filters_rastir_fields_from_schema(self, mock_enqueue):
        """Should exclude rastir_* fields from args_schema."""
        tool1 = MagicMock()
        tool1.name = "search"
        tool1.description = "Search"
        tool1.inputSchema = {
            "properties": {
                "query": {"type": "string"},
                "rastir_trace_id": {"type": "string"},
                "rastir_span_id": {"type": "string"},
            },
            "required": ["query"],
        }

        resp = MagicMock()
        resp.tools = [tool1]
        session = AsyncMock()
        session.list_tools = AsyncMock(return_value=resp)

        tools = asyncio.get_event_loop().run_until_complete(
            mcp_to_langchain_tools(session)
        )

        fields = tools[0].args_schema.model_fields
        assert "query" in fields
        assert "rastir_trace_id" not in fields
        assert "rastir_span_id" not in fields

    @patch("rastir.queue.enqueue_span")
    def test_args_schema_types(self, mock_enqueue):
        """Should map JSON schema types correctly."""
        tool1 = MagicMock()
        tool1.name = "test"
        tool1.description = "Test"
        tool1.inputSchema = {
            "properties": {
                "name": {"type": "string"},
                "count": {"type": "integer"},
                "ratio": {"type": "number"},
                "flag": {"type": "boolean"},
            },
            "required": ["name"],
        }

        resp = MagicMock()
        resp.tools = [tool1]
        session = AsyncMock()
        session.list_tools = AsyncMock(return_value=resp)

        tools = asyncio.get_event_loop().run_until_complete(
            mcp_to_langchain_tools(session)
        )

        fields = tools[0].args_schema.model_fields
        assert fields["name"].is_required()
        assert not fields["count"].is_required()

    @patch("rastir.queue.enqueue_span")
    def test_calls_tool_via_session(self, mock_enqueue):
        """Should call session.call_tool with correct args."""
        tool1 = MagicMock()
        tool1.name = "greet"
        tool1.description = "Greet"
        tool1.inputSchema = {
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }

        content_item = MagicMock()
        content_item.text = "Hello!"
        call_result = MagicMock()
        call_result.content = [content_item]

        resp = MagicMock()
        resp.tools = [tool1]
        session = AsyncMock()
        session.list_tools = AsyncMock(return_value=resp)
        session.call_tool = AsyncMock(return_value=call_result)

        tools = asyncio.get_event_loop().run_until_complete(
            mcp_to_langchain_tools(session)
        )

        result = asyncio.get_event_loop().run_until_complete(
            tools[0].ainvoke({"name": "World"})
        )
        assert "Hello!" in result

    @patch("rastir.queue.enqueue_span")
    def test_trace_false_skips_wrapping(self, mock_enqueue):
        """With trace=False, no rastir_* injection."""
        tool1 = MagicMock()
        tool1.name = "ping"
        tool1.description = "Ping"
        tool1.inputSchema = {
            "properties": {"host": {"type": "string"}},
            "required": ["host"],
        }

        content_item = MagicMock()
        content_item.text = "pong"
        call_result = MagicMock()
        call_result.content = [content_item]

        resp = MagicMock()
        resp.tools = [tool1]
        session = AsyncMock()
        session.list_tools = AsyncMock(return_value=resp)
        session.call_tool = AsyncMock(return_value=call_result)

        tools = asyncio.get_event_loop().run_until_complete(
            mcp_to_langchain_tools(session, trace=False)
        )

        asyncio.get_event_loop().run_until_complete(
            tools[0].ainvoke({"host": "localhost"})
        )
        session.call_tool.assert_called_once()

    @patch("rastir.queue.enqueue_span")
    def test_empty_input_schema(self, mock_enqueue):
        """Should handle tools with no inputSchema."""
        tool1 = MagicMock()
        tool1.name = "noop"
        tool1.description = "No-op"
        tool1.inputSchema = None

        resp = MagicMock()
        resp.tools = [tool1]
        session = AsyncMock()
        session.list_tools = AsyncMock(return_value=resp)

        tools = asyncio.get_event_loop().run_until_complete(
            mcp_to_langchain_tools(session)
        )

        assert len(tools) == 1
        assert len(tools[0].args_schema.model_fields) == 0


if __name__ == "__main__":
    unittest.main()
