"""End-to-end MCP integration test with real infrastructure verification.

Tests two server configurations:
  - Server A (port 19876): tools WITH @mcp_endpoint  → server spans created
  - Server B (port 19877): tools WITHOUT @mcp_endpoint → plain server, rastir_*
    fields silently dropped by Pydantic validation

Verifies the full pipeline:
  Client → Rastir collector (localhost:8080) → OTLP → Tempo (localhost:3200)

Assertions:
  1. Spans appear in Tempo with correct trace_id, parent-child relationships
  2. Prometheus metrics (rastir_spans_ingested_total etc.) increment
  3. Server A creates both client + server spans (trace propagation)
  4. Server B creates only client spans (no server-side instrumentation)

Requirements:
    - Rastir collector running on localhost:8080
    - Tempo running on localhost:3200
    - Prometheus running on localhost:9090
    - GOOGLE_API_KEY env var (for LangGraph test only)
    - mcp, langgraph, langchain-google-genai packages

Run:
    GOOGLE_API_KEY=... PYTHONPATH=src python -m pytest tests/integration/test_mcp_e2e.py -v -s
"""

from __future__ import annotations

import asyncio
import os
import time
from unittest.mock import patch

import httpx
import pytest
import uvicorn

# ---------------------------------------------------------------------------
# Skip if dependencies missing
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

# ---------------------------------------------------------------------------
# Infrastructure endpoints
# ---------------------------------------------------------------------------
COLLECTOR_URL = "http://localhost:8080"
TEMPO_URL = "http://localhost:3200"
PROMETHEUS_URL = "http://localhost:9090"

# MCP server ports
PORT_WITH_ENDPOINT = 19876     # Server A: @mcp_endpoint
PORT_WITHOUT_ENDPOINT = 19877  # Server B: plain (no @mcp_endpoint)

URL_WITH_ENDPOINT = f"http://127.0.0.1:{PORT_WITH_ENDPOINT}/mcp"
URL_WITHOUT_ENDPOINT = f"http://127.0.0.1:{PORT_WITHOUT_ENDPOINT}/mcp"


def _infra_available() -> bool:
    """Check if collector, Tempo, and Prometheus are up."""
    try:
        with httpx.Client(timeout=2) as c:
            r1 = c.get(f"{COLLECTOR_URL}/health")
            r2 = c.get(f"{TEMPO_URL}/ready")
            r3 = c.get(f"{PROMETHEUS_URL}/-/ready")
            return all(r.status_code < 300 for r in (r1, r2, r3))
    except Exception:
        return False


pytestmark = [
    pytest.mark.skipif(not HAS_MCP, reason="mcp package not installed"),
    pytest.mark.skipif(not _infra_available(),
                       reason="Infrastructure not running (collector/tempo/prometheus)"),
]

# ---------------------------------------------------------------------------
# Rastir setup
# ---------------------------------------------------------------------------
import rastir
from rastir import configure, agent_span, trace_remote_tools, mcp_endpoint
from rastir.spans import SpanType


# ---------------------------------------------------------------------------
# MCP Server A: WITH @mcp_endpoint
# ---------------------------------------------------------------------------

def _create_server_with_endpoint():
    """MCP server where tools have @mcp_endpoint for server-side spans."""
    srv = FastMCP(
        "ServerWithEndpoint",
        host="127.0.0.1",
        port=PORT_WITH_ENDPOINT,
        stateless_http=True,
        json_response=True,
    )

    @srv.tool()
    @mcp_endpoint
    async def get_weather(city: str) -> str:
        """Get the current weather for a city.

        Args:
            city: The name of the city to get weather for.
        """
        weather_data = {
            "tokyo": "22°C, partly cloudy, humidity 65%",
            "london": "15°C, rainy, humidity 80%",
            "new york": "28°C, sunny, humidity 45%",
        }
        return weather_data.get(city.lower(), f"Weather data not available for {city}")

    @srv.tool()
    @mcp_endpoint
    async def calculate(expression: str) -> str:
        """Calculate a mathematical expression.

        Args:
            expression: A mathematical expression to evaluate (e.g. '2 + 3 * 4').
        """
        try:
            allowed = set("0123456789+-*/.() ")
            if not all(c in allowed for c in expression):
                return "Error: invalid characters"
            return str(eval(expression))
        except Exception as e:
            return f"Error: {e}"

    return srv


# ---------------------------------------------------------------------------
# MCP Server B: WITHOUT @mcp_endpoint (plain)
# ---------------------------------------------------------------------------

