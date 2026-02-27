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


class SpanType(str, Enum):
    """Semantic type of a span, determines metric derivation on the server."""

    TRACE = "trace"
    AGENT = "agent"
    LLM = "llm"
    TOOL = "tool"
    RETRIEVAL = "retrieval"
    METRIC = "metric"
    SYSTEM = "system"


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

    @property
    def duration_seconds(self) -> float:
        """Elapsed time in seconds. Returns 0 if span is still open."""
        if self.end_time is None:
            return 0.0
        return self.end_time - self.start_time

    def finish(self, status: SpanStatus = SpanStatus.OK) -> None:
        """Mark the span as completed."""
        self.end_time = time.time()
        self.status = status

    def record_error(self, error: BaseException) -> None:
        """Record an exception as a span event and set ERROR status."""
        self.status = SpanStatus.ERROR
        self.events.append(
            {
                "name": "exception",
                "attributes": {
                    "exception.type": type(error).__qualname__,
                    "exception.message": str(error),
                },
                "timestamp": time.time(),
            }
        )

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
