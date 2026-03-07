"""Span data model for Rastir.

Defines the SpanRecord dataclass and SpanStatus enum used throughout the
library. Spans are the core unit of telemetry — decorators create them,
the exporter serializes and pushes them to the collector server.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class SpanStatus(str, Enum):
    """Status of a completed span."""

    OK = "OK"
    ERROR = "ERROR"


# Per-trace time anchor: maps trace_id → (wall_time, mono_time).
# Root spans register an anchor; child spans derive start_time from it
# so that all spans in a trace are immune to WSL2/NTP clock drift.
_trace_time_anchor: dict[str, tuple[float, float]] = {}


class SpanType(str, Enum):
    """Semantic type of a span, determines metric derivation on the server."""

    TRACE = "trace"
    AGENT = "agent"
    LLM = "llm"
    TOOL = "tool"
    RETRIEVAL = "retrieval"
    METRIC = "metric"
    SYSTEM = "system"
    INFRA = "infra"
    EVALUATION = "evaluation"


@dataclass
class SpanRecord:
    """Mutable span record populated during function execution.

    Created at function entry (start_time set), completed at function exit
    (end_time, status, attributes finalized). Then enqueued for export.
    """

    name: str
    span_type: SpanType
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    parent_id: Optional[str] = None
    start_time: float = field(default_factory=time.time)
    end_time: Optional[float] = None
    status: SpanStatus = SpanStatus.OK
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    _mono_start: float = field(default_factory=time.monotonic, repr=False)

    def __post_init__(self) -> None:
        """Anchor start_time to the trace's root wall clock via monotonic offset.

        Prevents WSL2/NTP clock drift from placing child spans far ahead
        of (or behind) their parent.
        """
        if self.parent_id is None:
            # Root span — register its (wall, mono) pair as the anchor
            if len(_trace_time_anchor) > 10_000:
                _trace_time_anchor.clear()
            _trace_time_anchor[self.trace_id] = (self.start_time, self._mono_start)
        else:
            # Child span — derive start_time from the root's anchor
            anchor = _trace_time_anchor.get(self.trace_id)
            if anchor is not None:
                wall_anchor, mono_anchor = anchor
                self.start_time = wall_anchor + (self._mono_start - mono_anchor)

    def _reanchor(self) -> None:
        """Re-derive start_time from the trace anchor.

        Call after mutating trace_id/parent_id post-construction
        (e.g. in @mcp_endpoint where the span is created as a root
        then reparented to the client trace).
        """
        if self.parent_id is not None:
            anchor = _trace_time_anchor.get(self.trace_id)
            if anchor is not None:
                wall_anchor, mono_anchor = anchor
                self.start_time = wall_anchor + (self._mono_start - mono_anchor)

    @property
    def duration_seconds(self) -> float:
        """Elapsed time in seconds. Returns 0 if span is still open."""
        if self.end_time is None:
            return 0.0
        return self.end_time - self.start_time

    def finish(self, status: SpanStatus = SpanStatus.OK) -> None:
        """Mark the span as completed.

        Uses monotonic clock for duration to avoid WSL2/NTP clock drift,
        then derives end_time from start_time + duration.
        """
        elapsed = time.monotonic() - self._mono_start
        self.end_time = self.start_time + elapsed
        self.status = status

    def record_error(self, error: BaseException) -> None:
        """Record an exception as a span event and set ERROR status."""
        self.status = SpanStatus.ERROR
        error_type = type(error).__qualname__
        error_message = str(error)
        self.events.append(
            {
                "name": "exception",
                "attributes": {
                    "exception.type": error_type,
                    "exception.message": error_message,
                },
                "timestamp": time.time(),
            }
        )
        # Also store as span attributes so trace viewers can display them
        self.attributes["error.type"] = error_type
        self.attributes["error.message"] = error_message

    def set_attribute(self, key: str, value: Any) -> None:
        """Set a single span attribute."""
        self.attributes[key] = value

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dict matching the POST /v1/telemetry payload schema."""
        return {
            "type": "span",
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_id,
            "span_type": self.span_type.value,
            "name": self.name,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_ms": self.duration_seconds * 1000.0,
            "status": self.status.value,
            "attributes": dict(self.attributes),
            "events": list(self.events),
        }
