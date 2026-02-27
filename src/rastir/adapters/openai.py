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
