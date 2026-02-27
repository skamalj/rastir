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

import logging
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response

from rastir.server.config import ServerConfig, load_config, validate_config
from rastir.server.ingestion import IngestionWorker
from rastir.server.metrics import MetricsRegistry
from rastir.server.rate_limiter import RateLimiter
from rastir.server.structured_logging import configure_logging
from rastir.server.trace_store import TraceStore

logger = logging.getLogger("rastir.server")


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

    worker = IngestionWorker(
        metrics=metrics,
        trace_store=trace_store,
        max_queue_size=cfg.limits.max_queue_size,
        otlp_forwarder=otlp_forwarder,
        sampling=cfg.sampling,
        backpressure=cfg.backpressure,
    )

    rate_limiter: Optional[RateLimiter] = None
    if cfg.rate_limit.enabled:
        rate_limiter = RateLimiter(
            per_ip_rpm=cfg.rate_limit.per_ip_rpm,
            per_service_rpm=cfg.rate_limit.per_service_rpm,
            registry=metrics.registry,
        )

    return {
        "config": cfg,
        "metrics": metrics,
        "trace_store": trace_store,
        "otlp_forwarder": otlp_forwarder,
        "worker": worker,
        "rate_limiter": rate_limiter,
    }


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start and stop background components with graceful shutdown."""
    worker: IngestionWorker = app.state.worker
    worker.start()
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

    if cfg.shutdown.drain_queue:
        # Allow worker to drain remaining items within the grace period
        import asyncio
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
    configure_logging(structured=cfg.logging.structured, level=cfg.logging.level)

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
    uvicorn.run(app, host=cfg.server.host, port=cfg.server.port)


if __name__ == "__main__":
    main()
