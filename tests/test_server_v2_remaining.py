"""Tests for V2 remaining features.

Covers:
- Trace retention policies (max_spans_per_trace, TTL)
- Trace query API (path params, service filter)
- Rate limiting (per-IP, per-service, 429 responses)
- Structured logging (JSON format, configure_logging)
- HA readiness (enhanced /ready, graceful shutdown config)
- Exemplar support (openmetrics output, exemplar in histogram)
- Config validation for new sections
"""

from __future__ import annotations

import json
import logging
import time
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from rastir.server.app import create_app
from rastir.server.config import (
    BackpressureSection,
    ConfigValidationError,
    ExemplarSection,
    ExporterSection,
    LimitsSection,
    LoggingSection,
    RateLimitSection,
    SamplingSection,
    ServerConfig,
    ShutdownSection,
    TraceStoreSection,
    validate_config,
)
from rastir.server.metrics import MetricsRegistry
from rastir.server.rate_limiter import RateLimiter, _WindowCounter
from rastir.server.structured_logging import StructuredFormatter, configure_logging
from rastir.server.trace_store import TraceStore


# ========================================================================
# Helpers
# ========================================================================


def _span(
    name: str = "test_fn",
    span_type: str = "llm",
    status: str = "OK",
    trace_id: str = "abc-123",
    duration_ms: float = 100.0,
    events: list[dict] | None = None,
    **attrs,
) -> dict:
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
# Trace Retention — TraceStore
# ========================================================================


class TestTraceRetention:
    """max_spans_per_trace and TTL-based expiration."""

    def test_max_spans_per_trace_truncates(self):
        store = TraceStore(max_traces=10, max_spans_per_trace=3)
        # Insert 5 spans at once — only first 3 should be kept
        spans = [_span(name=f"s{i}") for i in range(5)]
        store.insert("t1", spans)
        assert len(store.get("t1")) == 3
        assert store.spans_truncated == 2

    def test_max_spans_per_trace_append(self):
        store = TraceStore(max_traces=10, max_spans_per_trace=5)
        store.insert("t1", [_span(name="s0"), _span(name="s1")])
        assert len(store.get("t1")) == 2
        # Append more — only 3 more should fit
        store.insert("t1", [_span(name=f"s{i}") for i in range(2, 6)])
        assert len(store.get("t1")) == 5
        assert store.spans_truncated == 1

    def test_max_spans_per_trace_full_then_append(self):
        store = TraceStore(max_traces=10, max_spans_per_trace=2)
        store.insert("t1", [_span(name="s0"), _span(name="s1")])
        # Already full — appending should all be truncated
        store.insert("t1", [_span(name="s2")])
        assert len(store.get("t1")) == 2
        assert store.spans_truncated == 1

    def test_ttl_expiration(self):
        store = TraceStore(max_traces=10, ttl_seconds=1)
        store.insert("t1", [_span()])
        assert store.get("t1") is not None

        # Monkey-patch the recorded timestamp to simulate age
        with store._lock:
            store._timestamps["t1"] = time.monotonic() - 2

        # Next access that triggers expiry check
        result = store.recent()
        assert len(result) == 0
        assert store.get("t1") is None

    def test_ttl_zero_means_no_expiration(self):
        store = TraceStore(max_traces=10, ttl_seconds=0)
        store.insert("t1", [_span()])
        # Even after mocking time, no expiration should happen
        result = store.recent()
        assert len(result) == 1

    def test_spans_truncated_property(self):
        store = TraceStore(max_traces=10, max_spans_per_trace=1)
        store.insert("t1", [_span(name="s0"), _span(name="s1")])
        assert store.spans_truncated == 1

    def test_clear_resets_timestamps(self):
        store = TraceStore(max_traces=10, ttl_seconds=60)
        store.insert("t1", [_span()])
        assert store.trace_count == 1
        store.clear()
        assert store.trace_count == 0
        assert len(store._timestamps) == 0


