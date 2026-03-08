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

from rastir.server.config import BackpressureSection, EvaluationSection, SamplingSection
from rastir.server.evaluation_queue import EvaluationQueue, EvaluationTask
from rastir.server.metrics import MetricsRegistry
from rastir.server.redaction import Redactor, redact_span
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
        redactor: Optional[Redactor] = None,
        drop_on_redaction_failure: bool = True,
        evaluation_queue: Optional[EvaluationQueue] = None,
        evaluation_config: Optional[EvaluationSection] = None,
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

        # Redaction config
        self._redactor = redactor
        self._drop_on_redaction_failure = drop_on_redaction_failure

        # Evaluation config
        self._eval_queue = evaluation_queue
        self._eval_config = evaluation_config or EvaluationSection()

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
        logger.debug("[WORKER] consumer loop started")
        while self._running:
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                # Periodic gauge refresh even when idle
                self._update_gauges()
                continue
            except Exception:
                logger.error("[WORKER] unexpected error getting from queue", exc_info=True)
                continue

            self._update_gauges()

            service, env, version, spans = item
            if service == "__stop__":
                logger.debug("[WORKER] received stop signal")
                break

            try:
                self._process_batch(service, env, version, spans)
            except Exception:
                logger.exception("[WORKER] CATCH-ALL: Error processing span batch")

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
        logger.debug(
            "[BATCH] Processing %d spans  service=%s env=%s version=%s  otlp=%s",
            len(spans), service, env, version,
            self._otlp is not None,
        )

        for idx, span in enumerate(spans):
            span_id = span.get("span_id", "?")[:12]
            trace_id = span.get("trace_id", "?")[:12]
            span_type = span.get("span_type", "?")
            span_name = span.get("name", "?")
            logger.debug(
                "[SPAN %d/%d] name=%s type=%s trace=%s… span=%s…",
                idx + 1, len(spans), span_name, span_type, trace_id, span_id,
            )
            logger.debug("[SPAN %d/%d] keys=%s", idx + 1, len(spans), sorted(span.keys()))
            logger.debug(
                "[SPAN %d/%d] attr_keys=%s",
                idx + 1, len(spans), sorted(span.get("attributes", {}).keys()),
            )

            # Sampling decision (computed first so metrics can use it for exemplars)
            store = self._should_store(span)

            # 1. Prometheus metrics — ALWAYS recorded (sampling flag controls exemplars)
            try:
                self._metrics.record_span(span, service=service, env=env, sampled=store)
                logger.debug("[SPAN %d] step-1 metrics OK", idx + 1)
            except Exception:
                logger.error("[SPAN %d] step-1 metrics FAILED", idx + 1, exc_info=True)

            # 2. Sampling counters
            logger.debug("[SPAN %d] step-2 sampling → store=%s", idx + 1, store)
            if store:
                self._metrics.spans_sampled.labels(service=service, env=env).inc()
            else:
                self._metrics.spans_dropped_by_sampling.labels(service=service, env=env).inc()

            # 3. Redaction (if sampled and redactor configured)
            if store and self._redactor is not None:
                attrs = span.get("attributes", {})
                if attrs.get("prompt_text") or attrs.get("completion_text"):
                    logger.debug("[SPAN %d] step-3 redaction running…", idx + 1)
                    try:
                        ok = redact_span(span, self._redactor, service, env)
                    except Exception:
                        ok = False
                        logger.error("[SPAN %d] step-3 redaction EXCEPTION", idx + 1, exc_info=True)
                    if ok:
                        self._metrics.redaction_applied.labels(
                            service=service, env=env,
                        ).inc()
                        logger.debug("[SPAN %d] step-3 redaction OK", idx + 1)
                    else:
                        self._metrics.redaction_failures.labels(
                            service=service, env=env,
                        ).inc()
                        if self._drop_on_redaction_failure:
                            logger.warning(
                                "[SPAN %d] step-3 DROPPING span %s due to redaction failure",
                                idx + 1, span.get("span_id", "?"),
                            )
                            continue  # DROP — raw text never stored
                else:
                    logger.debug("[SPAN %d] step-3 redaction skipped (no text)", idx + 1)
            else:
                logger.debug(
                    "[SPAN %d] step-3 skip  store=%s redactor=%s",
                    idx + 1, store, self._redactor is not None,
                )

            # 4. Trace store (if enabled and sampled in)
            trace_id_full = span.get("trace_id")
            if store and self._trace_store is not None and trace_id_full:
                try:
                    self._trace_store.insert(trace_id_full, [span])
                    logger.debug("[SPAN %d] step-4 trace_store OK", idx + 1)
                except Exception:
                    logger.error("[SPAN %d] step-4 trace_store FAILED", idx + 1, exc_info=True)
            else:
                logger.debug(
                    "[SPAN %d] step-4 skip  store=%s trace_store=%s trace_id=%s",
                    idx + 1, store, self._trace_store is not None, bool(trace_id_full),
                )

            # 5. OTLP forward (if configured and sampled in)
            if store and self._otlp is not None:
                logger.debug(
                    "[SPAN %d] step-5 OTLP forward starting…  span_keys=%s",
                    idx + 1, sorted(span.keys()),
                )
                try:
                    self._otlp.export_span(span, service=service, env=env, version=version)
                    logger.debug("[SPAN %d] step-5 OTLP forward OK (enqueued)", idx + 1)
                except Exception:
                    logger.error(
                        "[SPAN %d] step-5 OTLP forward FAILED  span=%s",
                        idx + 1, span, exc_info=True,
                    )
                    self._metrics.export_failures.labels(
                        service=service, env=env,
                    ).inc()
            else:
                logger.debug(
                    "[SPAN %d] step-5 skip  store=%s otlp=%s",
                    idx + 1, store, self._otlp is not None,
                )

            # 6. Evaluation enqueue (if evaluation enabled + sampled + non-error)
            is_error = span.get("status") == "ERROR"
            if store and not is_error and self._eval_queue is not None:
                logger.debug("[SPAN %d] step-6 eval enqueue check…", idx + 1)
                try:
                    self._maybe_enqueue_evaluation(span, service, env)
                    logger.debug("[SPAN %d] step-6 eval enqueue done", idx + 1)
                except Exception:
                    logger.error("[SPAN %d] step-6 eval enqueue FAILED", idx + 1, exc_info=True)
            else:
                logger.debug(
                    "[SPAN %d] step-6 skip  store=%s eval_queue=%s",
                    idx + 1, store, self._eval_queue is not None,
                )

        logger.debug("[BATCH] Finished processing %d spans", len(spans))

    # ----- evaluation enqueue ----------------------------------------------

    def _maybe_enqueue_evaluation(
        self, span: dict, service: str, env: str,
    ) -> None:
        """Conditionally enqueue a span for async evaluation.

        Only LLM spans with ``evaluation_enabled=True`` are considered.
        Evaluation sampling (per-span or global default) is applied here
        before enqueueing to avoid wasting queue capacity.
        """
        attrs = span.get("attributes", {})
        if not attrs.get("evaluation_enabled"):
            return

        # Evaluation sampling decision
        sample_rate = attrs.get(
            "evaluation_sample_rate",
            self._eval_config.default_sample_rate,
        )
        if sample_rate < 1.0 and random.random() >= sample_rate:
            return

        # Build evaluation task from the (already-redacted) span
        eval_types = attrs.get("evaluation_types", [])
        if not eval_types:
            return

        timeout_ms = attrs.get(
            "evaluation_timeout_ms",
            self._eval_config.default_timeout_ms,
        )

        # Use the trace epoch cached by the metrics layer (set from the
        # first span in this trace) so the X-Ray trace ID built by the
        # evaluation worker matches the one the OTLP exporter stored.
        trace_id_raw = span.get("trace_id", "")
        trace_epoch = self._metrics._trace_epoch_cache.get(
            trace_id_raw, span.get("start_time")
        )

        task = EvaluationTask(
            trace_id=trace_id_raw,
            parent_span_id=span.get("span_id", ""),
            service=service,
            env=env,
            model=attrs.get("model", "unknown"),
            provider=attrs.get("provider", "unknown"),
            agent=attrs.get("agent"),
            prompt_text=attrs.get("prompt_text"),
            completion_text=attrs.get("completion_text"),
            evaluation_types=list(eval_types),
            timeout_ms=timeout_ms,
            span_start_time=trace_epoch,
        )

        accepted = self._eval_queue.put(task)
        if not accepted:
            try:
                self._metrics.evaluation_dropped.labels(
                    service=service, env=env,
                ).inc()
            except AttributeError:
                pass  # metrics not yet defined
            logger.debug("Evaluation task dropped (queue full)")

    # ----- sampling --------------------------------------------------------

    def _should_store(self, span: dict) -> bool:
        """Decide whether a span should be stored/exported.

        Pure probabilistic sampling: each span is independently
        sampled with probability ``rate``.  When ``rate >= 1.0``
        (the default) every span is retained.
        """
        rate = self._sampling.rate
        if rate >= 1.0:
            return True
        if rate <= 0.0:
            return False
        return random.random() < rate

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
            eval_queue=self._eval_queue,
        )
