"""OpenAI provider adapter.

Handles ChatCompletion responses (non-streaming) and ChatCompletionChunk
objects (streaming). Extracts model, provider, token usage, finish_reason.

Priority: 150 (standard provider range).
"""

from __future__ import annotations

from typing import Any

from rastir.adapters.types import AdapterResult, BaseAdapter, TokenDelta


class OpenAIAdapter(BaseAdapter):
    """Adapter for OpenAI ChatCompletion and legacy Completion responses."""

    name = "openai"
    kind = "provider"
    priority = 150

    supports_tokens = True
    supports_streaming = True
    supports_request_metadata = True

    # SDK client class names used for request-phase detection
    _CLIENT_CLASSES = frozenset({"OpenAI", "AsyncOpenAI"})

    def can_handle(self, result: Any) -> bool:
        """Detect OpenAI response objects by class name to avoid hard import."""
        cls_name = type(result).__name__
        module = type(result).__module__ or ""
        return (
            cls_name in ("ChatCompletion", "Completion")
            and "openai" in module
        )

    def transform(self, result: Any) -> AdapterResult:
        model = getattr(result, "model", None)
        usage = getattr(result, "usage", None)
        tokens_input = None
        tokens_output = None

        if usage is not None:
            tokens_input = getattr(usage, "prompt_tokens", None)
            tokens_output = getattr(usage, "completion_tokens", None)

        # Extract finish_reason from first choice
        finish_reason = None
        choices = getattr(result, "choices", None)
        if choices and len(choices) > 0:
            finish_reason = getattr(choices[0], "finish_reason", None)

        return AdapterResult(
            model=model,
            provider="openai",
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            finish_reason=finish_reason,
        )

    def can_handle_stream(self, chunk: Any) -> bool:
        """Detect OpenAI streaming chunks."""
        cls_name = type(chunk).__name__
        module = type(chunk).__module__ or ""
        return cls_name == "ChatCompletionChunk" and "openai" in module

    def can_handle_request(self, args: tuple, kwargs: dict) -> bool:
        """Detect OpenAI client objects or model kwarg in request args."""
        # Check for OpenAI client object in args
        for arg in args:
            cls_name = type(arg).__name__
            module = type(arg).__module__ or ""
            if cls_name in self._CLIENT_CLASSES and "openai" in module:
                return True
        for val in kwargs.values():
            cls_name = type(val).__name__
            module = type(val).__module__ or ""
            if cls_name in self._CLIENT_CLASSES and "openai" in module:
                return True
        # Check for model kwarg with openai-style model names
        model = kwargs.get("model", "")
        if isinstance(model, str) and model.startswith(("gpt-", "o1-", "o3-", "chatgpt-")):
            return True
        return False

    def extract_request_metadata(
        self, args: tuple, kwargs: dict
    ) -> "RequestMetadata":
        """Extract model and provider from OpenAI request arguments."""
        from rastir.adapters.types import RequestMetadata
        span_attrs: dict = {"provider": "openai"}
        model = kwargs.get("model")
        if model and isinstance(model, str):
            span_attrs["model"] = model
        return RequestMetadata(span_attributes=span_attrs)

    def extract_stream_delta(self, chunk: Any) -> TokenDelta:
        """Extract token delta from a ChatCompletionChunk.

        OpenAI streaming chunks include usage in the final chunk
        (when stream_options={"include_usage": True}).
        """
        model = getattr(chunk, "model", None)
        usage = getattr(chunk, "usage", None)
        tokens_input = None
        tokens_output = None

        if usage is not None:
            tokens_input = getattr(usage, "prompt_tokens", None)
            tokens_output = getattr(usage, "completion_tokens", None)

        return TokenDelta(
            model=model,
            provider="openai",
            tokens_input=tokens_input,
            tokens_output=tokens_output,
        )
