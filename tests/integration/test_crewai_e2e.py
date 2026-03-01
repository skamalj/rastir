"""End-to-end integration tests for CrewAI adapter.

Tests exercise the FULL pipeline:
  Client decorator → Rastir collector (localhost:8080) → OTLP → Tempo (localhost:3200)
  Client decorator → Rastir collector → Prometheus (localhost:9090)

The CrewAI adapter detects ``CrewOutput`` and ``TaskOutput`` objects returned
from ``crew.kickoff()`` — it extracts token_usage, task count, and task
metadata.  These tests verify that adapter-extracted attributes arrive in
Tempo as ``rastir.*`` span attributes.

Infrastructure required:
    - Rastir collector on localhost:8080 (with RASTIR_SERVER_CONFIG)
    - Tempo on localhost:3200
    - Prometheus on localhost:9090
    - GOOGLE_API_KEY env var

Run:
    GOOGLE_API_KEY=... PYTHONPATH=src \\
        python -m pytest tests/integration/test_crewai_e2e.py -v -s
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx
import pytest

# ---------------------------------------------------------------------------
# Skip conditions
# ---------------------------------------------------------------------------
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")

try:
    from crewai import Agent, Task, Crew, LLM
    from crewai.tools import BaseTool

    HAS_CREWAI = True
except ImportError:
    HAS_CREWAI = False

# ---------------------------------------------------------------------------
# Infrastructure endpoints
# ---------------------------------------------------------------------------
COLLECTOR_URL = "http://localhost:8080"
TEMPO_URL = "http://localhost:3200"
PROMETHEUS_URL = "http://localhost:9090"


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
    pytest.mark.skipif(not HAS_CREWAI, reason="crewai package not installed"),
    pytest.mark.skipif(not GOOGLE_API_KEY, reason="GOOGLE_API_KEY not set"),
    pytest.mark.skipif(
        not _infra_available(),
        reason="Infrastructure not running (collector/tempo/prometheus)",
    ),
]

# ---------------------------------------------------------------------------
# Rastir imports
# ---------------------------------------------------------------------------
import rastir
from rastir import configure, agent_span, llm_span
from rastir.context import get_current_span


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

def _query_tempo_trace(
    trace_id: str, retries: int = 15, delay: float = 2.0,
) -> dict | None:
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
# CrewAI tool definitions
# ---------------------------------------------------------------------------

class CapitalLookupTool(BaseTool):
    """Look up the capital city of a country."""

    name: str = "capital_lookup"
    description: str = "Look up the capital of a country. Input: country name."

    def _run(self, country: str) -> str:
        capitals = {
            "france": "Paris",
            "japan": "Tokyo",
            "india": "New Delhi",
            "germany": "Berlin",
            "brazil": "Brasília",
        }
        return capitals.get(country.lower(), f"Capital of {country} not found")


class PopulationTool(BaseTool):
    """Look up the approximate population of a country."""

    name: str = "population_lookup"
    description: str = "Look up the approximate population of a country. Input: country name."

    def _run(self, country: str) -> str:
        populations = {
            "france": "67 million",
            "japan": "125 million",
            "india": "1.4 billion",
            "germany": "84 million",
            "brazil": "215 million",
        }
        return populations.get(country.lower(), f"Population of {country} not known")


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
        service="crewai-e2e-test",
        env="integration",
        push_url=COLLECTOR_URL,
        flush_interval=1,
        batch_size=10,
    )
    yield
    time.sleep(2)


# ============================================================================
# TEST 1: CrewAI single-agent crew — adapter metadata in Tempo
# ============================================================================

def test_crewai_single_agent_annotations():
    """Verify CrewAI adapter extracts metadata from CrewOutput into Tempo.

    Runs a real CrewAI crew with a Gemini LLM and a tool.  The @llm
    decorator wraps ``crew.kickoff()`` so the adapter's ``transform()``
    receives the ``CrewOutput``.

    Verified in Tempo:
      - span_type=llm on the @llm span
      - crewai_task_count >= 1 (adapter-extracted)
      - crewai_total_tokens (if present in CrewOutput token_usage)
      - agent_name on the @agent span
      - parent-child: agent → llm
    """
    baseline = _get_prometheus_metric_sum("rastir_spans_ingested_total")

    crewai_llm = LLM(
        model="gemini/gemini-2.5-flash",
        api_key=GOOGLE_API_KEY,
    )

    researcher = Agent(
        role="Geography Expert",
        goal="Answer geography questions using tools",
        backstory="You are a world geography expert. Always use your tools to look up facts.",
        llm=crewai_llm,
        tools=[CapitalLookupTool()],
        verbose=False,
    )

    task = Task(
        description="What is the capital of France? Use the capital_lookup tool to find out.",
        expected_output="The capital city name",
        agent=researcher,
    )

    crew = Crew(
        agents=[researcher],
        tasks=[task],
        verbose=False,
    )

    trace_id = None

    @agent_span(agent_name="crewai_geo_agent")
    def run_agent():
        nonlocal trace_id
        span = get_current_span()
        if span:
            trace_id = span.trace_id

        @llm_span(model="gemini-2.5-flash", provider="gemini")
        def invoke_crew():
            return crew.kickoff()

        return invoke_crew()

    result = run_agent()
    assert result is not None
    print(f"\n  CrewAI result: {result.raw[:200] if hasattr(result, 'raw') else result}")

    # -- Wait for spans to arrive --
    new_count = _wait_for_metric_increment(
        "rastir_spans_ingested_total", baseline, min_delta=2.0,
    )
    assert new_count > baseline, f"Spans not ingested: {baseline} → {new_count}"

    # -- Query Tempo --
    assert trace_id, "Failed to capture trace_id"
    trace_data = _query_tempo_trace(trace_id)
    assert trace_data, f"Trace {trace_id} not found in Tempo"
    tempo_spans = _extract_spans_from_tempo(trace_data)

    print(f"  Tempo trace {trace_id}: {len(tempo_spans)} spans")
    for s in tempo_spans:
        a = _get_tempo_span_attrs(s)
        print(f"    {s.get('name', '?'):30s} span_type={a.get('span_type', '?'):10s} attrs={a}")

    # -- Verify agent span --
    agent_spans = _find_tempo_spans_by_type(tempo_spans, "agent")
    assert len(agent_spans) >= 1, "No agent span in Tempo"
    agent_attrs = _get_tempo_span_attrs(agent_spans[0])
    assert agent_attrs.get("agent_name") == "crewai_geo_agent", (
        f"agent_name mismatch: {agent_attrs.get('agent_name')}"
    )
    print("  ✓ agent:  span_type=agent, agent_name=crewai_geo_agent")

    # -- Verify LLM span with CrewAI adapter metadata --
    llm_spans = _find_tempo_spans_by_type(tempo_spans, "llm")
    assert len(llm_spans) >= 1, "No LLM span in Tempo"
    llm_attrs = _get_tempo_span_attrs(llm_spans[0])

    assert llm_attrs.get("model") == "gemini-2.5-flash", (
        f"model mismatch: {llm_attrs.get('model')}"
    )
    assert llm_attrs.get("provider") == "gemini", (
        f"provider mismatch: {llm_attrs.get('provider')}"
    )
    assert llm_attrs.get("agent") == "crewai_geo_agent", (
        f"llm agent mismatch: {llm_attrs.get('agent')}"
    )
    print("  ✓ llm:    model=gemini-2.5-flash, provider=gemini, agent=crewai_geo_agent")

    # CrewAI adapter attributes
    assert "crewai_task_count" in llm_attrs, (
        f"crewai_task_count missing from LLM span attributes: {llm_attrs}"
    )
    assert llm_attrs["crewai_task_count"] >= 1, (
        f"crewai_task_count should be >= 1: {llm_attrs['crewai_task_count']}"
    )
    print(f"  ✓ crewai: crewai_task_count={llm_attrs['crewai_task_count']}")

    # Token usage — adapter should extract from CrewOutput.token_usage
    assert "crewai_total_tokens" in llm_attrs, (
        f"crewai_total_tokens missing from LLM span: {llm_attrs}"
    )
    assert llm_attrs["crewai_total_tokens"] > 0, (
        f"crewai_total_tokens should be > 0: {llm_attrs['crewai_total_tokens']}"
    )
    print(f"  ✓ crewai: crewai_total_tokens={llm_attrs['crewai_total_tokens']}")

    assert "crewai_successful_requests" in llm_attrs, (
        f"crewai_successful_requests missing: {llm_attrs}"
    )
    print(f"  ✓ crewai: crewai_successful_requests={llm_attrs['crewai_successful_requests']}")

    assert "tokens_input" in llm_attrs, f"tokens_input missing: {llm_attrs}"
    assert llm_attrs["tokens_input"] > 0
    assert "tokens_output" in llm_attrs, f"tokens_output missing: {llm_attrs}"
    assert llm_attrs["tokens_output"] > 0
    print(f"  ✓ tokens: tokens_input={llm_attrs['tokens_input']}, tokens_output={llm_attrs['tokens_output']}")

    # -- Verify parent-child: agent → llm --
    id_to_type = {
        s.get("spanId", ""): _get_tempo_span_attrs(s).get("span_type", "")
        for s in tempo_spans
    }
    llm_parent = llm_spans[0].get("parentSpanId", "")
    assert id_to_type.get(llm_parent) == "agent", (
        f"LLM parent is not agent: parent type={id_to_type.get(llm_parent)}"
    )
    print("  ✓ hierarchy: agent → llm")
    print("  ✓ TEST 1 PASSED")


# ============================================================================
# TEST 2: CrewAI multi-agent crew with multiple tasks
# ============================================================================

def test_crewai_multi_agent_annotations():
    """Verify multi-agent crew tracks multiple tasks in Tempo.

    Two agents, two tasks — one with capital_lookup, one with
    population_lookup.  Verifies crewai_task_count >= 2.
    """
    baseline = _get_prometheus_metric_sum("rastir_spans_ingested_total")

    crewai_llm = LLM(
        model="gemini/gemini-2.5-flash",
        api_key=GOOGLE_API_KEY,
    )

    geo_agent = Agent(
        role="Geography Researcher",
        goal="Find capital cities using tools",
        backstory="Expert in world capitals. Always use the capital_lookup tool.",
        llm=crewai_llm,
        tools=[CapitalLookupTool()],
        verbose=False,
    )

    demo_agent = Agent(
        role="Demographics Researcher",
        goal="Find population data using tools",
        backstory="Expert in demographics. Always use the population_lookup tool.",
        llm=crewai_llm,
        tools=[PopulationTool()],
        verbose=False,
    )

    task1 = Task(
        description="What is the capital of Japan? Use the capital_lookup tool.",
        expected_output="The capital city name",
        agent=geo_agent,
    )

    task2 = Task(
        description="What is the population of Japan? Use the population_lookup tool.",
        expected_output="The population figure",
        agent=demo_agent,
    )

    crew = Crew(
        agents=[geo_agent, demo_agent],
        tasks=[task1, task2],
        verbose=False,
    )

    trace_id = None

    @agent_span(agent_name="crewai_multi_agent")
    def run_agent():
        nonlocal trace_id
        span = get_current_span()
        if span:
            trace_id = span.trace_id

        @llm_span(model="gemini-2.5-flash", provider="gemini")
        def invoke_crew():
            return crew.kickoff()

        return invoke_crew()

    result = run_agent()
    assert result is not None
    print(f"\n  CrewAI multi-agent result: {result.raw[:200] if hasattr(result, 'raw') else result}")

    # -- Wait for spans --
    new_count = _wait_for_metric_increment(
        "rastir_spans_ingested_total", baseline, min_delta=2.0,
    )
    assert new_count > baseline, f"Spans not ingested: {baseline} → {new_count}"

    # -- Query Tempo --
    assert trace_id, "Failed to capture trace_id"
    trace_data = _query_tempo_trace(trace_id)
    assert trace_data, f"Trace {trace_id} not found in Tempo"
    tempo_spans = _extract_spans_from_tempo(trace_data)

    print(f"  Tempo trace {trace_id}: {len(tempo_spans)} spans")
    for s in tempo_spans:
        a = _get_tempo_span_attrs(s)
        print(f"    {s.get('name', '?'):30s} span_type={a.get('span_type', '?'):10s} attrs={a}")

    # -- Verify agent span --
    agent_spans = _find_tempo_spans_by_type(tempo_spans, "agent")
    assert len(agent_spans) >= 1, "No agent span in Tempo"
    agent_attrs = _get_tempo_span_attrs(agent_spans[0])
    assert agent_attrs.get("agent_name") == "crewai_multi_agent"
    print("  ✓ agent:  agent_name=crewai_multi_agent")

    # -- Verify LLM span with CrewAI multi-task metadata --
    llm_spans = _find_tempo_spans_by_type(tempo_spans, "llm")
    assert len(llm_spans) >= 1, "No LLM span in Tempo"
    llm_attrs = _get_tempo_span_attrs(llm_spans[0])

    assert llm_attrs.get("model") == "gemini-2.5-flash"
    assert llm_attrs.get("provider") == "gemini"

    assert "crewai_task_count" in llm_attrs, (
        f"crewai_task_count missing: {llm_attrs}"
    )
    assert llm_attrs["crewai_task_count"] >= 2, (
        f"crewai_task_count should be >= 2 for multi-task crew: {llm_attrs['crewai_task_count']}"
    )
    print(f"  ✓ crewai: crewai_task_count={llm_attrs['crewai_task_count']}")

    # Token usage
    assert "crewai_total_tokens" in llm_attrs, (
        f"crewai_total_tokens missing: {llm_attrs}"
    )
    assert llm_attrs["crewai_total_tokens"] > 0
    print(f"  ✓ crewai: crewai_total_tokens={llm_attrs['crewai_total_tokens']}")

    assert "tokens_input" in llm_attrs
    assert llm_attrs["tokens_input"] > 0
    assert "tokens_output" in llm_attrs
    assert llm_attrs["tokens_output"] > 0
    print(f"  ✓ tokens: tokens_input={llm_attrs['tokens_input']}, tokens_output={llm_attrs['tokens_output']}")

    if "crewai_successful_requests" in llm_attrs:
        print(f"  ✓ crewai: crewai_successful_requests={llm_attrs['crewai_successful_requests']}")

    # -- Verify parent-child --
    id_to_type = {
        s.get("spanId", ""): _get_tempo_span_attrs(s).get("span_type", "")
        for s in tempo_spans
    }
    llm_parent = llm_spans[0].get("parentSpanId", "")
    assert id_to_type.get(llm_parent) == "agent", (
        f"LLM parent is not agent: {id_to_type.get(llm_parent)}"
    )
    print("  ✓ hierarchy: agent → llm")
    print("  ✓ TEST 2 PASSED")


# ============================================================================
# TEST 3: Prometheus metrics for CrewAI spans
# ============================================================================

def test_crewai_prometheus_metrics():
    """Verify Prometheus span counters increment for CrewAI runs."""
    baseline_spans = _get_prometheus_metric_sum("rastir_spans_ingested_total")

    crewai_llm = LLM(
        model="gemini/gemini-2.5-flash",
        api_key=GOOGLE_API_KEY,
    )

    agent_obj = Agent(
        role="Quick Helper",
        goal="Answer simple questions",
        backstory="You give quick answers.",
        llm=crewai_llm,
        verbose=False,
    )

    task = Task(
        description="What is 2 + 2?",
        expected_output="The number 4",
        agent=agent_obj,
    )

    crew = Crew(
        agents=[agent_obj],
        tasks=[task],
        verbose=False,
    )

    @agent_span(agent_name="crewai_prometheus_agent")
    def run_agent():
        @llm_span(model="gemini-2.5-flash", provider="gemini")
        def invoke_crew():
            return crew.kickoff()

        return invoke_crew()

    run_agent()

    # Wait for counters — agent + llm = at least 2 spans
    new_spans = _wait_for_metric_increment(
        "rastir_spans_ingested_total", baseline_spans, min_delta=2.0,
    )
    span_delta = new_spans - baseline_spans

    print(f"\n  Spans ingested: {baseline_spans} → {new_spans} (+{span_delta})")
    assert span_delta >= 2, f"Span delta only {span_delta}, expected ≥2"
    print(f"  ✓ Span ingestion delta: +{span_delta}")
    print("  ✓ TEST 3 PASSED")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
