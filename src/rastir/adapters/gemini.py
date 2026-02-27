"""Google Gemini provider adapter.

Handles responses from the ``google-genai`` (google.genai) and
``google-generativeai`` (google.generativeai) Python SDKs.

Detection:
  - ``GenerateContentResponse`` class in ``google.genai`` or
    ``google.generativeai`` modules.

Metadata extraction:
  - model: from ``model_metadata.model`` or the candidates
  - tokens: from ``usage_metadata.prompt_token_count`` /
    ``candidates_token_count``
  - finish_reason: from ``candidates[0].finish_reason``

Priority: 150 (standard provider range).
"""

from __future__ import annotations

from typing import Any

from rastir.adapters.types import AdapterResult, BaseAdapter, TokenDelta


class GeminiAdapter(BaseAdapter):
    """Adapter for Google Gemini GenerateContentResponse objects."""

    name = "gemini"
    kind = "provider"
    priority = 150

    supports_tokens = True
    supports_streaming = True

    _KNOWN_CLASSES = frozenset({
        "GenerateContentResponse",
    })

    _KNOWN_MODULES = (
        "google.genai",
        "google.generativeai",
    )

    def can_handle(self, result: Any) -> bool:
        cls_name = type(result).__name__
        module = type(result).__module__ or ""
        return (
            cls_name in self._KNOWN_CLASSES
            and any(m in module for m in self._KNOWN_MODULES)
        )

    def transform(self, result: Any) -> AdapterResult:
        # Extract token usage from usage_metadata
        tokens_input = None
        tokens_output = None
        usage = getattr(result, "usage_metadata", None)
        if usage is not None:
            tokens_input = getattr(usage, "prompt_token_count", None)
            tokens_output = getattr(usage, "candidates_token_count", None)

        # Extract model name
        model = None
        # google-genai >= 1.0: result.model_version
        model = getattr(result, "model_version", None)

        # Extract finish reason from first candidate
        finish_reason = None
        candidates = getattr(result, "candidates", None)
        if candidates and len(candidates) > 0:
            fr = getattr(candidates[0], "finish_reason", None)
            if fr is not None:
                # Gemini uses enum — convert to string
                finish_reason = str(fr.name) if hasattr(fr, "name") else str(fr)

        return AdapterResult(
            model=model,
            provider="gemini",
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            finish_reason=finish_reason,
        )

    def can_handle_stream(self, chunk: Any) -> bool:
        """Detect Gemini streaming chunks (same class, iterated)."""
        cls_name = type(chunk).__name__
        module = type(chunk).__module__ or ""
        return (
            cls_name in self._KNOWN_CLASSES
            and any(m in module for m in self._KNOWN_MODULES)
        )

    def extract_stream_delta(self, chunk: Any) -> TokenDelta:
        """Extract delta from a Gemini streaming chunk.

        Gemini includes usage_metadata in each chunk (cumulative).
        We extract it and let the decorator accumulate from the last chunk.
        """
        model = getattr(chunk, "model_version", None)
        usage = getattr(chunk, "usage_metadata", None)
        tokens_input = None
        tokens_output = None

        if usage is not None:
            tokens_input = getattr(usage, "prompt_token_count", None)
            tokens_output = getattr(usage, "candidates_token_count", None)

        return TokenDelta(
            model=model,
            provider="gemini",
            tokens_input=tokens_input,
            tokens_output=tokens_output,
        )
