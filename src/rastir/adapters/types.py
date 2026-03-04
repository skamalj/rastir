"""Adapter data types for Rastir.

Defines AdapterResult, TokenDelta, RequestMetadata, and the BaseAdapter interface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class AdapterResult:
    """Result returned by an adapter's transform() method.

    Framework adapters primarily set unwrapped_result.
    Provider adapters populate semantic fields.
    """

    unwrapped_result: Any = None
    model: Optional[str] = None
    provider: Optional[str] = None
    tokens_input: Optional[int] = None
    tokens_output: Optional[int] = None
    finish_reason: Optional[str] = None
    extra_attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class RequestMetadata:
    """Metadata extracted from request-level arguments (pre-invocation).

    Used by adapters that need to inspect call kwargs (e.g., Bedrock
    guardrail configuration). Returned by extract_request_metadata().
    """

    span_attributes: dict[str, Any] = field(default_factory=dict)
    extra_attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class TokenDelta:
    """Token delta extracted from a single streaming chunk.

    ``usage_mode`` declares how the provider emits token counts:

    * ``"incremental"`` – each chunk carries a delta that must be summed.
    * ``"cumulative"`` – each chunk carries a running total; only the
      latest value should be kept (e.g., Gemini).

    Adapters MUST set ``usage_mode`` so the decorator accumulation
    logic handles the values correctly.
    """

    tokens_input: Optional[int] = None
    tokens_output: Optional[int] = None
    model: Optional[str] = None
    provider: Optional[str] = None
    usage_mode: Optional[str] = None  # "incremental" | "cumulative"


# ── Provider detection from module name ──────────────────────────────
# Maps module prefixes to canonical provider names. Used by framework
# adapters (LangChain, LangGraph) to determine the provider when they
# find a model object in request arguments.
_MODULE_PROVIDER_MAP: dict[str, str] = {
    "langchain_openai": "openai",
    "langchain_anthropic": "anthropic",
    "langchain_aws": "bedrock",
    "langchain_google": "gemini",
    "langchain_groq": "groq",
    "langchain_mistralai": "mistral",
    "langchain_cohere": "cohere",
    "crewai.llms.providers.openai": "openai",
    "crewai.llms.providers.anthropic": "anthropic",
    "crewai.llms.providers.gemini": "gemini",
    "crewai.llms.providers.groq": "groq",
    "llama_index.llms.openai": "openai",
    "llama_index.llms.anthropic": "anthropic",
    "llama_index.llms.gemini": "gemini",
    "llama_index.llms.azure_openai": "azure",
    "llama_index.llms.bedrock": "bedrock",
    "llama_index.llms.mistral": "mistral",
    "llama_index.llms.groq": "groq",
    "llama_index.llms.cohere": "cohere",
    "openai": "openai",
    "anthropic": "anthropic",
    "google.genai": "gemini",
    "google.generativeai": "gemini",
    "groq": "groq",
    "mistralai": "mistral",
    "cohere": "cohere",
}


def detect_provider_from_module(module: str) -> str:
    """Map a Python module name to a canonical provider string.

    Used by framework adapters to determine the provider from
    a model object's ``__module__``.
    """
    for prefix, provider in _MODULE_PROVIDER_MAP.items():
        if module.startswith(prefix):
            return provider
    return "unknown"


# ── Common model attribute names ────────────────────────────────────
# Ordered by specificity: ``model_name`` (LangChain-OpenAI),
# ``model`` (Anthropic, native SDKs), ``model_id`` (Bedrock).
COMMON_MODEL_ATTRS: tuple[str, ...] = ("model_name", "model", "model_id", "modelId")


class BaseAdapter:
    """Base class for all adapters.

    Subclasses must implement can_handle() and transform().
    Streaming adapters may optionally implement can_handle_stream()
    and extract_stream_delta().
    Request-aware adapters may implement extract_request_metadata().

    Capability flags declare what the adapter supports. Decorators
    must not assume capabilities that are not declared.
    """

    name: str = "base"
    kind: str = "provider"  # "framework" | "provider" | "fallback"
    priority: int = 100  # Higher = evaluated first

    # Capability flags — subclasses override as needed
    supports_tokens: bool = False
    supports_streaming: bool = False
    supports_request_metadata: bool = False
    supports_guardrail_metadata: bool = False

    def can_handle(self, result: Any) -> bool:
        """Return True if this adapter can handle the given result."""
        return False

    def transform(self, result: Any) -> AdapterResult:
        """Extract metadata from the result (response phase)."""
        return AdapterResult()

    def can_handle_request(self, args: tuple, kwargs: dict[str, Any]) -> bool:
        """Return True if this adapter can extract request-level metadata.

        Called before function execution. Default returns False.
        Override in adapters that inspect call arguments (e.g., Bedrock
        guardrail configuration).
        """
        return False

    def extract_request_metadata(
        self, args: tuple, kwargs: dict[str, Any]
    ) -> RequestMetadata:
        """Extract metadata from call arguments (request phase).

        Called before function execution when can_handle_request() is True.
        Returns span attributes to set before the function runs.
        """
        return RequestMetadata()

    # ---- Helpers for request-phase scanning ----

    @staticmethod
    def _find_in_args(
        args: tuple,
        kwargs: dict[str, Any],
        predicate: Any,
    ) -> Any:
        """Scan positional args and kwarg values for the first match.

        ``predicate(obj) -> bool`` is called for each value. Returns
        the first matching object or ``None``.
        """
        for arg in args:
            if predicate(arg):
                return arg
        for val in kwargs.values():
            if predicate(val):
                return val
        return None

    @staticmethod
    def _extract_model_attr(obj: Any) -> Optional[str]:
        """Read the first non-empty model attribute from an object."""
        for attr in COMMON_MODEL_ATTRS:
            try:
                val = getattr(obj, attr, None)
                if val and isinstance(val, str):
                    return val
            except Exception:
                continue
        return None

    def can_handle_stream(self, chunk: Any) -> bool:
        """Return True if this adapter can handle a streaming chunk."""
        return False

    def extract_stream_delta(self, chunk: Any) -> TokenDelta:
        """Extract token delta from a single streaming chunk."""
        return TokenDelta()
