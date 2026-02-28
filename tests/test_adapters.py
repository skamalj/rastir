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


# ---- Azure OpenAI ----

def _make_azure_openai_chat_completion(model="gpt-4o", prompt_tokens=10,
                                        completion_tokens=20, finish_reason="stop"):
    """Create a mock Azure OpenAI ChatCompletion with Azure headers."""
    cls = type("ChatCompletion", (), {"__module__": "openai.types.chat.chat_completion"})
    obj = cls.__new__(cls)
    obj.model = model
    obj.usage = _Usage(prompt_tokens=prompt_tokens,
                       completion_tokens=completion_tokens,
                       total_tokens=prompt_tokens + completion_tokens)
    obj.choices = [_OpenAIChoice(finish_reason)]
    # Add Azure-specific raw response with headers
    raw_cls = type("_RawResponse", (), {})
    raw = raw_cls.__new__(raw_cls)
    raw.headers = {"x-ms-region": "eastus", "x-ms-client-request-id": "abc-123"}
    obj._raw_response = raw
    return obj


def _make_azure_openai_chunk(model="gpt-4o"):
    """Create a mock Azure OpenAI streaming chunk with Azure headers."""
    cls = type("ChatCompletionChunk", (), {
        "__module__": "openai.types.chat.chat_completion_chunk"})
    obj = cls.__new__(cls)
    obj.model = model
    obj.usage = None
    raw_cls = type("_RawResponse", (), {})
    raw = raw_cls.__new__(raw_cls)
    raw.headers = {"x-ms-region": "westus2"}
    obj._raw_response = raw
    return obj


# ---- Gemini ----

def _make_gemini_response(model_version="gemini-1.5-pro", prompt_tokens=12,
                           candidates_tokens=22, finish_reason_name="STOP"):
    cls = type("GenerateContentResponse", (), {
        "__module__": "google.genai.types"})
    obj = cls.__new__(cls)
    obj.model_version = model_version
    obj.usage_metadata = _Usage(prompt_token_count=prompt_tokens,
                                 candidates_token_count=candidates_tokens)
    # Finish reason as enum-like object
    fr_cls = type("FinishReason", (), {"name": finish_reason_name})
    cand_cls = type("Candidate", (), {})
    cand = cand_cls.__new__(cand_cls)
    cand.finish_reason = fr_cls()
    obj.candidates = [cand]
    return obj


def _make_gemini_chunk(model_version="gemini-1.5-pro"):
    cls = type("GenerateContentResponse", (), {
        "__module__": "google.generativeai.types"})
    obj = cls.__new__(cls)
    obj.model_version = model_version
    obj.usage_metadata = _Usage(prompt_token_count=5, candidates_token_count=10)
    obj.candidates = []
    return obj


# ---- Cohere ----

def _make_cohere_response(model="command-r-plus", input_tokens=15,
                           output_tokens=25, finish_reason="COMPLETE"):
    cls = type("NonStreamedChatResponse", (), {"__module__": "cohere.types"})
    obj = cls.__new__(cls)
    obj.model = model
    obj.meta = _Usage(
        billed_units=_Usage(input_tokens=input_tokens, output_tokens=output_tokens),
        tokens=None,
    )
    # Finish reason as enum-like
    fr_cls = type("FinishReason", (), {"value": finish_reason})
    obj.finish_reason = fr_cls()
    return obj


def _make_cohere_stream_end(model="command-r-plus", input_tokens=15,
                             output_tokens=25):
    cls = type("StreamedChatResponse_StreamEnd", (), {"__module__": "cohere.types"})
    obj = cls.__new__(cls)
    resp = _Usage(
        model=model,
        meta=_Usage(
            billed_units=_Usage(input_tokens=input_tokens, output_tokens=output_tokens),
        ),
    )
    obj.response = resp
    return obj


# ---- Mistral ----

