"""FastAPI application — the Rastir collector server.

Endpoints
---------
- ``POST /v1/telemetry`` — Ingest span batches from client libraries.
- ``GET  /metrics``       — Prometheus exposition endpoint.
- ``GET  /v1/traces``     — Query trace store (optional, debug mode).
- ``GET  /health``        — Liveness probe.
- ``GET  /ready``         — Readiness probe.

Entry point
-----------
Run directly::

    rastir-server            # uses pyproject.toml [project.scripts]
    python -m rastir.server   # uses __main__.py

Or programmatically::

    from rastir.server.app import create_app
    app = create_app()
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response

from rastir.server.config import ServerConfig, load_config, validate_config
from rastir.server.evaluation_queue import InMemoryEvaluationQueue
from rastir.server.evaluation_worker import EvaluationWorkerPool
from rastir.server.evaluators.builtins import (
    HallucinationEvaluator,
    JudgeConfig,
    ToxicityEvaluator,
)
from rastir.server.evaluators.registry import EvaluatorRegistry
from rastir.server.ingestion import IngestionWorker
from rastir.server.metrics import MetricsRegistry
from rastir.server.rate_limiter import RateLimiter
from rastir.server.redaction import NoOpRedactor, RegexRedactor
from rastir.server.sre_engine import SREEngine
from rastir.server.structured_logging import configure_logging
from rastir.server.trace_store import TraceStore

logger = logging.getLogger("rastir.server")


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


def _build_redactor(cfg: ServerConfig):
    """Build the redactor from server config."""
    if not cfg.redaction.enabled:
        return NoOpRedactor()

    custom = list(cfg.redaction.custom_patterns) if cfg.redaction.custom_patterns else None
    return RegexRedactor(
        extra_patterns=custom,
        max_text_length=cfg.redaction.max_text_length,
    )


def _build_evaluation_components(cfg: ServerConfig):
    """Build evaluation queue and evaluator registry.

    Returns ``(eval_queue, eval_registry)``; both may be ``None``
    when evaluation is disabled.
    """
    if not cfg.evaluation.enabled:
        return None, None

    eval_queue = InMemoryEvaluationQueue(
        max_size=cfg.evaluation.queue_size,
        drop_policy=cfg.evaluation.drop_policy,
    )

    eval_registry = EvaluatorRegistry(
        max_types=cfg.evaluation.max_evaluation_types,
    )

    # Register built-in evaluators
    judge_cfg = JudgeConfig(
        model=cfg.evaluation.judge_model,
        provider=cfg.evaluation.judge_provider,
        api_key=cfg.evaluation.judge_api_key,
        base_url=cfg.evaluation.judge_base_url,
    )
    eval_registry.register(ToxicityEvaluator(config=judge_cfg))
    eval_registry.register(HallucinationEvaluator(config=judge_cfg))

    logger.info(
        "Evaluation pipeline enabled: queue_size=%d, evaluators=%s",
        cfg.evaluation.queue_size,
        eval_registry.list_types(),
    )

    return eval_queue, eval_registry


# ---------------------------------------------------------------------------
# Application state (stored on the app instance via app.state)
# ---------------------------------------------------------------------------


def _build_components(cfg: ServerConfig) -> dict[str, Any]:
    """Instantiate all server subsystems from config."""
    metrics = MetricsRegistry(
        max_label_value_length=cfg.limits.max_label_value_length,
        cardinality_caps={
            "model": cfg.limits.cardinality_model,
            "provider": cfg.limits.cardinality_provider,
            "tool_name": cfg.limits.cardinality_tool_name,
            "agent": cfg.limits.cardinality_agent,
            "error_type": cfg.limits.cardinality_error_type,
        },
        duration_buckets=cfg.histograms.duration_buckets,
        tokens_buckets=cfg.histograms.tokens_buckets,
        exemplars_enabled=cfg.exemplars.enabled,
    )

    trace_store: Optional[TraceStore] = None
    if cfg.trace_store.enabled:
        trace_store = TraceStore(
            max_traces=cfg.limits.max_traces,
            max_spans_per_trace=cfg.trace_store.max_spans_per_trace,
            ttl_seconds=cfg.trace_store.ttl_seconds,
        )

    otlp_forwarder = None
    if cfg.exporter.enabled:
        try:
            from rastir.server.otlp_exporter import OTLPForwarder

            otlp_forwarder = OTLPForwarder(
                endpoint=cfg.exporter.otlp_endpoint,  # type: ignore[arg-type]
                batch_size=cfg.exporter.batch_size,
                flush_interval_ms=cfg.exporter.flush_interval * 1000,
            )
        except ImportError:
            logger.warning(
                "OTLP exporter requested but opentelemetry packages not installed. "
                "Install with: pip install rastir[server]"
            )

    # Evaluation queue + registry (must exist before IngestionWorker)
    eval_queue, eval_registry = _build_evaluation_components(cfg)

    worker = IngestionWorker(
        metrics=metrics,
        trace_store=trace_store,
        max_queue_size=cfg.limits.max_queue_size,
        otlp_forwarder=otlp_forwarder,
        sampling=cfg.sampling,
        backpressure=cfg.backpressure,
        redactor=_build_redactor(cfg),
        drop_on_redaction_failure=cfg.redaction.drop_on_failure,
        evaluation_queue=eval_queue,
        evaluation_config=cfg.evaluation,
    )

    # Evaluation worker pool
    eval_worker: Optional[EvaluationWorkerPool] = None
    if cfg.evaluation.enabled and eval_queue is not None:
        eval_worker = EvaluationWorkerPool(
            evaluation_queue=eval_queue,
            registry=eval_registry,
            metrics=metrics,
            concurrency=cfg.evaluation.worker_concurrency,
            emit_fn=worker.enqueue,
        )

    rate_limiter: Optional[RateLimiter] = None
    if cfg.rate_limit.enabled:
        rate_limiter = RateLimiter(
            per_ip_rpm=cfg.rate_limit.per_ip_rpm,
            per_service_rpm=cfg.rate_limit.per_service_rpm,
            registry=metrics.registry,
        )

    # SRE engine (V7)
    sre_engine: Optional[SREEngine] = None
    if cfg.sre.enabled:
        sre_engine = SREEngine(cfg=cfg.sre, registry=metrics.registry)
        worker.set_sre_engine(sre_engine)

    return {
        "config": cfg,
        "metrics": metrics,
        "trace_store": trace_store,
        "otlp_forwarder": otlp_forwarder,
        "worker": worker,
        "rate_limiter": rate_limiter,
        "eval_queue": eval_queue,
        "eval_registry": eval_registry,
        "eval_worker": eval_worker,
        "sre_engine": sre_engine,
    }


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start and stop background components with graceful shutdown."""
    worker: IngestionWorker = app.state.worker
    worker.start()

    # Start evaluation worker pool (if enabled)
    eval_worker: Optional[EvaluationWorkerPool] = app.state.eval_worker
    if eval_worker is not None:
        eval_worker.start()

    # Start SRE engine (if enabled)
    sre_engine: Optional[SREEngine] = app.state.sre_engine
    if sre_engine is not None:
        await sre_engine.start()

    cfg: ServerConfig = app.state.config
    logger.info(
        "Rastir server started on %s:%d",
        cfg.server.host,
        cfg.server.port,
    )
    yield
    # Graceful shutdown
    grace = cfg.shutdown.grace_period_seconds
    logger.info("Shutting down (grace_period=%ds, drain_queue=%s)", grace, cfg.shutdown.drain_queue)

    # Stop SRE engine
    if sre_engine is not None:
        await sre_engine.stop()

    # Stop evaluation workers first (they emit spans back to ingestion)
    if eval_worker is not None:
        try:
            await asyncio.wait_for(eval_worker.stop(), timeout=grace // 2 or grace)
        except asyncio.TimeoutError:
            logger.warning("Evaluation worker shutdown timed out")

    if cfg.shutdown.drain_queue:
        # Allow worker to drain remaining items within the grace period
        try:
            await asyncio.wait_for(worker.stop(), timeout=grace)
        except asyncio.TimeoutError:
            logger.warning(
                "Shutdown grace period expired (%ds) with %d items remaining in queue",
                grace, worker.queue_size,
            )
    else:
        await worker.stop()

    otlp = app.state.otlp_forwarder
    if otlp is not None:
        otlp.shutdown()
    logger.info("Rastir server stopped")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_app(config: Optional[ServerConfig] = None) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        config: Server configuration. If ``None``, loads from YAML /
                env vars / defaults via ``load_config()``.
    """
    cfg = config or load_config()
    validate_config(cfg)

    # Configure logging before anything else
    configure_logging(
        structured=cfg.logging.structured,
        level=cfg.logging.level,
        log_file=cfg.logging.log_file,
    )

    components = _build_components(cfg)

    app = FastAPI(
        title="Rastir Collector",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Attach components to app.state for access in route handlers
    for name, obj in components.items():
        setattr(app.state, name, obj)

    # Register routes
    _register_routes(app, cfg)

    return app


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _register_routes(app: FastAPI, cfg: ServerConfig) -> None:
    """Attach all endpoint handlers to the app."""

    # --- POST /v1/telemetry ------------------------------------------------

    @app.post("/v1/telemetry", status_code=202)
    async def ingest_telemetry(request: Request):
        """Accept a span batch from the client library."""
        worker: IngestionWorker = request.app.state.worker
        mt_cfg = request.app.state.config.multi_tenant

        try:
            body = await request.json()
        except Exception:
            logger.error("[ROUTE] Failed to parse JSON body", exc_info=True)
            raise HTTPException(status_code=400, detail="Invalid JSON")

        # Validate required fields
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Payload must be a JSON object")

        spans = body.get("spans")
        if not isinstance(spans, list) or len(spans) == 0:
            raise HTTPException(status_code=400, detail="'spans' must be a non-empty array")

        service = body.get("service", "unknown")
        env = body.get("env", "unknown")
        version = body.get("version", "")

        logger.debug(
            "[ROUTE] POST /v1/telemetry  service=%s env=%s version=%s spans=%d",
            service, env, version, len(spans),
        )
        for i, s in enumerate(spans):
            logger.debug(
                "[ROUTE] span[%d] name=%s type=%s trace=%s span=%s parent=%s start=%s end=%s keys=%s",
                i,
                s.get("name"), s.get("span_type"),
                str(s.get("trace_id", ""))[:12],
                str(s.get("span_id", ""))[:12],
                s.get("parent_span_id") or s.get("parent_id"),
                s.get("start_time"), s.get("end_time"),
                sorted(s.keys()),
            )

        # Rate limiting
        rl: Optional[RateLimiter] = request.app.state.rate_limiter
        if rl is not None:
            client_ip = request.client.host if request.client else "unknown"
            blocked = rl.check(client_ip, service)
            if blocked:
                raise HTTPException(
                    status_code=429,
                    detail=f"Rate limited ({blocked})",
                )

        # Multi-tenant: inject tenant label if enabled
        if mt_cfg.enabled:
            tenant = request.headers.get(mt_cfg.header_name, "default")
            for span in spans:
                attrs = span.setdefault("attributes", {})
                attrs["tenant"] = tenant

        accepted = worker.enqueue(service, env, version, spans)
        if not accepted:
            raise HTTPException(status_code=429, detail="Ingestion queue full")

        return {"status": "accepted", "spans_received": len(spans)}

    # --- GET /metrics ------------------------------------------------------

    @app.get("/metrics")
    async def prometheus_metrics(request: Request):
        """Prometheus exposition endpoint."""
        registry: MetricsRegistry = request.app.state.metrics
        worker: IngestionWorker = request.app.state.worker
        trace_store: Optional[TraceStore] = request.app.state.trace_store
        # Refresh operational gauges right before scrape for freshness
        registry.update_operational_gauges(
            queue_size=worker.queue_size,
            queue_maxsize=worker.queue_maxsize,
            trace_store=trace_store,
            eval_queue=request.app.state.eval_queue,
        )
        data, content_type = registry.generate()
        return Response(content=data, media_type=content_type)

    # --- GET /v1/traces (optional) -----------------------------------------

    @app.get("/v1/traces/{trace_id}")
    async def get_trace_by_id(request: Request, trace_id: str):
        """Get all spans for a specific trace by path parameter."""
        store: Optional[TraceStore] = request.app.state.trace_store
        if store is None:
            raise HTTPException(status_code=404, detail="Trace store is disabled")

        spans = store.get(trace_id)
        if spans is None:
            raise HTTPException(status_code=404, detail="Trace not found")
        return {"trace_id": trace_id, "spans": spans}

    @app.get("/v1/traces")
    async def query_traces(
        request: Request,
        trace_id: Optional[str] = None,
        service: Optional[str] = None,
        limit: int = 20,
    ):
        """Query the in-memory trace store.

        - With ``trace_id``: returns all spans for that trace.
        - With ``service``: filters traces containing spans from that service.
        - Without filters: returns the most recent trace summaries.
        """
        store: Optional[TraceStore] = request.app.state.trace_store
        if store is None:
            raise HTTPException(status_code=404, detail="Trace store is disabled")

        if trace_id:
            spans = store.get(trace_id)
            if spans is None:
                raise HTTPException(status_code=404, detail="Trace not found")
            return {"trace_id": trace_id, "spans": spans}

        if service:
            return {"traces": store.search(service=service, limit=limit)}

        return {"traces": store.recent(limit=limit)}

    # --- GET /health -------------------------------------------------------

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    # --- GET /ready --------------------------------------------------------

    @app.get("/ready")
    async def readiness(request: Request):
        worker: IngestionWorker = request.app.state.worker
        bp_cfg = request.app.state.config.backpressure
        queue_pct = (worker.queue_size / worker.queue_maxsize) * 100 if worker.queue_maxsize else 0

        reasons: list[str] = []
        if queue_pct >= bp_cfg.hard_limit_pct:
            reasons.append(f"queue_pct={queue_pct:.1f}% >= hard_limit={bp_cfg.hard_limit_pct}%")

        # Exporter health: if configured but in a failed state
        otlp = request.app.state.otlp_forwarder
        if otlp is not None and hasattr(otlp, "healthy") and not otlp.healthy:
            reasons.append("otlp_exporter_unhealthy")

        ready = len(reasons) == 0
        status_code = 200 if ready else 503
        body = {
            "status": "ready" if ready else "not_ready",
            "queue_pct": round(queue_pct, 1),
        }
        if reasons:
            body["reasons"] = reasons

        import json as _json
        return Response(
            content=_json.dumps(body),
            status_code=status_code,
            media_type="application/json",
        )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the server via uvicorn (``rastir-server`` console script)."""
    import uvicorn

    cfg = load_config()
    app = create_app(cfg)
    # log_config=None prevents uvicorn from overriding our handlers
    uvicorn.run(
        app,
        host=cfg.server.host,
        port=cfg.server.port,
        log_config=None,
    )


if __name__ == "__main__":
    main()
