"""Adapter registry — resolves LLM responses through the 3-phase pipeline.

Phase 1: Framework unwrap (priority 200-300)
Phase 2: Provider extraction (priority 100-199)
Phase 3: Fallback (priority 0)

Adapters are registered at import time. Resolution is O(N) where N is
the number of registered adapters.
"""

from __future__ import annotations

import logging
from typing import Optional

from rastir.adapters.types import AdapterResult, BaseAdapter, RequestMetadata, TokenDelta

logger = logging.getLogger("rastir")

# Global adapter registry, sorted by priority (descending) at registration.
_adapters: list[BaseAdapter] = []
_sorted = False


def register(adapter: BaseAdapter) -> None:
    """Register an adapter. Can be called at import time."""
    global _sorted
    _adapters.append(adapter)
    _sorted = False
    logger.debug("Registered adapter: %s (kind=%s, priority=%d)", adapter.name, adapter.kind, adapter.priority)


def _ensure_sorted() -> None:
    """Sort adapters by descending priority (lazy, once)."""
    global _sorted
    if not _sorted:
        _adapters.sort(key=lambda a: a.priority, reverse=True)
        _sorted = True


def resolve(result: object) -> Optional[AdapterResult]:
    """Run the 3-phase adapter resolution pipeline.

    Phase 1: Framework adapters unwrap the result (may recurse).
    Phase 2: Provider adapters extract metadata.
    Phase 3: Fallback adapter returns unknown.

    Returns the merged AdapterResult, or None if no adapters registered.
    """
    if not _adapters:
        return None

    _ensure_sorted()

    unwrapped = result
    framework_attrs: dict[str, object] = {}
    framework_tokens_input: int | None = None
    framework_tokens_output: int | None = None
    framework_model: str | None = None
    framework_finish_reason: str | None = None

    # Phase 1: Framework unwrap
    max_unwrap = 5  # prevent infinite loops
    for _ in range(max_unwrap):
        matched = False
        for adapter in _adapters:
            if adapter.kind != "framework":
                continue
            if adapter.can_handle(unwrapped):
                try:
                    ar = adapter.transform(unwrapped)
                    # Always capture framework extra_attributes
                    framework_attrs.update(ar.extra_attributes)
                    # Capture framework-level metadata if provided
                    if ar.tokens_input is not None:
                        framework_tokens_input = ar.tokens_input
                    if ar.tokens_output is not None:
                        framework_tokens_output = ar.tokens_output
                    if ar.model and ar.model != "unknown":
                        framework_model = ar.model
                    if ar.finish_reason and ar.finish_reason != "unknown":
                        framework_finish_reason = ar.finish_reason
                    if ar.unwrapped_result is not None:
                        unwrapped = ar.unwrapped_result
                        matched = True
                        break  # restart framework phase with unwrapped result
                except Exception:
                    logger.debug("Framework adapter %s failed", adapter.name, exc_info=True)
        if not matched:
            break

    def _merge_framework_metadata(ar: AdapterResult) -> AdapterResult:
        """Merge framework-level metadata into the adapter result."""
        for k, v in framework_attrs.items():
            if k not in ar.extra_attributes:
                ar.extra_attributes[k] = v
        if ar.tokens_input is None and framework_tokens_input is not None:
            ar.tokens_input = framework_tokens_input
        if ar.tokens_output is None and framework_tokens_output is not None:
            ar.tokens_output = framework_tokens_output
        if (ar.model is None or ar.model == "unknown") and framework_model:
            ar.model = framework_model
        if (ar.finish_reason is None or ar.finish_reason == "unknown") and framework_finish_reason:
            ar.finish_reason = framework_finish_reason
        return ar

    # Phase 2: Provider extraction
    for adapter in _adapters:
        if adapter.kind != "provider":
            continue
        if adapter.can_handle(unwrapped):
            try:
                ar = adapter.transform(unwrapped)
                return _merge_framework_metadata(ar)
            except Exception:
                logger.debug("Provider adapter %s failed", adapter.name, exc_info=True)

    # Phase 3: Fallback
    for adapter in _adapters:
        if adapter.kind != "fallback":
            continue
        try:
            ar = adapter.transform(unwrapped)
            return _merge_framework_metadata(ar)
        except Exception:
            logger.debug("Fallback adapter %s failed", adapter.name, exc_info=True)

    return None


def resolve_stream_chunk(chunk: object) -> Optional[TokenDelta]:
    """Find an adapter that can handle a streaming chunk and extract delta."""
    _ensure_sorted()

    for adapter in _adapters:
        if adapter.can_handle_stream(chunk):
            try:
                return adapter.extract_stream_delta(chunk)
            except Exception:
                logger.debug("Stream adapter %s failed", adapter.name, exc_info=True)
    return None


def resolve_request(args: tuple, kwargs: dict) -> Optional[RequestMetadata]:
    """Run request-phase extraction across adapters.

    Called before function execution to extract request-level metadata
    (e.g., Bedrock guardrail configuration from kwargs).

    Only adapters with supports_request_metadata=True are considered.
    Returns merged RequestMetadata from the first matching adapter,
    or None if no adapter handles the request.
    """
    if not _adapters:
        return None

    _ensure_sorted()

    for adapter in _adapters:
        if not adapter.supports_request_metadata:
            continue
        if adapter.can_handle_request(args, kwargs):
            try:
                return adapter.extract_request_metadata(args, kwargs)
            except Exception:
                logger.debug("Request adapter %s failed", adapter.name, exc_info=True)
    return None


def clear_registry() -> None:
    """Clear all registered adapters. For testing only."""
    global _sorted
    _adapters.clear()
    _sorted = False


def get_registered_adapters() -> list[BaseAdapter]:
    """Return a copy of the registered adapters list."""
    _ensure_sorted()
    return list(_adapters)
