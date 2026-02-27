"""Decorators for Rastir instrumentation.

Provides the six semantic decorators:
- @trace      — root/general span, entry points
- @agent      — agent span, sets agent identity in context
- @metric     — generic function metrics only (no span)
- @llm        — LLM call span + adapter-based metadata extraction
- @tool       — tool execution span
- @retrieval  — retrieval/vector operation span

All decorators support both sync and async functions.
Decorators never perform network I/O — they emit spans to an internal queue.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import time
from typing import Any, Callable, Optional, TypeVar, overload

from rastir.context import (
    end_span,
    get_current_agent,
    reset_current_agent,
    set_current_agent,
    start_span,
)
from rastir.queue import enqueue_span
from rastir.spans import SpanRecord, SpanStatus, SpanType

logger = logging.getLogger("rastir")

F = TypeVar("F", bound=Callable[..., Any])


# ---------------------------------------------------------------------------
# @trace
# ---------------------------------------------------------------------------


@overload
def trace(func: F) -> F: ...


@overload
def trace(
    *,
    name: str | None = None,
    emit_metric: bool = False,
) -> Callable[[F], F]: ...


def trace(
    func: F | None = None,
    *,
    name: str | None = None,
    emit_metric: bool = False,
) -> F | Callable[[F], F]:
    """Create a trace span around a function.

    Can be used bare (@trace) or with arguments (@trace(name="my_op")).

    Args:
        func: The function to decorate (when used bare).
        name: Span name. Defaults to the function name.
        emit_metric: If True, record duration as a span attribute for
            metric emission. Independent of @metric.
    """

    def decorator(fn: F) -> F:
        span_name = name or fn.__name__

        if asyncio.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                span, token = start_span(span_name, SpanType.TRACE)
                if emit_metric:
                    span.set_attribute("emit_metric", True)
                try:
                    result = await fn(*args, **kwargs)
                    span.finish(SpanStatus.OK)
                    return result
                except BaseException as exc:
                    span.record_error(exc)
                    span.finish(SpanStatus.ERROR)
                    raise
                finally:
                    end_span(token)
                    enqueue_span(span)

            return async_wrapper  # type: ignore[return-value]

        else:

            @functools.wraps(fn)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                span, token = start_span(span_name, SpanType.TRACE)
                if emit_metric:
                    span.set_attribute("emit_metric", True)
                try:
                    result = fn(*args, **kwargs)
                    span.finish(SpanStatus.OK)
                    return result
                except BaseException as exc:
                    span.record_error(exc)
                    span.finish(SpanStatus.ERROR)
                    raise
                finally:
                    end_span(token)
                    enqueue_span(span)

            return sync_wrapper  # type: ignore[return-value]

    if func is not None:
        return decorator(func)
    return decorator  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# @agent
# ---------------------------------------------------------------------------


@overload
def agent(func: F) -> F: ...


@overload
def agent(
    *,
    agent_name: str | None = None,
) -> Callable[[F], F]: ...


def agent(
    func: F | None = None,
    *,
    agent_name: str | None = None,
) -> F | Callable[[F], F]:
    """Mark a function as an agent entry point.

    Creates an agent-typed span and sets the agent identity in context
    so child @llm / @tool / @retrieval spans inherit the agent label.

    Args:
        func: The function to decorate (when used bare).
        agent_name: Agent name. Defaults to the function name.
    """

    def decorator(fn: F) -> F:
        resolved_name = agent_name or fn.__name__

        if asyncio.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                span, span_token = start_span(resolved_name, SpanType.AGENT)
                span.set_attribute("agent_name", resolved_name)
                agent_token = set_current_agent(resolved_name)
                try:
                    result = await fn(*args, **kwargs)
                    span.finish(SpanStatus.OK)
                    return result
                except BaseException as exc:
                    span.record_error(exc)
                    span.finish(SpanStatus.ERROR)
                    raise
                finally:
                    reset_current_agent(agent_token)
                    end_span(span_token)
                    enqueue_span(span)

            return async_wrapper  # type: ignore[return-value]

        else:

            @functools.wraps(fn)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                span, span_token = start_span(resolved_name, SpanType.AGENT)
                span.set_attribute("agent_name", resolved_name)
                agent_token = set_current_agent(resolved_name)
                try:
                    result = fn(*args, **kwargs)
                    span.finish(SpanStatus.OK)
                    return result
                except BaseException as exc:
                    span.record_error(exc)
                    span.finish(SpanStatus.ERROR)
                    raise
                finally:
                    reset_current_agent(agent_token)
                    end_span(span_token)
                    enqueue_span(span)

            return sync_wrapper  # type: ignore[return-value]

    if func is not None:
        return decorator(func)
    return decorator  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# @metric
# ---------------------------------------------------------------------------


@overload
def metric(func: F) -> F: ...


@overload
def metric(
    *,
    name: str | None = None,
) -> Callable[[F], F]: ...


def metric(
    func: F | None = None,
    *,
    name: str | None = None,
) -> F | Callable[[F], F]:
    """Emit generic function-level metrics (calls, duration, failures).

    Creates a metric-type span that the server will use to derive:
    - <name>_calls_total
    - <name>_duration_seconds
    - <name>_failures_total

    No AI-specific logic. Independent from @trace.

    Args:
        func: The function to decorate (when used bare).
        name: Metric base name. Defaults to the function name.
    """

    def decorator(fn: F) -> F:
        metric_name = name or fn.__name__

        if asyncio.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                span, token = start_span(metric_name, SpanType.METRIC)
                span.set_attribute("metric_name", metric_name)
                try:
                    result = await fn(*args, **kwargs)
                    span.finish(SpanStatus.OK)
                    return result
                except BaseException as exc:
                    span.record_error(exc)
                    span.finish(SpanStatus.ERROR)
                    raise
                finally:
                    end_span(token)
                    enqueue_span(span)

            return async_wrapper  # type: ignore[return-value]

        else:

            @functools.wraps(fn)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                span, token = start_span(metric_name, SpanType.METRIC)
                span.set_attribute("metric_name", metric_name)
                try:
                    result = fn(*args, **kwargs)
                    span.finish(SpanStatus.OK)
                    return result
                except BaseException as exc:
                    span.record_error(exc)
                    span.finish(SpanStatus.ERROR)
                    raise
                finally:
                    end_span(token)
                    enqueue_span(span)

            return sync_wrapper  # type: ignore[return-value]

    if func is not None:
        return decorator(func)
    return decorator  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# @llm
# ---------------------------------------------------------------------------


def llm(
    func: F | None = None,
    *,
    model: str | None = None,
    provider: str | None = None,
    streaming: bool | None = None,
) -> F | Callable[[F], F]:
    """Instrument an LLM call.

    Creates an LLM-typed span, runs adapter resolution on the return value
    to extract model, provider, tokens, and cost. If the function returns
    a generator/async-generator, automatically switches to streaming
    accumulation mode.

    Args:
        func: The function to decorate (when used bare).
        model: Override model name (adapter auto-detects if not set).
        provider: Override provider name (adapter auto-detects if not set).
        streaming: Force streaming mode. Auto-detected from return type
            if not set.
    """

    def decorator(fn: F) -> F:
        # Determine if the function is a generator/async-generator at definition time
        is_async = asyncio.iscoroutinefunction(fn)
        is_gen = inspect.isgeneratorfunction(fn)
        is_async_gen = inspect.isasyncgenfunction(fn)

        if is_async_gen or (streaming is True and is_async):

            @functools.wraps(fn)
            async def async_gen_wrapper(*args: Any, **kwargs: Any) -> Any:
                span, token = start_span(fn.__name__, SpanType.LLM)
                _set_llm_base_attrs(span, model, provider)
                _extract_request_metadata(span, args, kwargs)
                try:
                    async for chunk in fn(*args, **kwargs):
                        yield chunk
                        _accumulate_stream_chunk(span, chunk)
                    span.finish(SpanStatus.OK)
                except BaseException as exc:
                    span.record_error(exc)
                    span.finish(SpanStatus.ERROR)
                    raise
                finally:
                    _finalize_llm_span(span)
                    end_span(token)
                    enqueue_span(span)

            return async_gen_wrapper  # type: ignore[return-value]

        elif is_gen or (streaming is True and not is_async):

            @functools.wraps(fn)
            def gen_wrapper(*args: Any, **kwargs: Any) -> Any:
                span, token = start_span(fn.__name__, SpanType.LLM)
                _set_llm_base_attrs(span, model, provider)
                _extract_request_metadata(span, args, kwargs)
                try:
                    for chunk in fn(*args, **kwargs):
                        yield chunk
                        _accumulate_stream_chunk(span, chunk)
                    span.finish(SpanStatus.OK)
                except BaseException as exc:
                    span.record_error(exc)
                    span.finish(SpanStatus.ERROR)
                    raise
                finally:
                    _finalize_llm_span(span)
                    end_span(token)
                    enqueue_span(span)

            return gen_wrapper  # type: ignore[return-value]

        elif is_async:

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                span, token = start_span(fn.__name__, SpanType.LLM)
                _set_llm_base_attrs(span, model, provider)
                _extract_request_metadata(span, args, kwargs)
                try:
                    result = await fn(*args, **kwargs)
                    _extract_llm_metadata(span, result)
                    span.finish(SpanStatus.OK)
                    return result
                except BaseException as exc:
                    span.record_error(exc)
                    span.finish(SpanStatus.ERROR)
                    raise
                finally:
                    _finalize_llm_span(span)
                    end_span(token)
                    enqueue_span(span)

            return async_wrapper  # type: ignore[return-value]

        else:

            @functools.wraps(fn)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                span, token = start_span(fn.__name__, SpanType.LLM)
                _set_llm_base_attrs(span, model, provider)
                _extract_request_metadata(span, args, kwargs)
                try:
                    result = fn(*args, **kwargs)
                    _extract_llm_metadata(span, result)
                    span.finish(SpanStatus.OK)
                    return result
                except BaseException as exc:
                    span.record_error(exc)
                    span.finish(SpanStatus.ERROR)
                    raise
                finally:
                    _finalize_llm_span(span)
                    end_span(token)
                    enqueue_span(span)

            return sync_wrapper  # type: ignore[return-value]

    if func is not None:
        return decorator(func)
    return decorator  # type: ignore[return-value]


def _set_llm_base_attrs(
    span: SpanRecord,
    model_override: str | None,
    provider_override: str | None,
) -> None:
    """Set base LLM attributes on the span (before adapter resolution)."""
    if model_override:
        span.set_attribute("model", model_override)
    if provider_override:
        span.set_attribute("provider", provider_override)

    # Inject agent label from context if running under @agent
    agent_name = get_current_agent()
    if agent_name:
        span.set_attribute("agent", agent_name)


def _extract_request_metadata(span: SpanRecord, args: tuple, kwargs: dict) -> None:
    """Run request-phase adapter extraction (pre-invocation).

    Provider-agnostic: passes args/kwargs to the adapter registry.
    Adapters that support request metadata (e.g., Bedrock guardrails)
    can inspect the call arguments and annotate the span.
    """
    try:
        from rastir.adapters.registry import resolve_request
        req_meta = resolve_request(args, kwargs)
        if req_meta:
            for k, v in req_meta.span_attributes.items():
                span.set_attribute(k, v)
            for k, v in req_meta.extra_attributes.items():
                span.set_attribute(k, v)
    except ImportError:
        pass
    except Exception:
        logger.debug("Request metadata extraction failed", exc_info=True)


def _extract_llm_metadata(span: SpanRecord, result: Any) -> None:
    """Run adapter resolution on a non-streaming LLM result.

    Adapters are imported lazily to avoid circular dependencies.
    If no adapter is registered yet, falls back to unknown.
    """
    try:
        from rastir.adapters.registry import resolve
        adapter_result = resolve(result)
        if adapter_result:
            # Only set if not already overridden by decorator params
            if "model" not in span.attributes:
                span.set_attribute("model", adapter_result.model or "unknown")
            if "provider" not in span.attributes:
                span.set_attribute("provider", adapter_result.provider or "unknown")
            if adapter_result.tokens_input is not None:
                span.set_attribute("tokens_input", adapter_result.tokens_input)
            if adapter_result.tokens_output is not None:
                span.set_attribute("tokens_output", adapter_result.tokens_output)
            if adapter_result.finish_reason:
                span.set_attribute("finish_reason", adapter_result.finish_reason)
            if adapter_result.extra_attributes:
                for k, v in adapter_result.extra_attributes.items():
                    span.set_attribute(k, v)
    except ImportError:
        logger.debug("Adapter registry not available, skipping metadata extraction")
    except Exception:
        logger.debug("Adapter resolution failed", exc_info=True)

    # Ensure model and provider are always set
    if "model" not in span.attributes:
        span.set_attribute("model", "unknown")
    if "provider" not in span.attributes:
        span.set_attribute("provider", "unknown")


def _accumulate_stream_chunk(span: SpanRecord, chunk: Any) -> None:
    """Accumulate token deltas from a streaming chunk.

    Tries adapter-based stream extraction. If no adapter handles
    the chunk, silently skips.
    """
    try:
        from rastir.adapters.registry import resolve_stream_chunk
        delta = resolve_stream_chunk(chunk)
        if delta:
            current_in = span.attributes.get("tokens_input", 0)
            current_out = span.attributes.get("tokens_output", 0)
            span.set_attribute("tokens_input", current_in + (delta.tokens_input or 0))
            span.set_attribute("tokens_output", current_out + (delta.tokens_output or 0))
            # Capture model/provider from first chunk that has it
            if delta.model and "model" not in span.attributes:
                span.set_attribute("model", delta.model)
            if delta.provider and "provider" not in span.attributes:
                span.set_attribute("provider", delta.provider)
    except ImportError:
        pass
    except Exception:
        logger.debug("Stream chunk extraction failed", exc_info=True)


def _finalize_llm_span(span: SpanRecord) -> None:
    """Ensure model/provider are set before the span is enqueued."""
    if "model" not in span.attributes:
        span.set_attribute("model", "unknown")
    if "provider" not in span.attributes:
        span.set_attribute("provider", "unknown")


# ---------------------------------------------------------------------------
# @tool
# ---------------------------------------------------------------------------


@overload
def tool(func: F) -> F: ...


@overload
def tool(
    *,
    tool_name: str | None = None,
) -> Callable[[F], F]: ...


def tool(
    func: F | None = None,
    *,
    tool_name: str | None = None,
) -> F | Callable[[F], F]:
    """Instrument a tool function call.

    Creates a tool-typed span with tool_name and agent label (if under @agent).

    Args:
        func: The function to decorate (when used bare).
        tool_name: Tool name. Defaults to the function name.
    """

    def decorator(fn: F) -> F:
        resolved_name = tool_name or fn.__name__

        if asyncio.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                span, token = start_span(resolved_name, SpanType.TOOL)
                span.set_attribute("tool_name", resolved_name)
                agent_name = get_current_agent()
                if agent_name:
                    span.set_attribute("agent", agent_name)
                try:
                    result = await fn(*args, **kwargs)
                    span.finish(SpanStatus.OK)
                    return result
                except BaseException as exc:
                    span.record_error(exc)
                    span.finish(SpanStatus.ERROR)
                    raise
                finally:
                    end_span(token)
                    enqueue_span(span)

            return async_wrapper  # type: ignore[return-value]

        else:

            @functools.wraps(fn)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                span, token = start_span(resolved_name, SpanType.TOOL)
                span.set_attribute("tool_name", resolved_name)
                agent_name = get_current_agent()
                if agent_name:
                    span.set_attribute("agent", agent_name)
                try:
                    result = fn(*args, **kwargs)
                    span.finish(SpanStatus.OK)
                    return result
                except BaseException as exc:
                    span.record_error(exc)
                    span.finish(SpanStatus.ERROR)
                    raise
                finally:
                    end_span(token)
                    enqueue_span(span)

            return sync_wrapper  # type: ignore[return-value]

    if func is not None:
        return decorator(func)
    return decorator  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# @retrieval
# ---------------------------------------------------------------------------


def retrieval(
    func: F | None = None,
    *,
    name: str | None = None,
    doc_count_extractor: Callable[[Any], int] | None = None,
) -> F | Callable[[F], F]:
    """Instrument a retrieval/vector search function.

    Creates a retrieval-typed span. Attempts to extract document count
    from the return value via adapter logic or a user-supplied extractor.

    Args:
        func: The function to decorate (when used bare).
        name: Span name. Defaults to the function name.
        doc_count_extractor: Optional callable to extract document count
            from the return value. E.g., lambda r: len(r.hits).
    """

    def decorator(fn: F) -> F:
        span_name = name or fn.__name__

        if asyncio.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                span, token = start_span(span_name, SpanType.RETRIEVAL)
                agent_name = get_current_agent()
                if agent_name:
                    span.set_attribute("agent", agent_name)
                try:
                    result = await fn(*args, **kwargs)
                    _extract_doc_count(span, result, doc_count_extractor)
                    span.finish(SpanStatus.OK)
                    return result
                except BaseException as exc:
                    span.record_error(exc)
                    span.finish(SpanStatus.ERROR)
                    raise
                finally:
                    end_span(token)
                    enqueue_span(span)

            return async_wrapper  # type: ignore[return-value]

        else:

            @functools.wraps(fn)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                span, token = start_span(span_name, SpanType.RETRIEVAL)
                agent_name = get_current_agent()
                if agent_name:
                    span.set_attribute("agent", agent_name)
                try:
                    result = fn(*args, **kwargs)
                    _extract_doc_count(span, result, doc_count_extractor)
                    span.finish(SpanStatus.OK)
                    return result
                except BaseException as exc:
                    span.record_error(exc)
                    span.finish(SpanStatus.ERROR)
                    raise
                finally:
                    end_span(token)
                    enqueue_span(span)

            return sync_wrapper  # type: ignore[return-value]

    if func is not None:
        return decorator(func)
    return decorator  # type: ignore[return-value]


def _extract_doc_count(
    span: SpanRecord,
    result: Any,
    extractor: Callable[[Any], int] | None,
) -> None:
    """Try to determine document count from the retrieval result."""
    count: int | None = None

    # 1. User-supplied extractor takes priority
    if extractor is not None:
        try:
            count = extractor(result)
        except Exception:
            logger.debug("Custom doc_count_extractor failed", exc_info=True)

    # 2. Try common patterns
    if count is None:
        try:
            if isinstance(result, list):
                count = len(result)
            elif hasattr(result, "documents"):
                count = len(result.documents)
            elif hasattr(result, "page_content"):
                count = 1  # single document
        except Exception:
            logger.debug("Auto doc count extraction failed", exc_info=True)

    if count is not None:
        span.set_attribute("retrieved_documents_count", count)
