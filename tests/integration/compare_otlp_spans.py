"""Compare the ReadableSpan that the server builds vs a standard OTel span.

This sends a span through normal OTel SDK (which works) and prints the
ReadableSpan details, so we can compare with what the server builds.
"""

import time
import uuid
import json

from opentelemetry.sdk.trace import TracerProvider, ReadableSpan
from opentelemetry.sdk.trace.export import (
    SpanExporter,
    SpanExportResult,
    BatchSpanProcessor,
    SimpleSpanProcessor,
)
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.trace import SpanContext, SpanKind, TraceFlags
from opentelemetry.trace.status import Status, StatusCode


def _dump_span(label: str, s: ReadableSpan):
    """Print all relevant fields of a ReadableSpan."""
    ctx = s.context
    print(f"\n{'='*70}")
    print(f"  {label}")
    print(f"{'='*70}")
    print(f"  name:       {s.name}")
    print(f"  trace_id:   {hex(ctx.trace_id)}  ({ctx.trace_id.bit_length()} bits)")
    print(f"  span_id:    {hex(ctx.span_id)}  ({ctx.span_id.bit_length()} bits)")
    print(f"  trace_flags:{ctx.trace_flags}  sampled={ctx.trace_flags.sampled}")
    print(f"  is_remote:  {ctx.is_remote}")
    if s.parent:
        print(f"  parent_id:  {hex(s.parent.span_id)}  ({s.parent.span_id.bit_length()} bits)")
    else:
        print(f"  parent_id:  None")
    print(f"  start_time: {s.start_time}")
    print(f"  end_time:   {s.end_time}")
    print(f"  kind:       {s.kind}")
    print(f"  status:     {s.status}")
    print(f"  resource:   {dict(s.resource.attributes)}")
    print(f"  attributes: {dict(s.attributes or {})}")


class CapturingExporter(SpanExporter):
    """Captures spans for inspection instead of exporting."""
    def __init__(self):
        self.spans = []
    def export(self, spans):
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS
    def shutdown(self):
        pass


# ── 1. Normal OTel SDK span (the "manual" reference) ──────────────────

print("\n" + "█" * 70)
print("  METHOD 1: Normal OTel SDK — Tracer.start_as_current_span()")
print("█" * 70)

resource = Resource.create({
    "service.name": "manual-test-svc",
    "deployment.environment": "test",
    "service.version": "1.0.0",
})

capturer = CapturingExporter()
provider = TracerProvider(resource=resource)
provider.add_span_processor(SimpleSpanProcessor(capturer))
tracer = provider.get_tracer("manual-test")

with tracer.start_as_current_span("manual_span") as span:
    span.set_attribute("rastir.model", "test-model")
    span.set_attribute("rastir.provider", "test")

provider.force_flush()
if capturer.spans:
    _dump_span("Normal OTel SDK Span", capturer.spans[0])
    normal_span = capturer.spans[0]

# ── 2. Server-style ReadableSpan (what OTLPForwarder builds) ──────────

print("\n" + "█" * 70)
print("  METHOD 2: Rastir server style — ReadableSpan() direct construction")
print("█" * 70)

# Simulate what the client sends: uuid4().hex for both trace_id and span_id
raw_trace_id = uuid.uuid4().hex  # 32 hex chars
raw_span_id = uuid.uuid4().hex   # 32 hex chars (the problem!)
raw_parent_id = uuid.uuid4().hex  # 32 hex chars

print(f"\n  Client IDs (as sent to server):")
print(f"    trace_id:  {raw_trace_id}  ({len(raw_trace_id)} chars)")
print(f"    span_id:   {raw_span_id}  ({len(raw_span_id)} chars)")
print(f"    parent_id: {raw_parent_id}  ({len(raw_parent_id)} chars)")

trace_id_int = int(raw_trace_id, 16)
span_id_int = int(raw_span_id, 16)
parent_id_int = int(raw_parent_id, 16)

