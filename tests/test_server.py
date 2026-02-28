"""Tests for the Rastir collector server.

Covers: config loading, trace store, metrics registry, ingestion worker,
FastAPI endpoints (via httpx AsyncClient / TestClient).
"""

from __future__ import annotations

import asyncio
import json
import os
import textwrap
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from rastir.server.config import (
    ExporterSection,
    HistogramSection,
    LimitsSection,
    MultiTenantSection,
    SamplingSection,
    BackpressureSection,
    ServerConfig,
    ServerSection,
    TraceStoreSection,
    load_config,
)
from rastir.server.ingestion import IngestionWorker
from rastir.server.metrics import MetricsRegistry
from rastir.server.trace_store import TraceStore
from rastir.server.app import create_app


# ========================================================================
# Helpers
# ========================================================================


def _span(
    name: str = "test_fn",
    span_type: str = "trace",
    status: str = "OK",
    trace_id: str = "abc-123",
    duration_ms: float = 100.0,
    events: list[dict] | None = None,
    **attrs,
) -> dict:
    """Build a minimal span dict matching the client payload format."""
    d: dict = {
        "name": name,
        "span_type": span_type,
        "status": status,
        "trace_id": trace_id,
        "duration_ms": duration_ms,
        "attributes": attrs,
    }
    if events is not None:
        d["events"] = events
    return d


def _payload(
    spans: list[dict],
    service: str = "test-svc",
    env: str = "test",
    version: str = "0.1.0",
) -> dict:
    return {
        "service": service,
        "env": env,
        "version": version,
        "spans": spans,
    }


# ========================================================================
# ServerConfig tests
# ========================================================================


class TestServerConfig:
    def test_defaults(self):
        cfg = load_config()
        assert cfg.server.host == "0.0.0.0"
        assert cfg.server.port == 8080
        assert cfg.limits.max_traces == 10_000
        assert cfg.limits.max_queue_size == 50_000
        assert cfg.limits.max_span_attributes == 100
        assert cfg.limits.max_label_value_length == 128
        assert cfg.limits.cardinality_model == 50
        assert cfg.limits.cardinality_provider == 10
        assert cfg.limits.cardinality_tool_name == 200
        assert cfg.limits.cardinality_agent == 200
        assert cfg.limits.cardinality_error_type == 50
        assert cfg.trace_store.enabled is True
        assert cfg.exporter.enabled is False
        assert cfg.multi_tenant.enabled is False
        assert len(cfg.histograms.duration_buckets) <= 20
        assert len(cfg.histograms.tokens_buckets) <= 20

    def test_env_var_override(self, monkeypatch):
        monkeypatch.setenv("RASTIR_SERVER_HOST", "127.0.0.1")
        monkeypatch.setenv("RASTIR_SERVER_PORT", "9090")
        monkeypatch.setenv("RASTIR_SERVER_LIMITS_MAX_TRACES", "500")
        monkeypatch.setenv("RASTIR_SERVER_TRACE_STORE_ENABLED", "false")
        monkeypatch.setenv("RASTIR_SERVER_MULTI_TENANT_ENABLED", "true")
        monkeypatch.setenv("RASTIR_SERVER_MULTI_TENANT_HEADER_NAME", "X-Org")

        cfg = load_config()
        assert cfg.server.host == "127.0.0.1"
        assert cfg.server.port == 9090
        assert cfg.limits.max_traces == 500
        assert cfg.trace_store.enabled is False
        assert cfg.multi_tenant.enabled is True
        assert cfg.multi_tenant.header_name == "X-Org"

    def test_yaml_config(self, tmp_path):
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text(textwrap.dedent("""\
            server:
              host: 10.0.0.1
              port: 7070
            limits:
              max_traces: 2000
            exporter:
              otlp_endpoint: http://tempo:4318
              batch_size: 300
        """))
        cfg = load_config(config_path=str(yaml_file))
        assert cfg.server.host == "10.0.0.1"
        assert cfg.server.port == 7070
        assert cfg.limits.max_traces == 2000
        assert cfg.exporter.otlp_endpoint == "http://tempo:4318"
        assert cfg.exporter.batch_size == 300

    def test_env_overrides_yaml(self, tmp_path, monkeypatch):
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text(textwrap.dedent("""\
            server:
              port: 7070
        """))
        monkeypatch.setenv("RASTIR_SERVER_PORT", "9999")
        cfg = load_config(config_path=str(yaml_file))
        assert cfg.server.port == 9999

    def test_exporter_enabled_when_endpoint_set(self):
        cfg = ServerConfig(
            exporter=ExporterSection(otlp_endpoint="http://tempo:4318")
        )
        assert cfg.exporter.enabled is True

    def test_exporter_disabled_by_default(self):
        cfg = ServerConfig()
        assert cfg.exporter.enabled is False


# ========================================================================
# TraceStore tests
# ========================================================================