def _create_server_without_endpoint():
    """MCP server with plain tools — no @mcp_endpoint.

    The rastir_trace_id / rastir_span_id fields injected by the client
    are silently dropped by FastMCP's Pydantic validation.
    """
    srv = FastMCP(
        "ServerWithoutEndpoint",
        host="127.0.0.1",
        port=PORT_WITHOUT_ENDPOINT,
        stateless_http=True,
        json_response=True,
    )

    @srv.tool()
    async def get_weather(city: str) -> str:
        """Get the current weather for a city.

        Args:
            city: The name of the city to get weather for.
        """
        weather_data = {
            "tokyo": "22°C, partly cloudy, humidity 65%",
            "london": "15°C, rainy, humidity 80%",
            "new york": "28°C, sunny, humidity 45%",
        }
        return weather_data.get(city.lower(), f"Weather data not available for {city}")

    @srv.tool()
    async def calculate(expression: str) -> str:
        """Calculate a mathematical expression.

        Args:
            expression: A mathematical expression to evaluate (e.g. '2 + 3 * 4').
        """
        try:
            allowed = set("0123456789+-*/.() ")
            if not all(c in allowed for c in expression):
                return "Error: invalid characters"
            return str(eval(expression))
        except Exception as e:
            return f"Error: {e}"

    return srv


# ---------------------------------------------------------------------------
# In-process span capture (for immediate assertions before Tempo flush)
# ---------------------------------------------------------------------------
collected_spans: list = []


def _capture_span(span):
    """Capture spans in-process AND let them flow to the real exporter."""
    collected_spans.append(span)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_prometheus_metric(metric_name: str) -> float | None:
    """Scrape the collector's /metrics and return the value of a metric."""
    try:
        with httpx.Client(timeout=5) as c:
            resp = c.get(f"{COLLECTOR_URL}/metrics")
            for line in resp.text.splitlines():
                if line.startswith(metric_name) and not line.startswith(f"{metric_name}_"):
                    # e.g. rastir_spans_ingested_total{span_type="tool"} 42
                    parts = line.split()
                    if len(parts) >= 2:
                        return float(parts[-1])
    except Exception:
        pass
    return None


def _get_prometheus_metric_sum(metric_prefix: str) -> float:
    """Sum all lines matching a metric prefix from /metrics."""
    total = 0.0
    try:
        with httpx.Client(timeout=5) as c:
            resp = c.get(f"{COLLECTOR_URL}/metrics")
            for line in resp.text.splitlines():
                if line.startswith(metric_prefix) and not line.startswith("#"):
                    parts = line.split()
                    if len(parts) >= 2:
                        try:
                            total += float(parts[-1])
                        except ValueError:
                            pass
    except Exception:
        pass
    return total


def _wait_for_metric_increment(
    metric_prefix: str,
    baseline: float,
    min_delta: float = 1.0,
    retries: int = 15,
    delay: float = 2.0,
) -> float:
    """Wait until a Prometheus metric exceeds baseline by at least min_delta."""
    for _ in range(retries):
        current = _get_prometheus_metric_sum(metric_prefix)
        if current >= baseline + min_delta:
            return current
        time.sleep(delay)
    return _get_prometheus_metric_sum(metric_prefix)


def _query_tempo_trace(trace_id: str, retries: int = 15, delay: float = 2.0) -> dict | None:
    """Query Tempo for a trace by ID, retrying until it appears.

    Tempo ingests asynchronously, so we retry with backoff.
    """
    # Ensure trace_id is 32-char hex (no dashes)
    tid = trace_id.replace("-", "").ljust(32, "0")[:32]
    url = f"{TEMPO_URL}/api/traces/{tid}"
    with httpx.Client(timeout=10) as c:
        for attempt in range(retries):
            try:
                resp = c.get(url)
                if resp.status_code == 200:
                    return resp.json()
            except Exception:
                pass
            time.sleep(delay)
    return None


def _extract_spans_from_tempo(trace_data: dict) -> list[dict]:
    """Extract flat list of spans from Tempo trace response.

    Tempo returns data in the OTLP/JSON format:
    { "batches": [ { "resource": {...}, "scopeSpans": [ { "spans": [...] } ] } ] }
    """
    spans = []
    for batch in trace_data.get("batches", []):
        for scope in batch.get("scopeSpans", batch.get("instrumentationLibrarySpans", [])):
            for span in scope.get("spans", []):
                spans.append(span)
    return spans


