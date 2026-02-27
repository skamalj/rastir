"""Ingestion queue and async worker.

Decouples the HTTP ingestion endpoint (``POST /v1/telemetry``) from the
metric-update and trace-storage path.  An ``asyncio.Queue`` buffers
incoming span batches; a background task drains them continuously.

Back-pressure: when the queue is full the API returns **429** and the
``ingestion_rejections_total`` metric is incremented.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Optional

from rastir.server.config import BackpressureSection, SamplingSection
from rastir.server.metrics import MetricsRegistry
from rastir.server.trace_store import TraceStore

logger = logging.getLogger("rastir.server")


class IngestionWorker:
    """Async background worker that processes ingested span batches.

    Each item on the queue is a tuple of ``(service, env, version, spans)``
    where *spans* is a list of raw span dicts from the client payload.
    """

    def __init__(
        self,
        metrics: MetricsRegistry,
        trace_store: Optional[TraceStore],
        max_queue_size: int = 50_000,
        otlp_forwarder: Any = None,
        sampling: Optional[SamplingSection] = None,
        backpressure: Optional[BackpressureSection] = None,
    ) -> None:
        self._metrics = metrics
        self._trace_store = trace_store
        self._otlp = otlp_forwarder
        self._queue: asyncio.Queue[tuple[str, str, str, list[dict]]] = asyncio.Queue(
            maxsize=max_queue_size
        )
        self._task: Optional[asyncio.Task] = None
        self._running = False

        # Sampling config
        self._sampling = sampling or SamplingSection()

        # Backpressure config
        self._bp = backpressure or BackpressureSection()
        self._soft_warned = False  # avoid log spam

    # ----- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Start the background consumer task."""
        if self._task is not None and not self._task.done():
            return
        self._running = True
        self._task = asyncio.ensure_future(self._run())
        logger.info("Ingestion worker started (queue_max=%d)", self._queue.maxsize)

    async def stop(self) -> None:
        """Signal the worker to stop and wait for it to finish."""
        self._running = False
        if self._task is not None:
            # Wake up the worker if it's waiting on an empty queue
            try:
                self._queue.put_nowait(("__stop__", "", "", []))
            except asyncio.QueueFull:
                pass
            await self._task
            self._task = None
        logger.info("Ingestion worker stopped")

    # ----- enqueue (called by HTTP handler) --------------------------------

    def enqueue(
        self,
        service: str,
        env: str,
        version: str,
        spans: list[dict],
    ) -> bool:
        """Add a span batch to the queue with backpressure controls.

        Returns ``True`` if accepted, ``False`` if rejected.

        Backpressure behaviour:
        - Below soft limit: accept normally.
        - Between soft and hard limit: accept but log warning.
        - At/above hard limit in ``reject`` mode: reject (429).
        - At/above hard limit in ``drop_oldest`` mode: evict oldest, accept.
        """
        maxsize = self._queue.maxsize or 1
        usage_pct = (self._queue.qsize() / maxsize) * 100.0

        # Soft-limit warning
        if usage_pct >= self._bp.soft_limit_pct:
            if not self._soft_warned:
                logger.warning(
                    "Queue usage %.1f%% exceeds soft limit (%.1f%%)",
                    usage_pct, self._bp.soft_limit_pct,
                )
                self._soft_warned = True
            self._metrics.backpressure_warnings.inc()
        else:
            self._soft_warned = False

        # Hard-limit handling
        if self._queue.full():
            if self._bp.mode == "drop_oldest":
                # Evict oldest batch to make room
                try:
                    self._queue.get_nowait()
                    self._metrics.spans_dropped_by_backpressure.inc()
                    logger.debug("Dropped oldest batch (drop_oldest mode)")
                except asyncio.QueueEmpty:
                    pass
            else:
                # Default reject mode
                self._metrics.ingestion_rejections.labels(
                    service=service, env=env,
                ).inc()
                logger.warning(
                    "Ingestion queue full — rejecting %d spans", len(spans)
                )
                return False

        try:
            self._queue.put_nowait((service, env, version, spans))
            self._update_gauges()
            return True
        except asyncio.QueueFull:
            self._metrics.ingestion_rejections.labels(
                service=service, env=env,
            ).inc()
            logger.warning("Ingestion queue full — rejecting %d spans", len(spans))
            return False

    # ----- consumer loop ---------------------------------------------------

    async def _run(self) -> None:
        """Drain items from the queue and process them."""
        while self._running:
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                # Periodic gauge refresh even when idle
                self._update_gauges()
                continue

            self._update_gauges()

            service, env, version, spans = item
            if service == "__stop__":
                break

            try:
                self._process_batch(service, env, version, spans)
            except Exception:
                logger.exception("Error processing span batch")

        # Final drain — process whatever remains
        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
                service, env, version, spans = item
                if service == "__stop__":
                    continue
                self._process_batch(service, env, version, spans)
            except asyncio.QueueEmpty:
                break

    def _process_batch(
        self,
        service: str,
        env: str,
        version: str,
        spans: list[dict],
    ) -> None:
        """Update metrics + optionally store/export each span.

        Metrics are *always* recorded (sampling never affects counters
        or histograms).  Trace storage and OTLP export are subject to
        the configured sampling policy.
        """
        self._metrics.record_ingested_spans(len(spans))

        for span in spans:
            # 1. Prometheus metrics — ALWAYS recorded
            self._metrics.record_span(span, service=service, env=env)

            # 2. Sampling decision (only affects storage + export)
            store = self._should_store(span)
            if store:
                self._metrics.spans_sampled.labels(service=service, env=env).inc()
            else:
                self._metrics.spans_dropped_by_sampling.labels(service=service, env=env).inc()

            # 3. Trace store (if enabled and sampled in)
            trace_id = span.get("trace_id")
            if store and self._trace_store is not None and trace_id:
                self._trace_store.insert(trace_id, [span])

            # 4. OTLP forward (if configured and sampled in)
            if store and self._otlp is not None:
                try:
                    self._otlp.export_span(span, service=service, env=env, version=version)
                except Exception:
                    logger.debug("OTLP forward error", exc_info=True)
                    self._metrics.export_failures.labels(
                        service=service, env=env,
                    ).inc()

    # ----- sampling --------------------------------------------------------

    def _should_store(self, span: dict) -> bool:
        """Decide whether a span should be stored/exported.

        If sampling is disabled every span is retained.  When enabled:
        - Error spans are always retained (if ``always_retain_errors``).
        - Spans exceeding ``latency_threshold_ms`` are always retained.
        - Otherwise, head-based probabilistic sampling at ``rate``.
        """
        if not self._sampling.enabled:
            return True

        # Always retain errors
        if self._sampling.always_retain_errors and span.get("status") == "ERROR":
            return True

        # Always retain high-latency spans
        threshold = self._sampling.latency_threshold_ms
        if threshold > 0:
            duration = span.get("duration_ms", 0) or 0
            if duration >= threshold:
                return True

        # Head-based probabilistic sampling
        return random.random() < self._sampling.rate

    # ----- diagnostics -----------------------------------------------------

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    @property
    def queue_maxsize(self) -> int:
        return self._queue.maxsize

    def _update_gauges(self) -> None:
        """Refresh all operational gauges via the metrics registry."""
        self._metrics.update_operational_gauges(
            queue_size=self._queue.qsize(),
            queue_maxsize=self._queue.maxsize,
            trace_store=self._trace_store,
        )
