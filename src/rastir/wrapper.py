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

from rastir.context import end_span, get_current_agent, start_span
from rastir.queue import enqueue_span
from rastir.spans import SpanStatus, SpanType

logger = logging.getLogger("rastir")

# Attributes to probe for a model display name on wrapped LLM objects.
_MODEL_NAME_ATTRS = ("model_name", "model", "model_id", "modelId")

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


def _is_mcp_session(obj: Any) -> bool:
    """Return True if *obj* is an MCP ClientSession (or already wrapped)."""
    cls = type(obj)
    module = getattr(cls, "__module__", "") or ""
    # Match mcp.client.session.ClientSession (standard MCP SDK)
    if cls.__name__ == "ClientSession" and "mcp" in module:
        return True
    # Already wrapped by wrap_mcp — has the marker
    if getattr(obj, "_rastir_mcp_wrapped", False):
        return True
    return False


def wrap(
    obj: Any,
    *,
    name: str | None = None,
    span_type: str = "infra",
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> Any:
    """Wrap an object so its public methods emit Rastir spans.

    **Smart detection**: if *obj* is an MCP ``ClientSession`` (from the
    ``mcp`` package), ``wrap()`` automatically delegates to the
    MCP-specific proxy that intercepts ``call_tool()`` and injects
    distributed trace context.  This means you can always use
    ``wrap(session)`` instead of the more explicit ``wrap_mcp(session)``.

    Args:
        obj: The object to wrap. Can be any Python object with callable
            public methods, or an MCP ``ClientSession``.
        name: Prefix for span names. Defaults to the class name.
            E.g., ``name="redis"`` → spans named ``redis.get``,
            ``redis.set``, etc.  Ignored for MCP sessions.
        span_type: The span type for wrapped methods. Must be one of:
            ``"infra"`` (default), ``"tool"``, ``"llm"``, ``"trace"``,
            ``"agent"``, ``"retrieval"``.  Ignored for MCP sessions.
        include: If provided, only wrap these method names.  Ignored for
            MCP sessions.
        exclude: If provided, skip these method names. Applied after
            include.  Ignored for MCP sessions.

    Returns:
        A proxy object that wraps the original, emitting spans for each
        method call. The returned object preserves ``isinstance``
        behaviour.

    Raises:
        ValueError: If ``span_type`` is not a recognised type.
        TypeError: If ``obj`` is already wrapped.
    """
    # --- MCP ClientSession auto-detection ---
    if _is_mcp_session(obj):
        from rastir.remote import wrap_mcp
        return wrap_mcp(obj)
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
            wrapper = _make_async_wrapper(
                original, span_name, span_type, wrapped_obj=obj,
            )
        else:
            wrapper = _make_sync_wrapper(
                original, span_name, span_type, wrapped_obj=obj,
            )

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
    method: Any, span_name: str, span_type: SpanType,
    wrapped_obj: Any = None,
) -> Any:
    """Create a sync wrapper that emits a span."""

    is_llm = span_type == SpanType.LLM
    is_tool = span_type == SpanType.TOOL

    @functools.wraps(method)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        span, token = start_span(span_name, span_type)
        # Propagate agent context from parent @agent / @langgraph_agent
        _agent = get_current_agent()
        if _agent:
            span.set_attribute("agent", _agent)
        span.attributes["wrap.method"] = method.__name__
        span.attributes["wrap.args_count"] = len(args)
        if kwargs:
            span.attributes["wrap.kwargs_keys"] = sorted(kwargs.keys())
        if is_llm:
            _set_llm_model_provider(span, wrapped_obj)
            _capture_llm_input(span, args, kwargs)
        if is_tool:
            _capture_tool_input(span, args, kwargs)
        # Snapshot cumulative token usage (e.g. CrewAI _token_usage)
        usage_before = _snapshot_token_usage(wrapped_obj) if is_llm else None
        try:
            result = method(*args, **kwargs)
            if is_llm:
                _enrich_llm_from_result(span, result)
                _apply_token_delta(span, usage_before, wrapped_obj)
            if is_tool:
                _capture_tool_output(span, result)
            span.finish(SpanStatus.OK)
            return result
        except BaseException as exc:
            span.record_error(exc)
            span.finish(SpanStatus.ERROR)
            raise
        finally:
            if is_llm:
                _finalize_llm(span)
            end_span(token)
            enqueue_span(span)

    return wrapper


