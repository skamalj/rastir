"""End-to-end integration tests for V6: Cost Observability + Streaming TTFT.

Tests exercise the FULL pipeline:
  Client decorator -> Rastir collector (localhost:8080) -> OTLP -> Tempo (localhost:3200)
  Client decorator -> Rastir collector -> Prometheus (localhost:9090)

Verified end-to-end:
  - ``cost_usd``, ``pricing_profile``, ``pricing_missing`` appear in Tempo spans
  - ``ttft_ms`` and ``streaming`` appear in Tempo spans for streaming calls
  - ``rastir_cost_total`` counter increments in Prometheus
  - ``rastir_cost_per_call_usd`` histogram observes values in Prometheus
  - ``rastir_pricing_missing_total`` counter increments when pricing not found
  - ``rastir_ttft_seconds`` histogram observes TTFT values in Prometheus

Infrastructure required:
    - Rastir collector on localhost:8080
    - Tempo on localhost:3200
    - Prometheus on localhost:9090
    - GOOGLE_API_KEY env var

Run:
    PYTHONPATH=src python -m pytest tests/integration/test_cost_ttft_e2e.py -v -s
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
COLLECTOR_URL = "http://localhost:8080"
TEMPO_URL = "http://localhost:3200"
PROMETHEUS_URL = "http://localhost:9090"

MODEL = "gemini-2.5-flash"


def _infra_available() -> bool:
    try:
        with httpx.Client(timeout=2) as c:
            r1 = c.get(f"{COLLECTOR_URL}/health")
            r2 = c.get(f"{TEMPO_URL}/ready")
            r3 = c.get(f"{PROMETHEUS_URL}/-/ready")
            return all(r.status_code < 300 for r in (r1, r2, r3))
    except Exception:
        return False


pytestmark = [
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
from rastir.config import reset_config, get_pricing_registry
from rastir.context import get_current_span


# ---------------------------------------------------------------------------
# Helpers -- Prometheus (scraped from collector /metrics)
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
# Helpers -- Tempo
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
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def setup_rastir():
    """Reset Rastir config before each test, allow flush after."""
    reset_config()
    yield
    time.sleep(2)


# ============================================================================
# TEST 1: Cost attributes in Tempo trace (non-streaming)
# ============================================================================

def test_cost_attributes_in_tempo():
    """Verify cost_usd, pricing_profile appear in Tempo LLM span.

    Makes a real Gemini API call with cost calculation enabled and
    pricing registered.  Verifies:
      - cost_usd > 0 in Tempo span attributes
      - pricing_profile matches configured value
      - pricing_missing is false
      - tokens_input > 0, tokens_output > 0
    """
    from google import genai
    client = genai.Client(api_key=GOOGLE_API_KEY)

    baseline = _get_prometheus_metric_sum("rastir_spans_ingested_total")

    configure(
        service="cost-tempo-e2e",
        env="integration",
        push_url=COLLECTOR_URL,
        flush_interval=1,
        batch_size=10,
        enable_cost_calculation=True,
        pricing_profile="e2e_test_q1",
    )
    registry = get_pricing_registry()
    assert registry is not None
    # Register Gemini pricing -- provider is "gemini" per adapter
    registry.register("gemini", MODEL, input_price=0.10, output_price=0.40)

    trace_id = None

    @agent_span(agent_name="cost_test_agent")
    def run_cost_test():
        nonlocal trace_id
        span = get_current_span()
        if span:
            trace_id = span.trace_id

        @llm_span
        def ask_gemini(prompt: str) -> Any:
            return client.models.generate_content(
                model=MODEL, contents=prompt,
            )

        return ask_gemini("What is 2+2? Answer with just the number.")

    result = run_cost_test()
    assert result is not None

    # -- Wait for spans to be ingested --
    new_count = _wait_for_metric_increment(
        "rastir_spans_ingested_total", baseline, min_delta=2.0,
    )
    assert new_count > baseline, f"Spans not ingested: {baseline} -> {new_count}"

    # -- Query Tempo --
    assert trace_id, "Failed to capture trace_id"
    trace_data = _query_tempo_trace(trace_id)
    assert trace_data, f"Trace {trace_id} not found in Tempo"
    tempo_spans = _extract_spans_from_tempo(trace_data)

    print(f"\n  Tempo trace {trace_id}: {len(tempo_spans)} spans")
    for s in tempo_spans:
        a = _get_tempo_span_attrs(s)
        print(f"    {s.get('name', '?'):30s} span_type={a.get('span_type', '?'):10s} attrs={a}")

    # -- Verify LLM span has cost attributes --
    llm_spans = _find_tempo_spans_by_type(tempo_spans, "llm")
    assert len(llm_spans) >= 1, "No LLM span in Tempo"
    llm_attrs = _get_tempo_span_attrs(llm_spans[0])

    assert llm_attrs.get("provider") == "gemini", (
        f"provider mismatch: {llm_attrs.get('provider')}"
    )
    print(f"  + provider: {llm_attrs.get('provider')}")

    # cost_usd should be present and > 0
    assert "cost_usd" in llm_attrs, (
        f"cost_usd missing from LLM span: {llm_attrs}"
    )
    assert llm_attrs["cost_usd"] > 0, (
        f"cost_usd should be > 0: {llm_attrs['cost_usd']}"
    )
    print(f"  + cost_usd: {llm_attrs['cost_usd']}")

    # pricing_profile should match
    assert llm_attrs.get("pricing_profile") == "e2e_test_q1", (
        f"pricing_profile mismatch: {llm_attrs.get('pricing_profile')}"
    )
    print(f"  + pricing_profile: {llm_attrs.get('pricing_profile')}")

    # pricing_missing should be false
    pm = llm_attrs.get("pricing_missing")
    assert pm is None or pm is False or pm == "false", (
        f"pricing_missing should be false when pricing is found: {pm}"
    )
    print(f"  + pricing_missing: {pm}")

    # Tokens should be present
    assert "tokens_input" in llm_attrs, f"tokens_input missing: {llm_attrs}"
    assert llm_attrs["tokens_input"] > 0
    assert "tokens_output" in llm_attrs, f"tokens_output missing: {llm_attrs}"
    assert llm_attrs["tokens_output"] > 0
    print(f"  + tokens: in={llm_attrs['tokens_input']}, out={llm_attrs['tokens_output']}")

    print("  + TEST 1 PASSED: cost attributes in Tempo")


# ============================================================================
# TEST 2: Pricing missing in Tempo + Prometheus
# ============================================================================

def test_pricing_missing_in_tempo_and_prometheus():
    """Verify pricing_missing=true when model is NOT registered.

    Makes a real Gemini call with cost enabled but NO pricing registered.
    Verifies:
      - cost_usd == 0 in Tempo
      - pricing_missing == true in Tempo
      - pricing_profile still set
      - rastir_pricing_missing_total counter increments in Prometheus
    """
    from google import genai
    client = genai.Client(api_key=GOOGLE_API_KEY)

    baseline_spans = _get_prometheus_metric_sum("rastir_spans_ingested_total")
    baseline_missing = _get_prometheus_metric_sum("rastir_pricing_missing_total")

    configure(
        service="cost-missing-e2e",
        env="integration",
        push_url=COLLECTOR_URL,
        flush_interval=1,
        batch_size=10,
        enable_cost_calculation=True,
        pricing_profile="missing_test",
    )
    # Registry created but we intentionally leave it empty

    trace_id = None

    @agent_span(agent_name="pricing_missing_agent")
    def run_missing_test():
        nonlocal trace_id
        span = get_current_span()
        if span:
            trace_id = span.trace_id

        @llm_span
        def ask_no_pricing(prompt: str) -> Any:
            return client.models.generate_content(
                model=MODEL, contents=prompt,
            )

        return ask_no_pricing("Say yes.")

    result = run_missing_test()
    assert result is not None

    # -- Wait for spans --
    new_count = _wait_for_metric_increment(
        "rastir_spans_ingested_total", baseline_spans, min_delta=2.0,
    )
    assert new_count > baseline_spans

    # -- Query Tempo --
    assert trace_id
    trace_data = _query_tempo_trace(trace_id)
    assert trace_data, f"Trace {trace_id} not found in Tempo"
    tempo_spans = _extract_spans_from_tempo(trace_data)

    print(f"\n  Tempo trace {trace_id}: {len(tempo_spans)} spans")
    for s in tempo_spans:
        a = _get_tempo_span_attrs(s)
        print(f"    {s.get('name', '?'):30s} span_type={a.get('span_type', '?'):10s} attrs={a}")

    llm_spans = _find_tempo_spans_by_type(tempo_spans, "llm")
    assert len(llm_spans) >= 1
    llm_attrs = _get_tempo_span_attrs(llm_spans[0])

    # cost_usd should be 0
    cost_usd = llm_attrs.get("cost_usd", 0)
    assert cost_usd == 0 or cost_usd == 0.0, (
        f"cost_usd should be 0 when pricing is missing: {cost_usd}"
    )
    print(f"  + cost_usd: {cost_usd}")

    # pricing_missing should be true
    pm = llm_attrs.get("pricing_missing")
    assert pm is True or pm == "true", (
        f"pricing_missing should be true: {pm}"
    )
    print(f"  + pricing_missing: {pm}")

    # pricing_profile still set
    assert llm_attrs.get("pricing_profile") == "missing_test", (
        f"pricing_profile mismatch: {llm_attrs.get('pricing_profile')}"
    )
    print(f"  + pricing_profile: {llm_attrs.get('pricing_profile')}")

    # -- Prometheus: rastir_pricing_missing_total increments --
    new_missing = _wait_for_metric_increment(
        "rastir_pricing_missing_total", baseline_missing, min_delta=1.0,
    )
    missing_delta = new_missing - baseline_missing
    assert missing_delta >= 1, (
        f"rastir_pricing_missing_total did not increment: {baseline_missing} -> {new_missing}"
    )
    print(f"  + Prometheus: rastir_pricing_missing_total +{missing_delta}")
    print("  + TEST 2 PASSED: pricing missing in Tempo + Prometheus")


# ============================================================================
# TEST 3: Cost counter + histogram in Prometheus
# ============================================================================

def test_cost_counter_in_prometheus():
    """Verify rastir_cost_total and rastir_cost_per_call_usd increment.

    Makes a priced LLM call and checks Prometheus metrics:
      - rastir_cost_total counter increases
      - rastir_cost_per_call_usd_count histogram count increases
    """
    from google import genai
    client = genai.Client(api_key=GOOGLE_API_KEY)

    baseline_cost = _get_prometheus_metric_sum("rastir_cost_total")
    baseline_hist = _get_prometheus_metric_sum("rastir_cost_per_call_usd_count")
    baseline_spans = _get_prometheus_metric_sum("rastir_spans_ingested_total")

    configure(
        service="cost-prom-e2e",
        env="integration",
        push_url=COLLECTOR_URL,
        flush_interval=1,
        batch_size=10,
        enable_cost_calculation=True,
        pricing_profile="prom_test",
    )
    registry = get_pricing_registry()
    registry.register("gemini", MODEL, input_price=0.10, output_price=0.40)

    @llm_span
    def ask_for_cost(prompt: str) -> Any:
        return client.models.generate_content(
            model=MODEL, contents=prompt,
        )

    ask_for_cost("What is the capital of France? One word.")

    # -- Wait for span ingestion --
    new_spans = _wait_for_metric_increment(
        "rastir_spans_ingested_total", baseline_spans, min_delta=1.0,
    )
    assert new_spans > baseline_spans

    # -- Check rastir_cost_total --
    new_cost = _wait_for_metric_increment(
        "rastir_cost_total", baseline_cost, min_delta=0.000001,
    )
    cost_delta = new_cost - baseline_cost
    assert cost_delta > 0, (
        f"rastir_cost_total did not increment: {baseline_cost} -> {new_cost}"
    )
    print(f"\n  + Prometheus: rastir_cost_total +{cost_delta:.6f}")

    # -- Check rastir_cost_per_call_usd histogram --
    new_hist = _wait_for_metric_increment(
        "rastir_cost_per_call_usd_count", baseline_hist, min_delta=1.0,
    )
    hist_delta = new_hist - baseline_hist
    assert hist_delta >= 1, (
        f"rastir_cost_per_call_usd_count did not increment: {baseline_hist} -> {new_hist}"
    )
    print(f"  + Prometheus: rastir_cost_per_call_usd_count +{hist_delta}")
    print("  + TEST 3 PASSED: cost counter + histogram in Prometheus")


# ============================================================================
# TEST 4: TTFT in Tempo trace (streaming)
# ============================================================================

def test_ttft_in_tempo_streaming():
    """Verify ttft_ms and streaming=true appear in Tempo for streaming calls.

    Makes a real streaming Gemini call.  Verifies:
      - streaming == true in Tempo span
      - ttft_ms > 0 in Tempo span
      - ttft_ms is a reasonable value (< 30 000 ms)
    """
    from google import genai
    client = genai.Client(api_key=GOOGLE_API_KEY)

    baseline = _get_prometheus_metric_sum("rastir_spans_ingested_total")

    configure(
        service="ttft-tempo-e2e",
        env="integration",
        push_url=COLLECTOR_URL,
        flush_interval=1,
        batch_size=10,
        enable_ttft=True,
    )

    trace_id = None

    @agent_span(agent_name="ttft_test_agent")
    def run_ttft_test():
        nonlocal trace_id
        span = get_current_span()
        if span:
            trace_id = span.trace_id

        @llm_span(streaming=True)
        def ask_streaming(prompt: str):
            response = client.models.generate_content_stream(
                model=MODEL, contents=prompt,
            )
            for chunk in response:
                yield chunk

        return list(ask_streaming("Count to 3 slowly."))

    chunks = run_ttft_test()
    assert len(chunks) > 0, "No streaming chunks received"
    print(f"\n  Received {len(chunks)} streaming chunks")

    # -- Wait for spans --
    new_count = _wait_for_metric_increment(
        "rastir_spans_ingested_total", baseline, min_delta=2.0,
    )
    assert new_count > baseline

    # -- Query Tempo --
    assert trace_id
    trace_data = _query_tempo_trace(trace_id)
    assert trace_data, f"Trace {trace_id} not found in Tempo"
    tempo_spans = _extract_spans_from_tempo(trace_data)

    print(f"  Tempo trace {trace_id}: {len(tempo_spans)} spans")
    for s in tempo_spans:
        a = _get_tempo_span_attrs(s)
        print(f"    {s.get('name', '?'):30s} span_type={a.get('span_type', '?'):10s} attrs={a}")

    llm_spans = _find_tempo_spans_by_type(tempo_spans, "llm")
    assert len(llm_spans) >= 1, "No LLM span in Tempo"
    llm_attrs = _get_tempo_span_attrs(llm_spans[0])

    # streaming flag
    streaming = llm_attrs.get("streaming")
    assert streaming is True or streaming == "true", (
        f"streaming should be true: {streaming}"
    )
    print(f"  + streaming: {streaming}")

    # ttft_ms present and reasonable
    assert "ttft_ms" in llm_attrs, (
        f"ttft_ms missing from streaming LLM span: {llm_attrs}"
    )
    ttft_ms = llm_attrs["ttft_ms"]
    assert isinstance(ttft_ms, (int, float)), f"ttft_ms should be numeric: {ttft_ms}"
    assert ttft_ms > 0, f"ttft_ms should be > 0: {ttft_ms}"
    assert ttft_ms < 30000, f"ttft_ms should be < 30s: {ttft_ms}"
    print(f"  + ttft_ms: {ttft_ms:.2f}ms")

    print("  + TEST 4 PASSED: TTFT in Tempo")


# ============================================================================
# TEST 5: TTFT histogram in Prometheus
# ============================================================================

def test_ttft_histogram_in_prometheus():
    """Verify rastir_ttft_seconds histogram gets samples after streaming.

    Checks:
      - rastir_ttft_seconds_count increments
      - rastir_ttft_seconds_sum > 0
    """
    from google import genai
    client = genai.Client(api_key=GOOGLE_API_KEY)

    baseline_ttft_count = _get_prometheus_metric_sum("rastir_ttft_seconds_count")
    baseline_spans = _get_prometheus_metric_sum("rastir_spans_ingested_total")

    configure(
        service="ttft-prom-e2e",
        env="integration",
        push_url=COLLECTOR_URL,
        flush_interval=1,
        batch_size=10,
        enable_ttft=True,
    )

    @llm_span(streaming=True)
    def stream_for_ttft(prompt: str):
        response = client.models.generate_content_stream(
            model=MODEL, contents=prompt,
        )
        for chunk in response:
            yield chunk

    chunks = list(stream_for_ttft("Say hello."))
    assert len(chunks) > 0

    # -- Wait for span ingestion --
    new_spans = _wait_for_metric_increment(
        "rastir_spans_ingested_total", baseline_spans, min_delta=1.0,
    )
    assert new_spans > baseline_spans

    # -- Check rastir_ttft_seconds_count --
    new_ttft_count = _wait_for_metric_increment(
        "rastir_ttft_seconds_count", baseline_ttft_count, min_delta=1.0,
    )
    ttft_delta = new_ttft_count - baseline_ttft_count
    assert ttft_delta >= 1, (
        f"rastir_ttft_seconds_count did not increment: {baseline_ttft_count} -> {new_ttft_count}"
    )
    print(f"\n  + Prometheus: rastir_ttft_seconds_count +{ttft_delta}")

    # Check sum is positive
    ttft_sum = _get_prometheus_metric_sum("rastir_ttft_seconds_sum")
    assert ttft_sum > 0, f"rastir_ttft_seconds_sum should be > 0: {ttft_sum}"
    print(f"  + Prometheus: rastir_ttft_seconds_sum = {ttft_sum:.6f}")
    print("  + TEST 5 PASSED: TTFT histogram in Prometheus")


# ============================================================================
# TEST 6: Cost + TTFT combined on streaming span
# ============================================================================

def test_cost_and_ttft_combined_in_tempo():
    """Verify both cost_usd and ttft_ms appear on the same streaming span.

    Makes a streaming Gemini call with cost + TTFT both enabled.
    Verifies in Tempo that the single LLM span carries both cost and
    TTFT attributes.  Also checks both Prometheus counters.
    """
    from google import genai
    client = genai.Client(api_key=GOOGLE_API_KEY)

    baseline_spans = _get_prometheus_metric_sum("rastir_spans_ingested_total")
    baseline_cost = _get_prometheus_metric_sum("rastir_cost_total")
    baseline_ttft = _get_prometheus_metric_sum("rastir_ttft_seconds_count")

    configure(
        service="combined-e2e",
        env="integration",
        push_url=COLLECTOR_URL,
        flush_interval=1,
        batch_size=10,
        enable_cost_calculation=True,
        pricing_profile="combined_test",
        enable_ttft=True,
    )
    registry = get_pricing_registry()
    registry.register("gemini", MODEL, input_price=0.10, output_price=0.40)

    trace_id = None

    @agent_span(agent_name="combined_test_agent")
    def run_combined():
        nonlocal trace_id
        span = get_current_span()
        if span:
            trace_id = span.trace_id

        @llm_span(streaming=True)
        def stream_with_cost(prompt: str):
            response = client.models.generate_content_stream(
                model=MODEL, contents=prompt,
            )
            for chunk in response:
                yield chunk

        return list(stream_with_cost("Count from 1 to 5."))

    chunks = run_combined()
    assert len(chunks) > 0
    print(f"\n  Received {len(chunks)} chunks")

    # -- Wait for spans --
    new_spans = _wait_for_metric_increment(
        "rastir_spans_ingested_total", baseline_spans, min_delta=2.0,
    )
    assert new_spans > baseline_spans

    # -- Query Tempo --
    assert trace_id
    trace_data = _query_tempo_trace(trace_id)
    assert trace_data, f"Trace {trace_id} not found in Tempo"
    tempo_spans = _extract_spans_from_tempo(trace_data)

    print(f"  Tempo trace {trace_id}: {len(tempo_spans)} spans")
    for s in tempo_spans:
        a = _get_tempo_span_attrs(s)
        print(f"    {s.get('name', '?'):30s} span_type={a.get('span_type', '?'):10s} attrs={a}")

    llm_spans = _find_tempo_spans_by_type(tempo_spans, "llm")
    assert len(llm_spans) >= 1
    llm_attrs = _get_tempo_span_attrs(llm_spans[0])

    # -- TTFT attributes --
    streaming = llm_attrs.get("streaming")
    assert streaming is True or streaming == "true", (
        f"streaming should be true: {streaming}"
    )
    assert "ttft_ms" in llm_attrs, f"ttft_ms missing: {llm_attrs}"
    assert llm_attrs["ttft_ms"] > 0
    print(f"  + ttft_ms: {llm_attrs['ttft_ms']:.2f}ms")

    # -- Cost attributes --
    assert "cost_usd" in llm_attrs, f"cost_usd missing: {llm_attrs}"
    print(f"  + cost_usd: {llm_attrs['cost_usd']}")

    assert llm_attrs.get("pricing_profile") == "combined_test", (
        f"pricing_profile mismatch: {llm_attrs.get('pricing_profile')}"
    )
    print(f"  + pricing_profile: {llm_attrs.get('pricing_profile')}")

    # -- Verify parent-child: agent -> llm --
    id_to_type = {
        s.get("spanId", ""): _get_tempo_span_attrs(s).get("span_type", "")
        for s in tempo_spans
    }
    llm_parent = llm_spans[0].get("parentSpanId", "")
    assert id_to_type.get(llm_parent) == "agent", (
        f"LLM parent is not agent: {id_to_type.get(llm_parent)}"
    )
    print("  + hierarchy: agent -> llm")

    # -- Prometheus: both metrics incremented --
    new_cost = _wait_for_metric_increment(
        "rastir_cost_total", baseline_cost, min_delta=0.000001,
    )
    assert new_cost > baseline_cost, (
        f"rastir_cost_total did not increment: {baseline_cost} -> {new_cost}"
    )
    print(f"  + Prometheus: rastir_cost_total +{new_cost - baseline_cost:.6f}")

    new_ttft = _wait_for_metric_increment(
        "rastir_ttft_seconds_count", baseline_ttft, min_delta=1.0,
    )
    assert new_ttft > baseline_ttft, (
        f"rastir_ttft_seconds_count did not increment: {baseline_ttft} -> {new_ttft}"
    )
    print(f"  + Prometheus: rastir_ttft_seconds_count +{new_ttft - baseline_ttft}")
    print("  + TEST 6 PASSED: cost + TTFT combined")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
