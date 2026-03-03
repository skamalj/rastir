"""Tests for rastir.llm_discovery — LLM client auto-discovery & interception."""

import asyncio
import types
import pytest

from rastir.config import reset_config
from rastir.queue import drain_batch, reset_queue
from rastir.llm_discovery import (
    _recognize_openai,
    _recognize_azure_openai,
    _recognize_anthropic,
    _recognize_google_genai,
    _recognize_cohere,
    _recognize_mistral,
    _recognize_groq,
    _recognize_langchain,
    _recognize_bedrock,
    _recognize_llm_client,
    discover_llm_clients,
    install_interceptors,
    restore_originals,
)


@pytest.fixture(autouse=True)
def _clean():
    reset_queue()
    reset_config()
    yield
    reset_queue()
    reset_config()


# ---------------------------------------------------------------------------
# Helpers — lightweight fakes that satisfy recognizer duck-typing
# ---------------------------------------------------------------------------

def _make_type(name: str, module: str):
    """Create a new type with the given __name__ and __module__."""
    cls = type(name, (), {})
    cls.__module__ = module
    return cls


def _make_obj(name: str, module: str):
    cls = _make_type(name, module)
    return cls()


class _FakeCompletions:
    def create(self, **kw):
        return {"choices": []}


class _FakeChat:
    def __init__(self):
        self.completions = _FakeCompletions()


class _FakeOpenAI:
    __module__ = "openai._client"

    def __init__(self):
        self.chat = _FakeChat()


# Override class name
_FakeOpenAI.__name__ = "OpenAI"
_FakeOpenAI.__qualname__ = "OpenAI"

# Re-create as a proper type for type().__name__ to work
_OpenAI = type("OpenAI", (), {
    "__module__": "openai._client",
    "__init__": lambda self: setattr(self, "chat", _FakeChat()),
})

_AsyncOpenAI = type("AsyncOpenAI", (), {
    "__module__": "openai._client",
    "__init__": lambda self: setattr(self, "chat", _FakeChat()),
})

_AzureOpenAI = type("AzureOpenAI", (), {
    "__module__": "openai._client",
    "__init__": lambda self: setattr(self, "chat", _FakeChat()),
})

_AsyncAzureOpenAI = type("AsyncAzureOpenAI", (), {
    "__module__": "openai._client",
    "__init__": lambda self: setattr(self, "chat", _FakeChat()),
})


class _FakeAnthropicMessages:
    def create(self, **kw):
        return {}


_Anthropic = type("Anthropic", (), {
    "__module__": "anthropic._client",
    "__init__": lambda self: setattr(self, "messages", _FakeAnthropicMessages()),
})

_AsyncAnthropic = type("AsyncAnthropic", (), {
    "__module__": "anthropic._client",
    "__init__": lambda self: setattr(self, "messages", _FakeAnthropicMessages()),
})

_GenerativeModel = type("GenerativeModel", (), {
    "__module__": "google.generativeai",
    "generate_content": lambda self, *a, **kw: {},
})

_CohereClient = type("ClientV2", (), {
    "__module__": "cohere._client",
    "chat": lambda self, *a, **kw: {},
})


class _FakeMistralChat:
    def complete(self, **kw):
        return {}

_Mistral = type("Mistral", (), {
    "__module__": "mistralai._client",
    "__init__": lambda self: setattr(self, "chat", _FakeMistralChat()),
})

_Groq = type("Groq", (), {
    "__module__": "groq._client",
    "__init__": lambda self: setattr(self, "chat", _FakeChat()),
})

_AsyncGroq = type("AsyncGroq", (), {
    "__module__": "groq._client",
    "__init__": lambda self: setattr(self, "chat", _FakeChat()),
})


# ---------------------------------------------------------------------------
# Recognizer tests
# ---------------------------------------------------------------------------

class TestRecognizeOpenAI:
    def test_sync_client(self):
        obj = _OpenAI()
        targets = _recognize_openai(obj)
        assert len(targets) == 1
        target_obj, method, is_async = targets[0]
        assert method == "create"
        assert is_async is False
        assert target_obj is obj.chat.completions

    def test_async_client(self):
        obj = _AsyncOpenAI()
        targets = _recognize_openai(obj)
        assert len(targets) == 1
        assert targets[0][2] is True

    def test_ignores_azure(self):
        obj = _AzureOpenAI()
        targets = _recognize_openai(obj)
        assert targets == []

    def test_unrelated_object(self):
        targets = _recognize_openai("hello")
        assert targets == []


