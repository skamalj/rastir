"""Transport layer tests — TelemetryClient, BackgroundExporter, and wiring.

Uses httpx's MockTransport to intercept HTTP requests without a real server.
"""

from __future__ import annotations

import json
import time
import threading
from unittest.mock import patch

import httpx
import pytest

from rastir.config import ExporterConfig, GlobalConfig, configure, reset_config
from rastir.queue import drain_batch, enqueue_span, reset_queue
from rastir.spans import SpanRecord, SpanStatus, SpanType
from rastir.transport import (
    BackgroundExporter,
    TelemetryClient,
    get_export_stats,
    reset_export_stats,
    start_exporter,
    stop_exporter,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(
    push_url: str = "http://localhost:9000",
    api_key: str | None = None,
    batch_size: int = 100,
    flush_interval: int = 1,
    timeout: int = 2,
    max_retries: int = 3,
    retry_backoff: float = 0.5,
    shutdown_timeout: float = 5.0,
) -> GlobalConfig:
    return GlobalConfig(
        service="test-svc",
        env="test",
        version="0.1.0",
        exporter=ExporterConfig(
            push_url=push_url,
            api_key=api_key,
            batch_size=batch_size,
            flush_interval=flush_interval,
            timeout=timeout,
            max_retries=max_retries,
            retry_backoff=retry_backoff,
            shutdown_timeout=shutdown_timeout,
        ),
    )


def _make_span(name: str = "test_span") -> SpanRecord:
    span = SpanRecord(name=name, span_type=SpanType.TRACE)
    span.finish(SpanStatus.OK)
    return span


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_state():
    """Reset all global state between tests."""
    reset_export_stats()
    reset_queue()
    reset_config()
    yield
    stop_exporter()
    reset_export_stats()
    reset_queue()
    reset_config()


# ========================================================================
# TelemetryClient tests
# ========================================================================


class TestTelemetryClient:
    def test_successful_send(self):
        """Batch should be sent and accepted on 200."""
        requests_received = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_received.append(request)
            return httpx.Response(200, json={"status": "ok"})

        config = _make_config()
        client = TelemetryClient(config)
        # Inject mock transport
        client._client = httpx.Client(
            transport=httpx.MockTransport(handler),
            headers=client._client.headers,
            timeout=client._client.timeout,
        )

        spans = [_make_span("span1"), _make_span("span2")]
        result = client.send_batch(spans)

        assert result is True
        assert len(requests_received) == 1

        # Verify payload structure
        body = json.loads(requests_received[0].content)
        assert body["service"] == "test-svc"
        assert body["env"] == "test"
        assert body["version"] == "0.1.0"
        assert len(body["spans"]) == 2
        assert body["spans"][0]["name"] == "span1"
        assert body["spans"][0]["type"] == "span"
        assert body["spans"][0]["span_type"] == "trace"

        stats = get_export_stats()
        assert stats["export_successes"] == 1
        assert stats["spans_exported"] == 2

        client.close()

    def test_api_key_header(self):
        """API key should be sent as X-API-Key header."""
        received_headers = {}

        def handler(request: httpx.Request) -> httpx.Response:
            received_headers.update(dict(request.headers))
            return httpx.Response(200)

        config = _make_config(api_key="secret-key-123")
        client = TelemetryClient(config)
        client._client = httpx.Client(
            transport=httpx.MockTransport(handler),
            headers=client._client.headers,
            timeout=client._client.timeout,
        )

        client.send_batch([_make_span()])
        assert received_headers.get("x-api-key") == "secret-key-123"
        client.close()

    def test_retries_on_500(self):
        """Should retry on 5xx and succeed if a subsequent attempt works."""
        attempt_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count < 3:
                return httpx.Response(503)
            return httpx.Response(200)

        config = _make_config()
        client = TelemetryClient(config)
        client._client = httpx.Client(
            transport=httpx.MockTransport(handler),
            headers=client._client.headers,
            timeout=client._client.timeout,
        )

        result = client.send_batch([_make_span()])
        assert result is True
        assert attempt_count == 3

        stats = get_export_stats()
        assert stats["export_successes"] == 1
        client.close()

    def test_fails_after_max_retries(self):
        """Should give up after max retries and count as failure."""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        config = _make_config()
        client = TelemetryClient(config)
        client._client = httpx.Client(
            transport=httpx.MockTransport(handler),
            headers=client._client.headers,
            timeout=client._client.timeout,
        )

        result = client.send_batch([_make_span()])
        assert result is False

        stats = get_export_stats()
        assert stats["export_failures"] == 1
        assert stats["spans_dropped"] == 1
        client.close()

    def test_non_retryable_4xx(self):
        """400 should fail immediately without retrying."""
        attempt_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempt_count
            attempt_count += 1
            return httpx.Response(400, text="Bad Request")

        config = _make_config()
        client = TelemetryClient(config)
        client._client = httpx.Client(
            transport=httpx.MockTransport(handler),
            headers=client._client.headers,
            timeout=client._client.timeout,
        )

        result = client.send_batch([_make_span()])
        assert result is False
        assert attempt_count == 1  # No retries
        client.close()

    def test_retries_on_429(self):
        """429 (rate limited) should be retried."""
        attempt_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count < 2:
                return httpx.Response(429)
            return httpx.Response(200)

        config = _make_config()
        client = TelemetryClient(config)
        client._client = httpx.Client(
            transport=httpx.MockTransport(handler),
            headers=client._client.headers,
            timeout=client._client.timeout,
        )

        result = client.send_batch([_make_span()])
        assert result is True
        assert attempt_count == 2
        client.close()

    def test_connection_error_retried(self):
        """Connection errors should be retried."""
        attempt_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count < 3:
                raise httpx.ConnectError("connection refused")
            return httpx.Response(200)

        config = _make_config()
        client = TelemetryClient(config)
        client._client = httpx.Client(
            transport=httpx.MockTransport(handler),
            headers=client._client.headers,
            timeout=client._client.timeout,
        )

        result = client.send_batch([_make_span()])
        assert result is True
        assert attempt_count == 3
        client.close()

    def test_url_construction(self):
        """Push URL should have /v1/telemetry appended."""
        config = _make_config(push_url="http://collector:8080")
        client = TelemetryClient(config)
        assert client._url == "http://collector:8080/v1/telemetry"
        client.close()

    def test_url_trailing_slash_stripped(self):
        """Trailing slash in push_url should be handled."""
        config = _make_config(push_url="http://collector:8080/")
        client = TelemetryClient(config)
        assert client._url == "http://collector:8080/v1/telemetry"
        client.close()

    def test_custom_max_retries(self):
        """max_retries from config should control retry attempts."""
        attempt_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempt_count
            attempt_count += 1
            return httpx.Response(503)

        config = _make_config(max_retries=5, retry_backoff=0.01)
        client = TelemetryClient(config)
        client._client = httpx.Client(
            transport=httpx.MockTransport(handler),
            headers=client._client.headers,
            timeout=client._client.timeout,
        )

        result = client.send_batch([_make_span()])
        assert result is False
        assert attempt_count == 5  # Exactly max_retries attempts
        client.close()

    def test_single_retry_no_backoff_sleep(self):
        """With max_retries=1, should try once with no backoff sleep."""
        attempt_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempt_count
            attempt_count += 1
            return httpx.Response(500)

        config = _make_config(max_retries=1)
        client = TelemetryClient(config)
        client._client = httpx.Client(
            transport=httpx.MockTransport(handler),
            headers=client._client.headers,
            timeout=client._client.timeout,
        )

        result = client.send_batch([_make_span()])
        assert result is False
        assert attempt_count == 1
        client.close()

    def test_payload_serialization(self):
        """Verify span to_dict() output is included in payload."""
        captured_payload = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured_payload.update(json.loads(request.content))
            return httpx.Response(200)

        config = _make_config()
        client = TelemetryClient(config)
        client._client = httpx.Client(
            transport=httpx.MockTransport(handler),
            headers=client._client.headers,
            timeout=client._client.timeout,
        )

        span = SpanRecord(name="test_fn", span_type=SpanType.LLM)
        span.set_attribute("model", "gpt-4")
        span.set_attribute("provider", "openai")
        span.set_attribute("tokens_input", 100)
        span.finish(SpanStatus.OK)

        client.send_batch([span])

        span_data = captured_payload["spans"][0]
        assert span_data["span_type"] == "llm"
        assert span_data["attributes"]["model"] == "gpt-4"
        assert span_data["attributes"]["tokens_input"] == 100
        assert span_data["status"] == "OK"
        assert span_data["end_time"] is not None
        client.close()


# ========================================================================
# BackgroundExporter tests
# ========================================================================


class TestBackgroundExporter:
    def test_start_and_stop(self):
        """Exporter thread should start and stop cleanly."""
        config = _make_config(flush_interval=1)
        exporter = BackgroundExporter(config)

        # Patch the client to avoid real HTTP calls
        exporter._client = _NoopClient()

        exporter.start()
        assert exporter.is_running

        exporter.stop(timeout=3.0)
        assert not exporter.is_running

    def test_flushes_on_stop(self):
        """Remaining spans should be flushed when exporter stops."""
        sent_spans = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            sent_spans.extend(body.get("spans", []))
            return httpx.Response(200)

        config = _make_config(flush_interval=60)  # Long interval — won't fire
        exporter = BackgroundExporter(config)
        exporter._client._client = httpx.Client(
            transport=httpx.MockTransport(handler),
            headers=exporter._client._client.headers,
            timeout=exporter._client._client.timeout,
        )

        # Enqueue spans
        for i in range(5):
            enqueue_span(_make_span(f"span_{i}"))

        exporter.start()
        # Stop immediately — should drain remaining spans
        exporter.stop(timeout=5.0)

        assert len(sent_spans) == 5

    def test_periodic_flush(self):
        """Exporter should flush spans periodically."""
        sent_batches = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            sent_batches.append(body.get("spans", []))
            return httpx.Response(200)

        config = _make_config(flush_interval=1, batch_size=10)
        exporter = BackgroundExporter(config)
        exporter._client._client = httpx.Client(
            transport=httpx.MockTransport(handler),
            headers=exporter._client._client.headers,
            timeout=exporter._client._client.timeout,
        )

        # Enqueue some spans
        for i in range(3):
            enqueue_span(_make_span(f"span_{i}"))

        exporter.start()
        # Wait for at least one flush cycle
        time.sleep(2.5)
        exporter.stop(timeout=3.0)

        total_spans = sum(len(b) for b in sent_batches)
        assert total_spans == 3

    def test_double_start_is_noop(self):
        """Starting an already-running exporter should be a no-op."""
        config = _make_config()
        exporter = BackgroundExporter(config)
        exporter._client = _NoopClient()

        exporter.start()
        thread_id_1 = exporter._thread.ident

        exporter.start()  # Should not create a new thread
        thread_id_2 = exporter._thread.ident

        assert thread_id_1 == thread_id_2
        exporter.stop()

    def test_shutdown_timeout_from_config(self):
        """stop() without explicit timeout should use config shutdown_timeout."""
        config = _make_config(flush_interval=60, shutdown_timeout=1.0)
        exporter = BackgroundExporter(config)
        exporter._client = _NoopClient()

        exporter.start()
        assert exporter.is_running

        # stop() should use shutdown_timeout=1.0 from config
        exporter.stop()
        assert not exporter.is_running

    def test_stop_explicit_timeout_overrides_config(self):
        """stop(timeout=X) should override the config shutdown_timeout."""
        config = _make_config(flush_interval=60, shutdown_timeout=30.0)
        exporter = BackgroundExporter(config)
        exporter._client = _NoopClient()

        exporter.start()
        assert exporter.is_running

        start_t = time.monotonic()
        exporter.stop(timeout=1.0)
        elapsed = time.monotonic() - start_t

        assert not exporter.is_running
        # Should finish quickly (well under the 30s config value)
        assert elapsed < 5.0


# ========================================================================
# Module-level start/stop tests
# ========================================================================


class TestModuleLevelExporter:
    def test_start_exporter_with_push_url(self):
        """start_exporter should create and start when push_url is set."""
        config = _make_config()
        # Patch httpx.Client to avoid real connections
        with patch.object(TelemetryClient, '__init__', lambda self, cfg: _init_noop_client(self, cfg)):
            exp = start_exporter(config)
            assert exp is not None
            assert exp.is_running
            stop_exporter()

    def test_start_exporter_without_push_url(self):
        """start_exporter should return None when push is disabled."""
        config = GlobalConfig(service="test", env="test")
        exp = start_exporter(config)
        assert exp is None

    def test_configure_starts_exporter(self):
        """configure(push_url=...) should auto-start the exporter."""
        with patch.object(TelemetryClient, '__init__', lambda self, cfg: _init_noop_client(self, cfg)):
            configure(
                service="test-svc",
                env="test",
                push_url="http://localhost:9000",
            )
            stats = get_export_stats()
            # Exporter should have started (we just verify it doesn't crash)
            # Stop it for cleanup
            stop_exporter()

    def test_configure_no_push_url_no_exporter(self):
        """configure() without push_url should not start exporter."""
        configure(service="test-svc", env="test")
        # Should not crash, exporter not started


# ========================================================================
# Helpers
# ========================================================================


class _NoopClient:
    """A TelemetryClient stand-in that does nothing."""

    def send_batch(self, spans):
        return True

    def close(self):
        pass


def _init_noop_client(self, cfg):
    """Patch init for TelemetryClient to avoid real httpx.Client."""
    self._config = cfg
    self._exporter_config = cfg.exporter
    self._max_retries = cfg.exporter.max_retries
    self._initial_backoff = cfg.exporter.retry_backoff
    self._url = f"{cfg.exporter.push_url}/v1/telemetry"
    self._client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200)),
    )
