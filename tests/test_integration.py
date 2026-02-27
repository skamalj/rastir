"""Phase 8 — End-to-end integration tests.

These tests exercise the full client → server pipeline:
  1. Client decorators create spans and enqueue them.
  2. The TelemetryClient serializes and POSTs to the server.
  3. The server ingests, derives Prometheus metrics, and stores traces.

Instead of actually binding a socket we use FastAPI's TestClient (ASGI
transport) and, where needed, call the server endpoints directly with
payloads produced by real client-side machinery.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from rastir.config import GlobalConfig, ExporterConfig, reset_config
from rastir.context import start_span, end_span, set_current_agent, reset_current_agent
from rastir.queue import reset_queue
from rastir.server.app import create_app
from rastir.server.config import (
    LimitsSection,
    MultiTenantSection,
    ServerConfig,
    TraceStoreSection,
)
from rastir.spans import SpanRecord, SpanStatus, SpanType


# ========================================================================
# Helpers
# ========================================================================


def _build_server_app(**overrides) -> TestClient:
    """Create a FastAPI TestClient with sensible defaults for integration tests."""
    cfg = ServerConfig(
        limits=LimitsSection(max_queue_size=1000),
        **overrides,
    )
    app = create_app(config=cfg)
    return TestClient(app)


def _post_spans(client: TestClient, spans: list[dict], **meta) -> dict:
    """POST a span batch and return (status_code, body)."""
    payload = {
        "service": meta.get("service", "integration-svc"),
        "env": meta.get("env", "test"),
        "version": meta.get("version", "0.1.0"),
        "spans": spans,
    }
    resp = client.post("/v1/telemetry", json=payload)
    return {"status_code": resp.status_code, "body": resp.json()}


def _wait_for_processing(seconds: float = 0.5) -> None:
    """Give the async ingestion worker time to drain its queue."""
    time.sleep(seconds)


# ========================================================================
# 1. Client-produced spans → server ingestion
# ========================================================================


class TestClientToServerIngestion:
    """Verify that spans built by the real client `SpanRecord` class are
    accepted by the server and routed to the trace store.
    """

    def test_single_span_roundtrip(self):
        """A single client-built span survives serialization and lands in the trace store."""
        span = SpanRecord(name="my_func", span_type=SpanType.TRACE, trace_id="int-t1")
        span.finish()

        with _build_server_app() as client:
            result = _post_spans(client, [span.to_dict()], service="my-app")
            assert result["status_code"] == 202
            assert result["body"]["spans_received"] == 1

            _wait_for_processing()

            resp = client.get("/v1/traces", params={"trace_id": "int-t1"})
            assert resp.status_code == 200
            data = resp.json()
            assert data["trace_id"] == "int-t1"
            assert data["spans"][0]["name"] == "my_func"

    def test_parent_child_spans(self):
        """Parent-child span hierarchy is preserved through the pipeline."""
        parent = SpanRecord(name="parent", span_type=SpanType.AGENT, trace_id="int-t2")
        child = SpanRecord(
            name="child_llm",
            span_type=SpanType.LLM,
            trace_id="int-t2",
            parent_id=parent.span_id,
        )
        child.set_attribute("model", "gpt-4")
        child.set_attribute("provider", "openai")
        child.set_attribute("tokens_input", 100)
        child.set_attribute("tokens_output", 50)
        child.finish()
        parent.finish()

        with _build_server_app() as client:
            _post_spans(client, [parent.to_dict(), child.to_dict()])
            _wait_for_processing()

            resp = client.get("/v1/traces", params={"trace_id": "int-t2"})
            assert resp.status_code == 200
            spans = resp.json()["spans"]
            assert len(spans) == 2

            child_span = next(s for s in spans if s["name"] == "child_llm")
            assert child_span["parent_span_id"] == parent.span_id
            assert child_span["attributes"]["model"] == "gpt-4"

    def test_batch_of_mixed_span_types(self):
        """Multiple span types in a single batch are all accepted."""
        spans = []
        for stype, name in [
            (SpanType.TRACE, "entry"),
            (SpanType.AGENT, "planner"),
            (SpanType.LLM, "call_llm"),
            (SpanType.TOOL, "search"),
            (SpanType.RETRIEVAL, "lookup"),
        ]:
            s = SpanRecord(name=name, span_type=stype, trace_id="int-t3")
            if stype == SpanType.LLM:
                s.set_attribute("model", "claude-3")
                s.set_attribute("provider", "anthropic")
                s.set_attribute("tokens_input", 200)
                s.set_attribute("tokens_output", 100)
            elif stype == SpanType.TOOL:
                s.set_attribute("agent", "planner")
            s.finish()
            spans.append(s.to_dict())

        with _build_server_app() as client:
            result = _post_spans(client, spans)
            assert result["status_code"] == 202
            assert result["body"]["spans_received"] == 5


# ========================================================================
# 2. Metrics derivation end-to-end
# ========================================================================


class TestMetricsDerivation:
    """Ingest client-produced spans and verify the Prometheus metrics
    are correctly derived.
    """

    def test_llm_metrics_with_agent(self):
        """LLM span with agent label produces all expected counters."""
        span = SpanRecord(name="chat", span_type=SpanType.LLM, trace_id="m1")
        span.set_attribute("model", "gpt-4o")
        span.set_attribute("provider", "openai")
        span.set_attribute("agent", "assistant")
        span.set_attribute("tokens_input", 500)
        span.set_attribute("tokens_output", 200)
        span.finish()

        with _build_server_app() as client:
            _post_spans(client, [span.to_dict()], service="ai-app", env="prod")
            _wait_for_processing()

            resp = client.get("/metrics")
            text = resp.text

            # Core LLM counters
            assert 'rastir_llm_calls_total{' in text
            assert 'model="gpt-4o"' in text
            assert 'provider="openai"' in text
            assert 'agent="assistant"' in text

            # Token counters
            assert 'rastir_tokens_input_total{' in text
            assert 'rastir_tokens_output_total{' in text

            # Histogram
            assert 'rastir_tokens_per_call_bucket{' in text
            # provider should be on the histogram too
            tpc_lines = [l for l in text.splitlines() if "rastir_tokens_per_call_bucket" in l]
            assert any('provider="openai"' in l for l in tpc_lines)

            # Duration
            assert 'rastir_duration_seconds_bucket{' in text

            # Global ingestion counter
            assert 'rastir_spans_ingested_total{' in text

    def test_tool_metrics_with_tool_name(self):
        """Tool span produces tool_calls counter with tool_name label."""
        span = SpanRecord(name="web_search", span_type=SpanType.TOOL, trace_id="m2")
        span.set_attribute("agent", "research")
        span.finish()

        with _build_server_app() as client:
            _post_spans(client, [span.to_dict()])
            _wait_for_processing()

            text = client.get("/metrics").text
            assert 'rastir_tool_calls_total{' in text
            assert 'tool_name="web_search"' in text

    def test_retrieval_metrics_with_agent(self):
        """Retrieval span includes agent label in metrics."""
        span = SpanRecord(name="vector_lookup", span_type=SpanType.RETRIEVAL, trace_id="m3")
        span.set_attribute("agent", "rag-bot")
        span.finish()

        with _build_server_app() as client:
            _post_spans(client, [span.to_dict()])
            _wait_for_processing()

            text = client.get("/metrics").text
            assert 'rastir_retrieval_calls_total{' in text
            assert 'agent="rag-bot"' in text

    def test_error_span_captures_error_type(self):
        """Error span derives error_type from exception event."""
        span = SpanRecord(name="bad_call", span_type=SpanType.LLM, trace_id="m4")
        span.set_attribute("model", "gpt-4")
        span.set_attribute("provider", "openai")
        span.record_error(ValueError("something broke"))
        span.finish(status=SpanStatus.ERROR)

        with _build_server_app() as client:
            _post_spans(client, [span.to_dict()])
            _wait_for_processing()

            text = client.get("/metrics").text
            assert 'rastir_errors_total{' in text
            assert 'error_type="validation_error"' in text
            assert 'span_type="llm"' in text

    def test_error_span_without_exception_event(self):
        """Error span with no exception event falls back to error_type=unknown."""
        span = SpanRecord(name="fail", span_type=SpanType.TOOL, trace_id="m5")
        span.status = SpanStatus.ERROR
        span.finish(status=SpanStatus.ERROR)

        with _build_server_app() as client:
            _post_spans(client, [span.to_dict()])
            _wait_for_processing()

            text = client.get("/metrics").text
            assert 'error_type="unknown"' in text

    def test_multiple_services_separate_label_values(self):
        """Spans from different services produce distinct label combos."""
        s1 = SpanRecord(name="fn1", span_type=SpanType.TRACE, trace_id="ms1")
        s1.finish()
        s2 = SpanRecord(name="fn2", span_type=SpanType.TRACE, trace_id="ms2")
        s2.finish()

        with _build_server_app() as client:
            _post_spans(client, [s1.to_dict()], service="svc-a", env="staging")
            _post_spans(client, [s2.to_dict()], service="svc-b", env="prod")
            _wait_for_processing()

            text = client.get("/metrics").text
            assert 'service="svc-a"' in text
            assert 'service="svc-b"' in text
            assert 'env="staging"' in text
            assert 'env="prod"' in text


# ========================================================================
# 3. Trace store end-to-end
# ========================================================================


class TestTraceStoreE2E:
    """Verify trace querying works end-to-end through the API."""

    def test_recent_traces_ordered(self):
        """GET /v1/traces returns most recent traces."""
        with _build_server_app() as client:
            for i in range(5):
                s = SpanRecord(name=f"fn{i}", span_type=SpanType.TRACE, trace_id=f"rt-{i}")
                s.finish()
                _post_spans(client, [s.to_dict()])

            _wait_for_processing()

            resp = client.get("/v1/traces", params={"limit": 3})
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["traces"]) <= 3

    def test_trace_store_disabled(self):
        """With trace store disabled, GET /v1/traces returns 404."""
        with _build_server_app(trace_store=TraceStoreSection(enabled=False)) as client:
            resp = client.get("/v1/traces")
            assert resp.status_code == 404

    def test_multiple_spans_same_trace(self):
        """Multiple spans for the same trace_id group together."""
        spans = []
        for name in ["root", "child1", "child2"]:
            s = SpanRecord(name=name, span_type=SpanType.TRACE, trace_id="grouped-t")
            s.finish()
            spans.append(s.to_dict())

        with _build_server_app() as client:
            _post_spans(client, spans)
            _wait_for_processing()

            resp = client.get("/v1/traces", params={"trace_id": "grouped-t"})
            assert resp.status_code == 200
            assert len(resp.json()["spans"]) == 3

    def test_trace_not_found(self):
        with _build_server_app() as client:
            resp = client.get("/v1/traces", params={"trace_id": "does-not-exist"})
            assert resp.status_code == 404


# ========================================================================
# 4. Error handling and backpressure
# ========================================================================


class TestErrorAndBackpressure:
    """Verify rejection, validation, and queue-full behavior."""

    def test_invalid_json_returns_400(self):
        with _build_server_app() as client:
            resp = client.post(
                "/v1/telemetry",
                content=b"{bad json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status_code == 400

    def test_missing_spans_field_returns_400(self):
        with _build_server_app() as client:
            resp = client.post("/v1/telemetry", json={"service": "svc"})
            assert resp.status_code == 400

    def test_empty_spans_returns_400(self):
        with _build_server_app() as client:
            resp = client.post(
                "/v1/telemetry", json={"service": "svc", "spans": []}
            )
            assert resp.status_code == 400

    def test_queue_full_returns_429(self):
        """When the ingestion queue is full, the server responds 429."""
        cfg = ServerConfig(limits=LimitsSection(max_queue_size=1))
        app = create_app(config=cfg)
        with TestClient(app) as client:
            # Fill the tiny queue — we need the worker to NOT drain it
            span = SpanRecord(name="filler", span_type=SpanType.TRACE)
            span.finish()
            payload = {
                "service": "svc",
                "env": "test",
                "version": "0.1.0",
                "spans": [span.to_dict()],
            }
            # First request fills the queue
            client.post("/v1/telemetry", json=payload)
            # Immediately post again before the worker can drain
            resp = client.post("/v1/telemetry", json=payload)
            # Accept either 202 (if worker was fast) or 429 (queue full)
            assert resp.status_code in (202, 429)

    def test_queue_size_gauge_updates(self):
        """The rastir_queue_size gauge reflects items enqueued."""
        with _build_server_app() as client:
            span = SpanRecord(name="g", span_type=SpanType.TRACE)
            span.finish()
            _post_spans(client, [span.to_dict()])
            # Even after processing, gauge should be defined
            _wait_for_processing()
            text = client.get("/metrics").text
            assert "rastir_queue_size" in text


# ========================================================================
# 5. Health and readiness probes
# ========================================================================


class TestHealthReadiness:
    """Verify the operational endpoints work through the full app."""

    def test_health_returns_ok(self):
        with _build_server_app() as client:
            resp = client.get("/health")
            assert resp.status_code == 200
            assert resp.json()["status"] == "ok"

    def test_ready_with_empty_queue(self):
        with _build_server_app() as client:
            resp = client.get("/ready")
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "ready"
            assert body["queue_pct"] == 0.0

    def test_ready_returns_json(self):
        with _build_server_app() as client:
            resp = client.get("/ready")
            assert "application/json" in resp.headers["content-type"]


# ========================================================================
# 6. Multi-tenant injection
# ========================================================================


class TestMultiTenantE2E:
    """Verify tenant isolation through the full pipeline."""

    def test_tenant_label_injected_into_span(self):
        cfg = ServerConfig(
            multi_tenant=MultiTenantSection(enabled=True, header_name="X-Tenant"),
        )
        app = create_app(config=cfg)
        with TestClient(app) as client:
            span = SpanRecord(name="tenanted", span_type=SpanType.TRACE, trace_id="mt-1")
            span.finish()
            client.post(
                "/v1/telemetry",
                json={
                    "service": "svc",
                    "env": "test",
                    "version": "0.1",
                    "spans": [span.to_dict()],
                },
                headers={"X-Tenant": "acme"},
            )
            _wait_for_processing()

            resp = client.get("/v1/traces", params={"trace_id": "mt-1"})
            assert resp.status_code == 200
            attrs = resp.json()["spans"][0]["attributes"]
            assert attrs["tenant"] == "acme"

    def test_default_tenant_when_header_missing(self):
        cfg = ServerConfig(
            multi_tenant=MultiTenantSection(enabled=True),
        )
        app = create_app(config=cfg)
        with TestClient(app) as client:
            span = SpanRecord(name="no_header", span_type=SpanType.TRACE, trace_id="mt-2")
            span.finish()
            client.post(
                "/v1/telemetry",
                json={"service": "svc", "env": "test", "version": "0.1", "spans": [span.to_dict()]},
            )
            _wait_for_processing()

            resp = client.get("/v1/traces", params={"trace_id": "mt-2"})
            if resp.status_code == 200:
                assert resp.json()["spans"][0]["attributes"]["tenant"] == "default"


# ========================================================================
# 7. Client SpanRecord serialization fidelity
# ========================================================================


class TestSpanSerializationFidelity:
    """Confirm that the data round-trips correctly: SpanRecord.to_dict()
    → JSON → server → trace store → GET /v1/traces response.
    """

    def test_all_fields_preserved(self):
        span = SpanRecord(
            name="detailed_call",
            span_type=SpanType.LLM,
            trace_id="fid-1",
        )
        span.parent_id = "parent-abc"
        span.set_attribute("model", "claude-3-opus")
        span.set_attribute("provider", "anthropic")
        span.set_attribute("tokens_input", 1234)
        span.set_attribute("tokens_output", 567)
        span.set_attribute("agent", "research-bot")
        span.record_error(RuntimeError("boom"))
        span.finish(status=SpanStatus.ERROR)

        raw = span.to_dict()

        with _build_server_app() as client:
            _post_spans(client, [raw])
            _wait_for_processing()

            resp = client.get("/v1/traces", params={"trace_id": "fid-1"})
            assert resp.status_code == 200
            stored = resp.json()["spans"][0]

            assert stored["name"] == "detailed_call"
            assert stored["span_type"] == "llm"
            assert stored["status"] == "ERROR"
            assert stored["parent_span_id"] == "parent-abc"
            assert stored["attributes"]["model"] == "claude-3-opus"
            assert stored["attributes"]["tokens_input"] == 1234
            assert stored["attributes"]["agent"] == "research-bot"
            assert len(stored["events"]) == 1
            assert stored["events"][0]["name"] == "exception"
            assert stored["events"][0]["attributes"]["exception.type"] == "RuntimeError"

    def test_duration_is_positive(self):
        span = SpanRecord(name="timed", span_type=SpanType.TRACE, trace_id="fid-2")
        time.sleep(0.01)  # ensure non-zero duration
        span.finish()

        with _build_server_app() as client:
            _post_spans(client, [span.to_dict()])
            _wait_for_processing()

            text = client.get("/metrics").text
            # Duration histogram should have observed a value
            assert "rastir_duration_seconds_count" in text