class TestRecognizeAzureOpenAI:
    def test_sync_client(self):
        obj = _AzureOpenAI()
        targets = _recognize_azure_openai(obj)
        assert len(targets) == 1
        assert targets[0][1] == "create"
        assert targets[0][2] is False

    def test_async_client(self):
        obj = _AsyncAzureOpenAI()
        targets = _recognize_azure_openai(obj)
        assert len(targets) == 1
        assert targets[0][2] is True


class TestRecognizeAnthropic:
    def test_sync_client(self):
        obj = _Anthropic()
        targets = _recognize_anthropic(obj)
        assert len(targets) == 1
        assert targets[0][1] == "create"
        assert targets[0][2] is False

    def test_async_client(self):
        obj = _AsyncAnthropic()
        targets = _recognize_anthropic(obj)
        assert len(targets) == 1
        assert targets[0][2] is True


class TestRecognizeGoogleGenAI:
    def test_generative_model(self):
        obj = _GenerativeModel()
        targets = _recognize_google_genai(obj)
        assert len(targets) == 1
        assert targets[0][1] == "generate_content"


class TestRecognizeCohere:
    def test_client_v2(self):
        obj = _CohereClient()
        targets = _recognize_cohere(obj)
        assert len(targets) == 1
        assert targets[0][1] == "chat"


class TestRecognizeMistral:
    def test_mistral(self):
        obj = _Mistral()
        targets = _recognize_mistral(obj)
        assert len(targets) == 1
        assert targets[0][1] == "complete"


class TestRecognizeGroq:
    def test_sync_client(self):
        obj = _Groq()
        targets = _recognize_groq(obj)
        assert len(targets) == 1
        assert targets[0][1] == "create"
        assert targets[0][2] is False

    def test_async_client(self):
        obj = _AsyncGroq()
        targets = _recognize_groq(obj)
        assert len(targets) == 1
        assert targets[0][2] is True


class TestRecognizeLangChain:
    def test_base_chat_model(self):
        # Build a fake BaseChatModel in the langchain namespace
        BaseChatModel = type("BaseChatModel", (), {"__module__": "langchain_core.language_models.chat_models"})
        MyChatModel = type("MyChatModel", (BaseChatModel,), {
            "invoke": lambda self, *a, **kw: {},
            "ainvoke": lambda self, *a, **kw: {},
        })
        obj = MyChatModel()
        targets = _recognize_langchain(obj)
        assert len(targets) == 2
        methods = {t[1] for t in targets}
        assert methods == {"invoke", "ainvoke"}


class TestRecognizeBedrock:
    def test_bedrock_runtime(self):
        class FakeServiceModel:
            service_name = "bedrock-runtime"
        class FakeMeta:
            service_model = FakeServiceModel()
        BedrockRuntime = type("BedrockRuntime", (), {
            "__module__": "botocore.client",
            "invoke_model": lambda self, **kw: {},
        })
        obj = BedrockRuntime()
        obj.meta = FakeMeta()
        targets = _recognize_bedrock(obj)
        assert len(targets) == 1
        assert targets[0][1] == "invoke_model"


class TestRecognizePipeline:
    def test_dispatches_to_correct_recognizer(self):
        obj = _OpenAI()
        targets = _recognize_llm_client(obj)
        assert len(targets) == 1
        assert targets[0][1] == "create"

    def test_returns_empty_for_unknown(self):
        assert _recognize_llm_client(42) == []
        assert _recognize_llm_client("hello") == []
        assert _recognize_llm_client(None) == []


# ---------------------------------------------------------------------------
# discover_llm_clients tests
# ---------------------------------------------------------------------------

class TestDiscoverLLMClients:
    def test_finds_client_in_args(self):
        client = _OpenAI()
        def my_func(c):
            pass
        targets = discover_llm_clients(my_func, (client,), {})
        assert len(targets) == 1

    def test_finds_client_in_kwargs(self):
        client = _Anthropic()
        def my_func(client=None):
            pass
        targets = discover_llm_clients(my_func, (), {"client": client})
        assert len(targets) == 1

    def test_finds_client_in_closure(self):
        client = _OpenAI()
        def outer():
            def inner():
                _ = client  # capture in closure
            return inner
        fn = outer()
        targets = discover_llm_clients(fn, (), {})
        assert len(targets) == 1

    def test_deduplicates(self):
        """Same object in args and closure should only appear once."""
        client = _OpenAI()
        def outer():
            def inner(c):
                _ = client
            return inner
        fn = outer()
        targets = discover_llm_clients(fn, (client,), {})
        # Should only get 1 target (deduplicated by id)
        assert len(targets) == 1

    def test_no_clients(self):
        def my_func(x, y):
            pass
        targets = discover_llm_clients(my_func, (1, 2), {})
        assert targets == []


