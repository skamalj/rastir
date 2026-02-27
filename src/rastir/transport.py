"""HTTP transport for pushing telemetry to the collector server.

Provides:
- TelemetryClient: synchronous httpx client that serializes span batches and
  POSTs them to /v1/telemetry with retry + backoff.
- BackgroundExporter: daemon thread that periodically drains the span
  queue and pushes via the TelemetryClient.

The exporter never blocks decorated functions. All network I/O happens
in the background thread.

Shutdown note:
    The atexit handler performs a final flush, which may delay process
    exit by up to ~8.5s if the collector is unreachable (5s join +
    3.5s retry backoff). Call stop_exporter() explicitly for faster
    shutdown, or reduce the timeout via configure().
"""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import threading
import time
from typing import Optional

import httpx

from rastir.config import ExporterConfig, GlobalConfig, get_config
from rastir.queue import drain_batch, queue_size
from rastir.spans import SpanRecord

logger = logging.getLogger("rastir")

# Internal counters for observability of the exporter itself
_export_successes: int = 0
_export_failures: int = 0
_spans_exported: int = 0
_spans_dropped: int = 0


# ---------------------------------------------------------------------------
# TelemetryClient — serializes and sends span batches
# ---------------------------------------------------------------------------


class TelemetryClient:
    """Synchronous httpx client for pushing span batches.

    Uses a persistent connection pool for efficiency. Retries transient
    failures (5xx, timeouts, connection errors) with exponential backoff.
    """

    # Transient HTTP status codes that warrant a retry
    _RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

    def __init__(self, config: GlobalConfig) -> None:
        self._config = config
        self._exporter_config = config.exporter
        self._max_retries = self._exporter_config.max_retries
        self._initial_backoff = self._exporter_config.retry_backoff
        self._url = f"{self._exporter_config.push_url.rstrip('/')}/v1/telemetry"

        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "User-Agent": "rastir/0.1.0",
        }
        if self._exporter_config.api_key:
            headers["X-API-Key"] = self._exporter_config.api_key

        self._client = httpx.Client(
            timeout=httpx.Timeout(self._exporter_config.timeout, connect=5.0),
            headers=headers,
        )

    def send_batch(self, spans: list[SpanRecord]) -> bool:
        """Serialize and send a batch of spans.

        Returns True if the batch was accepted (2xx), False otherwise.
        Retries on transient failures with exponential backoff.
        """
        global _export_successes, _export_failures, _spans_exported, _spans_dropped

        payload = self._build_payload(spans)
        backoff = self._initial_backoff

        for attempt in range(1, self._max_retries + 1):
            try:
                response = self._client.post(self._url, content=payload)

                if response.status_code < 300:
                    _export_successes += 1
                    _spans_exported += len(spans)
                    return True

                if response.status_code in self._RETRYABLE_STATUS:
                    logger.warning(
                        "Telemetry push failed (attempt %d/%d): HTTP %d",
                        attempt, self._max_retries, response.status_code,
                    )
                    if attempt < self._max_retries:
                        time.sleep(backoff)
                        backoff *= 2
                        continue
                else:
                    # Non-retryable client error (4xx except 429)
                    logger.error(
                        "Telemetry push rejected: HTTP %d — %s",
                        response.status_code,
                        response.text[:200],
                    )
                    _export_failures += 1
                    _spans_dropped += len(spans)
                    return False

            except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
                logger.warning(
                    "Telemetry push error (attempt %d/%d): %s",
                    attempt, self._max_retries, exc,
                )
                if attempt < self._max_retries:
                    time.sleep(backoff)
                    backoff *= 2
                    continue
            except Exception:
                logger.debug("Unexpected telemetry push error", exc_info=True)
                _export_failures += 1
                _spans_dropped += len(spans)
                return False

        # All retries exhausted
        _export_failures += 1
        _spans_dropped += len(spans)
        logger.error("Telemetry push failed after %d retries, dropping %d spans",
                      self._max_retries, len(spans))
        return False

    def _build_payload(self, spans: list[SpanRecord]) -> bytes:
        """Build the JSON payload for POST /v1/telemetry."""
        payload = {
            "service": self._config.service,
            "env": self._config.env,
            "version": self._config.version,
            "spans": [span.to_dict() for span in spans],
        }
        return json.dumps(payload, default=str).encode("utf-8")

    def close(self) -> None:
        """Close the underlying HTTP client."""
        try:
            self._client.close()
        except Exception:
            logger.debug("Error closing telemetry client", exc_info=True)