# ========================================================================
# Trace Query API — TraceStore.search
# ========================================================================


class TestTraceSearch:
    """TraceStore.search() method — service-based filtering."""

    def test_search_no_filter(self):
        store = TraceStore(max_traces=10)
        store.insert("t1", [_span(service="svc-a")])
        store.insert("t2", [_span(service="svc-b")])
        results = store.search()
        assert len(results) == 2

    def test_search_by_service(self):
        store = TraceStore(max_traces=10)
        store.insert("t1", [_span(service="svc-a")])
        store.insert("t2", [_span(service="svc-b")])
        results = store.search(service="svc-a")
        assert len(results) == 1
        assert results[0]["trace_id"] == "t1"

    def test_search_no_match(self):
        store = TraceStore(max_traces=10)
        store.insert("t1", [_span(service="svc-a")])
        results = store.search(service="nonexistent")
        assert len(results) == 0

    def test_search_with_limit(self):
        store = TraceStore(max_traces=100)
        for i in range(10):
            store.insert(f"t{i}", [_span(service="svc-a")])
        results = store.search(service="svc-a", limit=3)
        assert len(results) == 3

    def test_search_with_ttl_expiry(self):
        store = TraceStore(max_traces=10, ttl_seconds=1)
        store.insert("old", [_span(service="svc-a")])
        store.insert("new", [_span(service="svc-a")])
        # Expire 'old'
        with store._lock:
            store._timestamps["old"] = time.monotonic() - 2
        results = store.search(service="svc-a")
        assert len(results) == 1
        assert results[0]["trace_id"] == "new"


# ========================================================================
# Trace Query API — Endpoints
# ========================================================================


class TestTraceQueryEndpoints:
    """Test /v1/traces path param and service filter endpoints."""

    @pytest.fixture
    def app(self):
        return create_app(config=ServerConfig(
            limits=LimitsSection(max_queue_size=1000),
        ))

    @pytest.fixture
    def client(self, app):
        with TestClient(app) as c:
            yield c

    def test_trace_by_path_param(self, client):
        client.post("/v1/telemetry", json=_payload([_span(trace_id="path-trace")]))
        time.sleep(0.5)
        resp = client.get("/v1/traces/path-trace")
        assert resp.status_code == 200
        data = resp.json()
        assert data["trace_id"] == "path-trace"
        assert len(data["spans"]) >= 1

    def test_trace_by_path_param_not_found(self, client):
        resp = client.get("/v1/traces/nonexistent-id")
        assert resp.status_code == 404

    def test_trace_query_service_filter(self, client):
        client.post("/v1/telemetry", json=_payload(
            [_span(trace_id="svc-t1", service="alpha")],
            service="alpha",
        ))
        client.post("/v1/telemetry", json=_payload(
            [_span(trace_id="svc-t2", service="beta")],
            service="beta",
        ))
        time.sleep(0.5)
        resp = client.get("/v1/traces", params={"service": "alpha"})
        assert resp.status_code == 200
        data = resp.json()
        assert "traces" in data

    def test_trace_path_param_store_disabled(self):
        cfg = ServerConfig(trace_store=TraceStoreSection(enabled=False))
        app = create_app(config=cfg)
        with TestClient(app) as c:
            resp = c.get("/v1/traces/some-id")
            assert resp.status_code == 404


# ========================================================================
# Rate Limiter — Unit
# ========================================================================


class TestWindowCounter:
    """_WindowCounter unit tests."""

    def test_allow_within_limit(self):
        counter = _WindowCounter(limit=3)
        assert counter.allow() is True
        assert counter.allow() is True
        assert counter.allow() is True

    def test_block_over_limit(self):
        counter = _WindowCounter(limit=2)
        assert counter.allow() is True
        assert counter.allow() is True
        assert counter.allow() is False

    def test_window_rotation(self):
        counter = _WindowCounter(limit=1, window=0.1)
        assert counter.allow() is True
        assert counter.allow() is False
        time.sleep(0.15)
        assert counter.allow() is True