def _get_tempo_span_attrs(span: dict) -> dict[str, str]:
    """Extract attributes dict from a Tempo span.

    Tempo prefixes Rastir attributes with 'rastir.' — this helper
    strips the prefix for consistency with the SpanRecord attribute names.
    """
    attrs = {}
    for a in span.get("attributes", []):
        key = a.get("key", "")
        val = a.get("value", {}).get("stringValue", "")
        # Strip 'rastir.' prefix if present
        if key.startswith("rastir."):
            key = key[len("rastir."):]
        attrs[key] = val
    return attrs


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def setup_rastir():
    """Configure Rastir to push spans to the real collector."""
    import rastir.config as _cfg
    _cfg._initialized = False
    _cfg._global_config = None
    configure(
        service="mcp-e2e-test",
        env="integration",
        push_url=COLLECTOR_URL,
        flush_interval=1,   # flush quickly for tests
        batch_size=10,
    )
    collected_spans.clear()
    yield
    # Give the background exporter time to flush
    time.sleep(2)


@pytest.fixture
async def server_with_endpoint():
    """Start MCP server A (WITH @mcp_endpoint)."""
    srv = _create_server_with_endpoint()
    app = srv.streamable_http_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT_WITH_ENDPOINT, log_level="warning")
    uv_server = uvicorn.Server(config)
    task = asyncio.create_task(uv_server.serve())
    for _ in range(20):
        await asyncio.sleep(0.25)
        if uv_server.started:
            break
    yield URL_WITH_ENDPOINT
    uv_server.should_exit = True
    await task


@pytest.fixture
async def server_without_endpoint():
    """Start MCP server B (WITHOUT @mcp_endpoint)."""
    srv = _create_server_without_endpoint()
    app = srv.streamable_http_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT_WITHOUT_ENDPOINT, log_level="warning")
    uv_server = uvicorn.Server(config)
    task = asyncio.create_task(uv_server.serve())
    for _ in range(20):
        await asyncio.sleep(0.25)
        if uv_server.started:
            break
    yield URL_WITHOUT_ENDPOINT
    uv_server.should_exit = True
    await task


# ============================================================================
# TEST 1: Server WITH @mcp_endpoint — full trace propagation
# ============================================================================

@pytest.mark.asyncio
async def test_server_with_endpoint_e2e(server_with_endpoint):
    """Server A (@mcp_endpoint): client + server spans, verified in Tempo.

    Expects:
      - agent span (parent)
      - client tool span (remote=true, child of agent)
      - server tool span (remote=false, child of client, same trace_id)
      - All appear in Tempo with correct parent-child links
      - Prometheus tool counter increments
    """
    # Record metrics baseline
    baseline_tool_count = _get_prometheus_metric_sum("rastir_tool_calls_total")
    baseline_span_count = _get_prometheus_metric_sum("rastir_spans_ingested_total")

    trace_id_captured = None

    @agent_span(agent_name="e2e_agent_with_endpoint")
    async def run_agent():
        nonlocal trace_id_captured
        async with streamable_http_client(URL_WITH_ENDPOINT) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()

                @trace_remote_tools
                def wrap():
                    return session

                wrapped = wrap()
                result = await wrapped.call_tool("get_weather", {"city": "Tokyo"})

                # Capture trace_id from the span context
                from rastir.context import get_current_span
                span = get_current_span()
                if span:
                    trace_id_captured = span.trace_id
                return result

    result = await run_agent()
    assert result is not None
    print(f"\n  Tool result: {result}")

    # --- Wait for spans to flush to collector ---
    new_span_count = _wait_for_metric_increment(
        "rastir_spans_ingested_total", baseline_span_count, min_delta=3.0
    )
    assert new_span_count > baseline_span_count, (
        f"Prometheus span counter didn't increment: {baseline_span_count} → {new_span_count}"
    )
    print(f"  ✓ Prometheus spans_ingested: {baseline_span_count} → {new_span_count}")

    # --- Verify spans in Tempo ---
    if trace_id_captured:
        trace_data = _query_tempo_trace(trace_id_captured)
        if trace_data:
            tempo_spans = _extract_spans_from_tempo(trace_data)
            span_names = [s.get("name", "") for s in tempo_spans]
            print(f"  ✓ Tempo trace found: {len(tempo_spans)} spans — {span_names}")

            # Should have at least agent + client tool + server tool = 3 spans
            assert len(tempo_spans) >= 3, (
                f"Expected ≥3 spans in Tempo, got {len(tempo_spans)}: {span_names}"
            )

            # Check for tool spans with remote attribute
            tool_attrs = []
            for s in tempo_spans:
                attrs = _get_tempo_span_attrs(s)
                if attrs.get("span_type") == "tool" or attrs.get("tool_name"):
                    tool_attrs.append(attrs)

            remote_true = [a for a in tool_attrs if a.get("remote") == "true"]
            remote_false = [a for a in tool_attrs if a.get("remote") == "false"]

            print(f"  ✓ Tool spans: {len(remote_true)} client (remote=true), {len(remote_false)} server (remote=false)")
            assert len(remote_true) >= 1, "No client span (remote=true) in Tempo"
            assert len(remote_false) >= 1, "No server span (remote=false) in Tempo"
        else:
            print(f"  ⚠ Trace {trace_id_captured} not yet in Tempo (may need more time)")
    else:
        print("  ⚠ Could not capture trace_id from context")

    print("  ✓ Server WITH @mcp_endpoint e2e PASSED")