def _make_async_wrapper(
    method: Any, span_name: str, span_type: SpanType,
    wrapped_obj: Any = None,
) -> Any:
    """Create an async wrapper that emits a span."""

    is_llm = span_type == SpanType.LLM
    is_tool = span_type == SpanType.TOOL

    @functools.wraps(method)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        span, token = start_span(span_name, span_type)
        # Propagate agent context from parent @agent / @langgraph_agent
        _agent = get_current_agent()
        if _agent:
            span.set_attribute("agent", _agent)
        span.attributes["wrap.method"] = method.__name__
        span.attributes["wrap.args_count"] = len(args)
        if kwargs:
            span.attributes["wrap.kwargs_keys"] = sorted(kwargs.keys())
        if is_llm:
            _set_llm_model_provider(span, wrapped_obj)
            _capture_llm_input(span, args, kwargs)
        if is_tool:
            _capture_tool_input(span, args, kwargs)
        # Snapshot cumulative token usage (e.g. CrewAI _token_usage)
        usage_before = _snapshot_token_usage(wrapped_obj) if is_llm else None
        try:
            result = await method(*args, **kwargs)
            if is_llm:
                _enrich_llm_from_result(span, result)
                _apply_token_delta(span, usage_before, wrapped_obj)
            if is_tool:
                _capture_tool_output(span, result)
            span.finish(SpanStatus.OK)
            return result
        except BaseException as exc:
            span.record_error(exc)
            span.finish(SpanStatus.ERROR)
            raise
        finally:
            if is_llm:
                _finalize_llm(span)
            end_span(token)
            enqueue_span(span)

    return wrapper


# ---------------------------------------------------------------------------
# LLM span enrichment helpers (used when span_type == LLM)
# ---------------------------------------------------------------------------


def _set_llm_model_provider(span: Any, wrapped_obj: Any) -> None:
    """Extract model name and provider from the wrapped LLM object."""
    if wrapped_obj is None:
        return

    # Model name from common attributes
    for attr in _MODEL_NAME_ATTRS:
        val = getattr(wrapped_obj, attr, None)
        if val and isinstance(val, str):
            span.set_attribute("model", val)
            break

    # Provider from module path
    try:
        from rastir.adapters.types import detect_provider_from_module
        module = getattr(type(wrapped_obj), "__module__", "") or ""
        provider = detect_provider_from_module(module)
        if provider and provider != "unknown":
            span.set_attribute("provider", provider)
    except ImportError:
        pass


def _capture_llm_input(span: Any, args: tuple, kwargs: dict) -> None:
    """Capture LLM input text from method arguments.

    Handles common LangChain patterns:
    - First positional arg as a list of messages (BaseMessage, tuples, dicts)
    - First positional arg as a string
    - ``messages``/``input``/``prompt`` kwargs
    """
    raw_input = None

    # 1. Check kwargs for common names
    for key in ("messages", "input", "prompt", "contents", "user_msg", "chat_history"):
        val = kwargs.get(key)
        if val is not None:
            raw_input = val
            break

    # 2. First positional arg (LangChain model.invoke(input))
    if raw_input is None and args:
        raw_input = args[0]

    if raw_input is None:
        return

    text = _stringify_messages(raw_input)
    if text:
        span.set_attribute("input", text)


def _stringify_messages(value: Any) -> str | None:
    """Convert messages / prompt to a string for span attributes."""
    if isinstance(value, str):
        return value

    if isinstance(value, list):
        parts = []
        for item in value:
            # LangChain BaseMessage (has .type and .content)
            role = getattr(item, "type", None)
            content = getattr(item, "content", None)
            if role and isinstance(content, str):
                parts.append(f"{role}: {content}")
                continue
            # Tuple (role, content)
            if isinstance(item, tuple) and len(item) == 2:
                parts.append(f"{item[0]}: {item[1]}")
                continue
            # Dict {"role": ..., "content": ...}
            if isinstance(item, dict):
                r = item.get("role", "")
                c = item.get("content", "")
                if c:
                    parts.append(f"{r}: {c}" if r else str(c))
                continue
            # Fallback: stringify
            parts.append(str(item))
        return "\n".join(parts) if parts else None

    # Fallback
    try:
        return str(value)
    except Exception:
        return None


