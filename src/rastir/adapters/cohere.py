"""Cohere provider adapter.

Handles responses from the ``cohere`` Python SDK (v2 API).

Detection:
  - ``ChatResponse`` or ``NonStreamedChatResponse`` class in ``cohere``
    module namespace.

Metadata extraction:
  - model: ``result.model`` or ``result.meta.model``
  - tokens: ``result.meta.billed_units.input_tokens`` /
    ``output_tokens`` or ``result.meta.tokens``
  - finish_reason: ``result.finish_reason``

Priority: 150 (standard provider range).
"""

from __future__ import annotations

from typing import Any

from rastir.adapters.types import AdapterResult, BaseAdapter, TokenDelta


class CohereAdapter(BaseAdapter):
    """Adapter for Cohere ChatResponse / NonStreamedChatResponse objects."""

    name = "cohere"
    kind = "provider"
    priority = 150

    supports_tokens = True
    supports_streaming = True
    supports_request_metadata = True

    _KNOWN_CLASSES = frozenset({
        "ChatResponse",
        "NonStreamedChatResponse",
    })

    _STREAM_CLASSES = frozenset({
        "StreamedChatResponse_TextGeneration",
        "StreamedChatResponse_StreamEnd",
        "ChatStreamEndEvent",
        "StreamEndStreamedChatResponse",
    })

    def can_handle(self, result: Any) -> bool:
        cls_name = type(result).__name__
        module = type(result).__module__ or ""
        return cls_name in self._KNOWN_CLASSES and "cohere" in module

    def transform(self, result: Any) -> AdapterResult:
        # Model
        model = getattr(result, "model", None)

        # Token usage — Cohere v2 uses meta.billed_units or meta.tokens
        tokens_input = None
        tokens_output = None
        meta = getattr(result, "meta", None)
        if meta is not None:
            # v2: billed_units
            billed = getattr(meta, "billed_units", None)
            if billed is not None:
                tokens_input = getattr(billed, "input_tokens", None)
                tokens_output = getattr(billed, "output_tokens", None)
            # v1 fallback: meta.tokens
            if tokens_input is None:
                tokens_obj = getattr(meta, "tokens", None)
                if tokens_obj is not None:
                    tokens_input = getattr(tokens_obj, "input_tokens", None)
                    tokens_output = getattr(tokens_obj, "output_tokens", None)

        # Finish reason
        finish_reason = getattr(result, "finish_reason", None)
        if finish_reason is not None and hasattr(finish_reason, "value"):
            finish_reason = finish_reason.value

        return AdapterResult(
            model=model,
            provider="cohere",
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            finish_reason=finish_reason,
        )

    def can_handle_stream(self, chunk: Any) -> bool:
        cls_name = type(chunk).__name__
        module = type(chunk).__module__ or ""
        return cls_name in self._STREAM_CLASSES and "cohere" in module

    def extract_stream_delta(self, chunk: Any) -> TokenDelta:
        """Extract from Cohere stream end event (tokens in final chunk)."""
        model = None
        tokens_input = None
        tokens_output = None

        # StreamEnd carries response object with meta
        response = getattr(chunk, "response", None)
        if response is not None:
            model = getattr(response, "model", None)
            meta = getattr(response, "meta", None)
            if meta is not None:
                billed = getattr(meta, "billed_units", None)
                if billed is not None:
                    tokens_input = getattr(billed, "input_tokens", None)
                    tokens_output = getattr(billed, "output_tokens", None)

        return TokenDelta(
            model=model,
            provider="cohere",
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            usage_mode="incremental",
        )

    def can_handle_request(self, args: tuple, kwargs: dict) -> bool:
        """Detect Cohere client objects in request args."""
        for arg in (*args, *kwargs.values()):
            cls_name = type(arg).__name__
            module = type(arg).__module__ or ""
            if cls_name in ("Client", "AsyncClient", "ClientV2") and "cohere" in module:
                return True
        model = kwargs.get("model", "")
        if isinstance(model, str) and model.startswith(("command",)):
            return True
        return False

    def extract_request_metadata(
        self, args: tuple, kwargs: dict
    ) -> "RequestMetadata":
        """Extract model and provider from Cohere request arguments."""
        from rastir.adapters.types import RequestMetadata
        span_attrs: dict = {"provider": "cohere"}
        model = kwargs.get("model")
        if model and isinstance(model, str):
            span_attrs["model"] = model
        return RequestMetadata(span_attributes=span_attrs)
