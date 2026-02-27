"""In-memory trace ring buffer.

Stores recent traces (grouped by ``trace_id``) in a bounded data
structure.  When the buffer exceeds ``max_traces``, the oldest traces
are evicted in FIFO order.

V2 additions:
- ``max_spans_per_trace`` — cap per-trace span list.
- ``ttl_seconds``         — optional TTL-based expiration.

Thread-/async-safe: all mutations are protected by a ``threading.Lock``
so that the asyncio ingestion worker and the query endpoint can operate
concurrently without corruption.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Optional

logger = logging.getLogger("rastir.server")


class TraceStore:
    """Bounded in-memory trace store backed by an ``OrderedDict``.

    Traces are keyed by ``trace_id``.  Each value is a list of span
    dicts belonging to that trace.  Insertion order determines eviction
    priority (FIFO — oldest trace first).
    """

    def __init__(
        self,
        max_traces: int = 10_000,
        max_spans_per_trace: int = 500,
        ttl_seconds: int = 0,
    ) -> None:
        self._max_traces = max_traces
        self._max_spans_per_trace = max_spans_per_trace
        self._ttl = ttl_seconds  # 0 = disabled
        # OrderedDict preserves insertion order for FIFO eviction
        self._traces: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
        self._timestamps: dict[str, float] = {}  # trace_id → last-update epoch
        self._lock = threading.Lock()
        self._total_spans: int = 0
        self._evicted_traces: int = 0
        self._spans_truncated: int = 0

    # ----- mutators --------------------------------------------------------

    def insert(self, trace_id: str, spans: list[dict[str, Any]]) -> None:
        """Insert or append spans for a given trace.

        If adding this trace would exceed ``max_traces`` and the
        trace_id is new, the oldest trace is evicted first.
        Per-trace span count is capped at ``max_spans_per_trace``.
        """
        with self._lock:
            # Expire stale traces if TTL is configured
            if self._ttl > 0:
                self._expire_stale()

            now = time.monotonic()
            if trace_id in self._traces:
                existing = self._traces[trace_id]
                room = self._max_spans_per_trace - len(existing)
                if room <= 0:
                    self._spans_truncated += len(spans)
                    self._traces.move_to_end(trace_id)
                    self._timestamps[trace_id] = now
                    return
                accepted = spans[:room]
                if len(spans) > room:
                    self._spans_truncated += len(spans) - room
                existing.extend(accepted)
                self._total_spans += len(accepted)
                self._traces.move_to_end(trace_id)
            else:
                # Evict oldest if at capacity
                while len(self._traces) >= self._max_traces:
                    evicted_id, evicted_spans = self._traces.popitem(last=False)
                    self._total_spans -= len(evicted_spans)
                    self._timestamps.pop(evicted_id, None)
                    self._evicted_traces += 1
                accepted = spans[: self._max_spans_per_trace]
                if len(spans) > self._max_spans_per_trace:
                    self._spans_truncated += len(spans) - self._max_spans_per_trace
                self._traces[trace_id] = list(accepted)
                self._total_spans += len(accepted)

            self._timestamps[trace_id] = now

    def _expire_stale(self) -> None:
        """Remove traces older than TTL. Must be called under _lock."""
        if self._ttl <= 0:
            return
        cutoff = time.monotonic() - self._ttl
        expired_ids = [
            tid for tid, ts in self._timestamps.items() if ts < cutoff
        ]
        for tid in expired_ids:
            spans = self._traces.pop(tid, None)
            if spans is not None:
                self._total_spans -= len(spans)
                self._evicted_traces += 1
            self._timestamps.pop(tid, None)

    # ----- queries ---------------------------------------------------------

    def get(self, trace_id: str) -> Optional[list[dict[str, Any]]]:
        """Return spans for a trace, or ``None`` if not found."""
        with self._lock:
            spans = self._traces.get(trace_id)
            if spans is not None:
                return list(spans)  # defensive copy
            return None

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return the most recent *limit* trace summaries (id + span count)."""
        with self._lock:
            if self._ttl > 0:
                self._expire_stale()
            result = []
            for trace_id in reversed(self._traces):
                result.append({
                    "trace_id": trace_id,
                    "span_count": len(self._traces[trace_id]),
                })
                if len(result) >= limit:
                    break
            return result

    def search(self, service: Optional[str] = None, limit: int = 20) -> list[dict[str, Any]]:
        """Search traces, optionally filtering by service name.

        Returns summaries (trace_id, span_count) of matching traces.
        """
        with self._lock:
            if self._ttl > 0:
                self._expire_stale()
            result = []
            for trace_id in reversed(self._traces):
                spans = self._traces[trace_id]
                if service is not None:
                    # Check if any span in the trace has the matching service
                    match = any(
                        s.get("attributes", {}).get("service") == service
                        or s.get("service") == service
                        for s in spans
                    )
                    if not match:
                        continue
                result.append({
                    "trace_id": trace_id,
                    "span_count": len(spans),
                })
                if len(result) >= limit:
                    break
            return result

    # ----- stats -----------------------------------------------------------

    @property
    def trace_count(self) -> int:
        with self._lock:
            return len(self._traces)

    @property
    def span_count(self) -> int:
        with self._lock:
            return self._total_spans

    @property
    def evicted_traces(self) -> int:
        return self._evicted_traces

    @property
    def spans_truncated(self) -> int:
        """Number of spans dropped due to per-trace cap."""
        return self._spans_truncated

    def clear(self) -> None:
        """Remove all traces. For testing."""
        with self._lock:
            self._traces.clear()
            self._timestamps.clear()
            self._total_spans = 0
