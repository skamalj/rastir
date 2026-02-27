"""Adapter data types for Rastir.

Defines AdapterResult, TokenDelta, and the BaseAdapter interface.
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
    """

    name: str = "base"
    kind: str = "provider"  # "framework" | "provider" | "fallback"
    priority: int = 100  # Higher = evaluated first

    def can_handle(self, result: Any) -> bool:
        """Return True if this adapter can handle the given result."""
        return False

    def transform(self, result: Any) -> AdapterResult:
        """Extract metadata from the result."""
        return AdapterResult()

    def can_handle_stream(self, chunk: Any) -> bool:
        """Return True if this adapter can handle a streaming chunk."""
        return False

    def extract_stream_delta(self, chunk: Any) -> TokenDelta:
        """Extract token delta from a single streaming chunk."""
        return TokenDelta()
