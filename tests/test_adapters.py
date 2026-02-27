"""Adapter tests — covers all V1 adapters and the resolution pipeline.

Uses mock objects that mimic SDK response structures to avoid
requiring actual SDK installations in CI.
"""

from __future__ import annotations

import pytest

from rastir.adapters.registry import (
    clear_registry,
    get_registered_adapters,
    register,
    resolve,
    resolve_stream_chunk,
)
from rastir.adapters.types import AdapterResult, TokenDelta

# ========================================================================
# Mock SDK response objects
# ========================================================================
# We use `type()` to dynamically create classes with the exact __name__
# and __module__ that the adapters check via `type(result).__name__` and
# `type(result).__module__`.


class _Usage:
    """Shared usage/token mock."""

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


# ---- OpenAI ----

class _OpenAIChoice:
    def __init__(self, finish_reason="stop"):
        self.finish_reason = finish_reason


def _make_openai_chat_completion(model="gpt-4o", prompt_tokens=10,
                                  completion_tokens=20, finish_reason="stop"):
    """Create a mock ChatCompletion with correct type name/module."""
    cls = type("ChatCompletion", (), {"__module__": "openai.types.chat.chat_completion"})
    obj = cls.__new__(cls)
    obj.model = model
    obj.usage = _Usage(prompt_tokens=prompt_tokens,
                       completion_tokens=completion_tokens,
                       total_tokens=prompt_tokens + completion_tokens)
    obj.choices = [_OpenAIChoice(finish_reason)]
    return obj


def _make_openai_chunk(model="gpt-4o", prompt_tokens=None, completion_tokens=None):
    """Create a mock ChatCompletionChunk with correct type name/module."""
    cls = type("ChatCompletionChunk", (), {
        "__module__": "openai.types.chat.chat_completion_chunk"})
    obj = cls.__new__(cls)
    obj.model = model
    if prompt_tokens is not None or completion_tokens is not None:
        obj.usage = _Usage(prompt_tokens=prompt_tokens,
                           completion_tokens=completion_tokens)
    else:
        obj.usage = None
    return obj


# ---- Anthropic ----

def _make_anthropic_message(model="claude-3-5-sonnet", input_tokens=15,
                             output_tokens=25, stop_reason="end_turn"):
    cls = type("Message", (), {"__module__": "anthropic.types"})
    obj = cls.__new__(cls)
    obj.model = model
    obj.usage = _Usage(input_tokens=input_tokens, output_tokens=output_tokens)
    obj.stop_reason = stop_reason
    return obj


def _make_anthropic_raw_message_start(model="claude-3-5-sonnet", input_tokens=15):
    cls = type("RawMessageStartEvent", (), {"__module__": "anthropic.types"})
    obj = cls.__new__(cls)
    obj.message = _make_anthropic_message(model=model, input_tokens=input_tokens,
                                           output_tokens=0)
    return obj


def _make_anthropic_raw_message_delta(output_tokens=25):
    cls = type("RawMessageDeltaEvent", (), {"__module__": "anthropic.types"})
    obj = cls.__new__(cls)
    obj.usage = _Usage(output_tokens=output_tokens)
    return obj


# ---- Bedrock ----

def _bedrock_response(model_id="anthropic.claude-3-sonnet-20240229-v1:0",
                       input_tokens=12, output_tokens=18,
                       stop_reason="end_turn"):
    return {
        "output": {"message": {"content": [{"text": "Hello"}]}},
        "usage": {"inputTokens": input_tokens, "outputTokens": output_tokens},
        "stopReason": stop_reason,
        "ResponseMetadata": {
            "HTTPHeaders": {"x-amzn-bedrock-model-id": model_id},
        },
    }


# ---- LangChain ----

def _make_ai_message(content="Hello", response_metadata=None,
                      additional_kwargs=None, usage_metadata=None):
    cls = type("AIMessage", (), {"__module__": "langchain_core.messages.ai"})
    obj = cls.__new__(cls)
    obj.content = content
    obj.response_metadata = response_metadata or {}
    obj.additional_kwargs = additional_kwargs or {}
    obj.usage_metadata = usage_metadata
    return obj


# ========================================================================
# Fixtures
# ========================================================================


