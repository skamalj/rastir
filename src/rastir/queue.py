"""Internal span queue — bridge between decorators and the exporter.

Decorators enqueue completed spans here. The exporter drains this queue
in batches and pushes to the collector server. This separation ensures
decorators never perform network I/O.
"""

from __future__ import annotations

import logging
import queue
from typing import Optional

from rastir.spans import SpanRecord

logger = logging.getLogger("rastir")

# Bounded in-memory queue. If full, oldest spans are dropped (not blocked).
_DEFAULT_MAX_SIZE = 10_000
_span_queue: queue.Queue[SpanRecord] = queue.Queue(maxsize=_DEFAULT_MAX_SIZE)


def enqueue_span(span: SpanRecord) -> None:
    """Enqueue a completed span for export.

    If the queue is full, the span is dropped with a warning log.
    This ensures decorators never block on a full queue.
    """
    try:
        _span_queue.put_nowait(span)
    except queue.Full:
        logger.warning(
            "Span queue full (%d), dropping span: %s",
            _span_queue.maxsize,
            span.name,
        )


def drain_batch(max_size: int) -> list[SpanRecord]:
    """Drain up to max_size spans from the queue.

    Non-blocking — returns whatever is available up to the limit.
    """
    batch: list[SpanRecord] = []
    for _ in range(max_size):
        try:
            batch.append(_span_queue.get_nowait())
        except queue.Empty:
            break
    return batch


def queue_size() -> int:
    """Return the current number of spans in the queue."""
    return _span_queue.qsize()


def reset_queue(max_size: Optional[int] = None) -> None:
    """Reset the span queue. Intended for testing only."""
    global _span_queue
    _span_queue = queue.Queue(maxsize=max_size or _DEFAULT_MAX_SIZE)
