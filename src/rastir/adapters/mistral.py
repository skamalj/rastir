"""Mistral AI provider adapter.

Handles responses from the ``mistralai`` Python SDK.

Detection:
  - ``ChatCompletionResponse`` class in ``mistralai`` module namespace.

Metadata extraction:
  - model: ``result.model``
  - tokens: ``result.usage.prompt_tokens`` / ``completion_tokens``
  - finish_reason: ``result.choices[0].finish_reason``

Priority: 150 (standard provider range).
"""

from __future__ import annotations

from typing import Any

from rastir.adapters.types import AdapterResult, BaseAdapter, TokenDelta


class MistralAdapter(BaseAdapter):
    """Adapter for Mistral ChatCompletionResponse objects."""

    name = "mistral"
    kind = "provider"
    priority = 150

    supports_tokens = True
    supports_streaming = True

    _KNOWN_CLASSES = frozenset({
        "ChatCompletionResponse",
    })

    _STREAM_CLASSES = frozenset({
        "CompletionChunk",
        "ChatCompletionStreamResponse",
    })

    def can_handle(self, result: Any) -> bool:
        cls_name = type(result).__name__
        module = type(result).__module__ or ""
        return cls_name in self._KNOWN_CLASSES and "mistralai" in module

    def transform(self, result: Any) -> AdapterResult:
        model = getattr(result, "model", None)

        # Token usage
        tokens_input = None
        tokens_output = None
        usage = getattr(result, "usage", None)
        if usage is not None:
            tokens_input = getattr(usage, "prompt_tokens", None)
            tokens_output = getattr(usage, "completion_tokens", None)

        # Finish reason from first choice
        finish_reason = None
        choices = getattr(result, "choices", None)
        if choices and len(choices) > 0:
            fr = getattr(choices[0], "finish_reason", None)
            if fr is not None:
                finish_reason = str(fr.value) if hasattr(fr, "value") else str(fr)

        return AdapterResult(
            model=model,
            provider="mistral",
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            finish_reason=finish_reason,
        )

    def can_handle_stream(self, chunk: Any) -> bool:
        cls_name = type(chunk).__name__
        module = type(chunk).__module__ or ""
        return cls_name in self._STREAM_CLASSES and "mistralai" in module

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
            provider="mistral",
            tokens_input=tokens_input,
            tokens_output=tokens_output,
        )