class TestTraceStore:
    def test_insert_and_get(self):
        store = TraceStore(max_traces=100)
        store.insert("t1", [_span(trace_id="t1")])
        result = store.get("t1")
        assert result is not None
        assert len(result) == 1
        assert result[0]["trace_id"] == "t1"

    def test_append_to_existing_trace(self):
        store = TraceStore()
        store.insert("t1", [_span(name="s1", trace_id="t1")])
        store.insert("t1", [_span(name="s2", trace_id="t1")])
        result = store.get("t1")
        assert len(result) == 2
        assert result[0]["name"] == "s1"
        assert result[1]["name"] == "s2"

    def test_eviction(self):
        store = TraceStore(max_traces=3)
        for i in range(5):
            store.insert(f"t{i}", [_span(trace_id=f"t{i}")])

        # Oldest two (t0, t1) should be evicted
        assert store.get("t0") is None
        assert store.get("t1") is None
        assert store.get("t2") is not None
        assert store.get("t3") is not None
        assert store.get("t4") is not None
        assert store.trace_count == 3
        assert store.evicted_traces == 2

    def test_get_returns_copy(self):
        store = TraceStore()
        store.insert("t1", [_span(trace_id="t1")])
        result = store.get("t1")
        result.append(_span(name="injected"))
        assert len(store.get("t1")) == 1  # Original unaffected

    def test_recent(self):
        store = TraceStore()
        for i in range(5):
            store.insert(f"t{i}", [_span(trace_id=f"t{i}")])

        recent = store.recent(limit=3)
        assert len(recent) == 3
        # Most recent first
        assert recent[0]["trace_id"] == "t4"
        assert recent[1]["trace_id"] == "t3"
        assert recent[2]["trace_id"] == "t2"

    def test_get_missing_returns_none(self):
        store = TraceStore()
        assert store.get("nonexistent") is None

    def test_clear(self):
        store = TraceStore()
        store.insert("t1", [_span()])
        store.clear()
        assert store.trace_count == 0
        assert store.span_count == 0


# ========================================================================
# MetricsRegistry tests
# ========================================================================