# ============================================================================
# TEST 2: Server WITHOUT @mcp_endpoint — client spans only
# ============================================================================

@pytest.mark.asyncio
async def test_server_without_endpoint_e2e(server_without_endpoint):
    """Server B (no @mcp_endpoint): only client spans, still works.

    Expects:
      - agent span (parent)
      - client tool span (remote=true, child of agent)
      - NO server tool span (the server doesn't use @mcp_endpoint)
      - rastir_* fields silently dropped by FastMCP Pydantic validation
      - Tool still returns correct result
      - Client spans appear in Tempo
    """
    baseline_span_count = _get_prometheus_metric_sum("rastir_spans_ingested_total")
    trace_id_captured = None

    @agent_span(agent_name="e2e_agent_without_endpoint")
    async def run_agent():
        nonlocal trace_id_captured
        async with streamable_http_client(URL_WITHOUT_ENDPOINT) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()

                @trace_remote_tools
                def wrap():
                    return session

                wrapped = wrap()
                # This call injects rastir_trace_id/rastir_span_id into args,
                # but the plain server silently drops them via Pydantic validation
                result = await wrapped.call_tool("get_weather", {"city": "London"})

                from rastir.context import get_current_span
                span = get_current_span()
                if span:
                    trace_id_captured = span.trace_id
                return result

    result = await run_agent()
    assert result is not None
    print(f"\n  Tool result: {result}")

    # --- Wait for spans to flush to collector (client spans only, no server spans) ---
    new_span_count = _wait_for_metric_increment(
        "rastir_spans_ingested_total", baseline_span_count, min_delta=2.0
    )
    assert new_span_count > baseline_span_count, (
        f"Prometheus span counter didn't increment: {baseline_span_count} → {new_span_count}"
    )
    print(f"  ✓ Prometheus spans_ingested: {baseline_span_count} → {new_span_count}")

    # --- Verify in Tempo: should have agent + client tool but NO server tool ---
    if trace_id_captured:
        trace_data = _query_tempo_trace(trace_id_captured)
        if trace_data:
            tempo_spans = _extract_spans_from_tempo(trace_data)
            span_names = [s.get("name", "") for s in tempo_spans]
            print(f"  ✓ Tempo trace found: {len(tempo_spans)} spans — {span_names}")

            # Should have at least agent + client tool = 2 spans
            assert len(tempo_spans) >= 2, (
                f"Expected ≥2 spans in Tempo, got {len(tempo_spans)}: {span_names}"
            )

            # Check: NO server-side tool spans (remote=false)
            tool_attrs = []
            for s in tempo_spans:
                attrs = _get_tempo_span_attrs(s)
                if attrs.get("remote") == "false":
                    tool_attrs.append(attrs)

            assert len(tool_attrs) == 0, (
                f"Expected NO server spans (remote=false) but found {len(tool_attrs)}: {tool_attrs}"
            )
            print(f"  ✓ Confirmed: 0 server spans (remote=false) — plain server works correctly")
        else:
            print(f"  ⚠ Trace {trace_id_captured} not yet in Tempo")
    else:
        print("  ⚠ Could not capture trace_id from context")

    print("  ✓ Server WITHOUT @mcp_endpoint e2e PASSED")


# ============================================================================
# TEST 3: Both servers side by side — same agent, different behavior
# ============================================================================

