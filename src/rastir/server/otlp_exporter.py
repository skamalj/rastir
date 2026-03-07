"""Optional OTLP span forwarding.

Converts ingested span dicts into OpenTelemetry SDK ``ReadableSpan``
objects — preserving original trace/span/parent IDs and timestamps —
and exports them via the official ``OTLPSpanExporter`` using a
``BatchSpanProcessor``.

The forwarder is only initialised when ``exporter.otlp_endpoint`` is
configured.  It is safe to skip — the ingestion worker checks for
``None`` before calling.
"""

from __future__ import annotations

import logging
import platform
import time
from typing import Optional, Sequence

logger = logging.getLogger("rastir.server")

# Lazy imports so the module can be imported without opentelemetry
# installed (it's only needed when OTLP export is configured).
_otel_available: Optional[bool] = None


def _check_otel() -> bool:
    global _otel_available
    if _otel_available is None:
        try:
            from opentelemetry.sdk.trace import TracerProvider  # noqa: F401
            _otel_available = True
        except ImportError:
            _otel_available = False
    return _otel_available


def _hex_to_trace_id(hex_str: str, start_epoch: float | None = None) -> int:
    """Convert a hex trace-id to a 128-bit integer.

    OTel trace IDs are 128-bit (32 hex chars). If the input is longer,
    it is truncated to the first 32 chars.

    When *start_epoch* is provided the first 4 bytes are overwritten
    with the unix timestamp so that AWS X-Ray (which derives its
    trace-header timestamp from those bytes) indexes the trace
    correctly.
    """
    raw = int(hex_str[:32], 16)
    if start_epoch is not None:
        ts = int(start_epoch) & 0xFFFFFFFF
        # Clear first 4 bytes and set them to the timestamp
        raw = (ts << 96) | (raw & ((1 << 96) - 1))
    return raw


def _hex_to_span_id(hex_str: str) -> int:
    """Convert a hex span-id to a 64-bit integer.

    OTel span IDs are 64-bit (16 hex chars). Rastir clients generate
    ``uuid4().hex`` (32 chars / 128 bits) for span_id, so we must
    truncate to 16 hex chars to fit the OTel spec.
    """
    return int(hex_str[:16], 16)


class _LoggingExporterWrapper:
    """Wrapper around OTLPSpanExporter that logs every export() call detail."""

    def __init__(self, inner):
        self._inner = inner

    def export(self, spans):
        for i, s in enumerate(spans):
            ctx = s.context
            logger.debug(
                "[OTLP-EXPORT] span[%d] name=%r trace_id=%s (%d bits) "
                "span_id=%s (%d bits) start=%s end=%s resource=%s attrs=%s",
                i,
                s.name,
                hex(ctx.trace_id), ctx.trace_id.bit_length(),
                hex(ctx.span_id), ctx.span_id.bit_length(),
                s.start_time,
                s.end_time,
                {k: v for k, v in (s.resource.attributes or {}).items()},
                dict(s.attributes or {}),
            )
        result = self._inner.export(spans)
        logger.debug("[OTLP-EXPORT] export() returned → %s  (%d spans)", result, len(spans))
        return result

    def shutdown(self):
        return self._inner.shutdown()

    def force_flush(self, timeout_millis=None):
        if timeout_millis is not None:
            return self._inner.force_flush(timeout_millis)
        return self._inner.force_flush()


