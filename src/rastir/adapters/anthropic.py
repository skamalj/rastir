"""Anthropic provider adapter.

Handles Anthropic Message responses (non-streaming) and streaming event
objects. Extracts model, provider, input/output tokens, stop_reason.

Priority: 150 (standard provider range).
"""

from __future__ import annotations

from typing import Any

from rastir.adapters.types import AdapterResult, BaseAdapter, TokenDelta


class AnthropicAdapter(BaseAdapter):
    """Adapter for Anthropic Message responses."""

    name = "anthropic"
    kind = "provider"
    priority = 150

    supports_tokens = True
    supports_streaming = True
    supports_request_metadata = True

    _CLIENT_CLASSES = frozenset({"Anthropic", "AsyncAnthropic"})

    def can_handle(self, result: Any) -> bool:
        """Detect Anthropic Message objects by class name."""
        cls_name = type(result).__name__
        module = type(result).__module__ or ""
        return cls_name == "Message" and "anthropic" in module

    def transform(self, result: Any) -> AdapterResult:
        model = getattr(result, "model", None)
        usage = getattr(result, "usage", None)
        tokens_input = None
        tokens_output = None

        if usage is not None:
            tokens_input = getattr(usage, "input_tokens", None)
            tokens_output = getattr(usage, "output_tokens", None)

        finish_reason = getattr(result, "stop_reason", None)

        return AdapterResult(
            model=model,
            provider="anthropic",
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            finish_reason=finish_reason,
        )

    def can_handle_stream(self, chunk: Any) -> bool:
        """Detect Anthropic streaming events."""
        cls_name = type(chunk).__name__
        module = type(chunk).__module__ or ""
        return (
            cls_name in ("RawMessageStartEvent", "RawMessageDeltaEvent")
            and "anthropic" in module
        )

    def extract_stream_delta(self, chunk: Any) -> TokenDelta:
        """Extract token delta from Anthropic streaming events.

        - RawMessageStartEvent: contains the initial Message with usage
        - RawMessageDeltaEvent: contains output_tokens in usage
        """
        cls_name = type(chunk).__name__
        model = None
        tokens_input = None
        tokens_output = None

        if cls_name == "RawMessageStartEvent":
            message = getattr(chunk, "message", None)
            if message:
                model = getattr(message, "model", None)
                usage = getattr(message, "usage", None)
                if usage:
                    tokens_input = getattr(usage, "input_tokens", None)

        elif cls_name == "RawMessageDeltaEvent":
            usage = getattr(chunk, "usage", None)
            if usage:
                tokens_output = getattr(usage, "output_tokens", None)

        return TokenDelta(
            model=model,
            provider="anthropic",
            tokens_input=tokens_input,
            tokens_output=tokens_output,
        )

    def can_handle_request(self, args: tuple, kwargs: dict) -> bool:
        """Detect Anthropic client objects or model kwarg in request args."""
        for arg in args:
            cls_name = type(arg).__name__
            module = type(arg).__module__ or ""
            if cls_name in self._CLIENT_CLASSES and "anthropic" in module:
                return True
        for val in kwargs.values():
            cls_name = type(val).__name__
            module = type(val).__module__ or ""
            if cls_name in self._CLIENT_CLASSES and "anthropic" in module:
                return True
        model = kwargs.get("model", "")
        if isinstance(model, str) and model.startswith(("claude-",)):
            return True
        return False

    def extract_request_metadata(
        self, args: tuple, kwargs: dict
    ) -> "RequestMetadata":
        """Extract model and provider from Anthropic request arguments."""
        from rastir.adapters.types import RequestMetadata
        span_attrs: dict = {"provider": "anthropic"}
        model = kwargs.get("model")
        if model and isinstance(model, str):
            span_attrs["model"] = model
        return RequestMetadata(span_attributes=span_attrs)
