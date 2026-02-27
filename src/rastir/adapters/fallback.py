"""Generic/Fallback adapter — always matches, returns unknown metadata.

Priority 0: last in the resolution chain.
Ensures spans are always emitted even for unrecognized response types.
"""

from __future__ import annotations

from typing import Any

from rastir.adapters.types import AdapterResult, BaseAdapter


class FallbackAdapter(BaseAdapter):
    """Fallback adapter that matches any result."""

    name = "fallback"
    kind = "fallback"
    priority = 0

    def can_handle(self, result: Any) -> bool:
        return True

    def transform(self, result: Any) -> AdapterResult:
        return AdapterResult(
            model="unknown",
            provider="unknown",
        )
