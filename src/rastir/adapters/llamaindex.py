"""LlamaIndex framework adapter.

Detects LlamaIndex response objects (``Response``, ``StreamingResponse``,
``AgentChatResponse``) and unwraps the underlying LLM completion so
that downstream provider adapters can extract model, tokens, and
provider metadata.

LlamaIndex wraps provider responses in its own schema objects.
The adapter inspects:
  - ``response.source_nodes`` for retrieval metadata
  - ``response.metadata`` for LLM call info
  - ``response.raw`` for the underlying provider response (if available)

Priority: 240 (framework range 200-300, below LangChain at 250
so LangChain-specific objects are handled first when both are present).
"""

from __future__ import annotations

from typing import Any

from rastir.adapters.types import AdapterResult, BaseAdapter, TokenDelta


class LlamaIndexAdapter(BaseAdapter):
    """Adapter for LlamaIndex Response / AgentChatResponse objects."""

    name = "llamaindex"
    kind = "framework"
    priority = 240

    supports_tokens = True
    supports_streaming = True

    _KNOWN_CLASSES = frozenset({
        "Response",
        "StreamingResponse",
        "AgentChatResponse",
        "ChatResponse",
    })

    _KNOWN_MODULES = (
        "llama_index",
        "llama_index.core",
    )

    def can_handle(self, result: Any) -> bool:
        cls_name = type(result).__name__
        module = type(result).__module__ or ""
        return (
            cls_name in self._KNOWN_CLASSES
            and any(m in module for m in self._KNOWN_MODULES)
        )

    def transform(self, result: Any) -> AdapterResult:
        extra: dict[str, Any] = {}

        # Extract source node count for retrieval tracking
        source_nodes = getattr(result, "source_nodes", None)
        if source_nodes is not None:
            extra["llamaindex_source_node_count"] = len(source_nodes)

        # Extract metadata dict if available
        metadata = getattr(result, "metadata", None)
        if isinstance(metadata, dict):
            for k, v in metadata.items():
                extra[f"llamaindex_{k}"] = v

        # Try to unwrap the raw provider response
        raw = getattr(result, "raw", None)
        if raw is not None:
            return AdapterResult(
                unwrapped_result=raw,
                extra_attributes=extra,
            )

        # Try response field (ChatResponse has .message with raw)
        message = getattr(result, "message", None)
        if message is not None:
            raw_msg = getattr(message, "raw", None) or getattr(
                message, "additional_kwargs", {}
            ).get("raw_response")
            if raw_msg is not None:
                return AdapterResult(
                    unwrapped_result=raw_msg,
                    extra_attributes=extra,
                )

        # No raw response available — return extra attributes
        # for fallback adapter to pick up
        return AdapterResult(
            extra_attributes=extra,
        )

    def can_handle_stream(self, chunk: Any) -> bool:
        """LlamaIndex streaming yields string tokens or Response chunks."""
        cls_name = type(chunk).__name__
        module = type(chunk).__module__ or ""
        return (
            cls_name in ("StreamingResponse", "ChatResponse")
            and any(m in module for m in self._KNOWN_MODULES)
        )

    def extract_stream_delta(self, chunk: Any) -> TokenDelta:
        """Extract from LlamaIndex streaming chunk if metadata present."""
        raw = getattr(chunk, "raw", None)
        if raw is not None:
            # Delegate model/token extraction to provider adapter
            # by returning the raw object in the delta
            model = getattr(raw, "model", None)
            usage = getattr(raw, "usage", None)
            tokens_input = None
            tokens_output = None
            if usage is not None:
                tokens_input = getattr(usage, "prompt_tokens", None)
                tokens_output = getattr(usage, "completion_tokens", None)
            return TokenDelta(
                model=model,
                tokens_input=tokens_input,
                tokens_output=tokens_output,
                usage_mode="incremental",
            )
        return TokenDelta()
