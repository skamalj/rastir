"""Generic object wrapper for Rastir instrumentation.

Provides ``wrap(obj)`` which intercepts public methods on any Python
object, creating a span for each call. This enables observability for
infrastructure components (caches, databases, vector stores, etc.)
without requiring decorator access to their source code.

Design decisions:
  - Reuses the internal span engine (start_span → end_span → enqueue_span)
  - Supports both sync and async methods
  - Default span type is ``INFRA``; overridable to any SpanType
  - Prevents double-wrapping via a ``_rastir_wrapped`` marker
  - Preserves ``isinstance`` by using ``__class__`` delegation
  - Records method name, args count, kwargs keys as span attributes

Usage:
    import rastir

    wrapped_cache = rastir.wrap(redis_client, name="redis")
    wrapped_cache.get("key")  # creates an INFRA span: "redis.get"
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
from typing import Any, Optional

from rastir.context import end_span, start_span
from rastir.queue import enqueue_span
from rastir.spans import SpanStatus, SpanType

logger = logging.getLogger("rastir")

_WRAPPABLE_SPAN_TYPES = {
    "infra": SpanType.INFRA,
    "tool": SpanType.TOOL,
    "llm": SpanType.LLM,
    "trace": SpanType.TRACE,
    "agent": SpanType.AGENT,
    "retrieval": SpanType.RETRIEVAL,
}

# Marker attribute to prevent double-wrapping
_WRAPPED_MARKER = "_rastir_wrapped"


def wrap(
    obj: Any,
    *,
    name: str | None = None,
    span_type: str = "infra",
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> Any:
    """Wrap an object so its public methods emit Rastir spans.

    Args:
        obj: The object to wrap. Can be any Python object with callable
            public methods.
        name: Prefix for span names. Defaults to the class name.
            E.g., ``name="redis"`` → spans named ``redis.get``,
            ``redis.set``, etc.
        span_type: The span type for wrapped methods. Must be one of:
            ``"infra"`` (default), ``"tool"``, ``"llm"``, ``"trace"``,
            ``"agent"``, ``"retrieval"``.
        include: If provided, only wrap these method names.
        exclude: If provided, skip these method names. Applied after
            include.

    Returns:
        A proxy object that wraps the original, emitting spans for each
        method call. The returned object preserves ``isinstance``
        behaviour.

    Raises:
        ValueError: If ``span_type`` is not a recognised type.
        TypeError: If ``obj`` is already wrapped.
    """
    # Validate span type
    resolved_type = _WRAPPABLE_SPAN_TYPES.get(span_type.lower())
    if resolved_type is None:
        raise ValueError(
            f"Unknown span_type {span_type!r}. "
            f"Valid types: {sorted(_WRAPPABLE_SPAN_TYPES.keys())}"
        )

    # Prevent double-wrapping
    if getattr(obj, _WRAPPED_MARKER, False):
        logger.debug("Object %r is already wrapped, returning as-is", obj)
        return obj

    resolved_name = name or type(obj).__name__
    exclude_set = set(exclude) if exclude else set()

    return _WrappedProxy(
        obj,
        prefix=resolved_name,
        span_type=resolved_type,
        include=set(include) if include else None,
        exclude=exclude_set,
    )


class _WrappedProxy:
    """Transparent proxy that intercepts method calls."""

    __slots__ = (
        "_wrapped_obj",
        "_prefix",
        "_span_type",
        "_include",
        "_exclude",
        "_method_cache",
    )

    def __init__(
        self,
        obj: Any,
        prefix: str,
        span_type: SpanType,
        include: set[str] | None,
        exclude: set[str],
    ) -> None:
        # Use object.__setattr__ to bypass __setattr__ override
        object.__setattr__(self, "_wrapped_obj", obj)
        object.__setattr__(self, "_prefix", prefix)
        object.__setattr__(self, "_span_type", span_type)
        object.__setattr__(self, "_include", include)
        object.__setattr__(self, "_exclude", exclude)
        object.__setattr__(self, "_method_cache", {})

    @property  # type: ignore[misc]
    def __class__(self) -> type:
        """Preserve isinstance() by delegating __class__."""
        return type(object.__getattribute__(self, "_wrapped_obj"))

    def __getattr__(self, attr: str) -> Any:
        obj = object.__getattribute__(self, "_wrapped_obj")
        original = getattr(obj, attr)

        # Only wrap public callables
        if attr.startswith("_") or not callable(original):
            return original

        # Check include/exclude filters
        include = object.__getattribute__(self, "_include")
        exclude = object.__getattribute__(self, "_exclude")
        if include is not None and attr not in include:
            return original
        if attr in exclude:
            return original

        # Check cache
        cache = object.__getattribute__(self, "_method_cache")
        if attr in cache:
            return cache[attr]

        prefix = object.__getattribute__(self, "_prefix")
        span_type = object.__getattribute__(self, "_span_type")
        span_name = f"{prefix}.{attr}"

        if asyncio.iscoroutinefunction(original):
            wrapper = _make_async_wrapper(original, span_name, span_type)
        else:
            wrapper = _make_sync_wrapper(original, span_name, span_type)

        cache[attr] = wrapper
        return wrapper

    def __setattr__(self, attr: str, value: Any) -> None:
        obj = object.__getattribute__(self, "_wrapped_obj")
        setattr(obj, attr, value)

    def __delattr__(self, attr: str) -> None:
        obj = object.__getattribute__(self, "_wrapped_obj")
        delattr(obj, attr)

    def __repr__(self) -> str:
        obj = object.__getattribute__(self, "_wrapped_obj")
        prefix = object.__getattribute__(self, "_prefix")
        return f"<rastir.wrap({prefix}): {obj!r}>"

    def __str__(self) -> str:
        obj = object.__getattribute__(self, "_wrapped_obj")
        return str(obj)

    # Mark as wrapped to prevent double-wrapping
    @property
    def _rastir_wrapped(self) -> bool:
        return True


def _make_sync_wrapper(
    method: Any, span_name: str, span_type: SpanType
) -> Any:
    """Create a sync wrapper that emits a span."""

    @functools.wraps(method)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        span, token = start_span(span_name, span_type)
        span.attributes["wrap.method"] = method.__name__
        span.attributes["wrap.args_count"] = len(args)
        if kwargs:
            span.attributes["wrap.kwargs_keys"] = sorted(kwargs.keys())
        try:
            result = method(*args, **kwargs)
            span.finish(SpanStatus.OK)
            return result
        except BaseException as exc:
            span.record_error(exc)
            span.finish(SpanStatus.ERROR)
            raise
        finally:
            end_span(token)
            enqueue_span(span)

    return wrapper


def _make_async_wrapper(
    method: Any, span_name: str, span_type: SpanType
) -> Any:
    """Create an async wrapper that emits a span."""

    @functools.wraps(method)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        span, token = start_span(span_name, span_type)
        span.attributes["wrap.method"] = method.__name__
        span.attributes["wrap.args_count"] = len(args)
        if kwargs:
            span.attributes["wrap.kwargs_keys"] = sorted(kwargs.keys())
        try:
            result = await method(*args, **kwargs)
            span.finish(SpanStatus.OK)
            return result
        except BaseException as exc:
            span.record_error(exc)
            span.finish(SpanStatus.ERROR)
            raise
        finally:
            end_span(token)
            enqueue_span(span)

    return wrapper