class OTLPForwarder:
    """OTLP span exporter that preserves original trace / span IDs.

    Instead of creating new spans (which would generate new IDs), this
    builds ``ReadableSpan`` objects directly so the original trace_id,
    span_id, parent_span_id, and timestamps survive the round-trip to
    Tempo / Jaeger / any OTLP backend.

    Spans are forwarded via ``BatchSpanProcessor`` which handles
    batching, retries, and backpressure internally.
    """

    def __init__(
        self,
        endpoint: str,
        batch_size: int = 200,
        flush_interval_ms: int = 5000,
    ) -> None:
        if not _check_otel():
            raise ImportError(
                "opentelemetry-sdk and opentelemetry-exporter-otlp are required "
                "for OTLP forwarding. Install with: pip install rastir[server]"
            )

        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource

        self._Resource = Resource  # keep class ref for per-span resources
        self._default_resource = Resource.create({"service.name": "rastir-server"})
        self._resource_cache: dict[tuple, Resource] = {}
        self._trace_epoch_cache: dict[str, float] = {}
        self._provider = TracerProvider(resource=self._default_resource)
        raw_exporter = OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces")
        self._exporter = _LoggingExporterWrapper(raw_exporter)
        self._processor = BatchSpanProcessor(
            self._exporter,
            max_queue_size=batch_size * 10,
            max_export_batch_size=batch_size,
            schedule_delay_millis=flush_interval_ms,
        )
        self._provider.add_span_processor(self._processor)
        logger.info("OTLP forwarder initialized → %s", endpoint)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _get_resource(self, service: str, env: str, version: str) -> "Resource":
        """Return a cached Resource for the given service/env/version."""
        key = (service, env, version)
        if key not in self._resource_cache:
            self._resource_cache[key] = self._Resource.create({
                "service.name": service or "unknown",
                "deployment.environment": env or "unknown",
                "service.version": version or "",
                "process.runtime.name": platform.python_implementation(),
                "process.runtime.version": platform.python_version(),
            })
        return self._resource_cache[key]

    def export_span(
        self,
        span_dict: dict,
        service: str,
        env: str,
        version: str,
    ) -> None:
        """Build a ReadableSpan preserving original IDs and enqueue for export."""
        logger.debug(
            "[OTLP] export_span called  name=%s type=%s trace=%s span=%s parent=%s",
            span_dict.get("name"), span_dict.get("span_type"),
            str(span_dict.get("trace_id", ""))[:12],
            str(span_dict.get("span_id", ""))[:12],
            span_dict.get("parent_span_id") or span_dict.get("parent_id"),
        )
        readable = self._dict_to_readable_span(span_dict, service, env, version)
        if readable is not None:
            logger.debug(
                "[OTLP] ReadableSpan built OK → on_end()  trace_id=%s span_id=%s sampled=%s",
                hex(readable.context.trace_id),
                hex(readable.context.span_id),
                readable.context.trace_flags.sampled,
            )
            self._processor.on_end(readable)
            logger.debug("[OTLP] on_end() returned — span enqueued in BatchSpanProcessor")
        else:
            logger.warning("[OTLP] _dict_to_readable_span returned None — span NOT exported")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _dict_to_readable_span(
        self,
        span_dict: dict,
        service: str,
        env: str,
        version: str,
    ) -> Optional["ReadableSpan"]:  # type: ignore[name-defined]
        """Convert an ingested span dict to an OTel ReadableSpan."""
        from opentelemetry.sdk.trace import ReadableSpan
        from opentelemetry.trace import (
            SpanContext,
            SpanKind,
            StatusCode,
            TraceFlags,
        )
        from opentelemetry.trace.status import Status

        raw_trace_id = span_dict.get("trace_id")
        raw_span_id = span_dict.get("span_id")
        logger.debug(
            "[OTLP] _dict_to_readable_span  raw_trace_id=%r raw_span_id=%r",
            raw_trace_id, raw_span_id,
        )
        if not raw_trace_id or not raw_span_id:
            logger.warning("[OTLP] Skipping span — missing trace_id=%r span_id=%r", raw_trace_id, raw_span_id)
            return None

        # Compute start_epoch early — needed for X-Ray-compatible trace IDs.
        now = time.time()
        raw_start = span_dict.get("start_time")
        start_epoch = raw_start if raw_start is not None else now

        # All spans in the same trace must share the same epoch prefix
        # so X-Ray assembles them into one trace.
        if raw_trace_id not in self._trace_epoch_cache:
            # Bound cache size to prevent unbounded growth
            if len(self._trace_epoch_cache) > 10_000:
                self._trace_epoch_cache.clear()
            self._trace_epoch_cache[raw_trace_id] = start_epoch
        trace_epoch = self._trace_epoch_cache[raw_trace_id]

        trace_id = _hex_to_trace_id(raw_trace_id, start_epoch=trace_epoch)
        span_id = _hex_to_span_id(raw_span_id)

        # Parent context — support both "parent_span_id" and "parent_id"
        raw_parent = span_dict.get("parent_span_id") or span_dict.get("parent_id")
        parent_ctx = None
        if raw_parent:
            parent_ctx = SpanContext(
                trace_id=trace_id,
                span_id=_hex_to_span_id(raw_parent),
                is_remote=True,
                trace_flags=TraceFlags(TraceFlags.SAMPLED),
            )

        context = SpanContext(
            trace_id=trace_id,
            span_id=span_id,
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )

        # Attributes
        attrs = span_dict.get("attributes", {})
        otel_attrs: dict = {
            "rastir.service": service or "unknown",
            "rastir.env": env or "unknown",
            "rastir.version": version or "",
            "rastir.span_type": span_dict.get("span_type", "unknown"),
        }
        for k, v in attrs.items():
            if isinstance(v, (str, int, float, bool)):
                otel_attrs[f"rastir.{k}"] = v

        # Timestamps (epoch seconds → nanoseconds)
        # Handle None values explicitly (evaluation spans set start_time=None).
        # Also guard against clock drift (e.g. WSL2) where start > end;
        # prefer computing end from start + duration_ms when available.
        raw_end = span_dict.get("end_time")
        duration_ms = span_dict.get("duration_ms")

        if raw_start is None or raw_end is None:
            logger.debug(
                "[OTLP] timestamp fallback  start=%r end=%r → using now=%f",
                raw_start, raw_end, now,
            )

        end_epoch = raw_end if raw_end is not None else now

        start_ns = int(start_epoch * 1e9)
        end_ns = int(end_epoch * 1e9)

        # Fix reversed timestamps (clock drift / WSL2 skew).
        # When duration_ms is available and positive, use it to derive
        # end from the earlier timestamp.  Otherwise simply swap.
        if start_ns > end_ns:
            if duration_ms is not None and duration_ms > 0:
                # Use the earlier value as start and compute end from duration
                start_ns = min(start_ns, end_ns)
                end_ns = start_ns + int(duration_ms * 1_000_000)
            else:
                start_ns, end_ns = end_ns, start_ns
            logger.debug(
                "[OTLP] clock-drift corrected  start=%d end=%d (duration_ms=%s)",
                start_ns, end_ns, duration_ms,
            )

        # Status
        status_str = span_dict.get("status", "OK")
        if status_str == "ERROR":
            error_msg = span_dict.get("error", {}).get("message", "")
            status = Status(StatusCode.ERROR, error_msg)
        else:
            status = Status(StatusCode.OK)

        name = span_dict.get("name", "unknown")

        # Root spans (no parent) are entry-points → SERVER kind so ADOT
        # creates a proper X-Ray segment with full service metadata.
        # Child spans stay INTERNAL → X-Ray subsegments.
        kind = SpanKind.SERVER if parent_ctx is None else SpanKind.INTERNAL

        # Build the ReadableSpan directly — this preserves all original IDs
        span = ReadableSpan(
            name=name,
            context=context,
            parent=parent_ctx,
            resource=self._get_resource(service, env, version),
            attributes=otel_attrs,
            kind=kind,
            status=status,
            start_time=start_ns,
            end_time=end_ns,
        )
        return span

    def force_flush(self, timeout_ms: int = 10_000) -> None:
        """Force the BatchSpanProcessor to flush pending spans."""
        logger.debug("[OTLP] force_flush called (timeout=%dms)", timeout_ms)
        try:
            self._processor.force_flush(timeout_millis=timeout_ms)
            logger.debug("[OTLP] force_flush completed")
        except Exception:
            logger.error("[OTLP] force_flush FAILED", exc_info=True)

    def shutdown(self, timeout_ms: int = 10_000) -> None:
        """Flush and shut down the OTLP exporter."""
        logger.info("[OTLP] shutdown starting (timeout=%dms)", timeout_ms)
        try:
            self._processor.force_flush(timeout_millis=timeout_ms)
            self._provider.shutdown()
            logger.info("[OTLP] forwarder shut down OK")
        except Exception:
            logger.error("[OTLP] Error shutting down OTLP forwarder", exc_info=True)