print(f"\n  As integers:")
print(f"    trace_id:  {hex(trace_id_int)}  ({trace_id_int.bit_length()} bits)")
print(f"    span_id:   {hex(span_id_int)}  ({span_id_int.bit_length()} bits)")
print(f"    parent_id: {hex(parent_id_int)}  ({parent_id_int.bit_length()} bits)")

server_resource = Resource.create({
    "service.name": "server-test-svc",
    "deployment.environment": "test",
    "service.version": "1.0.0",
})

now = time.time()
start_ns = int((now - 3) * 1e9)
end_ns = int(now * 1e9)

context = SpanContext(
    trace_id=trace_id_int,
    span_id=span_id_int,
    is_remote=False,
    trace_flags=TraceFlags(TraceFlags.SAMPLED),
)

parent_ctx = SpanContext(
    trace_id=trace_id_int,
    span_id=parent_id_int,
    is_remote=True,
    trace_flags=TraceFlags(TraceFlags.SAMPLED),
)

server_span = ReadableSpan(
    name="server_built_span",
    context=context,
    parent=parent_ctx,
    resource=server_resource,
    attributes={"rastir.model": "test-model", "rastir.provider": "test"},
    kind=SpanKind.INTERNAL,
    status=Status(StatusCode.OK),
    start_time=start_ns,
    end_time=end_ns,
)

_dump_span("Server-built ReadableSpan", server_span)

# ── 3. KEY COMPARISON ────────────────────────────────────────────────

print("\n" + "█" * 70)
print("  COMPARISON")
print("█" * 70)

print(f"\n  Normal span_id bits:  {normal_span.context.span_id.bit_length()}")
print(f"  Server span_id bits:  {server_span.context.span_id.bit_length()}")
print(f"  OTel spec max:        64 bits for span_id, 128 bits for trace_id")
print()

if server_span.context.span_id.bit_length() > 64:
    print("  ⚠ SERVER SPAN_ID EXCEEDS 64 BITS!")
    print("    The client uses uuid4().hex (32 chars = 128 bits) for span_id.")
    print("    OTel spec requires span_id to be at most 64 bits (16 hex chars).")
    print("    This causes OTLP serialization to silently drop/corrupt the span.")
    print()
    print("  FIX: Truncate span_id to 16 hex chars before int() conversion:")
    print("    span_id = int(raw_span_id[:16], 16)")

# ── 4. Actually try exporting both to Tempo ──────────────────────────

print("\n" + "█" * 70)
print("  ACTUAL EXPORT TEST TO TEMPO")
print("█" * 70)

exporter = OTLPSpanExporter(endpoint="http://localhost:4318/v1/traces")

# Export the normal span
result1 = exporter.export([normal_span])
print(f"\n  Normal span export result: {result1}")

# Export the server-built span
result2 = exporter.export([server_span])
print(f"  Server span export result: {result2}")

# Now try with truncated span_id (the fix)
fixed_span_id = int(raw_span_id[:16], 16)
fixed_parent_id = int(raw_parent_id[:16], 16)

fixed_context = SpanContext(
    trace_id=trace_id_int,  # 128-bit is OK for trace_id
    span_id=fixed_span_id,  # 64-bit
    is_remote=False,
    trace_flags=TraceFlags(TraceFlags.SAMPLED),
)
fixed_parent_ctx = SpanContext(
    trace_id=trace_id_int,
    span_id=fixed_parent_id,
    is_remote=True,
    trace_flags=TraceFlags(TraceFlags.SAMPLED),
)

fixed_span = ReadableSpan(
    name="fixed_server_span",
    context=fixed_context,
    parent=fixed_parent_ctx,
    resource=server_resource,
    attributes={"rastir.model": "test-model", "rastir.provider": "test"},
    kind=SpanKind.INTERNAL,
    status=Status(StatusCode.OK),
    start_time=start_ns,
    end_time=end_ns,
)

_dump_span("Fixed Server Span (span_id truncated to 64 bits)", fixed_span)
result3 = exporter.export([fixed_span])
print(f"\n  Fixed span export result: {result3}")

exporter.shutdown()

print("\n  Done. Check Tempo for traces.")