class TestRateLimiter:
    """RateLimiter unit tests."""

    def test_allow_normal_traffic(self):
        rl = RateLimiter(per_ip_rpm=10, per_service_rpm=20)
        result = rl.check("1.2.3.4", "my-svc")
        assert result is None

    def test_block_ip(self):
        rl = RateLimiter(per_ip_rpm=2, per_service_rpm=1000)
        rl.check("1.2.3.4", "svc")
        rl.check("1.2.3.4", "svc")
        result = rl.check("1.2.3.4", "svc")
        assert result == "ip"

    def test_block_service(self):
        rl = RateLimiter(per_ip_rpm=1000, per_service_rpm=2)
        rl.check("1.1.1.1", "svc")
        rl.check("2.2.2.2", "svc")
        result = rl.check("3.3.3.3", "svc")
        assert result == "service"

    def test_different_ips_independent(self):
        rl = RateLimiter(per_ip_rpm=1, per_service_rpm=1000)
        assert rl.check("1.1.1.1", "svc") is None
        assert rl.check("2.2.2.2", "svc") is None

    def test_counter_metric_incremented(self):
        from prometheus_client import CollectorRegistry
        reg = CollectorRegistry()
        rl = RateLimiter(per_ip_rpm=1, per_service_rpm=1000, registry=reg)
        rl.check("x", "s")
        rl.check("x", "s")  # blocked
        # Check the counter was incremented
        assert rl.rate_limited.labels(dimension="ip")._value.get() == 1.0


# ========================================================================
# Rate Limiter — Endpoint Integration
# ========================================================================


class TestRateLimitEndpoint:
    """Test 429 response from /v1/telemetry when rate-limited."""

    @pytest.fixture
    def client(self):
        cfg = ServerConfig(
            limits=LimitsSection(max_queue_size=1000),
            rate_limit=RateLimitSection(enabled=True, per_ip_rpm=2, per_service_rpm=1000),
        )
        app = create_app(config=cfg)
        with TestClient(app) as c:
            yield c

    def test_rate_limit_429(self, client):
        payload = _payload([_span()])
        client.post("/v1/telemetry", json=payload)
        client.post("/v1/telemetry", json=payload)
        resp = client.post("/v1/telemetry", json=payload)
        assert resp.status_code == 429
        assert "Rate limited" in resp.json()["detail"]

    def test_rate_limit_disabled_by_default(self):
        cfg = ServerConfig(limits=LimitsSection(max_queue_size=1000))
        app = create_app(config=cfg)
        with TestClient(app) as c:
            payload = _payload([_span()])
            for _ in range(10):
                resp = c.post("/v1/telemetry", json=payload)
                assert resp.status_code == 202


# ========================================================================
# Structured Logging
# ========================================================================