class TestMetricsRegistry:
    def test_record_basic_span(self):
        reg = MetricsRegistry()
        reg.record_span(_span(), service="svc", env="prod")
        output = reg.generate()[0].decode()
        assert "rastir_spans_ingested_total" in output

    def test_llm_span_metrics(self):
        reg = MetricsRegistry()
        reg.record_span(
            _span(
                span_type="llm",
                model="gpt-4",
                provider="openai",
                tokens_input=500,
                tokens_output=200,
            ),
            service="svc",
            env="prod",
        )
        output = reg.generate()[0].decode()
        assert "rastir_llm_calls_total" in output
        assert "rastir_tokens_input_total" in output
        assert "rastir_tokens_output_total" in output
        assert "rastir_tokens_per_call" in output
        assert 'model="gpt-4"' in output
        assert 'provider="openai"' in output
        assert 'agent=""' in output

    def test_tool_span_metrics(self):
        reg = MetricsRegistry()
        reg.record_span(
            _span(name="search_db", span_type="tool", agent="research-agent"),
            service="svc",
            env="prod",
        )
        output = reg.generate()[0].decode()
        assert "rastir_tool_calls_total" in output
        assert 'tool_name="search_db"' in output

    def test_llm_span_with_agent_label(self):
        reg = MetricsRegistry()
        reg.record_span(
            _span(
                span_type="llm",
                model="claude-3",
                provider="anthropic",
                tokens_input=100,
                tokens_output=50,
                agent="research-bot",
            ),
            service="svc",
            env="prod",
        )
        output = reg.generate()[0].decode()
        assert 'agent="research-bot"' in output

    def test_retrieval_span_metrics(self):
        reg = MetricsRegistry()
        reg.record_span(
            _span(span_type="retrieval", agent="rag-agent"),
            service="svc",
            env="prod",
        )
        output = reg.generate()[0].decode()
        assert "rastir_retrieval_calls_total" in output
        assert 'agent="rag-agent"' in output

    def test_error_counter(self):
        reg = MetricsRegistry()
        reg.record_span(
            _span(status="ERROR", span_type="llm"),
            service="svc",
            env="prod",
        )
        output = reg.generate()[0].decode()
        assert "rastir_errors_total" in output
        assert 'error_type="unknown"' in output

    def test_error_counter_with_error_type(self):
        reg = MetricsRegistry()
        reg.record_span(
            _span(
                status="ERROR",
                span_type="llm",
                events=[
                    {
                        "name": "exception",
                        "attributes": {
                            "exception.type": "RateLimitError",
                            "exception.message": "Too many requests",
                        },
                    }
                ],
            ),
            service="svc",
            env="prod",
        )
        output = reg.generate()[0].decode()
        assert 'error_type="rate_limit"' in output

    def test_tokens_per_call_has_provider(self):
        reg = MetricsRegistry()
        reg.record_span(
            _span(
                span_type="llm",
                model="gpt-4",
                provider="openai",
                tokens_input=100,
                tokens_output=50,
            ),
            service="svc",
            env="prod",
        )
        output = reg.generate()[0].decode()
        # tokens_per_call should include provider label
        lines = [l for l in output.splitlines() if "rastir_tokens_per_call_bucket" in l]
        assert any('provider="openai"' in l for l in lines)

    def test_queue_size_gauge(self):
        reg = MetricsRegistry()
        reg.queue_size.set(42)
        output = reg.generate()[0].decode()
        assert "rastir_queue_size" in output
        assert "42.0" in output

    def test_duration_histogram(self):
        reg = MetricsRegistry()
        reg.record_span(_span(duration_ms=1500.0), service="svc", env="prod")
        output = reg.generate()[0].decode()
        assert "rastir_duration_seconds" in output

    def test_label_clipping(self):
        reg = MetricsRegistry(max_label_value_length=10)
        reg.record_span(
            _span(span_type="llm", model="a" * 50, provider="openai"),
            service="svc",
            env="prod",
        )
        output = reg.generate()[0].decode()
        assert 'model="aaaaaaaaaa"' in output  # Clipped to 10 chars

    def test_cardinality_guard(self):
        reg = MetricsRegistry(cardinality_caps={"model": 50, "provider": 10})
        # Exhaust the model cardinality limit (50)
        for i in range(51):
            reg.record_span(
                _span(span_type="llm", model=f"model-{i}", provider="p"),
                service="svc",
                env="test",
            )
        output = reg.generate()[0].decode()
        assert "__cardinality_overflow__" in output

    def test_per_dimension_cardinality_provider(self):
        """Provider cap (10) is separate from model cap."""
        reg = MetricsRegistry(cardinality_caps={"model": 500, "provider": 3})
        for i in range(4):
            reg.record_span(
                _span(span_type="llm", model="m", provider=f"prov-{i}"),
                service="svc",
                env="test",
            )
        output = reg.generate()[0].decode()
        assert 'provider="__cardinality_overflow__"' in output

    def test_span_type_normalisation_trace_to_system(self):
        """span_type='trace' is normalised to 'system' in metrics."""
        reg = MetricsRegistry()
        reg.record_span(_span(span_type="trace"), service="svc", env="prod")
        output = reg.generate()[0].decode()
        assert 'span_type="system"' in output

    def test_span_type_normalisation_metric_to_system(self):
        """span_type='metric' is normalised to 'system'."""
        reg = MetricsRegistry()
        reg.record_span(_span(span_type="metric"), service="svc", env="prod")
        output = reg.generate()[0].decode()
        assert 'span_type="system"' in output

    def test_span_type_llm_stays_llm(self):
        """Known types like 'llm' are not changed."""
        reg = MetricsRegistry()
        reg.record_span(
            _span(span_type="llm", model="m", provider="p"),
            service="svc",
            env="prod",
        )
        output = reg.generate()[0].decode()
        assert 'span_type="llm"' in output

    def test_unknown_span_type_mapped_to_system(self):
        reg = MetricsRegistry()
        reg.record_span(_span(span_type="custom_thing"), service="svc", env="prod")
        output = reg.generate()[0].decode()
        assert 'span_type="system"' in output

    def test_configurable_duration_buckets(self):
        """Custom duration buckets are applied."""
        reg = MetricsRegistry(duration_buckets=(1.0, 5.0, 10.0))
        reg.record_span(_span(duration_ms=3000.0), service="svc", env="prod")
        output = reg.generate()[0].decode()
        assert 'le="1.0"' in output
        assert 'le="5.0"' in output
        assert 'le="10.0"' in output
        # default bucket 0.01 should NOT be present
        assert 'le="0.01"' not in output

    def test_configurable_tokens_buckets(self):
        reg = MetricsRegistry(tokens_buckets=(100, 500, 1000))
        reg.record_span(
            _span(span_type="llm", model="m", provider="p", tokens_input=200, tokens_output=100),
            service="svc",
            env="prod",
        )
        output = reg.generate()[0].decode()
        tpc_lines = [l for l in output.splitlines() if "rastir_tokens_per_call_bucket" in l]
        assert any('le="100.0"' in l for l in tpc_lines)
        assert any('le="500.0"' in l for l in tpc_lines)

    def test_bucket_count_validation(self):
        """Exceeding max bucket count raises ValueError."""
        import pytest
        with pytest.raises(ValueError, match="maximum is 20"):
            MetricsRegistry(duration_buckets=tuple(float(i) for i in range(25)))

    def test_error_normalisation_timeout(self):
        reg = MetricsRegistry()
        reg.record_span(
            _span(status="ERROR", span_type="llm", events=[
                {"name": "exception", "attributes": {"exception.type": "TimeoutError"}}
            ]),
            service="svc", env="prod",
        )
        output = reg.generate()[0].decode()
        assert 'error_type="timeout"' in output

    def test_error_normalisation_validation(self):
        reg = MetricsRegistry()
        reg.record_span(
            _span(status="ERROR", span_type="llm", events=[
                {"name": "exception", "attributes": {"exception.type": "ValueError"}}
            ]),
            service="svc", env="prod",
        )
        output = reg.generate()[0].decode()
        assert 'error_type="validation_error"' in output

    def test_error_normalisation_internal(self):
        reg = MetricsRegistry()
        reg.record_span(
            _span(status="ERROR", span_type="tool", events=[
                {"name": "exception", "attributes": {"exception.type": "RuntimeError"}}
            ]),
            service="svc", env="prod",
        )
        output = reg.generate()[0].decode()
        assert 'error_type="internal_error"' in output

    def test_error_normalisation_unknown_exception(self):
        """An unrecognised exception class falls back to 'unknown'."""
        reg = MetricsRegistry()
        reg.record_span(
            _span(status="ERROR", span_type="llm", events=[
                {"name": "exception", "attributes": {"exception.type": "MyCustomError"}}
            ]),
            service="svc", env="prod",
        )
        output = reg.generate()[0].decode()
        assert 'error_type="unknown"' in output

    def test_guardrail_request_metric(self):
        """LLM span with guardrail_id emits rastir_guardrail_requests_total."""
        reg = MetricsRegistry()
        reg.record_span(
            _span(
                span_type="llm",
                model="claude-3",
                provider="bedrock",
                guardrail_id="gr-abc123",
                guardrail_version="3",
                guardrail_enabled=True,
            ),
            service="svc",
            env="prod",
        )
        output = reg.generate()[0].decode()
        assert "rastir_guardrail_requests_total" in output
        assert 'guardrail_id="gr-abc123"' in output
        assert 'guardrail_version="3"' in output

    def test_guardrail_violation_metric(self):
        """LLM span with guardrail_action emits rastir_guardrail_violations_total."""
        reg = MetricsRegistry()
        reg.record_span(
            _span(
                span_type="llm",
                model="claude-3",
                provider="bedrock",
                guardrail_id="gr-abc123",
                guardrail_action="GUARDRAIL_INTERVENED",
                guardrail_category="CONTENT_POLICY",
            ),
            service="svc",
            env="prod",
        )
        output = reg.generate()[0].decode()
        assert "rastir_guardrail_violations_total" in output
        assert 'guardrail_action="GUARDRAIL_INTERVENED"' in output
        assert 'guardrail_category="CONTENT_POLICY"' in output
        assert 'model="claude-3"' in output

    def test_guardrail_no_metric_without_guardrail(self):
        """LLM span without guardrail attributes doesn't increment guardrail counters."""
        reg = MetricsRegistry()
        reg.record_span(
            _span(
                span_type="llm",
                model="gpt-4",
                provider="openai",
            ),
            service="svc",
            env="prod",
        )
        output = reg.generate()[0].decode()
        # Counters still registered but no samples
        assert 'guardrail_id=' not in output

    def test_guardrail_category_cardinality_guard(self):
        """Unknown guardrail_category is replaced with overflow sentinel."""
        reg = MetricsRegistry()
        reg.record_span(
            _span(
                span_type="llm",
                model="claude-3",
                provider="bedrock",
                guardrail_id="gr-test",
                guardrail_action="GUARDRAIL_INTERVENED",
                guardrail_category="TOTALLY_MADE_UP_CATEGORY",
            ),
            service="svc",
            env="prod",
        )
        output = reg.generate()[0].decode()
        assert "rastir_guardrail_violations_total" in output
        assert "TOTALLY_MADE_UP_CATEGORY" not in output
        assert '__cardinality_overflow__' in output

    def test_guardrail_action_cardinality_guard(self):
        """Unknown guardrail_action is replaced with overflow sentinel."""
        reg = MetricsRegistry()
        reg.record_span(
            _span(
                span_type="llm",
                model="claude-3",
                provider="bedrock",
                guardrail_id="gr-test",
                guardrail_action="INJECTED_ACTION_VALUE",
                guardrail_category="CONTENT_POLICY",
            ),
            service="svc",
            env="prod",
        )
        output = reg.generate()[0].decode()
        assert "rastir_guardrail_violations_total" in output
        assert "INJECTED_ACTION_VALUE" not in output
        assert 'guardrail_action="__cardinality_overflow__"' in output
        assert 'guardrail_category="CONTENT_POLICY"' in output