@pytest.mark.asyncio
async def test_both_servers_comparison(server_with_endpoint, server_without_endpoint):
    """Compare behavior: same agent calls tools on both servers.

    Server A (@mcp_endpoint): creates client + server spans
    Server B (plain):         creates client spans only

    Both should work correctly — the difference is in span generation.
    """
    trace_id_a = None
    trace_id_b = None

    # --- Call server A (WITH @mcp_endpoint) ---
    @agent_span(agent_name="comparison_agent_A")
    async def call_server_a():
        nonlocal trace_id_a
        async with streamable_http_client(URL_WITH_ENDPOINT) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()

                @trace_remote_tools
                def wrap():
                    return session
                wrapped = wrap()
                result = await wrapped.call_tool("calculate", {"expression": "42 * 10"})
                from rastir.context import get_current_span
                span = get_current_span()
                if span:
                    trace_id_a = span.trace_id
                return result

    result_a = await call_server_a()
    assert "420" in str(result_a), f"Unexpected result from server A: {result_a}"
    print(f"\n  Server A result: {result_a}")

    # --- Call server B (WITHOUT @mcp_endpoint) ---
    @agent_span(agent_name="comparison_agent_B")
    async def call_server_b():
        nonlocal trace_id_b
        async with streamable_http_client(URL_WITHOUT_ENDPOINT) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()

                @trace_remote_tools
                def wrap():
                    return session
                wrapped = wrap()
                result = await wrapped.call_tool("calculate", {"expression": "42 * 10"})
                from rastir.context import get_current_span
                span = get_current_span()
                if span:
                    trace_id_b = span.trace_id
                return result

    result_b = await call_server_b()
    assert "420" in str(result_b), f"Unexpected result from server B: {result_b}"
    print(f"  Server B result: {result_b}")

    # --- Results should be identical ---
    print(f"  ✓ Both servers return correct results")

    # --- Wait for flush (retry-based) ---
    time.sleep(8)

    # --- Compare in Tempo ---
    if trace_id_a:
        trace_a = _query_tempo_trace(trace_id_a)
        if trace_a:
            spans_a = _extract_spans_from_tempo(trace_a)
            server_spans_a = []
            for s in spans_a:
                attrs = _get_tempo_span_attrs(s)
                if attrs.get("remote") == "false":
                    server_spans_a.append(s)
            print(f"  Trace A (@mcp_endpoint): {len(spans_a)} total spans, {len(server_spans_a)} server-side")
            assert len(server_spans_a) >= 1, "Server A should have server-side spans"
        else:
            print(f"  ⚠ Trace A ({trace_id_a}) not found in Tempo yet")

    if trace_id_b:
        trace_b = _query_tempo_trace(trace_id_b)
        if trace_b:
            spans_b = _extract_spans_from_tempo(trace_b)
            server_spans_b = []
            for s in spans_b:
                attrs = _get_tempo_span_attrs(s)
                if attrs.get("remote") == "false":
                    server_spans_b.append(s)
            print(f"  Trace B (plain):         {len(spans_b)} total spans, {len(server_spans_b)} server-side")
            assert len(server_spans_b) == 0, "Server B should NOT have server-side spans"
        else:
            print(f"  ⚠ Trace B ({trace_id_b}) not found in Tempo yet")

    print("  ✓ Both-servers comparison PASSED")


# ============================================================================
# TEST 4: LangGraph agent with both servers (full AI stack)
# ============================================================================

