"""Tests for rastir.decorators module."""

import asyncio
import pytest
from rastir.config import reset_config
from rastir.decorators import trace, agent, metric, llm, retrieval
from rastir.queue import drain_batch, reset_queue
from rastir.spans import SpanStatus, SpanType
from rastir.context import get_current_span, get_current_agent


@pytest.fixture(autouse=True)
def _clean():
    """Reset queue and config before each test."""
    reset_queue()
    reset_config()
    yield
    reset_queue()
    reset_config()


# ---------------------------------------------------------------------------
# @trace
# ---------------------------------------------------------------------------

class TestTrace:
    def test_bare_decorator(self):
        @trace
        def my_func():
            return 42

        result = my_func()
        assert result == 42
        spans = drain_batch(10)
        assert len(spans) == 1
        s = spans[0]
        assert s.name == "my_func"
        assert s.span_type == SpanType.TRACE
        assert s.status == SpanStatus.OK
        assert s.duration_seconds > 0

    def test_with_name(self):
        @trace(name="custom_name")
        def my_func():
            return 1

        my_func()
        spans = drain_batch(10)
        assert spans[0].name == "custom_name"

    def test_emit_metric_flag(self):
        @trace(emit_metric=True)
        def my_func():
            return 1

        my_func()
        spans = drain_batch(10)
        assert spans[0].attributes.get("emit_metric") is True

    def test_exception_records_error(self):
        @trace
        def failing():
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            failing()

        spans = drain_batch(10)
        assert len(spans) == 1
        s = spans[0]
        assert s.status == SpanStatus.ERROR
        assert len(s.events) == 1
        assert s.events[0]["attributes"]["exception.type"] == "ValueError"

    def test_async_trace(self):
        @trace
        async def async_func():
            return "async_result"

        result = asyncio.get_event_loop().run_until_complete(async_func())
        assert result == "async_result"
        spans = drain_batch(10)
        assert len(spans) == 1
        assert spans[0].name == "async_func"
        assert spans[0].status == SpanStatus.OK

    def test_preserves_function_metadata(self):
        @trace
        def documented_func():
            """My docstring."""
            pass

        assert documented_func.__name__ == "documented_func"
        assert documented_func.__doc__ == "My docstring."

    def test_nested_traces(self):
        @trace
        def outer():
            return inner()

        @trace
        def inner():
            return "result"

        outer()
        spans = drain_batch(10)
        assert len(spans) == 2
        # inner enqueued first (LIFO), then outer
        inner_span = spans[0]
        outer_span = spans[1]
        assert inner_span.parent_id == outer_span.span_id
        assert inner_span.trace_id == outer_span.trace_id


# ---------------------------------------------------------------------------
# @agent
# ---------------------------------------------------------------------------

class TestAgent:
    def test_bare_agent(self):
        @agent
        def my_agent():
            return "done"

        result = my_agent()
        assert result == "done"
        spans = drain_batch(10)
        assert len(spans) == 1
        s = spans[0]
        assert s.span_type == SpanType.AGENT
        assert s.attributes["agent_name"] == "my_agent"

    def test_agent_with_name(self):
        @agent(agent_name="research_bot")
        def my_agent():
            return "done"

        my_agent()
        spans = drain_batch(10)
        assert spans[0].attributes["agent_name"] == "research_bot"
        assert spans[0].name == "research_bot"

    def test_agent_sets_context_for_children(self):
        captured_agent = None

        @agent(agent_name="parent_agent")
        def my_agent():
            nonlocal captured_agent
            captured_agent = get_current_agent()
            return "done"

        my_agent()
        assert captured_agent == "parent_agent"
        # Agent context should be cleared after
        assert get_current_agent() is None

    def test_agent_label_injected_into_child_llm(self):
        @agent(agent_name="qa_agent")
        def my_agent():
            return call_llm()

        @llm
        def call_llm():
            return "response"

        my_agent()
        spans = drain_batch(10)
        llm_span = [s for s in spans if s.span_type == SpanType.LLM][0]
        assert llm_span.attributes.get("agent") == "qa_agent"

    def test_no_agent_label_under_plain_trace(self):
        @trace
        def my_trace():
            return call_llm()

        @llm
        def call_llm():
            return "response"

        my_trace()
        spans = drain_batch(10)
        llm_span = [s for s in spans if s.span_type == SpanType.LLM][0]
        assert "agent" not in llm_span.attributes

    def test_async_agent(self):
        @agent(agent_name="async_agent")
        async def my_agent():
            return "async_done"

        result = asyncio.get_event_loop().run_until_complete(my_agent())
        assert result == "async_done"
        spans = drain_batch(10)
        assert spans[0].span_type == SpanType.AGENT