def _enrich_llm_from_result(span: Any, result: Any) -> None:
    """Run adapter resolution on the LLM result to extract tokens and metadata."""
    # Capture output text
    output_text = _extract_output_text(result)
    if output_text:
        span.set_attribute("output", output_text)

    # Run adapter pipeline for tokens, model, provider
    try:
        from rastir.adapters.registry import resolve
        adapter_result = resolve(result)
        if adapter_result:
            if adapter_result.tokens_input is not None:
                span.set_attribute("tokens_input", adapter_result.tokens_input)
            if adapter_result.tokens_output is not None:
                span.set_attribute("tokens_output", adapter_result.tokens_output)
            # Response-phase model/provider upgrade
            if adapter_result.model and adapter_result.model != "unknown":
                span.set_attribute("model", adapter_result.model)
            if adapter_result.provider and adapter_result.provider != "unknown":
                span.set_attribute("provider", adapter_result.provider)
            if adapter_result.finish_reason:
                span.set_attribute("finish_reason", adapter_result.finish_reason)
            for k, v in adapter_result.extra_attributes.items():
                if k not in span.attributes:
                    span.set_attribute(k, v)
    except ImportError:
        logger.debug("Adapter registry not available for LLM enrichment")
    except Exception:
        logger.debug("LLM adapter resolution failed", exc_info=True)


def _extract_output_text(result: Any) -> str | None:
    """Extract text content from an LLM response object."""
    if isinstance(result, str):
        return result

    # LangChain AIMessage / content attribute
    content = getattr(result, "content", None)
    if isinstance(content, str) and content:
        return content

    # LlamaIndex ChatResponse — .message.content
    message = getattr(result, "message", None)
    if message is not None:
        msg_content = getattr(message, "content", None)
        if isinstance(msg_content, str) and msg_content:
            return msg_content
        # Tool-calling responses: extract tool_calls as output text
        blocks = getattr(message, "blocks", None)
        if blocks:
            tool_parts = []
            for blk in blocks:
                tn = getattr(blk, "tool_name", None)
                if tn:
                    tk = getattr(blk, "tool_kwargs", {})
                    tool_parts.append(f"tool_call: {tn}({tk})")
            if tool_parts:
                return "; ".join(tool_parts)
        ak_calls = getattr(message, "additional_kwargs", {}).get("tool_calls", [])
        if ak_calls:
            tool_parts = []
            for tc in ak_calls:
                fn = tc.get("function", {}) if isinstance(tc, dict) else getattr(tc, "function", None)
                if fn:
                    name = fn.get("name", "?") if isinstance(fn, dict) else getattr(fn, "name", "?")
                    args_str = fn.get("arguments", "") if isinstance(fn, dict) else getattr(fn, "arguments", "")
                    tool_parts.append(f"tool_call: {name}({args_str})")
            if tool_parts:
                return "; ".join(tool_parts)
    if isinstance(content, list) and content:
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif hasattr(item, "text"):
                parts.append(item.text)
        return "\n".join(parts) if parts else None

    # OpenAI ChatCompletion style
    choices = getattr(result, "choices", None)
    if choices:
        choice = choices[0]
        msg = getattr(choice, "message", None)
        if msg and hasattr(msg, "content"):
            return msg.content
        text = getattr(choice, "text", None)
        if text:
            return text

    # Gemini style
    text = getattr(result, "text", None)
    if isinstance(text, str):
        return text

    return None


# ---------------------------------------------------------------------------
# Tool span enrichment helpers (used when span_type == TOOL)
# ---------------------------------------------------------------------------

_TOOL_INPUT_MAX = 2000   # truncate long tool inputs
_TOOL_OUTPUT_MAX = 4000  # truncate long tool outputs


