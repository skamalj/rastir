"""Evaluation worker pool — async dispatch to ThreadPoolExecutor.

Drains the evaluation queue and fans out each ``EvaluationTask`` to
the evaluator registry.  Each evaluation type runs as a separate
``concurrent.futures`` task with enforced timeout.  Results are
emitted as child spans (``span_type=evaluation``) correlated to the
original LLM span via ``trace_id`` / ``parent_span_id``.

Design invariants
-----------------
- Evaluation never blocks or slows ingestion.
- Failures in evaluation never propagate to the ingestion path.
- Each evaluation type produces a separate evaluation span.
- Timeout → partial emit (completed evaluations are still emitted).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FuturesTimeout
from typing import TYPE_CHECKING, Optional

from rastir.server.evaluation_queue import EvaluationQueue, EvaluationTask
from rastir.server.evaluators.registry import EvaluatorRegistry
from rastir.server.evaluators.types import EvaluationResult

if TYPE_CHECKING:
    from rastir.server.metrics import MetricsRegistry

logger = logging.getLogger("rastir.server")


def _eval_span_dict(
    task: EvaluationTask,
    result: EvaluationResult,
    duration_ms: float,
    evaluator_model: str = "",
    evaluator_provider: str = "",
) -> dict:
    """Build a span dict for an evaluation result.

    The span is correlated to the original LLM span:
    - ``trace_id``:  same as the LLM span's trace.
    - ``parent_id``: the LLM span's ``span_id``.
    - ``span_id``:   a newly generated UUID.
    - ``span_type``: ``evaluation``.
    """
    status = "OK" if result.error is None else "ERROR"
    attrs: dict = {
        "evaluation_type": result.evaluation_type,
        "evaluation_score": result.score,
        "evaluation_passed": result.passed,
        "model": task.model,
        "provider": task.provider,
        "service": task.service,
        "env": task.env,
        "evaluator_model": evaluator_model,
        "evaluator_provider": evaluator_provider,
    }
    if task.agent:
        attrs["agent"] = task.agent
    if result.details:
        attrs["evaluation_details"] = result.details
    if result.error:
        attrs["evaluation_error"] = result.error

    span: dict = {
        "trace_id": task.trace_id,
        "span_id": uuid.uuid4().hex,
        "parent_id": task.parent_span_id,
        "name": f"evaluate:{result.evaluation_type}",
        "span_type": "evaluation",
        "status": status,
        "start_time": None,  # filled in by caller or omitted
        "duration_ms": duration_ms,
        "duration_seconds": duration_ms / 1000.0,
        "attributes": attrs,
        "events": [],
    }
    return span


class EvaluationWorkerPool:
    """Async evaluation worker backed by a ``ThreadPoolExecutor``.

    Lifecycle
    ---------
    1. ``start()`` — launches an ``asyncio.Task`` that drains the queue.
    2. Queue items are submitted to the thread pool (one future per
       evaluation type).
    3. Results are collected with per-task timeout enforcement.
    4. Evaluation spans are emitted via an ``emit_fn`` callback
       (typically ``IngestionWorker.enqueue``).
    5. ``stop()`` — signals the drain loop to exit and joins workers.
    """

    def __init__(
        self,
        evaluation_queue: EvaluationQueue,
        registry: EvaluatorRegistry,
        metrics: "MetricsRegistry",
        *,
        concurrency: int = 4,
        emit_fn: Optional[object] = None,
    ) -> None:
        self._queue = evaluation_queue
        self._registry = registry
        self._metrics = metrics
        self._concurrency = concurrency
        self._emit_fn = emit_fn  # callable(service, env, version, spans) -> bool
        self._pool: Optional[ThreadPoolExecutor] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False

    # ---- lifecycle --------------------------------------------------------

    def start(self) -> None:
        """Start the evaluation drain loop and thread pool."""
        if self._task is not None and not self._task.done():
            return
        self._pool = ThreadPoolExecutor(
            max_workers=self._concurrency,
            thread_name_prefix="rastir-eval",
        )
        self._running = True
        self._task = asyncio.ensure_future(self._drain_loop())
        logger.info(
            "Evaluation worker pool started (concurrency=%d, queue_max=%d)",
            self._concurrency,
            self._queue.maxsize,
        )

    async def stop(self) -> None:
        """Signal the drain loop to stop and shut down the thread pool."""
        self._running = False
        if self._task is not None:
            await self._task
            self._task = None
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=False)
            self._pool = None
        logger.info("Evaluation worker pool stopped")

    # ---- drain loop -------------------------------------------------------

    async def _drain_loop(self) -> None:
        """Continuously drain the evaluation queue.

        Uses ``run_in_executor`` to offload the blocking ``queue.get()``
        call so we don't block the asyncio event loop.
        """
        loop = asyncio.get_running_loop()

        while self._running:
            try:
                task = await loop.run_in_executor(
                    None,  # default executor for the blocking get
                    self._queue.get,
                    1.0,  # timeout seconds
                )
            except Exception:
                continue

            if task is None:
                # Timeout — no work, loop again
                continue

            try:
                await self._process_task(task)
            except Exception:
                logger.exception("Unexpected error processing evaluation task")

    # ---- per-task processing ----------------------------------------------

    async def _process_task(self, task: EvaluationTask) -> None:
        """Run all requested evaluation types for a single task.

        Each evaluation type is submitted to the thread pool.
        A per-task timeout is enforced; completed evaluations are
        emitted even if some time out (partial emit).
        """
        if self._pool is None:
            return

        loop = asyncio.get_running_loop()
        eval_types = task.evaluation_types
        if not eval_types:
            return

        # Resolve evaluators (skip unknown types)
        to_run: list[tuple[str, object]] = []
        for et in eval_types:
            evaluator = self._registry.get(et)
            if evaluator is None:
                logger.debug("No evaluator registered for type %r — skipping", et)
                continue
            to_run.append((et, evaluator))

        if not to_run:
            return

        timeout_s = task.timeout_ms / 1000.0

        # Submit all evaluation types to the pool concurrently
        futures: list[tuple[str, Future, float, float, str, str]] = []
        for et, evaluator in to_run:
            t0 = time.monotonic()
            wall_start = time.time()
            future = self._pool.submit(evaluator.evaluate, task)
            ev_model = getattr(evaluator, "evaluator_model", "")
            ev_provider = getattr(evaluator, "evaluator_provider", "")
            futures.append((et, future, t0, wall_start, ev_model, ev_provider))

        # Collect results with overall timeout
        spans_to_emit: list[dict] = []

        for et, future, t0, wall_start, ev_model, ev_provider in futures:
            try:
                result: EvaluationResult = await asyncio.wait_for(
                    loop.run_in_executor(None, future.result, timeout_s),
                    timeout=timeout_s,
                )
                duration_ms = (time.monotonic() - t0) * 1000.0

                # Record metrics
                self._record_eval_metrics(task, result, duration_ms, ev_model, ev_provider)

                # Build evaluation span with proper wall-clock timestamps
                span = _eval_span_dict(task, result, duration_ms, ev_model, ev_provider)
                span["start_time"] = wall_start
                span["end_time"] = wall_start + duration_ms / 1000.0
                spans_to_emit.append(span)

            except (asyncio.TimeoutError, FuturesTimeout):
                duration_ms = (time.monotonic() - t0) * 1000.0
                logger.warning(
                    "Evaluation %r timed out after %.0fms for trace %s",
                    et, duration_ms, task.trace_id,
                )
                # Emit a timeout span
                timeout_result = EvaluationResult(
                    evaluation_type=et,
                    score=0.0,
                    passed=False,
                    error=f"Evaluation timed out after {timeout_s:.1f}s",
                )
                self._record_eval_metrics(task, timeout_result, duration_ms, ev_model, ev_provider)
                span = _eval_span_dict(task, timeout_result, duration_ms, ev_model, ev_provider)
                span["start_time"] = wall_start
                span["end_time"] = wall_start + duration_ms / 1000.0
                spans_to_emit.append(span)
                # Cancel the future if still running
                future.cancel()

            except Exception as exc:
                duration_ms = (time.monotonic() - t0) * 1000.0
                logger.warning("Evaluation %r failed: %s", et, exc)
                error_result = EvaluationResult(
                    evaluation_type=et,
                    score=0.0,
                    passed=False,
                    error=str(exc),
                )
                self._record_eval_metrics(task, error_result, duration_ms, ev_model, ev_provider)
                span = _eval_span_dict(task, error_result, duration_ms, ev_model, ev_provider)
                span["start_time"] = wall_start
                span["end_time"] = wall_start + duration_ms / 1000.0
                spans_to_emit.append(span)

        # Emit all evaluation spans via the ingestion pipeline
        if spans_to_emit and self._emit_fn is not None:
            try:
                self._emit_fn(
                    task.service,
                    task.env,
                    "",  # version
                    spans_to_emit,
                )
            except Exception:
                logger.debug("Failed to emit evaluation spans", exc_info=True)

    # ---- metrics helpers --------------------------------------------------

    def _record_eval_metrics(
        self,
        task: EvaluationTask,
        result: EvaluationResult,
        duration_ms: float,
        evaluator_model: str = "",
        evaluator_provider: str = "",
    ) -> None:
        """Update Prometheus metrics for an evaluation result."""
        labels = {
            "service": task.service,
            "env": task.env,
            "model": task.model,
            "provider": task.provider,
            "agent": task.agent or "",
            "evaluation_type": result.evaluation_type,
            "evaluator_model": evaluator_model,
            "evaluator_provider": evaluator_provider,
        }

        # Build exemplar linking back to the original LLM span's trace.
        exemplar = None
        trace_id = task.trace_id
        if trace_id and len(trace_id) == 32:
            import time as _time
            epoch = int(task.span_start_time or task.enqueued_at or _time.time())
            xray_tid = f"1-{epoch:08x}-{trace_id[8:]}"
            exemplar = {"trace_id": xray_tid}
        elif trace_id:
            exemplar = {"trace_id": trace_id}

        try:
            self._metrics.evaluation_runs.labels(**labels).inc(exemplar=exemplar)
            # Initialise failures counter for this label set so it always
            # exists in Prometheus (avoids empty-series issues in dashboards).
            self._metrics.evaluation_failures.labels(**labels)

            if result.error is not None:
                self._metrics.evaluation_failures.labels(**labels).inc(exemplar=exemplar)

            self._metrics.evaluation_latency.labels(**labels).observe(
                duration_ms / 1000.0, exemplar=exemplar
            )

            self._metrics.evaluation_score.labels(**labels).set(result.score)
        except AttributeError:
            # Metrics not yet defined (Phase 5) — silently skip
            pass
        except Exception:
            logger.debug("Failed to record evaluation metrics", exc_info=True)
