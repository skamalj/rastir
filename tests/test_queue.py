"""Tests for rastir.queue module."""

import pytest
from rastir.queue import enqueue_span, drain_batch, queue_size, reset_queue
from rastir.spans import SpanRecord, SpanType


@pytest.fixture(autouse=True)
def _clean_queue():
    reset_queue()
    yield
    reset_queue()


class TestSpanQueue:
    def test_enqueue_and_drain(self):
        span = SpanRecord(name="test", span_type=SpanType.LLM)
        span.finish()
        enqueue_span(span)
        assert queue_size() == 1

        batch = drain_batch(10)
        assert len(batch) == 1
        assert batch[0] is span
        assert queue_size() == 0

    def test_drain_respects_max_size(self):
        for i in range(5):
            s = SpanRecord(name=f"span_{i}", span_type=SpanType.TOOL)
            s.finish()
            enqueue_span(s)

        batch = drain_batch(3)
        assert len(batch) == 3
        assert queue_size() == 2

    def test_drain_empty_queue(self):
        batch = drain_batch(10)
        assert batch == []

    def test_queue_overflow_drops_span(self):
        reset_queue(max_size=2)
        s1 = SpanRecord(name="s1", span_type=SpanType.LLM)
        s2 = SpanRecord(name="s2", span_type=SpanType.LLM)
        s3 = SpanRecord(name="s3", span_type=SpanType.LLM)

        enqueue_span(s1)
        enqueue_span(s2)
        enqueue_span(s3)  # should be dropped

        assert queue_size() == 2
        batch = drain_batch(10)
        names = [s.name for s in batch]
        assert names == ["s1", "s2"]