@pytest.fixture(autouse=True)
def _fresh_registry():
    """Reset and re-register all adapters for each test."""
    clear_registry()
    # Import adapters module which auto-registers everything
    from rastir.adapters.openai import OpenAIAdapter
    from rastir.adapters.anthropic import AnthropicAdapter
    from rastir.adapters.bedrock import BedrockAdapter
    from rastir.adapters.langchain import LangChainAdapter
    from rastir.adapters.langgraph import LangGraphAdapter
    from rastir.adapters.retrieval import RetrievalAdapter
    from rastir.adapters.tool import ToolAdapter
    from rastir.adapters.fallback import FallbackAdapter

    register(LangGraphAdapter())
    register(LangChainAdapter())
    register(OpenAIAdapter())
    register(AnthropicAdapter())
    register(BedrockAdapter())
    register(RetrievalAdapter())
    register(ToolAdapter())
    register(FallbackAdapter())
    yield
    clear_registry()


# ========================================================================
# Registry tests
# ========================================================================


class TestRegistry:
    def test_adapters_registered(self):
        adapters = get_registered_adapters()
        names = [a.name for a in adapters]
        assert "openai" in names
        assert "anthropic" in names
        assert "bedrock" in names
        assert "langchain" in names
        assert "langgraph" in names
        assert "retrieval" in names
        assert "fallback" in names

    def test_priority_ordering(self):
        adapters = get_registered_adapters()
        priorities = [a.priority for a in adapters]
        assert priorities == sorted(priorities, reverse=True)

    def test_langgraph_first(self):
        """LangGraph adapter should be first (highest priority framework)."""
        adapters = get_registered_adapters()
        assert adapters[0].name == "langgraph"
        assert adapters[0].kind == "framework"
        assert adapters[1].name == "langchain"
        assert adapters[1].kind == "framework"


# ========================================================================
# OpenAI adapter tests
# ========================================================================


class TestOpenAIAdapter:
    def test_detect_chat_completion(self):
        result = _make_openai_chat_completion()
        ar = resolve(result)
        assert ar is not None
        assert ar.provider == "openai"
        assert ar.model == "gpt-4o"
        assert ar.tokens_input == 10
        assert ar.tokens_output == 20
        assert ar.finish_reason == "stop"

    def test_negative_detection(self):
        """Plain dict should not match OpenAI adapter."""
        ar = resolve({"model": "gpt-4"})
        assert ar is not None
        # Should hit fallback
        assert ar.provider == "unknown"

    def test_stream_chunk(self):
        chunk = _make_openai_chunk(model="gpt-4o", prompt_tokens=10,
                                    completion_tokens=20)
        delta = resolve_stream_chunk(chunk)
        assert delta is not None
        assert delta.provider == "openai"
        assert delta.model == "gpt-4o"
        assert delta.tokens_input == 10
        assert delta.tokens_output == 20

    def test_stream_chunk_no_usage(self):
        """Intermediate chunks without usage should still be handled."""
        chunk = _make_openai_chunk(model="gpt-4o")
        delta = resolve_stream_chunk(chunk)
        assert delta is not None
        assert delta.tokens_input is None
        assert delta.tokens_output is None


# ========================================================================
# Anthropic adapter tests
# ========================================================================


class TestAnthropicAdapter:
    def test_detect_message(self):
        result = _make_anthropic_message()
        ar = resolve(result)
        assert ar is not None
        assert ar.provider == "anthropic"
        assert ar.model == "claude-3-5-sonnet"
        assert ar.tokens_input == 15
        assert ar.tokens_output == 25
        assert ar.finish_reason == "end_turn"

    def test_negative_detection(self):
        """A random object named Message from a non-anthropic module shouldn't match."""

        class Message:
            __module__ = "mylib.types"

        ar = resolve(Message())
        assert ar is not None
        assert ar.provider == "unknown"

    def test_stream_message_start(self):
        chunk = _make_anthropic_raw_message_start(model="claude-3-5-sonnet",
                                                   input_tokens=15)
        delta = resolve_stream_chunk(chunk)
        assert delta is not None
        assert delta.provider == "anthropic"
        assert delta.model == "claude-3-5-sonnet"
        assert delta.tokens_input == 15

    def test_stream_message_delta(self):
        chunk = _make_anthropic_raw_message_delta(output_tokens=25)
        delta = resolve_stream_chunk(chunk)
        assert delta is not None
        assert delta.provider == "anthropic"
        assert delta.tokens_output == 25


# ========================================================================
# Bedrock adapter tests
# ========================================================================