# ---------------------------------------------------------------------------
# @metric
# ---------------------------------------------------------------------------

class TestMetric:
    def test_bare_metric(self):
        @metric
        def compute():
            return 100

        result = compute()
        assert result == 100
        spans = drain_batch(10)
        assert len(spans) == 1
        s = spans[0]
        assert s.span_type == SpanType.METRIC
        assert s.attributes["metric_name"] == "compute"

    def test_metric_with_name(self):
        @metric(name="custom_metric")
        def compute():
            return 1

        compute()
        spans = drain_batch(10)
        assert spans[0].attributes["metric_name"] == "custom_metric"

    def test_metric_failure(self):
        @metric
        def fail_func():
            raise RuntimeError("oops")

        with pytest.raises(RuntimeError):
            fail_func()

        spans = drain_batch(10)
        assert spans[0].status == SpanStatus.ERROR

    def test_metric_independent_from_trace(self):
        """@metric and @trace can be stacked independently."""
        @trace
        @metric
        def double_observed():
            return "ok"

        double_observed()
        spans = drain_batch(10)
        assert len(spans) == 2
        types = {s.span_type for s in spans}
        assert SpanType.METRIC in types
        assert SpanType.TRACE in types


# ---------------------------------------------------------------------------
# @llm
# ---------------------------------------------------------------------------

class TestLLM:
    def test_bare_llm(self):
        @llm
        def call_openai():
            return "Hello world"

        result = call_openai()
        assert result == "Hello world"
        spans = drain_batch(10)
        assert len(spans) == 1
        s = spans[0]
        assert s.span_type == SpanType.LLM
        # Fallback adapter should set unknown
        assert s.attributes.get("model") == "unknown"
        assert s.attributes.get("provider") == "unknown"

    def test_llm_with_overrides(self):
        @llm(model="gpt-4", provider="openai")
        def call_openai():
            return "response"

        call_openai()
        spans = drain_batch(10)
        s = spans[0]
        assert s.attributes["model"] == "gpt-4"
        assert s.attributes["provider"] == "openai"

    def test_llm_error(self):
        @llm
        def bad_call():
            raise ConnectionError("timeout")

        with pytest.raises(ConnectionError):
            bad_call()

        spans = drain_batch(10)
        assert spans[0].status == SpanStatus.ERROR

    def test_async_llm(self):
        @llm(model="claude-3")
        async def async_call():
            return "async response"

        result = asyncio.get_event_loop().run_until_complete(async_call())
        assert result == "async response"
        spans = drain_batch(10)
        assert spans[0].attributes["model"] == "claude-3"

    def test_llm_streaming_sync(self):
        @llm
        def stream_call():
            yield "chunk1"
            yield "chunk2"
            yield "chunk3"

        chunks = list(stream_call())
        assert chunks == ["chunk1", "chunk2", "chunk3"]
        spans = drain_batch(10)
        assert len(spans) == 1
        assert spans[0].span_type == SpanType.LLM
        assert spans[0].status == SpanStatus.OK

    def test_llm_streaming_async(self):
        @llm
        async def async_stream():
            yield "a"
            yield "b"

        async def consume():
            result = []
            async for chunk in async_stream():
                result.append(chunk)
            return result

        chunks = asyncio.get_event_loop().run_until_complete(consume())
        assert chunks == ["a", "b"]
        spans = drain_batch(10)
        assert len(spans) == 1
        assert spans[0].span_type == SpanType.LLM

    def test_llm_agent_label(self):
        @agent(agent_name="my_agent")
        def run():
            return do_llm()

        @llm
        def do_llm():
            return "ok"

        run()
        spans = drain_batch(10)
        llm_span = [s for s in spans if s.span_type == SpanType.LLM][0]
        assert llm_span.attributes["agent"] == "my_agent"


