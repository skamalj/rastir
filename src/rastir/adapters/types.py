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
    """Token delta extracted from a single streaming chunk."""

    tokens_input: Optional[int] = None
    tokens_output: Optional[int] = None
    model: Optional[str] = None
    provider: Optional[str] = None


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

    def can_handle_stream(self, chunk: Any) -> bool:
        """Return True if this adapter can handle a streaming chunk."""
        return False

    def extract_stream_delta(self, chunk: Any) -> TokenDelta:
        """Extract token delta from a single streaming chunk."""
        return TokenDelta()