class TestBedrockAdapter:
    def test_detect_converse_response(self):
        result = _bedrock_response()
        ar = resolve(result)
        assert ar is not None
        assert ar.provider == "anthropic"
        assert ar.model == "claude-3-sonnet-20240229-v1:0"
        assert ar.tokens_input == 12
        assert ar.tokens_output == 18
        assert ar.finish_reason == "end_turn"

    def test_model_id_parsing(self):
        """Various Bedrock model IDs should be parsed correctly."""
        from rastir.adapters.bedrock import BedrockAdapter
        adapter = BedrockAdapter()

        assert adapter._parse_model_id("anthropic.claude-3-sonnet") == (
            "claude-3-sonnet", "anthropic")
        assert adapter._parse_model_id("amazon.titan-text-express-v1") == (
            "titan-text-express-v1", "amazon")
        assert adapter._parse_model_id("meta.llama3-70b-instruct-v1:0") == (
            "llama3-70b-instruct-v1:0", "meta")
        assert adapter._parse_model_id(None) == ("unknown", "bedrock")

    def test_negative_detection(self):
        """A dict without output key shouldn't match."""
        ar = resolve({"choices": [{"text": "Hi"}]})
        assert ar is not None
        assert ar.provider == "unknown"

    def test_missing_model_id(self):
        """Response without model ID in headers should still work."""
        result = {
            "output": {"message": {"content": [{"text": "Hello"}]}},
            "usage": {"inputTokens": 5, "outputTokens": 10},
            "ResponseMetadata": {"HTTPHeaders": {}},
        }
        ar = resolve(result)
        assert ar is not None
        assert ar.model == "unknown"
        assert ar.tokens_input == 5


# ========================================================================
# LangChain adapter tests
# ========================================================================


class TestLangChainAdapter:
    def test_detect_ai_message(self):
        """AIMessage should be detected as a LangChain wrapper."""
        msg = _make_ai_message(response_metadata={
            "token_usage": {"prompt_tokens": 10, "completion_tokens": 20},
            "model_name": "gpt-4",
            "finish_reason": "stop",
        })
        ar = resolve(msg)
        assert ar is not None
        # No native unwrapped → should hit fallback, but extras should carry forward
        assert ar.extra_attributes.get("tokens_input") == 10
        assert ar.extra_attributes.get("tokens_output") == 20
        assert ar.extra_attributes.get("model") == "gpt-4"
        assert ar.extra_attributes.get("finish_reason") == "stop"

    def test_unwrap_to_native(self):
        """AIMessage with raw provider response should delegate to provider adapter."""
        openai_native = _make_openai_chat_completion(model="gpt-4o")
        msg = _make_ai_message(
            response_metadata={"raw": openai_native},
        )
        ar = resolve(msg)
        assert ar is not None
        # Should have been unwrapped → OpenAI adapter matches
        assert ar.provider == "openai"
        assert ar.model == "gpt-4o"
        assert ar.tokens_input == 10

    def test_usage_metadata_dict(self):
        """usage_metadata as dict should be extracted."""
        msg = _make_ai_message(usage_metadata={
            "input_tokens": 100,
            "output_tokens": 50,
        })
        ar = resolve(msg)
        assert ar is not None
        assert ar.extra_attributes.get("tokens_input") == 100
        assert ar.extra_attributes.get("tokens_output") == 50

    def test_usage_metadata_object(self):
        """usage_metadata as pydantic-like object should be extracted."""
        usage_obj = _Usage(input_tokens=30, output_tokens=40)
        msg = _make_ai_message(usage_metadata=usage_obj)
        ar = resolve(msg)
        assert ar is not None
        assert ar.extra_attributes.get("tokens_input") == 30
        assert ar.extra_attributes.get("tokens_output") == 40

    def test_negative_detection(self):
        """A non-LangChain class named AIMessage shouldn't match."""

        class AIMessage:
            __module__ = "mylib.messages"

        ar = resolve(AIMessage())
        assert ar is not None
        assert ar.provider == "unknown"

    def test_anthropic_metadata(self):
        """LangChain response_metadata with Anthropic-style keys."""
        msg = _make_ai_message(response_metadata={
            "usage": {"input_tokens": 20, "output_tokens": 30},
            "model": "claude-3",
            "stop_reason": "end_turn",
        })
        ar = resolve(msg)
        assert ar is not None
        assert ar.extra_attributes.get("tokens_input") == 20
        assert ar.extra_attributes.get("tokens_output") == 30
        assert ar.extra_attributes.get("model") == "claude-3"
        assert ar.extra_attributes.get("finish_reason") == "end_turn"