# ---------------------------------------------------------------------------
# install_interceptors / restore_originals tests
# ---------------------------------------------------------------------------

class TestInstallInterceptors:
    def test_patches_and_restores(self):
        client = _OpenAI()
        original_create = client.chat.completions.create
        targets = _recognize_llm_client(client)

        # Mock span
        _attrs = {}
        span = types.SimpleNamespace(
            attributes=_attrs,
            set_attribute=lambda key, val: _attrs.__setitem__(key, val),
        )

        originals = install_interceptors(targets, span)
        # Method should be patched
        assert client.chat.completions.create is not original_create
        assert hasattr(client.chat.completions.create, "_rastir_intercepted")

        # Restore
        restore_originals(originals)
        # After restore the _rastir_intercepted marker should be gone
        assert not hasattr(client.chat.completions.create, "_rastir_intercepted")

    def test_double_intercept_prevention(self):
        client = _OpenAI()
        targets = _recognize_llm_client(client)

        _attrs = {}
        span = types.SimpleNamespace(
            attributes=_attrs,
            set_attribute=lambda key, val: _attrs.__setitem__(key, val),
        )

        originals1 = install_interceptors(targets, span)
        interceptor1 = client.chat.completions.create
        # Try installing again on the same target
        originals2 = install_interceptors(targets, span)
        interceptor2 = client.chat.completions.create

        # Should not double-patch — second install returns empty
        assert len(originals2) == 0
        assert interceptor2 is interceptor1

        restore_originals(originals1)


# ---------------------------------------------------------------------------
# End-to-end test with @llm decorator
# ---------------------------------------------------------------------------

class TestLLMDecoratorDiscovery:
    def test_auto_discovery_captures_metadata(self):
        """@llm-decorated function with an OpenAI client in closure.

        The function returns only the text, but the interceptor should
        capture the full response metadata onto the span.
        """
        from rastir.decorators import llm
        from rastir.spans import SpanType

        # Build a fake OpenAI client whose create() returns a rich response
        client = _OpenAI()

        # Build a mock response the OpenAI adapter can recognise
        # (class name must be 'ChatCompletion' in an 'openai' module)
        _ChatCompletion = type("ChatCompletion", (), {
            "__module__": "openai.types.chat.chat_completion",
        })
        _FakeUsage = type("CompletionUsage", (), {})

        usage = _FakeUsage()
        usage.prompt_tokens = 10
        usage.completion_tokens = 20
        usage.total_tokens = 30

        _FakeMessage = type("Message", (), {})
        message = _FakeMessage()
        message.role = "assistant"
        message.content = "Hello world"

        _FakeChoice = type("Choice", (), {})
        choice = _FakeChoice()
        choice.message = message
        choice.finish_reason = "stop"

        response = _ChatCompletion()
        response.model = "gpt-4"
        response.choices = [choice]
        response.usage = usage

        # Patch the fake client to return our mock response
        client.chat.completions.create = lambda **kw: response

        @llm()
        def ask_question(question: str) -> str:
            resp = client.chat.completions.create(
                model="gpt-4",
                messages=[{"role": "user", "content": question}],
            )
            return resp.choices[0].message.content  # returns plain text

        result = ask_question("What is 2+2?")
        assert result == "Hello world"

        spans = drain_batch(10)
        assert len(spans) == 1
        s = spans[0]
        assert s.span_type == SpanType.LLM
        # These should have been captured by the interceptor from the
        # full response, NOT from the plain text return
        assert s.attributes.get("model") == "gpt-4"
        assert s.attributes.get("tokens_input") == 10
        assert s.attributes.get("tokens_output") == 20

    def test_no_discovery_silent_when_no_clients(self):
        """@llm with no discoverable clients should work normally."""
        from rastir.decorators import llm

        @llm()
        def simple():
            return "hi"

        result = simple()
        assert result == "hi"
        spans = drain_batch(10)
        assert len(spans) == 1
