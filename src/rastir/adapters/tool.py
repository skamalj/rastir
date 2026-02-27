"""Tool utility adapter.

Minimal adapter for tool results. Tools have arbitrary return types,
so this adapter does not extract structured metadata. Duration and
success/failure are handled by the @tool decorator itself.

This adapter simply recognizes that a result was handled so the
fallback adapter is not invoked unnecessarily, but intentionally
produces no semantic fields.

Priority: 10 (just above fallback, below retrieval).
"""

from __future__ import annotations

from typing import Any

from rastir.adapters.types import AdapterResult, BaseAdapter


class ToolAdapter(BaseAdapter):
    """No-op adapter for tool results — duration + status handled by decorator."""

    name = "tool"
    kind = "provider"
    priority = 10

    supports_tokens = False
    supports_streaming = False

    def can_handle(self, result: Any) -> bool:
        """Always returns False.

        Tool results have arbitrary types; there is no reliable way to
        detect a "tool result" generically. The @tool decorator records
        duration and success/failure directly without relying on adapter
        extraction for metadata. The fallback adapter handles any
        remaining cases.
        """
        return False

    def transform(self, result: Any) -> AdapterResult:
        return AdapterResult()