# ---------------------------------------------------------------------------
# BackgroundExporter — daemon thread that drains queue and pushes
# ---------------------------------------------------------------------------


class BackgroundExporter:
    """Background daemon thread that periodically flushes spans.

    The thread wakes up every flush_interval seconds, drains up to
    batch_size spans from the queue, and pushes them. On shutdown
    (via atexit or explicit stop), it performs a final drain.
    """

    def __init__(self, config: GlobalConfig) -> None:
        self._config = config
        self._client = TelemetryClient(config)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start the background export thread."""
        if self._thread is not None and self._thread.is_alive():
            logger.debug("Background exporter already running")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="rastir-exporter",
            daemon=True,
        )
        self._thread.start()
        atexit.register(self.stop)
        logger.debug("Background exporter started (interval=%ds, batch=%d)",
                      self._config.exporter.flush_interval,
                      self._config.exporter.batch_size)

    def stop(self, timeout: float | None = None) -> None:
        """Signal the background thread to stop and perform a final flush.

        Args:
            timeout: Max seconds to wait for the thread to finish.
                     Defaults to the configured shutdown_timeout.
        """
        if self._thread is None or not self._thread.is_alive():
            return

        if timeout is None:
            timeout = self._config.exporter.shutdown_timeout

        logger.debug("Stopping background exporter...")
        self._stop_event.set()
        self._thread.join(timeout=timeout)

        # Final drain — push any remaining spans
        self._flush_all()
        self._client.close()
        logger.debug("Background exporter stopped")

    def _run(self) -> None:
        """Main loop: sleep → drain → push. Repeats until stopped."""
        interval = self._config.exporter.flush_interval
        batch_size = self._config.exporter.batch_size

        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=interval)
            self._flush_once(batch_size)

    def _flush_once(self, batch_size: int) -> None:
        """Drain one batch and send it."""
        batch = drain_batch(batch_size)
        if batch:
            self._client.send_batch(batch)

    def _flush_all(self) -> None:
        """Drain and push all remaining spans in the queue."""
        batch_size = self._config.exporter.batch_size
        while queue_size() > 0:
            batch = drain_batch(batch_size)
            if not batch:
                break
            self._client.send_batch(batch)

    @property
    def is_running(self) -> bool:
        """Whether the background thread is alive."""
        return self._thread is not None and self._thread.is_alive()


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_exporter: Optional[BackgroundExporter] = None


def start_exporter(config: Optional[GlobalConfig] = None) -> Optional[BackgroundExporter]:
    """Start the background exporter if push is enabled.

    Called automatically by configure() when push_url is set.
    Returns the exporter instance, or None if push is disabled.
    """
    global _exporter

    if _exporter is not None and _exporter.is_running:
        logger.debug("Exporter already running")
        return _exporter

    cfg = config or get_config()
    if not cfg.exporter.enabled:
        logger.debug("Push disabled (no push_url) — exporter not started")
        return None

    _exporter = BackgroundExporter(cfg)
    _exporter.start()
    return _exporter


def stop_exporter() -> None:
    """Stop the background exporter. Safe to call even if not running."""
    global _exporter
    if _exporter is not None:
        _exporter.stop()
        _exporter = None


def get_export_stats() -> dict[str, int]:
    """Return internal exporter counters for diagnostics."""
    return {
        "export_successes": _export_successes,
        "export_failures": _export_failures,
        "spans_exported": _spans_exported,
        "spans_dropped": _spans_dropped,
    }


def reset_export_stats() -> None:
    """Reset export counters. For testing only."""
    global _export_successes, _export_failures, _spans_exported, _spans_dropped
    _export_successes = 0
    _export_failures = 0
    _spans_exported = 0
    _spans_dropped = 0