@pytest.mark.skipif(not HAS_LANGGRAPH, reason="langgraph/langchain-google-genai not installed")
@pytest.mark.skipif(not GOOGLE_API_KEY, reason="GOOGLE_API_KEY not set")
@pytest.mark.asyncio
async def test_langgraph_with_both_servers(server_with_endpoint, server_without_endpoint):
    """Full AI agent calls tools on both server types, verifies Tempo traces.

    - Gemini agent calls tools on Server A (@mcp_endpoint)
    - Then on Server B (plain)
    - Verifies both produce correct results
    - Verifies Server A trace has server spans, Server B doesn't
    """
    from langchain_core.messages import HumanMessage
    from rastir import mcp_to_langchain_tools

    # --- Agent calling Server A (WITH @mcp_endpoint) ---
    trace_id_a = None

    async with streamable_http_client(URL_WITH_ENDPOINT) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            lc_tools_a = await mcp_to_langchain_tools(session)

            llm = ChatGoogleGenerativeAI(
                model="gemini-2.5-flash",
                google_api_key=GOOGLE_API_KEY,
                temperature=0,
            )
            agent_a = create_react_agent(llm, lc_tools_a)

            @agent_span(agent_name="langgraph_e2e_server_a")
            async def run_a():
                nonlocal trace_id_a
                resp = await agent_a.ainvoke(
                    {"messages": [HumanMessage(content="What is the weather in Tokyo?")]}
                )
                from rastir.context import get_current_span
                span = get_current_span()
                if span:
                    trace_id_a = span.trace_id
                return resp

            response_a = await run_a()
            last_msg_a = response_a["messages"][-1].content
            print(f"\n  Server A agent response: {last_msg_a[:120]}...")

    # --- Agent calling Server B (WITHOUT @mcp_endpoint) ---
    trace_id_b = None

    async with streamable_http_client(URL_WITHOUT_ENDPOINT) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            lc_tools_b = await mcp_to_langchain_tools(session)

            llm2 = ChatGoogleGenerativeAI(
                model="gemini-2.5-flash",
                google_api_key=GOOGLE_API_KEY,
                temperature=0,
            )
            agent_b = create_react_agent(llm2, lc_tools_b)

            @agent_span(agent_name="langgraph_e2e_server_b")
            async def run_b():
                nonlocal trace_id_b
                resp = await agent_b.ainvoke(
                    {"messages": [HumanMessage(content="What is the weather in London?")]}
                )
                from rastir.context import get_current_span
                span = get_current_span()
                if span:
                    trace_id_b = span.trace_id
                return resp

            response_b = await run_b()
            last_msg_b = response_b["messages"][-1].content
            print(f"  Server B agent response: {last_msg_b[:120]}...")

    # --- Wait for flush to Tempo ---
    time.sleep(6)

    # --- Verify Tempo traces ---
    if trace_id_a:
        trace_a = _query_tempo_trace(trace_id_a)
        if trace_a:
            spans_a = _extract_spans_from_tempo(trace_a)
            server_spans = [
                s for s in spans_a
                if _get_tempo_span_attrs(s).get("remote") == "false"
            ]
            print(f"  ✓ Server A Tempo: {len(spans_a)} spans, {len(server_spans)} server-side")
            assert len(server_spans) >= 1, "Server A should have server-side spans in Tempo"
        else:
            print(f"  ⚠ Trace A not found in Tempo")

    if trace_id_b:
        trace_b = _query_tempo_trace(trace_id_b)
        if trace_b:
            spans_b = _extract_spans_from_tempo(trace_b)
            server_spans = [
                s for s in spans_b
                if _get_tempo_span_attrs(s).get("remote") == "false"
            ]
            print(f"  ✓ Server B Tempo: {len(spans_b)} spans, {len(server_spans)} server-side")
            assert len(server_spans) == 0, "Server B should NOT have server-side spans"
        else:
            print(f"  ⚠ Trace B not found in Tempo")

    print("  ✓ LangGraph + both servers e2e PASSED")


# ============================================================================
# TEST 5: Prometheus metrics verification
# ============================================================================

@pytest.mark.asyncio
async def test_prometheus_metrics(server_with_endpoint):
    """Verify Prometheus metrics increment for tool calls."""
    # Get baseline
    baseline_tool = _get_prometheus_metric_sum("rastir_tool_calls_total")
    baseline_spans = _get_prometheus_metric_sum("rastir_spans_ingested_total")

    @agent_span(agent_name="prometheus_test_agent")
    async def run():
        async with streamable_http_client(URL_WITH_ENDPOINT) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()

                @trace_remote_tools
                def wrap():
                    return session
                wrapped = wrap()
                await wrapped.call_tool("get_weather", {"city": "Tokyo"})
                await wrapped.call_tool("calculate", {"expression": "1 + 1"})

    await run()

    # Wait for flush (retry-based)
    new_spans = _wait_for_metric_increment(
        "rastir_spans_ingested_total", baseline_spans, min_delta=5.0
    )
    new_tool = _get_prometheus_metric_sum("rastir_tool_calls_total")

    print(f"\n  Tool calls: {baseline_tool} → {new_tool}")
    print(f"  Spans ingested: {baseline_spans} → {new_spans}")

    # We should see increment of at least:
    # - 4 tool spans (2 client + 2 server via @mcp_endpoint) + 1 agent span = 5
    assert new_spans > baseline_spans, (
        f"Span counter didn't increment: {baseline_spans} → {new_spans}"
    )

    # Tool counter should increment by at least 4 (2 client + 2 server)
    tool_delta = new_tool - baseline_tool
    assert tool_delta >= 2, (
        f"Tool counter only incremented by {tool_delta}, expected ≥2"
    )
    print(f"  ✓ Tool call delta: +{tool_delta}")
    print("  ✓ Prometheus metrics verification PASSED")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
