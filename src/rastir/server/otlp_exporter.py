"""Optional OTLP span forwarding.

Converts ingested span dicts into OpenTelemetry SDK ``ReadableSpan``
objects and exports them via the official ``OTLPSpanExporter`` using a
``BatchSpanProcessor``.

The forwarder is only initialised when ``exporter.otlp_endpoint`` is
configured. It is safe to skip — the ingestion worker checks for
``None`` before calling.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

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


class OTLPForwarder:
    """Stateless OTLP span exporter using the OpenTelemetry SDK.

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

        resource = Resource.create({"service.name": "rastir-server"})
        self._provider = TracerProvider(resource=resource)
        self._exporter = OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces")
        self._processor = BatchSpanProcessor(
            self._exporter,
            max_queue_size=batch_size * 10,
            max_export_batch_size=batch_size,
            schedule_delay_millis=flush_interval_ms,
        )
        self._provider.add_span_processor(self._processor)
        self._tracer = self._provider.get_tracer("rastir.forwarder")
        logger.info("OTLP forwarder initialized → %s", endpoint)

    def export_span(
        self,
        span_dict: dict,
        service: str,
        env: str,
        version: str,
    ) -> None:
        """Create an OTel span from a raw span dict and submit for export."""
        from opentelemetry.trace import StatusCode

        name = span_dict.get("name", "unknown")
        attrs = span_dict.get("attributes", {})

        # Build attribute dict for the OTel span
        otel_attrs = {
            "rastir.service": service,
            "rastir.env": env,
            "rastir.version": version,
            "rastir.span_type": span_dict.get("span_type", "unknown"),
        }
        for k, v in attrs.items():
            if isinstance(v, (str, int, float, bool)):
                otel_attrs[f"rastir.{k}"] = v

        span = self._tracer.start_span(name, attributes=otel_attrs)

        status_str = span_dict.get("status", "OK")
        if status_str == "ERROR":
            error_msg = span_dict.get("error", {}).get("message", "")
            span.set_status(StatusCode.ERROR, error_msg)
        else:
            span.set_status(StatusCode.OK)

        span.end()

    def shutdown(self, timeout_ms: int = 10_000) -> None:
        """Flush and shut down the OTLP exporter."""
        try:
            self._processor.force_flush(timeout_millis=timeout_ms)
            self._provider.shutdown()
            logger.info("OTLP forwarder shut down")
        except Exception:
            logger.warning("Error shutting down OTLP forwarder", exc_info=True)
