"""Adapter registry — resolves LLM responses through the 3-phase pipeline.

Phase 1: Framework unwrap (priority 200-300)
Phase 2: Provider extraction (priority 100-199)
Phase 3: Fallback (priority 0)

Adapters are registered at import time. Resolution is O(N) where N is
the number of registered adapters.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

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

    # Generic fallback: scan kwargs for common model parameter names.
    # This captures model metadata even when no provider adapter matches,
    # ensuring model survives on the span even if the call fails.
    meta = _scan_common_model_kwargs(kwargs)
    if meta:
        return meta

    return None


# Common parameter names for model across SDKs / user functions.
_COMMON_MODEL_KWARGS = ("model", "model_id", "modelId", "model_name")

# Attribute names to probe on objects passed as arguments (e.g.
# LangChain chat model objects like ChatOpenAI, ChatAnthropic, ChatBedrock).
_COMMON_MODEL_ATTRS = ("model_name", "model", "model_id", "modelId")


def _scan_common_model_kwargs(kwargs: dict) -> Optional[RequestMetadata]:
    """Fallback: extract model from common kwarg names or object attributes.

    When no adapter's can_handle_request() matched:
    1. Look for well-known parameter names (model, model_id, …) with string values.
    2. Scan the *values* of all arguments for objects that expose a model
       attribute (e.g. ``ChatOpenAI.model_name``, ``ChatAnthropic.model``).

    This ensures request-phase metadata is captured even for unknown
    providers, improving resilience when the API call fails before
    producing a response.
    """
    # 1. String kwargs named "model", "model_id", etc.
    for key in _COMMON_MODEL_KWARGS:
        value = kwargs.get(key)
        if value and isinstance(value, str):
            return RequestMetadata(
                span_attributes={"model": value},
                extra_attributes={},
            )

    # 2. Object introspection: scan all kwarg values for model attributes.
    for value in kwargs.values():
        meta = _extract_model_from_object(value)
        if meta:
            return meta

    return None


def _extract_model_from_object(obj: Any) -> Optional[RequestMetadata]:
    """Try to read a model name from an object's attributes.

    Handles LangChain chat model objects (ChatOpenAI, ChatAnthropic,
    ChatBedrock) and LangGraph CompiledGraph objects that contain
    a chat model inside their nodes.
    """
    # Direct attribute probe on the object itself
    for attr in _COMMON_MODEL_ATTRS:
        try:
            val = getattr(obj, attr, None)
            if val and isinstance(val, str):
                return RequestMetadata(
                    span_attributes={"model": val},
                    extra_attributes={},
                )
        except Exception:
            continue

    # LangGraph CompiledGraph: walk nodes to find the bound chat model.
    nodes = getattr(obj, "nodes", None)
    if isinstance(nodes, dict):
        for node in nodes.values():
            for accessor in ("bound", "func"):
                inner = getattr(node, accessor, None)
                if inner is not None:
                    meta = _extract_model_from_object(inner)
                    if meta:
                        return meta

    # RunnableBinding (LangChain) wraps another Runnable in .bound
    bound = getattr(obj, "bound", None)
    if bound is not None and bound is not obj:
        meta = _extract_model_from_object(bound)
        if meta:
            return meta

    # RunnableSequence first/last
    first = getattr(obj, "first", None)
    if first is not None and first is not obj:
        meta = _extract_model_from_object(first)
        if meta:
            return meta

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