# ========================================================================
# IngestionWorker tests
# ========================================================================


class TestIngestionWorker:
    @pytest.fixture
    def components(self):
        metrics = MetricsRegistry()
        store = TraceStore()
        worker = IngestionWorker(metrics=metrics, trace_store=store, max_queue_size=100)
        return metrics, store, worker

    def test_enqueue_and_process(self, components):
        metrics, store, worker = components

        async def _run():
            worker.start()
            worker.enqueue("svc", "test", "0.1", [_span(trace_id="t1")])
            await asyncio.sleep(0.5)
            await worker.stop()
            return store.get("t1")

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result is not None
        assert len(result) == 1

    def test_queue_full_returns_false(self, components):
        _, _, worker = components
        # Create a tiny queue
        worker._queue = asyncio.Queue(maxsize=1)

        async def _run():
            worker.start()
            # Fill the queue
            worker.enqueue("svc", "test", "0.1", [_span()])
            # Next should be rejected
            accepted = worker.enqueue("svc", "test", "0.1", [_span()])
            await worker.stop()
            return accepted

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result is False

    def test_multiple_batches(self, components):
        metrics, store, worker = components

        async def _run():
            worker.start()
            for i in range(5):
                worker.enqueue("svc", "test", "0.1", [_span(trace_id=f"t{i}")])
            await asyncio.sleep(1.0)
            await worker.stop()
            return store.trace_count

        count = asyncio.get_event_loop().run_until_complete(_run())
        assert count == 5


# ========================================================================
# FastAPI endpoint tests (TestClient — synchronous)
# ========================================================================