# ========================================================================
# Retrieval adapter tests
# ========================================================================


class TestRetrievalAdapter:
    def test_list_result(self):
        result = ["doc1", "doc2", "doc3"]
        ar = resolve(result)
        assert ar is not None
        assert ar.extra_attributes.get("retrieved_documents_count") == 3

    def test_tuple_result(self):
        result = ("doc1", "doc2")
        ar = resolve(result)
        assert ar is not None
        assert ar.extra_attributes.get("retrieved_documents_count") == 2

    def test_object_with_documents(self):
        class SearchResult:
            def __init__(self):
                self.documents = ["a", "b", "c", "d"]

        ar = resolve(SearchResult())
        assert ar is not None
        assert ar.extra_attributes.get("retrieved_documents_count") == 4

    def test_object_with_page_content(self):
        class Document:
            page_content = "Hello world"

        ar = resolve(Document())
        assert ar is not None
        assert ar.extra_attributes.get("retrieved_documents_count") == 1

    def test_empty_list(self):
        ar = resolve([])
        assert ar is not None
        assert ar.extra_attributes.get("retrieved_documents_count") == 0


# ========================================================================
# Fallback adapter tests
# ========================================================================


class TestFallbackAdapter:
    def test_unknown_object(self):
        class RandomThing:
            pass

        ar = resolve(RandomThing())
        assert ar is not None
        assert ar.model == "unknown"
        assert ar.provider == "unknown"

    def test_none_result(self):
        ar = resolve(None)
        assert ar is not None
        assert ar.provider == "unknown"

    def test_string_result(self):
        ar = resolve("hello world")
        assert ar is not None
        assert ar.provider == "unknown"


# ========================================================================
# Resolution pipeline tests
# ========================================================================


class TestResolutionPipeline:
    def test_framework_then_provider(self):
        """LangChain wrapping OpenAI should resolve through both phases."""
        openai_result = _make_openai_chat_completion(model="gpt-4o")
        lc_msg = _make_ai_message(
            response_metadata={"raw": openai_result},
        )
        ar = resolve(lc_msg)
        assert ar is not None
        assert ar.provider == "openai"
        assert ar.model == "gpt-4o"
        assert ar.tokens_input == 10
        assert ar.tokens_output == 20

    def test_framework_without_native(self):
        """LangChain without native result should fall back."""
        lc_msg = _make_ai_message()
        ar = resolve(lc_msg)
        assert ar is not None
        # No native → no unwrap → fallback
        assert ar.provider == "unknown"

    def test_empty_registry_returns_none(self):
        clear_registry()
        assert resolve("anything") is None

    def test_provider_priority_ordering(self):
        """Higher priority adapter should match first."""
        # OpenAI (150) should match before Retrieval (50)
        result = _make_openai_chat_completion()
        ar = resolve(result)
        assert ar is not None
        assert ar.provider == "openai"

    def test_bedrock_not_matching_plain_dict(self):
        """A plain dict without Bedrock markers shouldn't match Bedrock."""
        ar = resolve({"key": "value"})
        assert ar is not None
        assert ar.provider == "unknown"


# ========================================================================
# Conflict resolution tests
# ========================================================================


class TestConflictResolution:
    def test_same_priority_registration_order(self):
        """Two adapters at same priority — first registered wins."""
        clear_registry()

        from rastir.adapters.types import BaseAdapter

        class AdapterA(BaseAdapter):
            name = "adapter_a"
            kind = "provider"
            priority = 150

            def can_handle(self, result):
                return isinstance(result, str)

            def transform(self, result):
                return AdapterResult(provider="adapter_a")

        class AdapterB(BaseAdapter):
            name = "adapter_b"
            kind = "provider"
            priority = 150

            def can_handle(self, result):
                return isinstance(result, str)

            def transform(self, result):
                return AdapterResult(provider="adapter_b")

        register(AdapterA())
        register(AdapterB())

        ar = resolve("test")
        assert ar is not None
        assert ar.provider == "adapter_a"


# ========================================================================
# LangGraph adapter tests
# ========================================================================


def _make_langgraph_ai_message(content="Hello from agent", response_metadata=None,
                                usage_metadata=None):
    """Create a mock AIMessage with langchain_core module for LangGraph state."""
    cls = type("AIMessage", (), {"__module__": "langchain_core.messages.ai"})
    obj = cls.__new__(cls)
    obj.content = content
    obj.response_metadata = response_metadata or {}
    obj.additional_kwargs = {}
    obj.usage_metadata = usage_metadata
    return obj