def _make_mistral_response(model="mistral-large-latest", prompt_tokens=10,
                            completion_tokens=20, finish_reason="stop"):
    cls = type("ChatCompletionResponse", (), {"__module__": "mistralai.models"})
    obj = cls.__new__(cls)
    obj.model = model
    obj.usage = _Usage(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    fr_cls = type("FinishReason", (), {"value": finish_reason})
    choice_cls = type("Choice", (), {})
    choice = choice_cls.__new__(choice_cls)
    choice.finish_reason = fr_cls()
    obj.choices = [choice]
    return obj


def _make_mistral_chunk(model="mistral-large-latest", prompt_tokens=None,
                         completion_tokens=None):
    cls = type("CompletionChunk", (), {"__module__": "mistralai.models"})
    obj = cls.__new__(cls)
    obj.model = model
    if prompt_tokens is not None or completion_tokens is not None:
        obj.usage = _Usage(prompt_tokens=prompt_tokens,
                           completion_tokens=completion_tokens)
    else:
        obj.usage = None
    return obj


# ---- Groq ----

def _make_groq_response(model="llama-3.1-70b-versatile", prompt_tokens=8,
                         completion_tokens=16, finish_reason="stop",
                         queue_time=0.01, total_time=0.05):
    cls = type("ChatCompletion", (), {"__module__": "groq.types.chat.chat_completion"})
    obj = cls.__new__(cls)
    obj.model = model
    obj.usage = _Usage(prompt_tokens=prompt_tokens,
                       completion_tokens=completion_tokens,
                       queue_time=queue_time, total_time=total_time)
    obj.choices = [_OpenAIChoice(finish_reason)]
    return obj


def _make_groq_chunk(model="llama-3.1-70b-versatile"):
    cls = type("ChatCompletionChunk", (), {
        "__module__": "groq.types.chat.chat_completion_chunk"})
    obj = cls.__new__(cls)
    obj.model = model
    obj.usage = None
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
    from rastir.adapters.azure_openai import AzureOpenAIAdapter
    from rastir.adapters.anthropic import AnthropicAdapter
    from rastir.adapters.bedrock import BedrockAdapter
    from rastir.adapters.gemini import GeminiAdapter
    from rastir.adapters.cohere import CohereAdapter
    from rastir.adapters.crewai import CrewAIAdapter
    from rastir.adapters.mistral import MistralAdapter
    from rastir.adapters.groq import GroqAdapter
    from rastir.adapters.langchain import LangChainAdapter
    from rastir.adapters.langgraph import LangGraphAdapter
    from rastir.adapters.llamaindex import LlamaIndexAdapter
    from rastir.adapters.retrieval import RetrievalAdapter
    from rastir.adapters.tool import ToolAdapter
    from rastir.adapters.fallback import FallbackAdapter

    register(LangGraphAdapter())
    register(LangChainAdapter())
    register(LlamaIndexAdapter())
    register(CrewAIAdapter())
    register(AzureOpenAIAdapter())
    register(GroqAdapter())
    register(OpenAIAdapter())
    register(AnthropicAdapter())
    register(GeminiAdapter())
    register(CohereAdapter())
    register(MistralAdapter())
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
        assert "azure_openai" in names
        assert "anthropic" in names
        assert "bedrock" in names
        assert "gemini" in names
        assert "cohere" in names
        assert "mistral" in names
        assert "groq" in names
        assert "langchain" in names
        assert "langgraph" in names
        assert "llamaindex" in names
        assert "crewai" in names
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

    def test_guardrail_request_detection(self):
        """can_handle_request detects guardrailIdentifier in kwargs."""
        from rastir.adapters.bedrock import BedrockAdapter
        adapter = BedrockAdapter()
        assert adapter.can_handle_request((), {
            "guardrailIdentifier": "my-guardrail-id",
            "guardrailVersion": "1",
        })
        assert not adapter.can_handle_request((), {"modelId": "anthropic.claude"})

    def test_guardrail_request_metadata_extraction(self):
        """extract_request_metadata extracts guardrail config."""
        from rastir.adapters.bedrock import BedrockAdapter
        adapter = BedrockAdapter()
        meta = adapter.extract_request_metadata((), {
            "guardrailIdentifier": "gr-abc123",
            "guardrailVersion": "3",
        })
        assert meta.span_attributes["guardrail.id"] == "gr-abc123"
        assert meta.span_attributes["guardrail.version"] == "3"
        assert meta.span_attributes["guardrail.enabled"] is True
        assert meta.extra_attributes["guardrail_id"] == "gr-abc123"

    def test_guardrail_config_nested(self):
        """Nested guardrailConfig dict is also detected."""
        from rastir.adapters.bedrock import BedrockAdapter
        adapter = BedrockAdapter()
        assert adapter.can_handle_request((), {
            "guardrailConfig": {
                "guardrailIdentifier": "gr-nested",
                "guardrailVersion": "2",
            }
        })
        meta = adapter.extract_request_metadata((), {
            "guardrailConfig": {
                "guardrailIdentifier": "gr-nested",
                "guardrailVersion": "2",
            }
        })
        assert meta.span_attributes["guardrail.id"] == "gr-nested"

    def test_guardrail_response_intervention(self):
        """Response with guardrail intervention is detected."""
        result = {
            "output": {"message": {"content": [{"text": "Blocked"}]}},
            "usage": {"inputTokens": 10, "outputTokens": 5},
            "ResponseMetadata": {"HTTPHeaders": {}},
            "amazon-bedrock-guardrailAction": "GUARDRAIL_INTERVENED",
        }
        ar = resolve(result)
        assert ar is not None
        assert ar.extra_attributes.get("guardrail.triggered") is True
        assert ar.extra_attributes.get("guardrail.action") == "GUARDRAIL_INTERVENED"

    def test_guardrail_response_no_intervention(self):
        """Response without guardrail intervention has no guardrail attrs."""
        result = _bedrock_response()
        ar = resolve(result)
        assert ar is not None
        assert "guardrail.triggered" not in ar.extra_attributes

    def test_guardrail_trace_with_assessment(self):
        """Response with trace.guardrail containing assessments."""
        result = {
            "output": {"message": {"content": [{"text": "Blocked"}]}},
            "usage": {"inputTokens": 10, "outputTokens": 5},
            "ResponseMetadata": {"HTTPHeaders": {}},
            "trace": {
                "guardrail": {
                    "action": "GUARDRAIL_INTERVENED",
                    "inputAssessment": {
                        "contentPolicy": [
                            {"type": "CONTENT_POLICY", "action": "BLOCKED"}
                        ]
                    },
                    "outputAssessments": [],
                }
            },
        }
        ar = resolve(result)
        assert ar is not None
        assert ar.extra_attributes.get("guardrail.triggered") is True
        assert ar.extra_attributes.get("guardrail.category") == "CONTENT_POLICY"
        assert ar.extra_attributes.get("guardrail_category") == "CONTENT_POLICY"

    def test_guardrail_category_overflow(self):
        """Unknown category maps to __cardinality_overflow__."""
        from rastir.adapters.bedrock import BedrockAdapter
        adapter = BedrockAdapter()
        assert adapter._safe_category("UNKNOWN_POLICY") == "__cardinality_overflow__"
        assert adapter._safe_category("CONTENT_POLICY") == "CONTENT_POLICY"

    def test_guardrail_stream_chunk_metadata(self):
        """Bedrock streaming metadata chunk yields tokens."""
        from rastir.adapters.bedrock import BedrockAdapter
        adapter = BedrockAdapter()
        chunk = {
            "metadata": {
                "usage": {"inputTokens": 50, "outputTokens": 30},
            }
        }
        assert adapter.can_handle_stream(chunk)
        delta = adapter.extract_stream_delta(chunk)
        assert delta.tokens_input == 50
        assert delta.tokens_output == 30

    def test_capability_flags_guardrail(self):
        """Bedrock adapter has guardrail capability flags."""
        from rastir.adapters.bedrock import BedrockAdapter
        a = BedrockAdapter()
        assert a.supports_request_metadata is True
        assert a.supports_guardrail_metadata is True
        assert a.supports_streaming is True


# ========================================================================
# Azure OpenAI adapter tests
# ========================================================================


class TestAzureOpenAIAdapter:
    def test_detect_azure_chat_completion(self):
        result = _make_azure_openai_chat_completion()
        ar = resolve(result)
        assert ar is not None
        assert ar.provider == "azure_openai"
        assert ar.model == "gpt-4o"
        assert ar.tokens_input == 10
        assert ar.tokens_output == 20
        assert ar.finish_reason == "stop"

    def test_non_azure_openai_falls_through(self):
        """Standard OpenAI (no Azure headers) should NOT match Azure adapter."""
        result = _make_openai_chat_completion()
        ar = resolve(result)
        assert ar is not None
        assert ar.provider == "openai"  # Should hit standard OpenAI adapter

    def test_stream_chunk_azure(self):
        chunk = _make_azure_openai_chunk(model="gpt-4o")
        delta = resolve_stream_chunk(chunk)
        assert delta is not None
        assert delta.provider == "azure_openai"
        assert delta.model == "gpt-4o"

    def test_negative_plain_dict(self):
        ar = resolve({"model": "gpt-4"})
        assert ar.provider == "unknown"

    def test_capability_flags(self):
        from rastir.adapters.azure_openai import AzureOpenAIAdapter
        a = AzureOpenAIAdapter()
        assert a.supports_tokens is True
        assert a.supports_streaming is True


# ========================================================================
# Gemini adapter tests
# ========================================================================


class TestGeminiAdapter:
    def test_detect_gemini_response(self):
        result = _make_gemini_response()
        ar = resolve(result)
        assert ar is not None
        assert ar.provider == "gemini"
        assert ar.model == "gemini-1.5-pro"
        assert ar.tokens_input == 12
        assert ar.tokens_output == 22
        assert ar.finish_reason == "STOP"

    def test_negative_non_gemini(self):
        """Non-Gemini class should not match."""
        cls = type("GenerateContentResponse", (), {"__module__": "some.other.module"})
        obj = cls.__new__(cls)
        ar = resolve(obj)
        assert ar.provider == "unknown"

    def test_stream_chunk(self):
        chunk = _make_gemini_chunk()
        delta = resolve_stream_chunk(chunk)
        assert delta is not None
        assert delta.provider == "gemini"
        assert delta.model == "gemini-1.5-pro"
        assert delta.tokens_input == 5
        assert delta.tokens_output == 10

    def test_capability_flags(self):
        from rastir.adapters.gemini import GeminiAdapter
        a = GeminiAdapter()
        assert a.supports_tokens is True
        assert a.supports_streaming is True


# ========================================================================
# Cohere adapter tests
# ========================================================================


class TestCohereAdapter:
    def test_detect_cohere_response(self):
        result = _make_cohere_response()
        ar = resolve(result)
        assert ar is not None
        assert ar.provider == "cohere"
        assert ar.model == "command-r-plus"
        assert ar.tokens_input == 15
        assert ar.tokens_output == 25
        assert ar.finish_reason == "COMPLETE"

    def test_negative_non_cohere(self):
        cls = type("NonStreamedChatResponse", (), {"__module__": "other.module"})
        obj = cls.__new__(cls)
        ar = resolve(obj)
        assert ar.provider == "unknown"

    def test_stream_end(self):
        chunk = _make_cohere_stream_end()
        delta = resolve_stream_chunk(chunk)
        assert delta is not None
        assert delta.provider == "cohere"
        assert delta.model == "command-r-plus"
        assert delta.tokens_input == 15
        assert delta.tokens_output == 25

    def test_capability_flags(self):
        from rastir.adapters.cohere import CohereAdapter
        a = CohereAdapter()
        assert a.supports_tokens is True
        assert a.supports_streaming is True


# ========================================================================
# Mistral adapter tests
# ========================================================================


class TestMistralAdapter:
    def test_detect_mistral_response(self):
        result = _make_mistral_response()
        ar = resolve(result)
        assert ar is not None
        assert ar.provider == "mistral"
        assert ar.model == "mistral-large-latest"
        assert ar.tokens_input == 10
        assert ar.tokens_output == 20
        assert ar.finish_reason == "stop"

    def test_negative_non_mistral(self):
        cls = type("ChatCompletionResponse", (), {"__module__": "other.module"})
        obj = cls.__new__(cls)
        ar = resolve(obj)
        assert ar.provider == "unknown"

    def test_stream_chunk(self):
        chunk = _make_mistral_chunk(prompt_tokens=5, completion_tokens=10)
        delta = resolve_stream_chunk(chunk)
        assert delta is not None
        assert delta.provider == "mistral"
        assert delta.model == "mistral-large-latest"
        assert delta.tokens_input == 5
        assert delta.tokens_output == 10

    def test_stream_chunk_no_usage(self):
        chunk = _make_mistral_chunk()
        delta = resolve_stream_chunk(chunk)
        assert delta is not None
        assert delta.tokens_input is None
        assert delta.tokens_output is None

    def test_capability_flags(self):
        from rastir.adapters.mistral import MistralAdapter
        a = MistralAdapter()
        assert a.supports_tokens is True
        assert a.supports_streaming is True


# ========================================================================
# Groq adapter tests
# ========================================================================


class TestGroqAdapter:
    def test_detect_groq_response(self):
        result = _make_groq_response()
        ar = resolve(result)
        assert ar is not None
        assert ar.provider == "groq"
        assert ar.model == "llama-3.1-70b-versatile"
        assert ar.tokens_input == 8
        assert ar.tokens_output == 16
        assert ar.finish_reason == "stop"

    def test_groq_extra_timing(self):
        result = _make_groq_response(queue_time=0.01, total_time=0.05)
        ar = resolve(result)
        assert ar.extra_attributes.get("groq_queue_time") == 0.01
        assert ar.extra_attributes.get("groq_total_time") == 0.05

    def test_groq_not_confused_with_openai(self):
        """Groq module should NOT match OpenAI adapter."""
        result = _make_groq_response()
        ar = resolve(result)
        assert ar.provider == "groq"  # Not "openai"

    def test_openai_not_confused_with_groq(self):
        """OpenAI module should NOT match Groq adapter."""
        result = _make_openai_chat_completion()
        ar = resolve(result)
        assert ar.provider == "openai"  # Not "groq"

    def test_stream_chunk(self):
        chunk = _make_groq_chunk()
        delta = resolve_stream_chunk(chunk)
        assert delta is not None
        assert delta.provider == "groq"
        assert delta.model == "llama-3.1-70b-versatile"

    def test_negative_plain_dict(self):
        ar = resolve({"model": "llama"})
        assert ar.provider == "unknown"

    def test_capability_flags(self):
        from rastir.adapters.groq import GroqAdapter
        a = GroqAdapter()
        assert a.supports_tokens is True
        assert a.supports_streaming is True


# ========================================================================
# Adapter capability flags tests
# ========================================================================


class TestAdapterCapabilities:
    def test_all_adapters_have_capability_flags(self):
        """Every registered adapter must declare capability flags."""
        adapters = get_registered_adapters()
        for adapter in adapters:
            assert isinstance(adapter.supports_tokens, bool), \
                f"{adapter.name} missing supports_tokens"
            assert isinstance(adapter.supports_streaming, bool), \
                f"{adapter.name} missing supports_streaming"

    def test_request_metadata_defaults_false(self):
        """Base adapters default supports_request_metadata to False."""
        from rastir.adapters.types import BaseAdapter
        base = BaseAdapter()
        assert base.supports_request_metadata is False
        assert base.supports_guardrail_metadata is False

    def test_request_metadata_interface(self):
        """Base adapter request methods return safe defaults."""
        from rastir.adapters.types import BaseAdapter
        base = BaseAdapter()
        assert base.can_handle_request((), {}) is False
        meta = base.extract_request_metadata((), {})
        assert meta.span_attributes == {}
        assert meta.extra_attributes == {}


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


# ========================================================================
# LlamaIndex mock factories
# ========================================================================


def _make_llamaindex_response(response_text="Answer", source_node_count=2,
                               metadata=None, raw=None):
    """Create a mock LlamaIndex Response object."""
    cls = type("Response", (), {
        "__module__": "llama_index.core.base.response.schema"
    })
    obj = cls.__new__(cls)
    obj.response = response_text
    obj.source_nodes = [object() for _ in range(source_node_count)]
    obj.metadata = metadata or {}
    obj.raw = raw
    return obj


def _make_llamaindex_agent_response(response_text="Agent answer", raw=None):
    """Create a mock LlamaIndex AgentChatResponse."""
    cls = type("AgentChatResponse", (), {
        "__module__": "llama_index.core.base.response.schema"
    })
    obj = cls.__new__(cls)
    obj.response = response_text
    obj.source_nodes = []
    obj.metadata = {}
    obj.raw = raw
    return obj


def _make_llamaindex_chat_response(raw=None):
    """Create a mock LlamaIndex ChatResponse with message."""
    cls = type("ChatResponse", (), {
        "__module__": "llama_index.core.llms.types"
    })
    obj = cls.__new__(cls)
    obj.source_nodes = None
    obj.metadata = {}
    obj.raw = raw
    # ChatResponse has .message with raw
    msg = type("ChatMessage", (), {"__module__": "llama_index.core.llms.types"})
    msg_obj = msg.__new__(msg)
    msg_obj.raw = raw
    msg_obj.additional_kwargs = {}
    obj.message = msg_obj
    return obj


def _make_llamaindex_streaming_response():
    """Create a mock LlamaIndex StreamingResponse chunk."""
    cls = type("StreamingResponse", (), {
        "__module__": "llama_index.core.base.response.schema"
    })
    obj = cls.__new__(cls)
    obj.source_nodes = None
    obj.metadata = None
    obj.raw = None
    return obj


# ========================================================================
# LlamaIndex adapter tests
# ========================================================================


class TestLlamaIndexAdapter:
    """Tests for the LlamaIndex framework adapter."""

    def test_detect_response(self):
        """LlamaIndex Response object is detected."""
        resp = _make_llamaindex_response()
        ar = resolve(resp)
        assert ar is not None
        assert ar.extra_attributes.get("llamaindex_source_node_count") == 2

    def test_detect_agent_chat_response(self):
        """AgentChatResponse is detected."""
        resp = _make_llamaindex_agent_response()
        ar = resolve(resp)
        assert ar is not None
        assert ar.extra_attributes.get("llamaindex_source_node_count") == 0

    def test_unwrap_raw_openai(self):
        """Raw OpenAI response inside LlamaIndex Response is unwrapped."""
        openai_resp = _make_openai_chat_completion(
            model="gpt-4o", prompt_tokens=50, completion_tokens=25,
        )
        resp = _make_llamaindex_response(raw=openai_resp, source_node_count=3)
        ar = resolve(resp)
        assert ar is not None
        assert ar.provider == "openai"
        assert ar.model == "gpt-4o"
        assert ar.tokens_input == 50
        assert ar.tokens_output == 25
        assert ar.extra_attributes.get("llamaindex_source_node_count") == 3

    def test_unwrap_raw_anthropic(self):
        """Raw Anthropic response inside LlamaIndex Response is unwrapped."""
        anthropic_resp = _make_anthropic_message(
            model="claude-3-5-sonnet", input_tokens=40, output_tokens=60,
        )
        resp = _make_llamaindex_response(raw=anthropic_resp)
        ar = resolve(resp)
        assert ar is not None
        assert ar.provider == "anthropic"
        assert ar.model == "claude-3-5-sonnet"
        assert ar.tokens_input == 40
        assert ar.tokens_output == 60

    def test_chat_response_unwrap_via_message(self):
        """ChatResponse unwraps via .message.raw."""
        openai_resp = _make_openai_chat_completion(model="gpt-4o-mini")
        resp = _make_llamaindex_chat_response(raw=openai_resp)
        ar = resolve(resp)
        assert ar is not None
        assert ar.provider == "openai"
        assert ar.model == "gpt-4o-mini"

    def test_metadata_extraction(self):
        """Response metadata dict entries are prefixed with llamaindex_."""
        resp = _make_llamaindex_response(
            metadata={"model": "gpt-4o", "pipeline_type": "query"},
        )
        ar = resolve(resp)
        assert ar is not None
        assert ar.extra_attributes.get("llamaindex_model") == "gpt-4o"
        assert ar.extra_attributes.get("llamaindex_pipeline_type") == "query"

    def test_streaming_response_detected(self):
        """StreamingResponse is detected by can_handle."""
        resp = _make_llamaindex_streaming_response()
        ar = resolve(resp)
        assert ar is not None

    def test_negative_wrong_module(self):
        """Response-like object from wrong module should NOT match."""
        cls = type("Response", (), {"__module__": "fastapi"})
        obj = cls.__new__(cls)
        ar = resolve(obj)
        assert ar is not None
        assert ar.provider == "unknown"  # fallback

    def test_priority_between_langchain_and_providers(self):
        """LlamaIndex (240) should be below LangChain (250) and above providers."""
        from rastir.adapters.llamaindex import LlamaIndexAdapter
        from rastir.adapters.langchain import LangChainAdapter
        from rastir.adapters.openai import OpenAIAdapter
        assert LangChainAdapter.priority > LlamaIndexAdapter.priority
        assert LlamaIndexAdapter.priority > OpenAIAdapter.priority


# ========================================================================
# CrewAI mock factories
# ========================================================================


def _make_crewai_crew_output(raw="Final crew output", token_usage=None,
                              tasks_output=None, json_dict=None, pydantic=None):
    """Create a mock CrewAI CrewOutput object."""
    cls = type("CrewOutput", (), {"__module__": "crewai.crews.crew_output"})
    obj = cls.__new__(cls)
    obj.raw = raw
    obj.token_usage = token_usage or {}
    obj.tasks_output = tasks_output or []
    obj.json_dict = json_dict
    obj.pydantic = pydantic
    return obj


def _make_crewai_task_output(description="Research task", agent="Researcher",
                              raw="Task result", token_usage=None):
    """Create a mock CrewAI TaskOutput object."""
    cls = type("TaskOutput", (), {"__module__": "crewai.tasks.task_output"})
    obj = cls.__new__(cls)
    obj.description = description
    obj.agent = agent
    obj.raw = raw
    obj.name = None
    obj.token_usage = token_usage
    return obj


# ========================================================================
# CrewAI adapter tests
# ========================================================================


class TestCrewAIAdapter:
    """Tests for the CrewAI framework adapter."""

    def test_detect_crew_output(self):
        """CrewOutput object is detected."""
        resp = _make_crewai_crew_output()
        ar = resolve(resp)
        assert ar is not None

    def test_detect_task_output(self):
        """TaskOutput object is detected."""
        resp = _make_crewai_task_output()
        ar = resolve(resp)
        assert ar is not None
        assert ar.extra_attributes.get("crewai_task_description") == "Research task"
        assert ar.extra_attributes.get("crewai_agent") == "Researcher"

    def test_crew_output_token_usage(self):
        """Token usage is extracted from CrewOutput."""
        resp = _make_crewai_crew_output(
            token_usage={
                "prompt_tokens": 500,
                "completion_tokens": 200,
                "total_tokens": 700,
                "successful_requests": 3,
            }
        )
        ar = resolve(resp)
        assert ar is not None
        assert ar.tokens_input == 500
        assert ar.tokens_output == 200
        assert ar.extra_attributes.get("crewai_total_tokens") == 700
        assert ar.extra_attributes.get("crewai_successful_requests") == 3

    def test_crew_output_tasks_metadata(self):
        """Tasks metadata is extracted from CrewOutput."""
        t1 = _make_crewai_task_output(description="Research", agent="Researcher")
        t2 = _make_crewai_task_output(description="Write", agent="Writer")
        resp = _make_crewai_crew_output(tasks_output=[t1, t2])
        ar = resolve(resp)
        assert ar is not None
        assert ar.extra_attributes.get("crewai_task_count") == 2
        tasks = ar.extra_attributes.get("crewai_tasks")
        assert len(tasks) == 2
        assert tasks[0]["agent"] == "Researcher"
        assert tasks[1]["description"] == "Write"

    def test_crew_output_json_and_pydantic(self):
        """JSON and pydantic output flags are detected."""
        resp = _make_crewai_crew_output(
            json_dict={"key": "value"},
            pydantic=object(),
        )
        ar = resolve(resp)
        assert ar is not None
        assert ar.extra_attributes.get("crewai_has_json_output") is True
        assert ar.extra_attributes.get("crewai_has_pydantic_output") is True

    def test_task_output_token_usage(self):
        """Token usage on individual task."""
        resp = _make_crewai_task_output(
            token_usage={"prompt_tokens": 100, "completion_tokens": 50},
        )
        ar = resolve(resp)
        assert ar is not None
        assert ar.tokens_input == 100
        assert ar.tokens_output == 50

    def test_negative_wrong_module(self):
        """CrewOutput-like class from wrong module should NOT match."""
        cls = type("CrewOutput", (), {"__module__": "mylib.types"})
        obj = cls.__new__(cls)
        ar = resolve(obj)
        assert ar is not None
        assert ar.provider == "unknown"  # fallback

    def test_priority_in_framework_range(self):
        """CrewAI (245) should be in framework range."""
        from rastir.adapters.crewai import CrewAIAdapter
        from rastir.adapters.langchain import LangChainAdapter
        from rastir.adapters.llamaindex import LlamaIndexAdapter
        assert LangChainAdapter.priority > CrewAIAdapter.priority
        assert CrewAIAdapter.priority > LlamaIndexAdapter.priority
