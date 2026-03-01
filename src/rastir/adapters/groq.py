"""Groq provider adapter.

Handles responses from the ``groq`` Python SDK, which mirrors the
OpenAI SDK structure but returns Groq-specific class names.

Detection:
  - ``ChatCompletion`` class in ``groq`` module namespace (not ``openai``).

Metadata extraction:
  - model: ``result.model``
  - tokens: ``result.usage.prompt_tokens`` / ``completion_tokens``
  - finish_reason: ``result.choices[0].finish_reason``
  - extra: ``result.usage.queue_time``, ``result.usage.total_time``

Priority: 152 (above OpenAI at 150 so Groq is checked first;
both use ``ChatCompletion`` class name but different modules).
"""

from __future__ import annotations

from typing import Any

from rastir.adapters.types import AdapterResult, BaseAdapter, TokenDelta


class GroqAdapter(BaseAdapter):
    """Adapter for Groq ChatCompletion responses."""

    name = "groq"
    kind = "provider"
    priority = 152

    supports_tokens = True
    supports_streaming = True
    supports_request_metadata = True

    _CLIENT_CLASSES = frozenset({"Groq", "AsyncGroq"})

    def can_handle(self, result: Any) -> bool:
        cls_name = type(result).__name__
        module = type(result).__module__ or ""
        return (
            cls_name in ("ChatCompletion", "Completion")
            and "groq" in module
            and "openai" not in module
        )

    def transform(self, result: Any) -> AdapterResult:
        model = getattr(result, "model", None)

        tokens_input = None
        tokens_output = None
        extra: dict[str, Any] = {}

        usage = getattr(result, "usage", None)
        if usage is not None:
            tokens_input = getattr(usage, "prompt_tokens", None)
            tokens_output = getattr(usage, "completion_tokens", None)
            # Groq-specific timing
            queue_time = getattr(usage, "queue_time", None)
            if queue_time is not None:
                extra["groq_queue_time"] = queue_time
            total_time = getattr(usage, "total_time", None)
            if total_time is not None:
                extra["groq_total_time"] = total_time

        finish_reason = None
        choices = getattr(result, "choices", None)
        if choices and len(choices) > 0:
            finish_reason = getattr(choices[0], "finish_reason", None)

        return AdapterResult(
            model=model,
            provider="groq",
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            finish_reason=finish_reason,
            extra_attributes=extra,
        )

    def can_handle_stream(self, chunk: Any) -> bool:
        cls_name = type(chunk).__name__
        module = type(chunk).__module__ or ""
        return (
            cls_name == "ChatCompletionChunk"
            and "groq" in module
            and "openai" not in module
        )

    def extract_stream_delta(self, chunk: Any) -> TokenDelta:
        model = getattr(chunk, "model", None)
        usage = getattr(chunk, "usage", None)
        tokens_input = None
        tokens_output = None

        if usage is not None:
            tokens_input = getattr(usage, "prompt_tokens", None)
            tokens_output = getattr(usage, "completion_tokens", None)

        return TokenDelta(
            model=model,
            provider="groq",
            tokens_input=tokens_input,
            tokens_output=tokens_output,
        )

    def can_handle_request(self, args: tuple, kwargs: dict) -> bool:
        """Detect Groq client objects in request args."""
        for arg in (*args, *kwargs.values()):
            cls_name = type(arg).__name__
            module = type(arg).__module__ or ""
            if cls_name in self._CLIENT_CLASSES and "groq" in module:
                return True
        return False

    def extract_request_metadata(
        self, args: tuple, kwargs: dict
    ) -> "RequestMetadata":
        """Extract model and provider from Groq request arguments."""
        from rastir.adapters.types import RequestMetadata
        span_attrs: dict = {"provider": "groq"}
        model = kwargs.get("model")
        if model and isinstance(model, str):
            span_attrs["model"] = model
        return RequestMetadata(span_attributes=span_attrs)