def _make_human_message(content="Hello"):
    cls = type("HumanMessage", (), {"__module__": "langchain_core.messages.human"})
    obj = cls.__new__(cls)
    obj.content = content
    return obj


def _make_tool_message(content="Tool result"):
    cls = type("ToolMessage", (), {"__module__": "langchain_core.messages.tool"})
    obj = cls.__new__(cls)
    obj.content = content
    return obj


def _make_state_snapshot(values=None, next_nodes=(), tasks=(), metadata=None):
    """Create a mock StateSnapshot (NamedTuple-like) from langgraph.types."""
    cls = type("StateSnapshot", (), {"__module__": "langgraph.types"})
    obj = cls.__new__(cls)
    obj.values = values or {}
    obj.next = next_nodes
    obj.tasks = tasks
    obj.metadata = metadata
    obj.config = {}
    obj.created_at = None
    obj.parent_config = None
    obj.interrupts = ()
    return obj


def _make_pregel_task(name="agent_node", task_id="task-1"):
    """Create a mock PregelTask."""
    cls = type("PregelTask", (), {"__module__": "langgraph.types"})
    obj = cls.__new__(cls)
    obj.name = name
    obj.id = task_id
    obj.error = None
    obj.interrupts = ()
    obj.result = None
    return obj


def _make_ai_message_chunk(content="chunk", usage_metadata=None):
    """Create a mock AIMessageChunk for streaming."""
    cls = type("AIMessageChunk", (), {"__module__": "langchain_core.messages.ai"})
    obj = cls.__new__(cls)
    obj.content = content
    obj.usage_metadata = usage_metadata
    return obj