# ---------------------------------------------------------------------------
# @retrieval
# ---------------------------------------------------------------------------

class TestRetrieval:
    def test_bare_retrieval_with_list(self):
        @retrieval
        def search_docs(q):
            return ["doc1", "doc2", "doc3"]

        result = search_docs("query")
        assert len(result) == 3
        spans = drain_batch(10)
        assert len(spans) == 1
        s = spans[0]
        assert s.span_type == SpanType.RETRIEVAL
        assert s.attributes["retrieved_documents_count"] == 3

    def test_retrieval_with_custom_extractor(self):
        class SearchResult:
            def __init__(self):
                self.hits = [1, 2, 3, 4, 5]

        @retrieval(doc_count_extractor=lambda r: len(r.hits))
        def search(q):
            return SearchResult()

        search("test")
        spans = drain_batch(10)
        assert spans[0].attributes["retrieved_documents_count"] == 5

    def test_retrieval_with_documents_attr(self):
        class VectorResult:
            documents = ["a", "b"]

        @retrieval
        def vector_search(q):
            return VectorResult()

        vector_search("test")
        spans = drain_batch(10)
        assert spans[0].attributes["retrieved_documents_count"] == 2

    def test_retrieval_unknown_return_type(self):
        @retrieval
        def search(q):
            return 42  # not a list, no .documents

        search("test")
        spans = drain_batch(10)
        assert "retrieved_documents_count" not in spans[0].attributes

    def test_retrieval_agent_label(self):
        @agent(agent_name="rag_agent")
        def run():
            return search()

        @retrieval
        def search():
            return ["doc"]

        run()
        spans = drain_batch(10)
        ret_span = [s for s in spans if s.span_type == SpanType.RETRIEVAL][0]
        assert ret_span.attributes["agent"] == "rag_agent"

    def test_retrieval_failure(self):
        @retrieval
        def bad_search():
            raise IOError("connection lost")

        with pytest.raises(IOError):
            bad_search()

        spans = drain_batch(10)
        assert spans[0].status == SpanStatus.ERROR

    def test_async_retrieval(self):
        @retrieval
        async def async_search():
            return ["doc1", "doc2"]

        result = asyncio.get_event_loop().run_until_complete(async_search())
        assert result == ["doc1", "doc2"]
        spans = drain_batch(10)
        assert spans[0].attributes["retrieved_documents_count"] == 2


# ---------------------------------------------------------------------------
# Integration: full hierarchy
# ---------------------------------------------------------------------------

class TestFullHierarchy:
    def test_trace_agent_llm_hierarchy(self):
        """Full trace → agent → llm hierarchy."""

        @trace(name="api_request")
        def handle_request():
            return run_agent("query")

        @agent(agent_name="qa_agent")
        def run_agent(q):
            answer = do_llm(q)
            return answer

        @llm(model="gpt-4", provider="openai")
        def do_llm(context):
            return "answer"

        result = handle_request()
        assert result == "answer"

        spans = drain_batch(10)
        assert len(spans) == 3

        # Find by type
        trace_s = [s for s in spans if s.span_type == SpanType.TRACE][0]
        agent_s = [s for s in spans if s.span_type == SpanType.AGENT][0]
        llm_s = [s for s in spans if s.span_type == SpanType.LLM][0]

        # All share the same trace_id
        trace_ids = {s.trace_id for s in spans}
        assert len(trace_ids) == 1

        # Parent-child relationships
        assert agent_s.parent_id == trace_s.span_id
        assert llm_s.parent_id == agent_s.span_id

        # Agent label on children
        assert llm_s.attributes["agent"] == "qa_agent"
        assert "agent" not in trace_s.attributes

        # LLM attributes
        assert llm_s.attributes["model"] == "gpt-4"
        assert llm_s.attributes["provider"] == "openai"

    def test_all_spans_have_duration(self):
        @trace
        def root():
            return child()

        @llm
        def child():
            return "ok"

        root()
        spans = drain_batch(10)
        for s in spans:
            assert s.duration_seconds > 0
