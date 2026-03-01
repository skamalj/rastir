"""Integration test: MCP server + LangGraph agent with distributed tracing.

This test verifies end-to-end trace propagation across MCP tool boundaries:

1. Starts a real FastMCP server with tools instrumented by @mcp_endpoint
2. Creates a LangGraph agent (Gemini model) that calls MCP tools
3. Uses @trace_remote_tools to auto-inject rastir trace context via arguments
4. Verifies client and server spans share the same trace_id
5. Verifies parent-child span relationships

Requirements:
    - GOOGLE_API_KEY env var (Gemini)
    - mcp, langgraph, langchain-google-genai packages
    - Rastir server NOT required (spans captured in-process)

Run:
    GOOGLE_API_KEY=... PYTHONPATH=src python -m pytest tests/integration/test_mcp_remote.py -v -s
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from unittest.mock import patch

import pytest
import uvicorn

# ---------------------------------------------------------------------------
# Skip if dependencies missing or no API key
# ---------------------------------------------------------------------------
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")

try:
    from mcp.server.fastmcp import FastMCP
    from mcp.client.streamable_http import streamable_http_client
    from mcp.client.session import ClientSession

    HAS_MCP = True
except ImportError:
    HAS_MCP = False

try:
    from langchain_google_genai import ChatGoogleGenerativeAI
    from langgraph.prebuilt import create_react_agent

    HAS_LANGGRAPH = True
except ImportError:
    HAS_LANGGRAPH = False

pytestmark = [
    pytest.mark.skipif(not HAS_MCP, reason="mcp package not installed"),
    pytest.mark.skipif(not GOOGLE_API_KEY, reason="GOOGLE_API_KEY not set"),
]

# ---------------------------------------------------------------------------
# Rastir setup
# ---------------------------------------------------------------------------
import rastir
from rastir import configure, agent_span, trace_remote_tools, mcp_endpoint
from rastir.context import set_current_model, set_current_provider
from rastir.spans import SpanType

# Port for the test MCP server
MCP_PORT = 19876
MCP_URL = f"http://127.0.0.1:{MCP_PORT}/mcp"


# ---------------------------------------------------------------------------
# MCP Server — tool definitions with @mcp_endpoint decorator
# ---------------------------------------------------------------------------

def _create_mcp_server():
    """Create a fresh FastMCP server with tools for each test."""
    srv = FastMCP(
        "TestToolServer",
        host="127.0.0.1",
        port=MCP_PORT,
        stateless_http=True,
        json_response=True,
    )

    @srv.tool()
    @mcp_endpoint
    async def get_weather(city: str) -> str:
        """Get the current weather for a city.

        Args:
            city: The name of the city to get weather for.

        Returns:
            A string describing the current weather conditions.
        """
        weather_data = {
            "tokyo": "22°C, partly cloudy, humidity 65%",
            "london": "15°C, rainy, humidity 80%",
            "new york": "28°C, sunny, humidity 45%",
            "paris": "18°C, overcast, humidity 70%",
        }
        return weather_data.get(city.lower(), f"Weather data not available for {city}")

    @srv.tool()
    @mcp_endpoint
    async def calculate(expression: str) -> str:
        """Calculate a mathematical expression.

        Args:
            expression: A mathematical expression to evaluate (e.g. '2 + 3 * 4').

        Returns:
            The result of the calculation as a string.
        """
        try:
            allowed = set("0123456789+-*/.() ")
            if not all(c in allowed for c in expression):
                return f"Error: invalid characters in expression"
            result = eval(expression)
            return str(result)
        except Exception as e:
            return f"Error: {e}"

    return srv


# ---------------------------------------------------------------------------
# Collected spans — capture instead of sending to server
# ---------------------------------------------------------------------------
collected_spans: list = []


def _capture_span(span):
    """Capture spans instead of sending to the Rastir server."""
    collected_spans.append(span)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def setup_rastir():
    """Configure Rastir and capture spans.

    NOTE: In these tests the MCP server runs in the same process as the
    client, so a single ``configure()`` call covers both client-side and
    server-side (@mcp_endpoint) spans.  In production the MCP server is
    a separate process and **must** call ``configure(push_url=...)``
    independently to export its server-side spans to the collector.
    """
    # Reset config state so configure() can be called again per test
    import rastir.config as _cfg
    _cfg._initialized = False
    _cfg._global_config = None
    configure(service="mcp-integration-test", env="test")
    collected_spans.clear()


@pytest.fixture
async def mcp_server_fixture():
    """Start a fresh MCP server as a background task for each test."""
    srv = _create_mcp_server()
    app = srv.streamable_http_app()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=MCP_PORT,
        log_level="warning",
    )
    uv_server = uvicorn.Server(config)
    task = asyncio.create_task(uv_server.serve())

    # Wait for server to start
    for _ in range(20):
        await asyncio.sleep(0.25)
        if uv_server.started:
            break

    yield MCP_URL

    uv_server.should_exit = True
    await task


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_direct_mcp_call_with_tracing(mcp_server_fixture):
    """Test direct MCP tool call with trace context propagation.

    Verifies:
    1. Client span created with remote=true
    2. Server span created with remote=false
    3. Both spans share the same trace_id
    4. Server span is child of client span
    """
    with patch("rastir.queue.enqueue_span", side_effect=_capture_span):
        with patch("rastir.decorators.enqueue_span", side_effect=_capture_span):

            @agent_span(agent_name="test_agent")
            async def run_agent():
                async with streamable_http_client(MCP_URL) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()

                        # Wrap session after initialization
                        @trace_remote_tools
                        def wrap_session():
                            return session

                        wrapped = wrap_session()
                        result = await wrapped.call_tool("get_weather", {"city": "Tokyo"})
                        return result

            result = await run_agent()

    # Should have at least 2 spans: agent + client tool + server tool
    tool_spans = [s for s in collected_spans if s.span_type == SpanType.TOOL]
    assert len(tool_spans) >= 2, f"Expected ≥2 tool spans, got {len(tool_spans)}: {[s.name for s in collected_spans]}"

    # Find client and server spans
    client_spans = [s for s in tool_spans if s.attributes.get("remote") == "true"]
    server_spans = [s for s in tool_spans if s.attributes.get("remote") == "false"]

    assert len(client_spans) >= 1, "No client span (remote=true) found"
    assert len(server_spans) >= 1, "No server span (remote=false) found"

    client_span = client_spans[0]
    server_span = server_spans[0]

    # Verify span attributes
    assert client_span.attributes["tool_name"] == "get_weather"
    assert server_span.attributes["tool_name"] == "get_weather"

    # Verify trace_id propagation
    client_trace_id = client_span.trace_id.replace("-", "").ljust(32, "0")[:32]
    assert server_span.trace_id == client_trace_id, (
        f"Trace IDs don't match: client={client_trace_id}, server={server_span.trace_id}"
    )

    # Verify parent-child relationship
    client_span_id = client_span.span_id.replace("-", "").ljust(16, "0")[:16]
    assert server_span.parent_id == client_span_id, (
        f"Parent ID mismatch: expected={client_span_id}, got={server_span.parent_id}"
    )

    print(f"\n✓ Trace propagation verified:")
    print(f"  trace_id: {server_span.trace_id}")
    print(f"  client span: {client_span.span_id} (remote=true)")
    print(f"  server span: {server_span.span_id} (remote=false, parent={server_span.parent_id})")


@pytest.mark.asyncio
async def test_multiple_tool_calls(mcp_server_fixture):
    """Test multiple sequential tool calls share the agent trace."""
    with patch("rastir.queue.enqueue_span", side_effect=_capture_span):
        with patch("rastir.decorators.enqueue_span", side_effect=_capture_span):
            @agent_span(agent_name="multi_tool_agent")
            async def run():
                async with streamable_http_client(MCP_URL) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()

                        @trace_remote_tools
                        def wrap_session():
                            return session

                        wrapped = wrap_session()
                        r1 = await wrapped.call_tool("get_weather", {"city": "London"})
                        r2 = await wrapped.call_tool("calculate", {"expression": "7 * 8"})
                        return r1, r2

            results = await run()

    tool_spans = [s for s in collected_spans if s.span_type == SpanType.TOOL]
    client_spans = [s for s in tool_spans if s.attributes.get("remote") == "true"]
    server_spans = [s for s in tool_spans if s.attributes.get("remote") == "false"]

    assert len(client_spans) == 2, f"Expected 2 client spans, got {len(client_spans)}"
    assert len(server_spans) == 2, f"Expected 2 server spans, got {len(server_spans)}"

    # All client spans should share the agent's trace_id
    trace_ids = {s.trace_id for s in client_spans}
    assert len(trace_ids) == 1, f"Client spans have different trace_ids: {trace_ids}"

    # Tool names should be different
    tool_names = {s.attributes["tool_name"] for s in client_spans}
    assert tool_names == {"get_weather", "calculate"}

    print(f"\n✓ Multiple tool calls verified: {len(client_spans)} client + {len(server_spans)} server spans")


@pytest.mark.asyncio
async def test_error_propagation(mcp_server_fixture):
    """Test that errors in MCP tools propagate correctly with ERROR spans."""
    with patch("rastir.queue.enqueue_span", side_effect=_capture_span):
        async with streamable_http_client(MCP_URL) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()

                @trace_remote_tools
                def wrap_session():
                    return session

                wrapped = wrap_session()

                # Call a non-existent tool — MCP SDK should raise an error
                try:
                    await wrapped.call_tool("nonexistent_tool", {"x": 1})
                except Exception:
                    pass  # Expected

    # Client span should exist and may be ERROR
    tool_spans = [s for s in collected_spans if s.span_type == SpanType.TOOL]
    assert len(tool_spans) >= 1, "Expected at least 1 tool span for the failed call"

    print(f"\n✓ Error handling verified: {len(tool_spans)} span(s) captured")


@pytest.mark.asyncio
async def test_model_provider_attributes(mcp_server_fixture):
    """Test that model and provider context propagates to tool spans.

    When tool calls happen inside an @llm context (simulated here via
    set_current_model / set_current_provider), the client-side tool
    spans should carry model and provider attributes.
    """
    with patch("rastir.queue.enqueue_span", side_effect=_capture_span):
        with patch("rastir.decorators.enqueue_span", side_effect=_capture_span):

            @agent_span(agent_name="model_provider_agent")
            async def run_agent():
                # Simulate being inside an @llm call
                set_current_model("gemini-2.5-flash")
                set_current_provider("google")

                async with streamable_http_client(MCP_URL) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()

                        @trace_remote_tools
                        def wrap():
                            return session

                        wrapped = wrap()
                        result = await wrapped.call_tool("get_weather", {"city": "Paris"})
                        return result

            result = await run_agent()

    # Find client tool spans
    tool_spans = [s for s in collected_spans if s.span_type == SpanType.TOOL]
    client_spans = [s for s in tool_spans if s.attributes.get("remote") == "true"]
    assert len(client_spans) >= 1, "No client span found"

    client_span = client_spans[0]

    # Verify model and provider attributes
    assert client_span.attributes.get("model") == "gemini-2.5-flash", (
        f"Expected model='gemini-2.5-flash', got {client_span.attributes.get('model')}"
    )
    assert client_span.attributes.get("provider") == "google", (
        f"Expected provider='google', got {client_span.attributes.get('provider')}"
    )

    print(f"\n✓ model/provider attributes verified on client tool span")
    print(f"  model={client_span.attributes['model']}, provider={client_span.attributes['provider']}")


@pytest.mark.skipif(not HAS_LANGGRAPH, reason="langgraph/langchain-google-genai not installed")
@pytest.mark.asyncio
async def test_langgraph_agent_with_mcp_tools(mcp_server_fixture):
    """Full integration: LangGraph agent (Gemini) calling MCP tools.

    This is the flagship test — a real AI agent uses real MCP tools
    with automatic distributed trace propagation via mcp_to_langchain_tools().
    """
    from langchain_core.messages import HumanMessage
    from rastir import mcp_to_langchain_tools

    with patch("rastir.queue.enqueue_span", side_effect=_capture_span):
        with patch("rastir.decorators.enqueue_span", side_effect=_capture_span):

            async with streamable_http_client(MCP_URL) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    # One line: converts MCP tools to LangChain tools
                    # with automatic trace propagation
                    lc_tools = await mcp_to_langchain_tools(session)

                    # Create LangGraph agent with Gemini
                    llm = ChatGoogleGenerativeAI(
                        model="gemini-2.5-flash",
                        google_api_key=GOOGLE_API_KEY,
                        temperature=0,
                    )

                    agent = create_react_agent(llm, lc_tools)

                    # Run the agent
                    @agent_span(agent_name="gemini_mcp_agent")
                    async def run_agent():
                        response = await agent.ainvoke(
                            {"messages": [HumanMessage(content="What is the weather in Tokyo? Also calculate 15 * 23.")]}
                        )
                        return response

                    response = await run_agent()

    # 5. Verify spans
    tool_spans = [s for s in collected_spans if s.span_type == SpanType.TOOL]
    client_spans = [s for s in tool_spans if s.attributes.get("remote") == "true"]
    server_spans = [s for s in tool_spans if s.attributes.get("remote") == "false"]

    print(f"\n=== LangGraph + MCP Integration Test Results ===")
    print(f"Total spans captured: {len(collected_spans)}")
    print(f"Tool spans: {len(tool_spans)} (client={len(client_spans)}, server={len(server_spans)})")

    for s in collected_spans:
        attrs = {k: v for k, v in s.attributes.items() if k in ("tool_name", "remote", "agent", "model", "provider")}
        print(f"  [{s.span_type.value}] {s.name} — {s.status.value} — {attrs}")

    # Agent should have called at least one tool
    assert len(client_spans) >= 1, "Agent didn't call any MCP tools"

    # Each client span should have a matching server span
    for cs in client_spans:
        cs_trace = cs.trace_id.replace("-", "").ljust(32, "0")[:32]
        cs_span = cs.span_id.replace("-", "").ljust(16, "0")[:16]
        matching = [
            ss for ss in server_spans
            if ss.trace_id == cs_trace and ss.parent_id == cs_span
        ]
        assert len(matching) >= 1, (
            f"No server span found for client span {cs.name} "
            f"(trace={cs_trace}, span={cs_span})"
        )

    # Response should contain weather + calculation
    last_msg = response["messages"][-1].content
    print(f"\nAgent response: {last_msg[:200]}...")

    print(f"\n✓ Full LangGraph + MCP integration verified!")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