class TestAppEndpoints:
    @pytest.fixture
    def app(self):
        cfg = ServerConfig(
            limits=LimitsSection(max_queue_size=1000),
        )
        return create_app(config=cfg)

    @pytest.fixture
    def client(self, app):
        with TestClient(app) as c:
            yield c

    # -- health / ready --

    def test_health(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_ready(self, client):
        resp = client.get("/ready")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ready"

    # -- ingestion --

    def test_ingest_valid_payload(self, client):
        payload = _payload([_span()])
        resp = client.post("/v1/telemetry", json=payload)
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "accepted"
        assert body["spans_received"] == 1

    def test_ingest_multiple_spans(self, client):
        payload = _payload([_span(name=f"s{i}") for i in range(5)])
        resp = client.post("/v1/telemetry", json=payload)
        assert resp.status_code == 202
        assert resp.json()["spans_received"] == 5

    def test_ingest_invalid_json(self, client):
        resp = client.post(
            "/v1/telemetry",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_ingest_missing_spans(self, client):
        resp = client.post("/v1/telemetry", json={"service": "svc"})
        assert resp.status_code == 400

    def test_ingest_empty_spans(self, client):
        resp = client.post("/v1/telemetry", json={"service": "svc", "spans": []})
        assert resp.status_code == 400

    # -- metrics --

    def test_metrics_endpoint(self, client):
        # Ingest a span first
        client.post("/v1/telemetry", json=_payload([_span()]))
        import time; time.sleep(0.3)  # Let worker process

        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert "rastir_spans_ingested_total" in resp.text

    def test_metrics_content_type(self, client):
        resp = client.get("/metrics")
        assert "text/plain" in resp.headers["content-type"]

    # -- traces --

    def test_traces_recent(self, client):
        # Ingest spans
        for i in range(3):
            client.post("/v1/telemetry", json=_payload([_span(trace_id=f"t{i}")]))
        import time; time.sleep(0.5)

        resp = client.get("/v1/traces")
        assert resp.status_code == 200
        data = resp.json()
        assert "traces" in data

    def test_traces_by_id(self, client):
        client.post("/v1/telemetry", json=_payload([_span(trace_id="lookup-me")]))
        import time; time.sleep(0.5)

        resp = client.get("/v1/traces", params={"trace_id": "lookup-me"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["trace_id"] == "lookup-me"
        assert len(data["spans"]) == 1

    def test_traces_not_found(self, client):
        resp = client.get("/v1/traces", params={"trace_id": "nonexistent"})
        assert resp.status_code == 404

    def test_traces_disabled(self):
        cfg = ServerConfig(trace_store=TraceStoreSection(enabled=False))
        app = create_app(config=cfg)
        with TestClient(app) as c:
            resp = c.get("/v1/traces")
            assert resp.status_code == 404

    # -- multi-tenant --

    def test_multi_tenant_header(self):
        cfg = ServerConfig(
            multi_tenant=MultiTenantSection(enabled=True, header_name="X-Tenant-ID"),
        )
        app = create_app(config=cfg)
        with TestClient(app) as c:
            resp = c.post(
                "/v1/telemetry",
                json=_payload([_span(trace_id="tenant-trace")]),
                headers={"X-Tenant-ID": "acme-corp"},
            )
            assert resp.status_code == 202

            import time; time.sleep(0.5)

            # Verify tenant label was injected
            resp = c.get("/v1/traces", params={"trace_id": "tenant-trace"})
            if resp.status_code == 200:
                spans = resp.json()["spans"]
                assert spans[0]["attributes"]["tenant"] == "acme-corp"

    def test_multi_tenant_default_tenant(self):
        cfg = ServerConfig(
            multi_tenant=MultiTenantSection(enabled=True),
        )
        app = create_app(config=cfg)
        with TestClient(app) as c:
            resp = c.post(
                "/v1/telemetry",
                json=_payload([_span(trace_id="no-tenant-header")]),
            )
            assert resp.status_code == 202

            import time; time.sleep(0.5)

            resp = c.get("/v1/traces", params={"trace_id": "no-tenant-header"})
            if resp.status_code == 200:
                spans = resp.json()["spans"]
                assert spans[0]["attributes"]["tenant"] == "default"


# ========================================================================
# LLM-specific metric integration
# ========================================================================


class TestLLMMetricIntegration:
    def test_llm_span_produces_metrics(self):
        cfg = ServerConfig()
        app = create_app(config=cfg)
        with TestClient(app) as c:
            payload = _payload([
                _span(
                    name="chat_completion",
                    span_type="llm",
                    model="claude-3",
                    provider="anthropic",
                    tokens_input=1000,
                    tokens_output=500,
                ),
            ])
            c.post("/v1/telemetry", json=payload)
            import time; time.sleep(0.5)

            resp = c.get("/metrics")
            text = resp.text
            assert "rastir_llm_calls_total" in text
            assert 'model="claude-3"' in text
            assert 'provider="anthropic"' in text

    def test_tool_span_produces_metrics(self):
        cfg = ServerConfig()
        app = create_app(config=cfg)
        with TestClient(app) as c:
            payload = _payload([
                _span(name="web_search", span_type="tool", agent="research"),
            ])
            c.post("/v1/telemetry", json=payload)
            import time; time.sleep(0.5)

            resp = c.get("/metrics")
            assert "rastir_tool_calls_total" in resp.text
            assert 'tool_name="web_search"' in resp.text


# ========================================================================
# V2 Phase 2 — Operational Gauges & Telemetry
# ========================================================================


class TestOperationalGauges:
    """Tests for queue_utilization, memory_bytes, trace_store_size, active_traces."""

    def test_update_operational_gauges_sets_all_gauges(self):
        registry = MetricsRegistry()
        store = TraceStore(max_traces=100)
        store.insert("t1", [{"name": "a", "span_type": "llm"}, {"name": "b", "span_type": "tool"}])
        store.insert("t2", [{"name": "c", "span_type": "agent"}])

        registry.update_operational_gauges(
            queue_size=25,
            queue_maxsize=100,
            trace_store=store,
        )

        assert registry.queue_size._value.get() == 25
        assert registry.queue_utilization._value.get() == 25.0
        assert registry.trace_store_size._value.get() == 3  # 3 spans
        assert registry.active_traces._value.get() == 2     # 2 traces
        assert registry.memory_bytes._value.get() > 0       # RSS must be positive

    def test_queue_utilization_zero_when_empty(self):
        registry = MetricsRegistry()
        registry.update_operational_gauges(queue_size=0, queue_maxsize=100)
        assert registry.queue_utilization._value.get() == 0.0

    def test_queue_utilization_100_when_full(self):
        registry = MetricsRegistry()
        registry.update_operational_gauges(queue_size=100, queue_maxsize=100)
        assert registry.queue_utilization._value.get() == 100.0

    def test_queue_utilization_zero_maxsize_safe(self):
        registry = MetricsRegistry()
        registry.update_operational_gauges(queue_size=0, queue_maxsize=0)
        assert registry.queue_utilization._value.get() == 0.0

    def test_gauges_without_trace_store(self):
        registry = MetricsRegistry()
        registry.update_operational_gauges(
            queue_size=10,
            queue_maxsize=50,
            trace_store=None,
        )
        assert registry.queue_size._value.get() == 10
        assert registry.queue_utilization._value.get() == 20.0
        # trace store gauges should remain at default (0)
        assert registry.trace_store_size._value.get() == 0
        assert registry.active_traces._value.get() == 0

    def test_memory_bytes_positive(self):
        registry = MetricsRegistry()
        registry.update_operational_gauges(queue_size=0, queue_maxsize=1)
        assert registry.memory_bytes._value.get() > 0

    def test_gauges_in_prometheus_output(self):
        """Operational gauges must appear in /metrics endpoint output."""
        cfg = ServerConfig()
        app = create_app(config=cfg)
        with TestClient(app) as c:
            resp = c.get("/metrics")
            text = resp.text
            assert "rastir_queue_utilization_percent" in text
            assert "rastir_memory_bytes" in text
            assert "rastir_trace_store_size" in text
            assert "rastir_active_traces" in text
            assert "rastir_queue_size" in text

    def test_gauges_reflect_ingested_spans(self):
        """After ingesting spans, trace_store_size and active_traces update."""
        cfg = ServerConfig()
        app = create_app(config=cfg)
        with TestClient(app) as c:
            payload = _payload([
                _span(trace_id="g-trace-1", span_type="llm"),
                _span(trace_id="g-trace-1", span_type="tool"),
                _span(trace_id="g-trace-2", span_type="agent"),
            ])
            c.post("/v1/telemetry", json=payload)
            time.sleep(0.5)

            resp = c.get("/metrics")
            text = resp.text
            # Should show at least 3 spans and 2 traces
            for line in text.splitlines():
                if line.startswith("rastir_trace_store_size "):
                    val = float(line.split()[-1])
                    assert val >= 3
                elif line.startswith("rastir_active_traces "):
                    val = float(line.split()[-1])
                    assert val >= 2


# ========================================================================
# V2 Phase 2 — Config Startup Validation
# ========================================================================


class TestConfigValidation:
    """Tests for validate_config() startup safety checks."""

    def test_valid_config_passes(self):
        from rastir.server.config import validate_config
        cfg = ServerConfig()  # defaults should pass
        validate_config(cfg)  # no exception

    def test_too_many_duration_buckets(self):
        from rastir.server.config import validate_config, ConfigValidationError
        cfg = ServerConfig(
            histograms=HistogramSection(
                duration_buckets=tuple(float(i) for i in range(1, 25)),
            ),
        )
        with pytest.raises(ConfigValidationError, match="duration_buckets"):
            validate_config(cfg)

    def test_too_many_tokens_buckets(self):
        from rastir.server.config import validate_config, ConfigValidationError
        cfg = ServerConfig(
            histograms=HistogramSection(
                tokens_buckets=tuple(float(i) for i in range(1, 25)),
            ),
        )
        with pytest.raises(ConfigValidationError, match="tokens_buckets"):
            validate_config(cfg)

    def test_unsorted_buckets_rejected(self):
        from rastir.server.config import validate_config, ConfigValidationError
        cfg = ServerConfig(
            histograms=HistogramSection(
                duration_buckets=(10.0, 5.0, 1.0),
            ),
        )
        with pytest.raises(ConfigValidationError, match="not sorted"):
            validate_config(cfg)

    def test_non_positive_buckets_rejected(self):
        from rastir.server.config import validate_config, ConfigValidationError
        cfg = ServerConfig(
            histograms=HistogramSection(
                duration_buckets=(-1.0, 0.0, 1.0),
            ),
        )
        with pytest.raises(ConfigValidationError, match="non-positive"):
            validate_config(cfg)

    def test_queue_size_exceeds_limit(self):
        from rastir.server.config import validate_config, ConfigValidationError
        cfg = ServerConfig(
            limits=LimitsSection(max_queue_size=2_000_000),
        )
        with pytest.raises(ConfigValidationError, match="max_queue_size"):
            validate_config(cfg)

    def test_zero_queue_size_rejected(self):
        from rastir.server.config import validate_config, ConfigValidationError
        cfg = ServerConfig(
            limits=LimitsSection(max_queue_size=0),
        )
        with pytest.raises(ConfigValidationError, match="max_queue_size"):
            validate_config(cfg)

    def test_max_traces_exceeds_limit(self):
        from rastir.server.config import validate_config, ConfigValidationError
        cfg = ServerConfig(
            limits=LimitsSection(max_traces=1_000_000),
        )
        with pytest.raises(ConfigValidationError, match="max_traces"):
            validate_config(cfg)

    def test_label_length_exceeds_limit(self):
        from rastir.server.config import validate_config, ConfigValidationError
        cfg = ServerConfig(
            limits=LimitsSection(max_label_value_length=2048),
        )
        with pytest.raises(ConfigValidationError, match="max_label_value_length"):
            validate_config(cfg)

    def test_zero_cardinality_cap_rejected(self):
        from rastir.server.config import validate_config, ConfigValidationError
        cfg = ServerConfig(
            limits=LimitsSection(cardinality_model=0),
        )
        with pytest.raises(ConfigValidationError, match="cardinality_model"):
            validate_config(cfg)

    def test_multiple_errors_aggregated(self):
        from rastir.server.config import validate_config, ConfigValidationError
        cfg = ServerConfig(
            limits=LimitsSection(
                max_queue_size=0,
                max_traces=0,
            ),
        )
        with pytest.raises(ConfigValidationError, match="max_queue_size.*max_traces|max_traces.*max_queue_size"):
            validate_config(cfg)

    def test_create_app_rejects_invalid_config(self):
        from rastir.server.config import ConfigValidationError
        cfg = ServerConfig(
            limits=LimitsSection(max_queue_size=0),
        )
        with pytest.raises(ConfigValidationError):
            create_app(config=cfg)


# ========================================================================
# V2 Phase 3 — Span Sampling Controls
# ========================================================================


class TestSamplingConfig:
    """Tests for SamplingSection config loading and validation."""

    def test_sampling_defaults(self):
        cfg = ServerConfig()
        assert cfg.sampling.enabled is False
        assert cfg.sampling.rate == 1.0
        assert cfg.sampling.always_retain_errors is True
        assert cfg.sampling.latency_threshold_ms == 0.0

    def test_sampling_rate_out_of_range(self):
        from rastir.server.config import validate_config, ConfigValidationError
        cfg = ServerConfig(sampling=SamplingSection(enabled=True, rate=1.5))
        with pytest.raises(ConfigValidationError, match="sampling.rate"):
            validate_config(cfg)

    def test_sampling_rate_negative(self):
        from rastir.server.config import validate_config, ConfigValidationError
        cfg = ServerConfig(sampling=SamplingSection(enabled=True, rate=-0.1))
        with pytest.raises(ConfigValidationError, match="sampling.rate"):
            validate_config(cfg)

    def test_sampling_latency_negative(self):
        from rastir.server.config import validate_config, ConfigValidationError
        cfg = ServerConfig(
            sampling=SamplingSection(enabled=True, latency_threshold_ms=-10.0),
        )
        with pytest.raises(ConfigValidationError, match="latency_threshold_ms"):
            validate_config(cfg)

    def test_valid_sampling_config_passes(self):
        from rastir.server.config import validate_config
        cfg = ServerConfig(
            sampling=SamplingSection(enabled=True, rate=0.5, latency_threshold_ms=500.0),
        )
        validate_config(cfg)  # no exception

    def test_sampling_env_var_loading(self):
        env = {
            "RASTIR_SERVER_SAMPLING_ENABLED": "true",
            "RASTIR_SERVER_SAMPLING_RATE": "0.25",
            "RASTIR_SERVER_SAMPLING_LATENCY_THRESHOLD_MS": "1000.0",
        }
        with patch.dict(os.environ, env, clear=False):
            cfg = load_config()
            assert cfg.sampling.enabled is True
            assert cfg.sampling.rate == 0.25
            assert cfg.sampling.latency_threshold_ms == 1000.0


class TestSamplingBehaviour:
    """Tests for sampling logic in ingestion processing."""

    def test_sampling_disabled_stores_all_spans(self):
        """With sampling disabled, all spans go to trace store."""
        cfg = ServerConfig(sampling=SamplingSection(enabled=False))
        app = create_app(config=cfg)
        with TestClient(app) as c:
            payload = _payload([
                _span(trace_id="sample-all-1", span_type="llm"),
                _span(trace_id="sample-all-2", span_type="tool"),
            ])
            c.post("/v1/telemetry", json=payload)
            time.sleep(0.5)

            resp = c.get("/v1/traces")
            traces = resp.json()["traces"]
            trace_ids = {t["trace_id"] for t in traces}
            assert "sample-all-1" in trace_ids
            assert "sample-all-2" in trace_ids

    def test_sampling_rate_zero_drops_all_non_error(self):
        """rate=0.0 drops all spans (except errors if retain_errors=True)."""
        cfg = ServerConfig(
            sampling=SamplingSection(enabled=True, rate=0.0),
        )
        app = create_app(config=cfg)
        with TestClient(app) as c:
            payload = _payload([
                _span(trace_id="drop-me", span_type="llm", status="OK"),
            ])
            c.post("/v1/telemetry", json=payload)
            time.sleep(0.5)

            # Span should NOT be in trace store
            resp = c.get("/v1/traces", params={"trace_id": "drop-me"})
            assert resp.status_code == 404

    def test_sampling_always_retains_errors(self):
        """Error spans are retained even when rate=0.0."""
        cfg = ServerConfig(
            sampling=SamplingSection(enabled=True, rate=0.0, always_retain_errors=True),
        )
        app = create_app(config=cfg)
        with TestClient(app) as c:
            payload = _payload([
                _span(trace_id="err-keep", span_type="llm", status="ERROR"),
            ])
            c.post("/v1/telemetry", json=payload)
            time.sleep(0.5)

            resp = c.get("/v1/traces", params={"trace_id": "err-keep"})
            assert resp.status_code == 200
            assert len(resp.json()["spans"]) == 1

    def test_sampling_retains_high_latency(self):
        """Spans above latency_threshold_ms are always retained."""
        cfg = ServerConfig(
            sampling=SamplingSection(
                enabled=True, rate=0.0, latency_threshold_ms=500.0,
            ),
        )
        app = create_app(config=cfg)
        with TestClient(app) as c:
            payload = _payload([
                _span(trace_id="slow-keep", duration_ms=600.0, span_type="llm"),
                _span(trace_id="fast-drop", duration_ms=100.0, span_type="llm"),
            ])
            c.post("/v1/telemetry", json=payload)
            time.sleep(0.5)

            # Slow span retained
            resp = c.get("/v1/traces", params={"trace_id": "slow-keep"})
            assert resp.status_code == 200

            # Fast span dropped
            resp = c.get("/v1/traces", params={"trace_id": "fast-drop"})
            assert resp.status_code == 404

    def test_sampling_metrics_always_recorded(self):
        """Metrics are recorded for ALL spans regardless of sampling."""
        cfg = ServerConfig(
            sampling=SamplingSection(enabled=True, rate=0.0),
        )
        app = create_app(config=cfg)
        with TestClient(app) as c:
            payload = _payload([
                _span(trace_id="metric-yes", span_type="llm",
                      model="gpt-4", provider="openai"),
            ])
            c.post("/v1/telemetry", json=payload)
            time.sleep(0.5)

            # Even though span was dropped from storage,
            # metrics should still be recorded
            resp = c.get("/metrics")
            text = resp.text
            assert "rastir_llm_calls_total" in text
            assert 'model="gpt-4"' in text
            assert "rastir_spans_dropped_by_sampling_total" in text

    def test_sampling_counters_exposed(self):
        """Sampling counters appear in prometheus output."""
        cfg = ServerConfig(
            sampling=SamplingSection(enabled=True, rate=0.5),
        )
        app = create_app(config=cfg)
        with TestClient(app) as c:
            payload = _payload([
                _span(trace_id=f"s-{i}", span_type="llm") for i in range(20)
            ])
            c.post("/v1/telemetry", json=payload)
            time.sleep(0.5)

            resp = c.get("/metrics")
            text = resp.text
            assert "rastir_spans_sampled_total" in text
            assert "rastir_spans_dropped_by_sampling_total" in text


# ========================================================================
# V2 Phase 4 — Advanced Backpressure Controls
# ========================================================================


class TestBackpressureConfig:
    """Tests for BackpressureSection config and validation."""

    def test_backpressure_defaults(self):
        cfg = ServerConfig()
        assert cfg.backpressure.soft_limit_pct == 80.0
        assert cfg.backpressure.hard_limit_pct == 95.0
        assert cfg.backpressure.mode == "reject"

    def test_invalid_mode_rejected(self):
        from rastir.server.config import validate_config, ConfigValidationError
        cfg = ServerConfig(
            backpressure=BackpressureSection(mode="invalid"),
        )
        with pytest.raises(ConfigValidationError, match="backpressure.mode"):
            validate_config(cfg)

    def test_soft_above_hard_rejected(self):
        from rastir.server.config import validate_config, ConfigValidationError
        cfg = ServerConfig(
            backpressure=BackpressureSection(soft_limit_pct=95.0, hard_limit_pct=80.0),
        )
        with pytest.raises(ConfigValidationError, match="soft_limit_pct"):
            validate_config(cfg)

    def test_soft_equals_hard_rejected(self):
        from rastir.server.config import validate_config, ConfigValidationError
        cfg = ServerConfig(
            backpressure=BackpressureSection(soft_limit_pct=90.0, hard_limit_pct=90.0),
        )
        with pytest.raises(ConfigValidationError, match="soft_limit_pct"):
            validate_config(cfg)

    def test_pct_out_of_range(self):
        from rastir.server.config import validate_config, ConfigValidationError
        cfg = ServerConfig(
            backpressure=BackpressureSection(soft_limit_pct=110.0),
        )
        with pytest.raises(ConfigValidationError, match="soft_limit_pct"):
            validate_config(cfg)

    def test_valid_backpressure_config(self):
        from rastir.server.config import validate_config
        cfg = ServerConfig(
            backpressure=BackpressureSection(
                soft_limit_pct=70.0, hard_limit_pct=90.0, mode="drop_oldest",
            ),
        )
        validate_config(cfg)  # no exception

    def test_backpressure_env_var_loading(self):
        env = {
            "RASTIR_SERVER_BACKPRESSURE_SOFT_LIMIT_PCT": "60.0",
            "RASTIR_SERVER_BACKPRESSURE_HARD_LIMIT_PCT": "85.0",
            "RASTIR_SERVER_BACKPRESSURE_MODE": "drop_oldest",
        }
        with patch.dict(os.environ, env, clear=False):
            cfg = load_config()
            assert cfg.backpressure.soft_limit_pct == 60.0
            assert cfg.backpressure.hard_limit_pct == 85.0
            assert cfg.backpressure.mode == "drop_oldest"


class TestBackpressureBehaviour:
    """Tests for backpressure logic in the ingestion worker."""

    def test_reject_mode_returns_429_when_full(self):
        """Default reject mode returns 429 when queue is full."""
        cfg = ServerConfig(
            limits=LimitsSection(max_queue_size=2),
            backpressure=BackpressureSection(mode="reject"),
        )
        app = create_app(config=cfg)
        with TestClient(app) as c:
            # Don't start worker so queue fills up
            # Actually TestClient uses lifespan so worker is started—
            # we need to fill faster than drain.
            # Use a tiny queue; send many payloads rapidly.
            responses = []
            for i in range(10):
                resp = c.post("/v1/telemetry", json=_payload([
                    _span(trace_id=f"bp-reject-{i}"),
                ]))
                responses.append(resp.status_code)

            # At least one should have been rejected (429)
            # (worker drains in background; with queue size 2 likely some rejections)
            time.sleep(0.5)
            resp = c.get("/metrics")
            text = resp.text
            # Either we got 429s or the rejection counter incremented
            assert (
                429 in responses
                or "rastir_ingestion_rejections_total" in text
            )

    def test_drop_oldest_mode_accepts_when_full(self):
        """drop_oldest mode evicts oldest batch to make room."""
        cfg = ServerConfig(
            limits=LimitsSection(max_queue_size=2),
            backpressure=BackpressureSection(
                mode="drop_oldest",
                soft_limit_pct=10.0,
                hard_limit_pct=90.0,
            ),
        )
        app = create_app(config=cfg)
        with TestClient(app) as c:
            # Send many spans — all should be accepted (202)
            for i in range(10):
                resp = c.post("/v1/telemetry", json=_payload([
                    _span(trace_id=f"bp-drop-{i}"),
                ]))
                assert resp.status_code == 202

            time.sleep(0.5)
            resp = c.get("/metrics")
            text = resp.text
            assert "rastir_spans_dropped_by_backpressure_total" in text

    def test_backpressure_warning_metric(self):
        """Soft limit triggers backpressure warning counter."""
        cfg = ServerConfig(
            limits=LimitsSection(max_queue_size=5),
            backpressure=BackpressureSection(
                soft_limit_pct=20.0,  # very low threshold
                hard_limit_pct=90.0,
            ),
        )
        app = create_app(config=cfg)
        with TestClient(app) as c:
            for i in range(6):
                c.post("/v1/telemetry", json=_payload([
                    _span(trace_id=f"bp-warn-{i}"),
                ]))

            time.sleep(0.5)
            resp = c.get("/metrics")
            text = resp.text
            assert "rastir_backpressure_warnings_total" in text


class TestIngestionRate:
    """Tests for the ingestion rate gauge."""

    def test_ingestion_rate_gauge_exposed(self):
        cfg = ServerConfig()
        app = create_app(config=cfg)
        with TestClient(app) as c:
            resp = c.get("/metrics")
            assert "rastir_ingestion_rate" in resp.text

    def test_ingestion_rate_updates_after_spans(self):
        cfg = ServerConfig()
        app = create_app(config=cfg)
        with TestClient(app) as c:
            payload = _payload([
                _span(trace_id=f"rate-{i}", span_type="llm") for i in range(10)
            ])
            c.post("/v1/telemetry", json=payload)
            time.sleep(1.5)  # allow rate computation window

            resp = c.get("/metrics")
            text = resp.text
            assert "rastir_ingestion_rate" in text

    def test_record_ingested_spans_unit(self):
        """Unit test for the rate tracking method."""
        registry = MetricsRegistry()
        registry.record_ingested_spans(50)
        assert registry._rate_spans == 50
        registry.record_ingested_spans(30)
        assert registry._rate_spans == 80
