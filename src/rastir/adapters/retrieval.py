"""Retrieval utility adapter.

Extracts document count from retrieval results. This is a lightweight
adapter that returns document count in extra_attributes so the @retrieval
decorator can emit the `retrieved_documents_count` metric.

Handles:
  - list / tuple results → len(result)
  - Objects with .documents attribute → len(result.documents)
  - Objects with .page_content attribute → 1 (single document)

Priority: 50 (utility, below provider adapters, above fallback).
"""

from __future__ import annotations

from typing import Any

from rastir.adapters.types import AdapterResult, BaseAdapter


class RetrievalAdapter(BaseAdapter):
    """Adapter for retrieval results — extracts document count."""

    name = "retrieval"
    kind = "provider"
    priority = 50

    supports_tokens = False
    supports_streaming = False

    def can_handle(self, result: Any) -> bool:
        """Match list/tuple or objects with .documents or .page_content."""
        if isinstance(result, (list, tuple)):
            return True
        if hasattr(result, "documents"):
            return True
        if hasattr(result, "page_content"):
            return True
        return False

    def transform(self, result: Any) -> AdapterResult:
        doc_count = self._extract_count(result)
        extras: dict[str, Any] = {}
        if doc_count is not None:
            extras["retrieved_documents_count"] = doc_count

        return AdapterResult(
            model=None,
            provider=None,
            extra_attributes=extras,
        )

    @staticmethod
    def _extract_count(result: Any) -> int | None:
        """Try to compute a document count from the result."""
        if isinstance(result, (list, tuple)):
            return len(result)

        docs = getattr(result, "documents", None)
        if docs is not None:
            try:
                return len(docs)
            except TypeError:
                return None

        # Single document with .page_content
        if hasattr(result, "page_content"):
            return 1

        return None