def _capture_tool_input(span: Any, args: tuple, kwargs: dict) -> None:
    """Capture tool input arguments as a span attribute.

    For LangChain tools, ``args[0]`` is typically the tool input
    (a dict or string).  Falls back to stringifying all arguments.
    """
    raw_input = None

    # LangChain tool.invoke(input) — first positional arg
    if args:
        raw_input = args[0]
    elif kwargs:
        # Check common kwarg names
        for key in ("tool_input", "input", "query", "args"):
            val = kwargs.get(key)
            if val is not None:
                raw_input = val
                break
        # Fallback: capture all kwargs as tool input (e.g. CrewAI
        # calls tool.run(**arguments) with the tool's parameters).
        if raw_input is None:
            raw_input = dict(kwargs)

    if raw_input is None:
        return

    try:
        text = str(raw_input)
        if len(text) > _TOOL_INPUT_MAX:
            text = text[:_TOOL_INPUT_MAX] + "..."
        span.set_attribute("tool.input", text)
    except Exception:
        pass


def _capture_tool_output(span: Any, result: Any) -> None:
    """Capture tool return value as a span attribute."""
    if result is None:
        return

    try:
        # LangChain ToolMessage — has .content
        content = getattr(result, "content", None)
        if isinstance(content, str):
            text = content
        else:
            text = str(result)

        if len(text) > _TOOL_OUTPUT_MAX:
            text = text[:_TOOL_OUTPUT_MAX] + "..."
        span.set_attribute("tool.output", text)
    except Exception:
        pass


def _snapshot_token_usage(wrapped_obj: Any) -> dict | None:
    """Snapshot cumulative ``_token_usage`` from an LLM object.

    Frameworks like CrewAI track token usage internally on the LLM
    instance (``_token_usage`` dict with ``prompt_tokens``,
    ``completion_tokens``, etc.).  We snapshot before the call so we
    can compute a per-call delta after.
    """
    if wrapped_obj is None:
        return None
    usage = getattr(wrapped_obj, "_token_usage", None)
    if usage is not None and isinstance(usage, dict):
        return dict(usage)  # shallow copy
    return None


def _apply_token_delta(
    span: Any, before: dict | None, wrapped_obj: Any,
) -> None:
    """Compute per-call token delta from cumulative ``_token_usage``.

    Only applies if the adapter pipeline did not already set token
    attributes on the span (i.e. provider returned a rich response
    with usage data).
    """
    if before is None or wrapped_obj is None:
        return
    # Skip if adapter already extracted tokens
    if span.attributes.get("tokens_input") or span.attributes.get("tokens_output"):
        return
    after = getattr(wrapped_obj, "_token_usage", None)
    if after is None or not isinstance(after, dict):
        return
    prompt_delta = (after.get("prompt_tokens", 0) or 0) - (before.get("prompt_tokens", 0) or 0)
    completion_delta = (after.get("completion_tokens", 0) or 0) - (before.get("completion_tokens", 0) or 0)
    if prompt_delta > 0:
        span.set_attribute("tokens_input", prompt_delta)
    if completion_delta > 0:
        span.set_attribute("tokens_output", completion_delta)


def _finalize_llm(span: Any) -> None:
    """Ensure model/provider defaults and calculate cost."""
    if "model" not in span.attributes:
        span.set_attribute("model", "unknown")
    if "provider" not in span.attributes:
        span.set_attribute("provider", "unknown")

    # Cost calculation
    try:
        from rastir.config import get_config, get_pricing_registry
        cfg = get_config()
        if not cfg.cost.enabled:
            return
        registry = get_pricing_registry()
        if registry is None:
            return
        provider = span.attributes.get("provider", "unknown")
        model = span.attributes.get("model", "unknown")
        tokens_in = span.attributes.get("tokens_input", 0) or 0
        tokens_out = span.attributes.get("tokens_output", 0) or 0
        cost_usd, pricing_missing = registry.calculate_cost(
            provider, model, tokens_in, tokens_out,
        )
        span.set_attribute("cost_usd", cost_usd)
        span.set_attribute("pricing_missing", pricing_missing)
        span.set_attribute("pricing_profile", cfg.cost.pricing_profile)
    except Exception:
        logger.debug("LLM cost calculation failed", exc_info=True)