class TestLangGraphAdapter:
    """Tests for the LangGraph framework adapter."""

    def test_detect_state_dict_with_messages(self):
        """graph.invoke() returns a dict with 'messages' containing AIMessage objects."""
        human = _make_human_message("What is 2+2?")
        ai = _make_langgraph_ai_message(
            "The answer is 4.",
            response_metadata={
                "token_usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "model_name": "gpt-4o",
            },
        )
        state = {"messages": [human, ai]}

        ar = resolve(state)
        assert ar is not None
        # LangGraph extracts last AIMessage → LangChain adapter processes it
        assert ar.extra_attributes.get("langgraph_message_count") == 2
        assert ar.extra_attributes.get("langgraph_ai_message_count") == 1

    def test_state_dict_unwraps_to_langchain(self):
        """LangGraph unwraps AIMessage → LangChain adapter extracts metadata."""
        ai = _make_langgraph_ai_message(
            "Hello",
            response_metadata={
                "token_usage": {"prompt_tokens": 50, "completion_tokens": 30},
                "model_name": "gpt-4o",
                "finish_reason": "stop",
            },
        )
        state = {"messages": [ai]}

        ar = resolve(state)
        assert ar is not None
        # Metadata from LangChain adapter via extra_attributes
        assert ar.extra_attributes.get("tokens_input") == 50
        assert ar.extra_attributes.get("tokens_output") == 30
        assert ar.extra_attributes.get("model") == "gpt-4o"
        assert ar.extra_attributes.get("finish_reason") == "stop"

    def test_state_dict_with_tool_messages(self):
        """State with mixed message types — counts tool messages."""
        human = _make_human_message("Search for X")
        ai1 = _make_langgraph_ai_message("Let me search")
        tool = _make_tool_message("Search result: ...")
        ai2 = _make_langgraph_ai_message("Based on the search...")
        state = {"messages": [human, ai1, tool, ai2]}

        ar = resolve(state)
        assert ar is not None
        assert ar.extra_attributes.get("langgraph_message_count") == 4
        assert ar.extra_attributes.get("langgraph_ai_message_count") == 2
        assert ar.extra_attributes.get("langgraph_tool_message_count") == 1

    def test_state_snapshot(self):
        """StateSnapshot from graph.get_state() is detected."""
        ai = _make_langgraph_ai_message("Hello")
        task = _make_pregel_task(name="chatbot_node")
        snapshot = _make_state_snapshot(
            values={"messages": [ai]},
            next_nodes=("tools",),
            tasks=(task,),
            metadata={"step": 3, "source": "loop"},
        )

        ar = resolve(snapshot)
        assert ar is not None
        assert ar.extra_attributes.get("langgraph_next_nodes") == ["tools"]
        assert ar.extra_attributes.get("langgraph_task_count") == 1
        assert ar.extra_attributes.get("langgraph_task_names") == ["chatbot_node"]
        assert ar.extra_attributes.get("langgraph_step") == 3
        assert ar.extra_attributes.get("langgraph_source") == "loop"

    def test_state_snapshot_empty(self):
        """StateSnapshot with no values still resolves."""
        snapshot = _make_state_snapshot(values={}, next_nodes=(), tasks=())
        ar = resolve(snapshot)
        assert ar is not None

    def test_negative_plain_dict_no_messages(self):
        """Plain dict without 'messages' should NOT match LangGraph."""
        result = {"output": "hello", "status": "ok"}
        ar = resolve(result)
        assert ar is not None
        # Should fall through to fallback
        assert ar.provider == "unknown"

    def test_negative_dict_with_non_langchain_messages(self):
        """Dict with 'messages' but non-LangChain objects should NOT match."""
        state = {"messages": [{"role": "user", "content": "hi"}]}
        ar = resolve(state)
        assert ar is not None
        # Fallback, not LangGraph
        assert ar.provider == "unknown"

    def test_negative_wrong_module_state_snapshot(self):
        """StateSnapshot-like class from wrong module should NOT match."""
        cls = type("StateSnapshot", (), {"__module__": "mylib.types"})
        obj = cls.__new__(cls)
        obj.values = {}
        obj.next = ()
        ar = resolve(obj)
        assert ar is not None
        assert ar.provider == "unknown"

    def test_full_pipeline_langgraph_to_openai(self):
        """LangGraph → LangChain → OpenAI full unwrap chain."""
        openai_resp = _make_openai_chat_completion(
            model="gpt-4o", prompt_tokens=100, completion_tokens=50,
        )
        ai = _make_langgraph_ai_message(
            "Final answer",
            response_metadata={"raw": openai_resp},
        )
        state = {"messages": [_make_human_message("Question"), ai]}

        ar = resolve(state)
        assert ar is not None
        # Should have gone through LangGraph → LangChain → OpenAI
        assert ar.provider == "openai"
        assert ar.model == "gpt-4o"
        assert ar.tokens_input == 100
        assert ar.tokens_output == 50

    def test_stream_messages_mode(self):
        """stream_mode='messages' produces (AIMessageChunk, metadata) tuples."""
        chunk = _make_ai_message_chunk("token")
        meta = {
            "model_name": "gpt-4o",
            "ls_provider": "openai",
        }
        delta = resolve_stream_chunk((chunk, meta))
        assert delta is not None
        assert delta.model == "gpt-4o"
        assert delta.provider == "openai"

    def test_stream_messages_with_usage(self):
        """Streaming chunk with usage_metadata on the message."""
        usage = _Usage(input_tokens=100, output_tokens=50)
        chunk = _make_ai_message_chunk("final", usage_metadata=usage)
        meta = {"model_name": "claude-3-5-sonnet", "ls_provider": "anthropic"}
        delta = resolve_stream_chunk((chunk, meta))
        assert delta is not None
        assert delta.model == "claude-3-5-sonnet"
        assert delta.provider == "anthropic"
        assert delta.tokens_input == 100
        assert delta.tokens_output == 50

    def test_stream_non_langgraph_tuple_ignored(self):
        """Random tuple should not match LangGraph stream detection."""
        delta = resolve_stream_chunk(("hello", "world"))
        # No adapter should match a plain string tuple
        assert delta is None

    def test_state_dict_extracts_last_ai_message(self):
        """Multiple AIMessages — adapter extracts the LAST one."""
        ai1 = _make_langgraph_ai_message(
            "First",
            response_metadata={"model_name": "gpt-3.5-turbo"},
        )
        ai2 = _make_langgraph_ai_message(
            "Second (final)",
            response_metadata={
                "model_name": "gpt-4o",
                "token_usage": {"prompt_tokens": 200, "completion_tokens": 80},
            },
        )
        state = {"messages": [_make_human_message("Q"), ai1, ai2]}
        ar = resolve(state)
        assert ar is not None
        assert ar.extra_attributes.get("model") == "gpt-4o"
        assert ar.extra_attributes.get("tokens_input") == 200

    def test_adapter_priority_above_langchain(self):
        """LangGraph (260) should be resolved before LangChain (250)."""
        from rastir.adapters.langgraph import LangGraphAdapter
        from rastir.adapters.langchain import LangChainAdapter
        assert LangGraphAdapter.priority > LangChainAdapter.priority