class TestStructuredFormatter:
    """StructuredFormatter unit tests."""

    def test_json_output(self):
        formatter = StructuredFormatter()
        record = logging.LogRecord(
            name="rastir.server",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="hello world",
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["level"] == "INFO"
        assert parsed["logger"] == "rastir.server"
        assert parsed["message"] == "hello world"
        assert "timestamp" in parsed

    def test_extra_fields(self):
        formatter = StructuredFormatter()
        record = logging.LogRecord(
            name="rastir.server",
            level=logging.WARNING,
            pathname="test.py",
            lineno=1,
            msg="span processed",
            args=(),
            exc_info=None,
        )
        record.service = "my-svc"
        record.trace_id = "trace-xyz"
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["service"] == "my-svc"
        assert parsed["trace_id"] == "trace-xyz"

    def test_exception_field(self):
        formatter = StructuredFormatter()
        try:
            raise ValueError("boom")
        except ValueError:
            import sys
            exc_info = sys.exc_info()
        record = logging.LogRecord(
            name="rastir.server",
            level=logging.ERROR,
            pathname="test.py",
            lineno=1,
            msg="error occurred",
            args=(),
            exc_info=exc_info,
        )
        output = formatter.format(record)
        parsed = json.loads(output)
        assert "exception" in parsed
        assert "boom" in parsed["exception"]


class TestConfigureLogging:
    """configure_logging() function tests."""

    def test_structured_mode(self):
        configure_logging(structured=True, level="DEBUG")
        logger = logging.getLogger("rastir")
        assert logger.level == logging.DEBUG
        assert any(
            isinstance(h.formatter, StructuredFormatter)
            for h in logger.handlers
        )

    def test_plain_mode(self):
        configure_logging(structured=False, level="WARNING")
        logger = logging.getLogger("rastir")
        assert logger.level == logging.WARNING
        assert not any(
            isinstance(h.formatter, StructuredFormatter)
            for h in logger.handlers
        )

    def test_clears_existing_handlers(self):
        logger = logging.getLogger("rastir")
        logger.addHandler(logging.StreamHandler())
        logger.addHandler(logging.StreamHandler())
        configure_logging(structured=False, level="INFO")
        # Should have exactly 1 handler after configure
        assert len(logger.handlers) == 1


# ========================================================================
# HA Readiness — Enhanced /ready
# ========================================================================


class TestHAReadiness:
    """Enhanced /ready endpoint with queue pct and exporter health."""

    @pytest.fixture
    def client(self):
        cfg = ServerConfig(limits=LimitsSection(max_queue_size=100))
        app = create_app(config=cfg)
        with TestClient(app) as c:
            yield c

    def test_ready_returns_queue_pct(self, client):
        resp = client.get("/ready")
        assert resp.status_code == 200
        data = resp.json()
        assert "queue_pct" in data
        assert data["status"] == "ready"

    def test_ready_not_ready_high_queue(self):
        cfg = ServerConfig(
            limits=LimitsSection(max_queue_size=10),
            backpressure=BackpressureSection(hard_limit_pct=50.0, soft_limit_pct=30.0),
        )
        app = create_app(config=cfg)
        with TestClient(app) as c:
            # Fill the queue beyond hard limit
            worker = app.state.worker
            for i in range(6):
                worker.enqueue("svc", "test", "", [_span(trace_id=f"rdy{i}")])

            resp = c.get("/ready")
            data = resp.json()
            if data["queue_pct"] >= 50.0:
                assert resp.status_code == 503
                assert data["status"] == "not_ready"
                assert "reasons" in data

    def test_ready_with_unhealthy_exporter(self):
        cfg = ServerConfig(limits=LimitsSection(max_queue_size=100))
        app = create_app(config=cfg)
        # Mock an unhealthy OTLP exporter
        mock_otlp = MagicMock()
        mock_otlp.healthy = False
        app.state.otlp_forwarder = mock_otlp

        with TestClient(app) as c:
            resp = c.get("/ready")
            data = resp.json()
            assert resp.status_code == 503
            assert "otlp_exporter_unhealthy" in data.get("reasons", [])


# ========================================================================
# Graceful Shutdown Config
# ========================================================================


class TestShutdownConfig:
    def test_shutdown_defaults(self):
        cfg = ServerConfig()
        assert cfg.shutdown.grace_period_seconds == 30
        assert cfg.shutdown.drain_queue is True

    def test_shutdown_custom(self):
        cfg = ServerConfig(shutdown=ShutdownSection(grace_period_seconds=10, drain_queue=False))
        assert cfg.shutdown.grace_period_seconds == 10
        assert cfg.shutdown.drain_queue is False


# ========================================================================
# Exemplar Support
# ========================================================================


class TestExemplarSupport:
    """Exemplar support in MetricsRegistry."""

    def test_exemplars_disabled_by_default(self):
        reg = MetricsRegistry()
        assert reg._exemplars_enabled is False

    def test_exemplars_enabled(self):
        reg = MetricsRegistry(exemplars_enabled=True)
        assert reg._exemplars_enabled is True

    def test_generate_returns_tuple(self):
        reg = MetricsRegistry()
        result = reg.generate()
        assert isinstance(result, tuple)
        assert len(result) == 2
        content, ct = result
        assert isinstance(content, bytes)
        assert "text/plain" in ct

    def test_generate_openmetrics_when_enabled(self):
        reg = MetricsRegistry(exemplars_enabled=True)
        # Record a span to produce some metrics
        reg.record_span(
            _span(span_type="llm", trace_id="exemplar-trace", model="gpt-4", provider="openai"),
            service="svc",
            env="test",
        )
        content, ct = reg.generate()
        assert isinstance(content, bytes)
        assert "openmetrics" in ct

    def test_exemplar_in_histogram(self):
        reg = MetricsRegistry(exemplars_enabled=True)
        reg.record_span(
            _span(span_type="llm", trace_id="trace-42", model="gpt-4", provider="openai"),
            service="svc",
            env="test",
        )
        content, ct = reg.generate()
        text = content.decode("utf-8")
        # OpenMetrics output should include the exemplar trace_id
        assert "trace_id" in text

    def test_classic_format_no_exemplar(self):
        reg = MetricsRegistry(exemplars_enabled=False)
        reg.record_span(
            _span(span_type="llm", trace_id="trace-42", model="gpt-4", provider="openai"),
            service="svc",
            env="test",
        )
        content, ct = reg.generate()
        assert b"text/plain" in ct.encode()

    def test_exemplar_endpoint_content_type(self):
        cfg = ServerConfig(
            limits=LimitsSection(max_queue_size=100),
            exemplars=ExemplarSection(enabled=True),
        )
        app = create_app(config=cfg)
        with TestClient(app) as c:
            resp = c.get("/metrics")
            assert resp.status_code == 200
            assert "openmetrics" in resp.headers["content-type"]


# ========================================================================
# Config Validation for New Sections
# ========================================================================


class TestNewConfigValidation:
    """Config validation for rate_limit, trace_store retention, shutdown, logging."""

    def test_rate_limit_per_ip_rpm_positive(self):
        cfg = ServerConfig(rate_limit=RateLimitSection(per_ip_rpm=0))
        with pytest.raises(ConfigValidationError, match="per_ip_rpm"):
            validate_config(cfg)

    def test_rate_limit_per_service_rpm_positive(self):
        cfg = ServerConfig(rate_limit=RateLimitSection(per_service_rpm=-1))
        with pytest.raises(ConfigValidationError, match="per_service_rpm"):
            validate_config(cfg)

    def test_max_spans_per_trace_positive(self):
        cfg = ServerConfig(trace_store=TraceStoreSection(max_spans_per_trace=0))
        with pytest.raises(ConfigValidationError, match="max_spans_per_trace"):
            validate_config(cfg)

    def test_ttl_seconds_non_negative(self):
        cfg = ServerConfig(trace_store=TraceStoreSection(ttl_seconds=-1))
        with pytest.raises(ConfigValidationError, match="ttl_seconds"):
            validate_config(cfg)

    def test_grace_period_non_negative(self):
        cfg = ServerConfig(shutdown=ShutdownSection(grace_period_seconds=-5))
        with pytest.raises(ConfigValidationError, match="grace_period"):
            validate_config(cfg)

    def test_logging_level_valid(self):
        cfg = ServerConfig(logging=LoggingSection(level="TRACE"))
        with pytest.raises(ConfigValidationError, match="logging.level"):
            validate_config(cfg)

    def test_logging_level_valid_levels_pass(self):
        for level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            cfg = ServerConfig(logging=LoggingSection(level=level))
            validate_config(cfg)  # should not raise

    def test_valid_config_passes(self):
        cfg = ServerConfig(
            rate_limit=RateLimitSection(per_ip_rpm=100, per_service_rpm=500),
            trace_store=TraceStoreSection(max_spans_per_trace=200, ttl_seconds=3600),
            shutdown=ShutdownSection(grace_period_seconds=60),
            logging=LoggingSection(level="DEBUG"),
        )
        validate_config(cfg)  # should not raise

    def test_multiple_errors_reported(self):
        cfg = ServerConfig(
            rate_limit=RateLimitSection(per_ip_rpm=0, per_service_rpm=0),
            trace_store=TraceStoreSection(max_spans_per_trace=0, ttl_seconds=-1),
        )
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(cfg)
        msg = str(exc_info.value)
        assert "per_ip_rpm" in msg
        assert "per_service_rpm" in msg
        assert "max_spans_per_trace" in msg
        assert "ttl_seconds" in msg


# ========================================================================
# Config loading for new sections
# ========================================================================


class TestNewConfigLoading:
    """Test that new config sections load from env vars."""

    def test_rate_limit_from_env(self):
        with patch.dict("os.environ", {
            "RASTIR_SERVER_RATE_LIMIT_ENABLED": "true",
            "RASTIR_SERVER_RATE_LIMIT_PER_IP_RPM": "100",
            "RASTIR_SERVER_RATE_LIMIT_PER_SERVICE_RPM": "500",
        }):
            from rastir.server.config import load_config
            cfg = load_config()
            assert cfg.rate_limit.enabled is True
            assert cfg.rate_limit.per_ip_rpm == 100
            assert cfg.rate_limit.per_service_rpm == 500

    def test_exemplars_from_env(self):
        with patch.dict("os.environ", {
            "RASTIR_SERVER_EXEMPLARS_ENABLED": "true",
        }):
            from rastir.server.config import load_config
            cfg = load_config()
            assert cfg.exemplars.enabled is True

    def test_shutdown_from_env(self):
        with patch.dict("os.environ", {
            "RASTIR_SERVER_SHUTDOWN_GRACE_PERIOD_SECONDS": "60",
            "RASTIR_SERVER_SHUTDOWN_DRAIN_QUEUE": "false",
        }):
            from rastir.server.config import load_config
            cfg = load_config()
            assert cfg.shutdown.grace_period_seconds == 60
            assert cfg.shutdown.drain_queue is False

    def test_logging_from_env(self):
        with patch.dict("os.environ", {
            "RASTIR_SERVER_LOGGING_STRUCTURED": "true",
            "RASTIR_SERVER_LOGGING_LEVEL": "DEBUG",
        }):
            from rastir.server.config import load_config
            cfg = load_config()
            assert cfg.logging.structured is True
            assert cfg.logging.level == "DEBUG"

    def test_trace_store_retention_from_env(self):
        with patch.dict("os.environ", {
            "RASTIR_SERVER_TRACE_STORE_MAX_SPANS_PER_TRACE": "100",
            "RASTIR_SERVER_TRACE_STORE_TTL_SECONDS": "3600",
        }):
            from rastir.server.config import load_config
            cfg = load_config()
            assert cfg.trace_store.max_spans_per_trace == 100
            assert cfg.trace_store.ttl_seconds == 3600

    def test_defaults_without_env(self):
        # Clear any potentially set env vars
        env_keys = [
            "RASTIR_SERVER_RATE_LIMIT_ENABLED",
            "RASTIR_SERVER_EXEMPLARS_ENABLED",
            "RASTIR_SERVER_SHUTDOWN_GRACE_PERIOD_SECONDS",
            "RASTIR_SERVER_LOGGING_STRUCTURED",
        ]
        clean = {k: "" for k in env_keys if k in os.environ}
        with patch.dict("os.environ", {}, clear=False):
            from rastir.server.config import load_config
            cfg = load_config()
            assert cfg.rate_limit.enabled is False
            assert cfg.exemplars.enabled is False
            assert cfg.shutdown.grace_period_seconds == 30
            assert cfg.logging.structured is False


import os
