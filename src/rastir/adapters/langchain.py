"""LangChain framework adapter.

Detects LangChain AIMessage / ChatResult / LLMResult wrappers and
unwraps them so that downstream provider adapters can extract metadata.

Because LangChain wraps provider-native responses in its own objects,
this is a *framework* adapter (priority 250). The resolution pipeline
will restart after unwrapping, allowing the correct provider adapter
to match.

If the underlying provider response is not available (e.g., the user
used a pipe chain that discarded raw output), the adapter still
extracts whatever metadata LangChain exposes (response_metadata,
usage_metadata) and returns it as extra_attributes, enabling the
fallback adapter to pick them up.

Priority: 250 (framework range 200-300).
"""

from __future__ import annotations

from typing import Any

from rastir.adapters.types import (
    AdapterResult,
    BaseAdapter,
    RequestMetadata,
    detect_provider_from_module,
)


class LangChainAdapter(BaseAdapter):
    """Adapter for LangChain response wrappers."""

    name = "langchain"
    kind = "framework"
    priority = 250

    supports_tokens = True
    supports_streaming = False  # LangChain wraps provider streaming; provider adapters handle chunks
    supports_request_metadata = True

    # LangChain message / result class names we look for
    _KNOWN_CLASSES = frozenset({
        "AIMessage",
        "AIMessageChunk",
        "ChatResult",
        "LLMResult",
        "ChatGeneration",
        "Generation",
    })

    def can_handle(self, result: Any) -> bool:
        """Detect LangChain wrapper objects by class + module name."""
        cls_name = type(result).__name__
        module = type(result).__module__ or ""
        return (
            cls_name in self._KNOWN_CLASSES
            and ("langchain" in module or "langchain_core" in module)
        )

    def transform(self, result: Any) -> AdapterResult:
        """Unwrap LangChain wrapper to provider-native response (if available).

        Resolution order:
        1. response_metadata["raw"] — some LC providers stash native obj here
        2. additional_kwargs["raw_response"] — alternative location
        3. If neither found → extract LC metadata into extra_attributes
           and let fallback handle the provider side.
        """
        extras: dict[str, Any] = {}

        # ----- Try to find the native response object -----
        native = self._extract_native(result)

        # ----- Extract LangChain-level metadata regardless -----
        response_meta = getattr(result, "response_metadata", None)
        if isinstance(response_meta, dict):
            # Token usage  (OpenAI-style via LC)
            token_usage = response_meta.get("token_usage") or response_meta.get("usage")
            if isinstance(token_usage, dict):
                if "prompt_tokens" in token_usage:
                    extras["tokens_input"] = token_usage["prompt_tokens"]
                    extras["tokens_output"] = token_usage.get("completion_tokens")
                elif "input_tokens" in token_usage:
                    extras["tokens_input"] = token_usage["input_tokens"]
                    extras["tokens_output"] = token_usage.get("output_tokens")
            # Model name
            model_name = response_meta.get("model_name") or response_meta.get("model")
            if model_name:
                extras["model"] = model_name
            # Finish reason
            finish = response_meta.get("finish_reason") or response_meta.get("stop_reason")
            if finish:
                extras["finish_reason"] = finish

        # usage_metadata (LangChain ≥ 0.2 standard)
        usage_meta = getattr(result, "usage_metadata", None)
        if isinstance(usage_meta, dict):
            if "input_tokens" in usage_meta:
                extras.setdefault("tokens_input", usage_meta["input_tokens"])
                extras.setdefault("tokens_output", usage_meta.get("output_tokens"))
        elif usage_meta is not None:
            # usage_metadata can be a pydantic model in newer LC
            input_t = getattr(usage_meta, "input_tokens", None)
            output_t = getattr(usage_meta, "output_tokens", None)
            if input_t is not None:
                extras.setdefault("tokens_input", input_t)
            if output_t is not None:
                extras.setdefault("tokens_output", output_t)

        return AdapterResult(
            unwrapped_result=native,
            extra_attributes=extras,
        )

    # ---- Request-phase metadata ----

    def can_handle_request(self, args: tuple, kwargs: dict) -> bool:
        """Detect LangChain chat-model objects in request arguments.

        Matches objects whose module starts with ``langchain`` and that
        expose a model attribute (``model_name``, ``model``, ``model_id``).
        Also matches ``RunnableBinding`` wrappers around such models.
        """
        return self._find_lc_model(args, kwargs) is not None

    def extract_request_metadata(
        self, args: tuple, kwargs: dict,
    ) -> RequestMetadata:
        """Extract model and provider from a LangChain model object."""
        obj = self._find_lc_model(args, kwargs)
        if obj is None:
            return RequestMetadata()

        # Unwrap RunnableBinding → .bound
        inner = getattr(obj, "bound", None)
        if inner is not None and inner is not obj:
            model_name = self._extract_model_attr(inner) or self._extract_model_attr(obj)
            module = getattr(type(inner), "__module__", "") or ""
        else:
            model_name = self._extract_model_attr(obj)
            module = getattr(type(obj), "__module__", "") or ""

        provider = detect_provider_from_module(module)
        return RequestMetadata(
            span_attributes={
                "model": model_name or "unknown",
                "provider": provider,
            },
        )

    def _find_lc_model(self, args: tuple, kwargs: dict) -> Any:
        """Find a LangChain model object in args/kwargs."""
        return self._find_in_args(args, kwargs, self._is_lc_model)

    @staticmethod
    def _is_lc_model(obj: Any) -> bool:
        """Check if obj looks like a LangChain chat model or RunnableBinding."""
        module = getattr(type(obj), "__module__", "") or ""
        if not module.startswith("langchain"):
            return False
        cls_name = type(obj).__name__
        # Direct model class (ChatOpenAI, ChatAnthropic, ChatBedrock, ...)
        if cls_name.startswith("Chat") or cls_name.endswith("LLM"):
            return True
        # RunnableBinding from .bind_tools()
        if cls_name == "RunnableBinding":
            inner = getattr(obj, "bound", None)
            if inner is not None:
                inner_cls = type(inner).__name__
                return inner_cls.startswith("Chat") or inner_cls.endswith("LLM")
        return False

    # ---- Response helpers ----

    @staticmethod
    def _extract_native(result: Any) -> Any:
        """Walk common LC wrapper locations for the raw provider response."""
        # AIMessage / AIMessageChunk
        response_meta = getattr(result, "response_metadata", None)
        if isinstance(response_meta, dict):
            raw = response_meta.get("raw")
            if raw is not None:
                return raw

        additional = getattr(result, "additional_kwargs", None)
        if isinstance(additional, dict):
            raw = additional.get("raw_response")
            if raw is not None:
                return raw

        # ChatResult → generations[0].message
        generations = getattr(result, "generations", None)
        if generations and len(generations) > 0:
            gen = generations[0]
            # LLMResult wraps a list of lists
            if isinstance(gen, list) and len(gen) > 0:
                gen = gen[0]
            msg = getattr(gen, "message", None)
            if msg is not None:
                return msg

        return None
