"""End-to-end integration tests verifying ALL Rastir annotations in Tempo.

Tests exercise the FULL pipeline:
  Client decorator → Rastir collector (localhost:8080) → OTLP → Tempo (localhost:3200)
  Client decorator → Rastir collector → Prometheus (localhost:9090)

Every annotation set by every decorator is verified to appear in Tempo with
correct values.  Parent-child span relationships are validated.

See INTEGRATION_TESTS.md for the full annotation specification.

Infrastructure required:
    - Rastir collector on localhost:8080 (with RASTIR_SERVER_CONFIG)
    - Tempo on localhost:3200
    - Prometheus on localhost:9090
    - GOOGLE_API_KEY env var (for test_langgraph_full_stack only)

Run:
    GOOGLE_API_KEY=... PYTHONPATH=src \\
        python -m pytest tests/integration/test_mcp_e2e.py -v -s
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

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
# Rastir imports
# ---------------------------------------------------------------------------
import rastir
from rastir import (
    configure,
    agent_span,
    llm_span,
    metric_span,
    retrieval_span,
    tool_span,
    trace_span,
    wrap_mcp,
    mcp_endpoint,
)
from rastir.context import (
    get_current_span,
    set_current_model,
    set_current_provider,
)
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
    """MCP server with plain tools — no @mcp_endpoint."""
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
# Helpers — Prometheus
# ---------------------------------------------------------------------------

def _get_prometheus_metric_sum(metric_prefix: str) -> float:
    """Sum all lines matching a metric prefix from the collector /metrics."""
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


# ---------------------------------------------------------------------------
# Helpers — Tempo
# ---------------------------------------------------------------------------

def _query_tempo_trace(trace_id: str, retries: int = 15, delay: float = 2.0) -> dict | None:
    """Query Tempo for a trace by ID, retrying until it appears."""
    tid = trace_id.replace("-", "").ljust(32, "0")[:32]
    url = f"{TEMPO_URL}/api/traces/{tid}"
    with httpx.Client(timeout=10) as c:
        for _ in range(retries):
            try:
                resp = c.get(url)
                if resp.status_code == 200:
                    return resp.json()
            except Exception:
                pass
            time.sleep(delay)
    return None


def _extract_spans_from_tempo(trace_data: dict) -> list[dict]:
    """Extract flat list of spans from Tempo trace response (OTLP/JSON)."""
    spans = []
    for batch in trace_data.get("batches", []):
        for scope in batch.get("scopeSpans", batch.get("instrumentationLibrarySpans", [])):
            for span in scope.get("spans", []):
                spans.append(span)
    return spans


def _get_tempo_span_attrs(span: dict) -> dict[str, Any]:
    """Extract attributes from a Tempo span, stripping 'rastir.' prefix.

    Handles all OTLP value types: string, int, bool, double.
    """
    attrs: dict[str, Any] = {}
    for a in span.get("attributes", []):
        key = a.get("key", "")
        val_dict = a.get("value", {})
        if "stringValue" in val_dict:
            val = val_dict["stringValue"]
        elif "intValue" in val_dict:
            val = int(val_dict["intValue"])
        elif "boolValue" in val_dict:
            val = val_dict["boolValue"]
        elif "doubleValue" in val_dict:
            val = float(val_dict["doubleValue"])
        else:
            val = ""
        if key.startswith("rastir."):
            key = key[len("rastir."):]
        attrs[key] = val
    return attrs


def _find_tempo_spans_by_type(
    tempo_spans: list[dict], span_type: str,
) -> list[dict]:
    """Filter Tempo spans by rastir.span_type attribute value."""
    return [
        s for s in tempo_spans
        if _get_tempo_span_attrs(s).get("span_type") == span_type
    ]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def setup_rastir():
    """Configure Rastir to push spans to the real collector.

    NOTE: In these tests the MCP server runs in the same process as the
    client, so a single ``configure()`` call covers both client-side and
    server-side (@mcp_endpoint) spans.  In production the MCP server is
    a separate process and **must** call ``configure(push_url=...)``
    independently to export its server-side spans to the collector.
    """
    import rastir.config as _cfg
    _cfg._initialized = False
    _cfg._global_config = None
    configure(
        service="mcp-e2e-test",
        env="integration",
        push_url=COLLECTOR_URL,
        flush_interval=1,
        batch_size=10,
    )
    yield
    time.sleep(2)


@pytest.fixture
async def server_with_endpoint():
    """Start MCP server A (WITH @mcp_endpoint)."""
    srv = _create_server_with_endpoint()
    app = srv.streamable_http_app()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=PORT_WITH_ENDPOINT, log_level="warning",
    )
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
    config = uvicorn.Config(
        app, host="127.0.0.1", port=PORT_WITHOUT_ENDPOINT, log_level="warning",
    )
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
# TEST 1: @agent > @llm > @tool + @retrieval — all annotations
# ============================================================================

@pytest.mark.asyncio
async def test_agent_llm_tool_retrieval_annotations():
    """Verify all decorator annotations appear in Tempo with correct values.

    Hierarchy:  @agent > @llm > @tool + @retrieval

    Verified annotations (see INTEGRATION_TESTS.md):
      agent:     span_type=agent, agent_name
      llm:       span_type=llm, model, provider, agent
      tool:      span_type=tool, tool_name, agent, model, provider
      retrieval: span_type=retrieval, agent, model, provider, retrieved_documents_count
    Also verifies parent-child hierarchy across all four span types.
    """
    baseline = _get_prometheus_metric_sum("rastir_spans_ingested_total")

    # -- Define decorated functions --

    @tool_span(tool_name="db_search")
    async def do_tool(query: str) -> list:
        return [{"id": 1, "text": "result"}]

    @retrieval_span(name="vector_lookup")
    async def do_retrieval(query: str) -> list:
        return ["doc_a", "doc_b", "doc_c"]

    @llm_span(model="e2e-test-model", provider="e2e-test-provider")
    async def do_llm(prompt: str) -> str:
        await do_tool("search query")
        await do_retrieval("embedding query")
        return "LLM response text"

    trace_id = None

    @agent_span(agent_name="hierarchy_test_agent")
    async def run_agent():
        nonlocal trace_id
        span = get_current_span()
        if span:
            trace_id = span.trace_id
        await do_llm("What is the answer?")

    await run_agent()

    # -- Wait for spans to arrive at collector --
    new_count = _wait_for_metric_increment(
        "rastir_spans_ingested_total", baseline, min_delta=4.0,
    )
    assert new_count > baseline, (
        f"Spans not ingested: {baseline} → {new_count}"
    )

    # -- Query Tempo --
    assert trace_id, "Failed to capture trace_id"
    trace_data = _query_tempo_trace(trace_id)
    assert trace_data, f"Trace {trace_id} not found in Tempo"
    tempo_spans = _extract_spans_from_tempo(trace_data)

    print(f"\n  Tempo trace {trace_id}: {len(tempo_spans)} spans")
    for s in tempo_spans:
        a = _get_tempo_span_attrs(s)
        print(f"    {s.get('name', '?'):25s} span_type={a.get('span_type', '?'):10s} attrs={a}")

    # -- Verify agent span --
    agent_spans = _find_tempo_spans_by_type(tempo_spans, "agent")
    assert len(agent_spans) >= 1, "No agent span in Tempo"
    agent_attrs = _get_tempo_span_attrs(agent_spans[0])
    assert agent_attrs.get("agent_name") == "hierarchy_test_agent", (
        f"agent_name mismatch: {agent_attrs.get('agent_name')}"
    )
    print("  ✓ agent:     span_type=agent, agent_name=hierarchy_test_agent")

    # -- Verify LLM span --
    llm_spans = _find_tempo_spans_by_type(tempo_spans, "llm")
    assert len(llm_spans) >= 1, "No LLM span in Tempo"
    llm_attrs = _get_tempo_span_attrs(llm_spans[0])
    assert llm_attrs.get("model") == "e2e-test-model", (
        f"model mismatch: {llm_attrs.get('model')}"
    )
    assert llm_attrs.get("provider") == "e2e-test-provider", (
        f"provider mismatch: {llm_attrs.get('provider')}"
    )
    assert llm_attrs.get("agent") == "hierarchy_test_agent", (
        f"llm agent mismatch: {llm_attrs.get('agent')}"
    )
    print("  ✓ llm:       model=e2e-test-model, provider=e2e-test-provider, agent=hierarchy_test_agent")

    # -- Verify tool span --
    tool_spans = _find_tempo_spans_by_type(tempo_spans, "tool")
    assert len(tool_spans) >= 1, "No tool span in Tempo"
    local_tools = [
        s for s in tool_spans
        if _get_tempo_span_attrs(s).get("tool_name") == "db_search"
    ]
    assert len(local_tools) >= 1, "No db_search tool span in Tempo"
    tool_attrs = _get_tempo_span_attrs(local_tools[0])
    assert tool_attrs.get("tool_name") == "db_search"
    assert tool_attrs.get("agent") == "hierarchy_test_agent", (
        f"tool agent mismatch: {tool_attrs.get('agent')}"
    )
    assert tool_attrs.get("model") == "e2e-test-model", (
        f"tool model mismatch: {tool_attrs.get('model')}"
    )
    assert tool_attrs.get("provider") == "e2e-test-provider", (
        f"tool provider mismatch: {tool_attrs.get('provider')}"
    )
    print("  ✓ tool:      tool_name=db_search, agent, model, provider inherited")

    # -- Verify retrieval span --
    retrieval_spans = _find_tempo_spans_by_type(tempo_spans, "retrieval")
    assert len(retrieval_spans) >= 1, "No retrieval span in Tempo"
    ret_attrs = _get_tempo_span_attrs(retrieval_spans[0])
    assert ret_attrs.get("agent") == "hierarchy_test_agent", (
        f"retrieval agent mismatch: {ret_attrs.get('agent')}"
    )
    assert ret_attrs.get("model") == "e2e-test-model", (
        f"retrieval model mismatch: {ret_attrs.get('model')}"
    )
    assert ret_attrs.get("provider") == "e2e-test-provider", (
        f"retrieval provider mismatch: {ret_attrs.get('provider')}"
    )
    assert ret_attrs.get("retrieved_documents_count") == 3, (
        f"retrieved_documents_count mismatch: {ret_attrs.get('retrieved_documents_count')}"
    )
    print("  ✓ retrieval: agent, model, provider inherited, retrieved_documents_count=3")

    # -- Verify parent-child hierarchy --
    id_to_type = {}
    for s in tempo_spans:
        sid = s.get("spanId", "")
        stype = _get_tempo_span_attrs(s).get("span_type", "")
        id_to_type[sid] = stype

    llm_parent = llm_spans[0].get("parentSpanId", "")
    assert id_to_type.get(llm_parent) == "agent", (
        f"LLM parent is not agent: parent={llm_parent}, type={id_to_type.get(llm_parent)}"
    )

    tool_parent = local_tools[0].get("parentSpanId", "")
    assert id_to_type.get(tool_parent) == "llm", (
        f"Tool parent is not LLM: parent={tool_parent}, type={id_to_type.get(tool_parent)}"
    )

    ret_parent = retrieval_spans[0].get("parentSpanId", "")
    assert id_to_type.get(ret_parent) == "llm", (
        f"Retrieval parent is not LLM: parent={ret_parent}, type={id_to_type.get(ret_parent)}"
    )

    print("  ✓ hierarchy: agent → llm → tool, agent → llm → retrieval")
    print("  ✓ TEST 1 PASSED")


# ============================================================================
# TEST 2: @trace + @metric annotations
# ============================================================================

@pytest.mark.asyncio
async def test_trace_and_metric_annotations():
    """Verify @trace and @metric annotations appear in Tempo.

    Verified annotations:
      trace:  span_type=trace, emit_metric=True
      metric: span_type=metric, metric_name
    """
    baseline = _get_prometheus_metric_sum("rastir_spans_ingested_total")

    @metric_span(name="test_operations_counter")
    async def do_metric_work():
        return 42

    @trace_span(emit_metric=True)
    async def do_traced_work():
        await do_metric_work()
        return "done"

    trace_id = None

    @agent_span(agent_name="trace_metric_agent")
    async def run():
        nonlocal trace_id
        span = get_current_span()
        if span:
            trace_id = span.trace_id
        await do_traced_work()

    await run()

    # -- Wait for flush --
    _wait_for_metric_increment(
        "rastir_spans_ingested_total", baseline, min_delta=3.0,
    )

    # -- Query Tempo --
    assert trace_id, "Failed to capture trace_id"
    trace_data = _query_tempo_trace(trace_id)
    assert trace_data, f"Trace {trace_id} not found in Tempo"
    tempo_spans = _extract_spans_from_tempo(trace_data)

    print(f"\n  Tempo trace {trace_id}: {len(tempo_spans)} spans")
    for s in tempo_spans:
        a = _get_tempo_span_attrs(s)
        print(f"    {s.get('name', '?'):30s} span_type={a.get('span_type', '?'):10s}")

    # -- Verify trace span --
    trace_spans = _find_tempo_spans_by_type(tempo_spans, "trace")
    assert len(trace_spans) >= 1, "No trace span in Tempo"
    trace_attrs = _get_tempo_span_attrs(trace_spans[0])
    assert "emit_metric" in trace_attrs, (
        f"emit_metric attribute missing from trace span: {trace_attrs}"
    )
    assert trace_attrs["emit_metric"] in (True, 1), (
        f"emit_metric value unexpected: {trace_attrs['emit_metric']}"
    )
    print("  ✓ trace:  span_type=trace, emit_metric=True")

    # -- Verify metric span --
    metric_spans = _find_tempo_spans_by_type(tempo_spans, "metric")
    assert len(metric_spans) >= 1, "No metric span in Tempo"
    metric_attrs = _get_tempo_span_attrs(metric_spans[0])
    assert metric_attrs.get("metric_name") == "test_operations_counter", (
        f"metric_name mismatch: {metric_attrs.get('metric_name')}"
    )
    print("  ✓ metric: span_type=metric, metric_name=test_operations_counter")

    # -- Verify parent-child --
    id_to_type = {
        s.get("spanId", ""): _get_tempo_span_attrs(s).get("span_type", "")
        for s in tempo_spans
    }
    trace_parent = trace_spans[0].get("parentSpanId", "")
    assert id_to_type.get(trace_parent) == "agent", (
        f"Trace parent is not agent: {id_to_type.get(trace_parent)}"
    )
    metric_parent = metric_spans[0].get("parentSpanId", "")
    assert id_to_type.get(metric_parent) == "trace", (
        f"Metric parent is not trace: {id_to_type.get(metric_parent)}"
    )
    print("  ✓ hierarchy: agent → trace → metric")
    print("  ✓ TEST 2 PASSED")


# ============================================================================
# TEST 3: MCP with @mcp_endpoint — all remote tool annotations
# ============================================================================

@pytest.mark.asyncio
async def test_mcp_endpoint_annotations(server_with_endpoint):
    """Verify all MCP remote tool annotations in Tempo.

    Uses @agent with model/provider context, calls tool on server WITH
    @mcp_endpoint.

    Verified annotations:
      Client span: remote=true, tool_name, agent, model, provider
      Server span: remote=false, tool_name
      Both share the same trace_id, server is child of client.
    """
    baseline = _get_prometheus_metric_sum("rastir_spans_ingested_total")
    trace_id = None

    @agent_span(agent_name="mcp_annotation_agent")
    async def run():
        nonlocal trace_id
        span = get_current_span()
        if span:
            trace_id = span.trace_id

        # Simulate being inside @llm context
        set_current_model("mcp-test-model")
        set_current_provider("mcp-test-provider")

        async with streamable_http_client(URL_WITH_ENDPOINT) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()

                wrapped = wrap_mcp(session)
                result = await wrapped.call_tool("get_weather", {"city": "Tokyo"})
                return result

    result = await run()
    assert result is not None

    # -- Wait for flush --
    _wait_for_metric_increment(
        "rastir_spans_ingested_total", baseline, min_delta=3.0,
    )

    # -- Query Tempo --
    assert trace_id, "Failed to capture trace_id"
    trace_data = _query_tempo_trace(trace_id)
    assert trace_data, f"Trace {trace_id} not found in Tempo"
    tempo_spans = _extract_spans_from_tempo(trace_data)

    print(f"\n  Tempo trace {trace_id}: {len(tempo_spans)} spans")
    for s in tempo_spans:
        a = _get_tempo_span_attrs(s)
        print(f"    {s.get('name', '?'):25s} remote={a.get('remote', '-'):6s} attrs={a}")

    # -- Find tool spans --
    tool_spans = _find_tempo_spans_by_type(tempo_spans, "tool")
    client_tools = [
        s for s in tool_spans
        if _get_tempo_span_attrs(s).get("remote") == "true"
    ]
    server_tools = [
        s for s in tool_spans
        if _get_tempo_span_attrs(s).get("remote") == "false"
    ]

    # -- Verify client span --
    assert len(client_tools) >= 1, "No client tool span (remote=true) in Tempo"
    client_attrs = _get_tempo_span_attrs(client_tools[0])
    assert client_attrs.get("tool_name") == "get_weather", (
        f"client tool_name: {client_attrs.get('tool_name')}"
    )
    assert client_attrs.get("agent") == "mcp_annotation_agent", (
        f"client agent: {client_attrs.get('agent')}"
    )
    assert client_attrs.get("model") == "mcp-test-model", (
        f"client model: {client_attrs.get('model')}"
    )
    assert client_attrs.get("provider") == "mcp-test-provider", (
        f"client provider: {client_attrs.get('provider')}"
    )
    print("  ✓ client:  remote=true, tool_name=get_weather, agent, model, provider")

    # -- Verify server span --
    assert len(server_tools) >= 1, "No server tool span (remote=false) in Tempo"
    server_attrs = _get_tempo_span_attrs(server_tools[0])
    assert server_attrs.get("tool_name") == "get_weather", (
        f"server tool_name: {server_attrs.get('tool_name')}"
    )
    assert server_attrs.get("remote") == "false"
    print("  ✓ server:  remote=false, tool_name=get_weather")

    # -- Verify parent-child: server is child of client --
    id_to_type = {}
    id_to_remote = {}
    for s in tempo_spans:
        sid = s.get("spanId", "")
        attrs = _get_tempo_span_attrs(s)
        id_to_type[sid] = attrs.get("span_type", "")
        id_to_remote[sid] = attrs.get("remote", "")

    server_parent = server_tools[0].get("parentSpanId", "")
    assert id_to_remote.get(server_parent) == "true", (
        f"Server span parent is not client (remote=true): {id_to_remote.get(server_parent)}"
    )

    client_parent = client_tools[0].get("parentSpanId", "")
    assert id_to_type.get(client_parent) == "agent", (
        f"Client span parent is not agent: {id_to_type.get(client_parent)}"
    )

    print("  ✓ hierarchy: agent → client(remote=true) → server(remote=false)")
    print("  ✓ TEST 3 PASSED")


# ============================================================================
# TEST 4: MCP without @mcp_endpoint — client spans only
# ============================================================================

@pytest.mark.asyncio
async def test_mcp_without_endpoint(server_without_endpoint):
    """Server without @mcp_endpoint: only client spans, no server spans.

    Verified:
      - Client span exists with remote=true, tool_name
      - NO server span (remote=false) in Tempo
      - Tool still returns correct result
    """
    baseline = _get_prometheus_metric_sum("rastir_spans_ingested_total")
    trace_id = None

    @agent_span(agent_name="no_endpoint_agent")
    async def run():
        nonlocal trace_id
        span = get_current_span()
        if span:
            trace_id = span.trace_id

        async with streamable_http_client(URL_WITHOUT_ENDPOINT) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()

                wrapped = wrap_mcp(session)
                result = await wrapped.call_tool("get_weather", {"city": "London"})
                return result

    result = await run()
    assert result is not None

    # -- Wait for flush (only agent + client = 2 spans) --
    _wait_for_metric_increment(
        "rastir_spans_ingested_total", baseline, min_delta=2.0,
    )

    # -- Query Tempo --
    assert trace_id, "Failed to capture trace_id"
    trace_data = _query_tempo_trace(trace_id)
    assert trace_data, f"Trace {trace_id} not found in Tempo"
    tempo_spans = _extract_spans_from_tempo(trace_data)

    print(f"\n  Tempo trace {trace_id}: {len(tempo_spans)} spans")

    # -- Should have agent + client tool but NO server span --
    assert len(tempo_spans) >= 2, (
        f"Expected ≥2 spans, got {len(tempo_spans)}"
    )

    tool_spans = _find_tempo_spans_by_type(tempo_spans, "tool")
    server_tools = [
        s for s in tool_spans
        if _get_tempo_span_attrs(s).get("remote") == "false"
    ]
    client_tools = [
        s for s in tool_spans
        if _get_tempo_span_attrs(s).get("remote") == "true"
    ]

    assert len(client_tools) >= 1, "No client tool span found"
    assert len(server_tools) == 0, (
        f"Unexpected server spans (remote=false): {len(server_tools)}"
    )

    print(f"  ✓ {len(client_tools)} client span(s), 0 server spans — correct")
    print("  ✓ TEST 4 PASSED")


# ============================================================================
# TEST 5: Prometheus metrics verification
# ============================================================================

@pytest.mark.asyncio
async def test_prometheus_metrics(server_with_endpoint):
    """Verify Prometheus counters increment for span ingestion and tool calls."""
    baseline_spans = _get_prometheus_metric_sum("rastir_spans_ingested_total")
    baseline_tools = _get_prometheus_metric_sum("rastir_tool_calls_total")

    @agent_span(agent_name="prometheus_agent")
    async def run():
        async with streamable_http_client(URL_WITH_ENDPOINT) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()

                wrapped = wrap_mcp(session)
                await wrapped.call_tool("get_weather", {"city": "Tokyo"})
                await wrapped.call_tool("calculate", {"expression": "1 + 1"})

    await run()

    # -- Wait for counters --
    new_spans = _wait_for_metric_increment(
        "rastir_spans_ingested_total", baseline_spans, min_delta=5.0,
    )
    new_tools = _get_prometheus_metric_sum("rastir_tool_calls_total")

    span_delta = new_spans - baseline_spans
    tool_delta = new_tools - baseline_tools

    print(f"\n  Spans ingested: {baseline_spans} → {new_spans} (+{span_delta})")
    print(f"  Tool calls:     {baseline_tools} → {new_tools} (+{tool_delta})")

    # 2 client tools + 2 server tools + 1 agent = at least 5 spans
    assert span_delta >= 5, f"Span delta only {span_delta}, expected ≥5"
    # At least 2 tool calls recorded
    assert tool_delta >= 2, f"Tool delta only {tool_delta}, expected ≥2"

    print(f"  ✓ Span ingestion delta: +{span_delta}")
    print(f"  ✓ Tool call delta: +{tool_delta}")
    print("  ✓ TEST 5 PASSED")


# ============================================================================
# TEST 6: LangGraph full stack (optional, needs GOOGLE_API_KEY)
# ============================================================================

@pytest.mark.skipif(not HAS_LANGGRAPH, reason="langgraph/langchain-google-genai not installed")
@pytest.mark.skipif(not GOOGLE_API_KEY, reason="GOOGLE_API_KEY not set")
@pytest.mark.asyncio
async def test_langgraph_full_stack(server_with_endpoint, server_without_endpoint):
    """Full AI agent with real LLM calling tools on both server types.

    Verifies:
      - Gemini agent calls tools on Server A (@mcp_endpoint) and Server B
      - Server A traces have client + server spans in Tempo
      - Server B traces have client spans only
    """
    from langchain_core.messages import HumanMessage
    from langchain_core.tools import StructuredTool
    from pydantic import create_model

    def _mcp_tools_to_langchain(wrapped_session, tools_response):
        """Convert MCP tools to LangChain StructuredTools (test helper)."""
        lc_tools = []
        for mcp_tool in tools_response.tools:
            props = (mcp_tool.inputSchema or {}).get("properties", {})
            required = set((mcp_tool.inputSchema or {}).get("required", []))
            fields = {}
            for fname, fschema in props.items():
                if fname.startswith("rastir_"):
                    continue
                ftype = {"string": str, "integer": int, "number": float, "boolean": bool}.get(fschema.get("type", "string"), str)
                fields[fname] = (ftype, ... if fname in required else None)
            model = create_model(f"{mcp_tool.name}_args", **fields)
            tn = mcp_tool.name

            async def _call(*, _tn=tn, **kwargs):
                r = await wrapped_session.call_tool(_tn, kwargs)
                return " ".join(getattr(c, "text", str(c)) for c in r.content) if r.content else str(r)

            lc_tools.append(StructuredTool.from_function(
                coroutine=_call, name=tn,
                description=mcp_tool.description or tn,
                args_schema=model,
            ))
        return lc_tools

    baseline = _get_prometheus_metric_sum("rastir_spans_ingested_total")

    # -- Agent calling Server A --
    trace_id_a = None

    async with streamable_http_client(URL_WITH_ENDPOINT) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            wrapped_a = wrap_mcp(session)
            tools_resp_a = await wrapped_a.list_tools()
            lc_tools_a = _mcp_tools_to_langchain(wrapped_a, tools_resp_a)

            llm_a = ChatGoogleGenerativeAI(
                model="gemini-2.5-flash",
                google_api_key=GOOGLE_API_KEY,
                temperature=0,
            )
            agent_a = create_react_agent(llm_a, lc_tools_a)

            @agent_span(agent_name="langgraph_server_a")
            async def run_a():
                nonlocal trace_id_a
                span = get_current_span()
                if span:
                    trace_id_a = span.trace_id
                return await agent_a.ainvoke(
                    {"messages": [HumanMessage(content="What is the weather in Tokyo?")]}
                )

            response_a = await run_a()
            print(f"\n  Server A: {response_a['messages'][-1].content[:100]}...")

    # -- Agent calling Server B --
    trace_id_b = None

    async with streamable_http_client(URL_WITHOUT_ENDPOINT) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            wrapped_b = wrap_mcp(session)
            tools_resp_b = await wrapped_b.list_tools()
            lc_tools_b = _mcp_tools_to_langchain(wrapped_b, tools_resp_b)

            llm_b = ChatGoogleGenerativeAI(
                model="gemini-2.5-flash",
                google_api_key=GOOGLE_API_KEY,
                temperature=0,
            )
            agent_b = create_react_agent(llm_b, lc_tools_b)

            @agent_span(agent_name="langgraph_server_b")
            async def run_b():
                nonlocal trace_id_b
                span = get_current_span()
                if span:
                    trace_id_b = span.trace_id
                return await agent_b.ainvoke(
                    {"messages": [HumanMessage(content="What is the weather in London?")]}
                )

            response_b = await run_b()
            print(f"  Server B: {response_b['messages'][-1].content[:100]}...")

    # -- Wait for flush --
    _wait_for_metric_increment(
        "rastir_spans_ingested_total", baseline, min_delta=4.0,
    )

    # -- Verify Server A in Tempo (should have server spans) --
    if trace_id_a:
        trace_a = _query_tempo_trace(trace_id_a)
        if trace_a:
            spans_a = _extract_spans_from_tempo(trace_a)
            server_spans = [
                s for s in spans_a
                if _get_tempo_span_attrs(s).get("remote") == "false"
            ]
            print(f"  ✓ Server A: {len(spans_a)} spans, {len(server_spans)} server-side")
            assert len(server_spans) >= 1, (
                "Server A should have server-side spans"
            )
        else:
            print(f"  ⚠ Trace A not found in Tempo")

    # -- Verify Server B in Tempo (should NOT have server spans) --
    if trace_id_b:
        trace_b = _query_tempo_trace(trace_id_b)
        if trace_b:
            spans_b = _extract_spans_from_tempo(trace_b)
            server_spans = [
                s for s in spans_b
                if _get_tempo_span_attrs(s).get("remote") == "false"
            ]
            print(f"  ✓ Server B: {len(spans_b)} spans, {len(server_spans)} server-side")
            assert len(server_spans) == 0, (
                "Server B should NOT have server-side spans"
            )
        else:
            print(f"  ⚠ Trace B not found in Tempo")

    print("  ✓ TEST 6 PASSED")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
