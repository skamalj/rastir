"""Tests for rastir.spans and rastir.context modules."""

import time
import pytest
from rastir.spans import SpanRecord, SpanStatus, SpanType
from rastir.context import (
    start_span,
    end_span,
    get_current_span,
    get_current_agent,
    set_current_agent,
    reset_current_agent,
)


class TestSpanRecord:
    def test_default_values(self):
        span = SpanRecord(name="test", span_type=SpanType.TRACE)
        assert span.name == "test"
        assert span.span_type == SpanType.TRACE
        assert span.status == SpanStatus.OK
        assert span.end_time is None
        assert span.parent_id is None
        assert len(span.trace_id) == 32  # hex uuid
        assert len(span.span_id) == 32
        assert span.duration_seconds == 0.0

    def test_finish(self):
        span = SpanRecord(name="test", span_type=SpanType.LLM)
        time.sleep(0.01)
        span.finish()
        assert span.end_time is not None
        assert span.duration_seconds > 0
        assert span.status == SpanStatus.OK

    def test_finish_with_error_status(self):
        span = SpanRecord(name="test", span_type=SpanType.TOOL)
        span.finish(status=SpanStatus.ERROR)
        assert span.status == SpanStatus.ERROR

    def test_record_error(self):
        span = SpanRecord(name="test", span_type=SpanType.LLM)
        try:
            raise ValueError("something went wrong")
        except ValueError as e:
            span.record_error(e)

        assert span.status == SpanStatus.ERROR
        assert len(span.events) == 1
        event = span.events[0]
        assert event["name"] == "exception"
        assert event["attributes"]["exception.type"] == "ValueError"
        assert event["attributes"]["exception.message"] == "something went wrong"

    def test_set_attribute(self):
        span = SpanRecord(name="test", span_type=SpanType.LLM)
        span.set_attribute("model", "gpt-4")
        span.set_attribute("tokens_input", 150)
        assert span.attributes["model"] == "gpt-4"
        assert span.attributes["tokens_input"] == 150

    def test_to_dict(self):
        span = SpanRecord(name="my_func", span_type=SpanType.AGENT)
        span.set_attribute("agent_name", "qa")
        span.finish()
        d = span.to_dict()
        assert d["type"] == "span"
        assert d["name"] == "my_func"
        assert d["span_type"] == "agent"
        assert d["status"] == "OK"
        assert d["attributes"]["agent_name"] == "qa"
        assert d["end_time"] is not None


class TestContext:
    def test_no_active_span_by_default(self):
        assert get_current_span() is None

    def test_start_and_end_span(self):
        span, token = start_span("root", SpanType.TRACE)
        assert get_current_span() is span
        assert span.parent_id is None

        end_span(token)
        assert get_current_span() is None

    def test_nested_spans_parent_child(self):
        root, root_token = start_span("root", SpanType.TRACE)
        child, child_token = start_span("child", SpanType.LLM)

        # Child inherits trace_id and links to parent
        assert child.trace_id == root.trace_id
        assert child.parent_id == root.span_id
        assert get_current_span() is child

        end_span(child_token)
        assert get_current_span() is root

        end_span(root_token)
        assert get_current_span() is None

    def test_deeply_nested_spans(self):
        s1, t1 = start_span("trace", SpanType.TRACE)
        s2, t2 = start_span("agent", SpanType.AGENT)
        s3, t3 = start_span("llm", SpanType.LLM)

        assert s3.trace_id == s1.trace_id
        assert s3.parent_id == s2.span_id
        assert s2.parent_id == s1.span_id

        end_span(t3)
        assert get_current_span() is s2
        end_span(t2)
        assert get_current_span() is s1
        end_span(t1)
        assert get_current_span() is None

    def test_root_span_gets_new_trace_id(self):
        s1, t1 = start_span("first", SpanType.TRACE)
        end_span(t1)

        s2, t2 = start_span("second", SpanType.TRACE)
        end_span(t2)

        assert s1.trace_id != s2.trace_id


class TestAgentContext:
    def test_no_agent_by_default(self):
        assert get_current_agent() is None

    def test_set_and_reset_agent(self):
        token = set_current_agent("research_agent")
        assert get_current_agent() == "research_agent"

        reset_current_agent(token)
        assert get_current_agent() is None

    def test_nested_agents(self):
        t1 = set_current_agent("outer_agent")
        assert get_current_agent() == "outer_agent"

        t2 = set_current_agent("inner_agent")
        assert get_current_agent() == "inner_agent"

        reset_current_agent(t2)
        assert get_current_agent() == "outer_agent"

        reset_current_agent(t1)
        assert get_current_agent() is None
