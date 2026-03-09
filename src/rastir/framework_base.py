"""Base class and utilities for framework instrumentors.

Provides ``FrameworkInstrumentor`` — an ABC that encapsulates the
per-framework detection, wrapping, and cleanup logic — plus
``make_framework_decorator`` which generates the decorator (sync/async,
bare/parameterised) from any instrumentor instance.

Also provides ``framework_agent`` — a single decorator that
auto-detects the framework from function arguments and applies the
correct instrumentor.

Usage::

    from rastir import framework_agent

    @framework_agent(agent_name="my_agent")
    def run(agent_or_graph, prompt):
        return agent_or_graph(prompt)

No framework SDK is imported at module scope — detection uses
class-name / module inspection only.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from abc import ABC, abstractmethod
from typing import Any, Callable, TypeVar

from rastir.remote import discover_mcp_client, inject_traceparent_into_mcp_clients

logger = logging.getLogger("rastir")

F = TypeVar("F", bound=Callable[..., Any])

# ---------------------------------------------------------------------------
# Registry of framework instrumentors (populated by each support module)
# ---------------------------------------------------------------------------
_instrumentor_registry: list = []  # list[FrameworkInstrumentor], typed after class


def register_instrumentor(inst: "FrameworkInstrumentor") -> None:
    """Register an instrumentor for auto-detection by ``framework_agent``."""
    _instrumentor_registry.append(inst)


# ---------------------------------------------------------------------------
# ABC
# ---------------------------------------------------------------------------

class FrameworkInstrumentor(ABC):
    """Standard interface that every framework integration implements.

    Subclasses provide three things:
      1. **detect** — duck-type check (class name + module)
      2. **wrap** — instrument the detected object for observability
      3. **restore** — undo all instrumentation cleanly
    """

    @abstractmethod
    def detect(self, obj: Any) -> bool:
        """Return ``True`` if *obj* is the framework object this
        instrumentor handles (e.g. a ``CompiledGraph``, ``Crew``, etc.)."""

    @abstractmethod
    def wrap(self, obj: Any, originals: Any) -> None:
        """Instrument *obj* (LLMs, tools, callbacks) for observability.

        Store enough state in *originals* to fully reverse via
        :meth:`restore`.
        """

    @abstractmethod
    def restore(self, originals: Any) -> None:
        """Undo everything done by :meth:`wrap`."""

    def create_originals(self) -> Any:
        """Create the container passed to ``wrap`` / ``restore``.

        Override if you need a ``list`` instead of the default ``dict``.
        """
        return {}

    def discover_extra_mcp_clients(
        self, obj: Any, mcp_clients: list[Any],
    ) -> None:
        """Hook for additional MCP client discovery on the framework
        object (e.g. CrewAI ``agent.mcps``).  Default is a no-op."""

    @property
    def agent_attr_name(self) -> str:
        """Span attribute key for the agent name.

        Override to ``"agent"`` for LangGraph compatibility.
        """
        return "agent_name"


# ---------------------------------------------------------------------------
# Shared helper — MCP client discovery in closures/globals
# ---------------------------------------------------------------------------

def walk_func_for_mcp_clients(
    func: Any, mcp_clients: list[Any],
) -> None:
    """Walk a function's closures and globals for MCP client objects.

    This was previously duplicated in every ``*_support.py`` module.
    """
    seen: set[int] = set()

    closure = getattr(func, "__closure__", None)
    if closure:
        for cell in closure:
            try:
                val = cell.cell_contents
            except ValueError:
                continue
            if id(val) not in seen:
                seen.add(id(val))
                mc = discover_mcp_client(val)
                if mc is not None:
                    mcp_clients.append(mc)

    code = getattr(func, "__code__", None)
    func_globals = getattr(func, "__globals__", None)
    if code is not None and func_globals is not None:
        for varname in code.co_names:
            val = func_globals.get(varname)
            if val is None or id(val) in seen:
                continue
            seen.add(id(val))
            mc = discover_mcp_client(val)
            if mc is not None:
                mcp_clients.append(mc)


# ---------------------------------------------------------------------------
# Generic sync / async impl
# ---------------------------------------------------------------------------

def _framework_impl_sync(
    fn: Callable,
    agent_name: str,
    args: tuple,
    kwargs: dict,
    instrumentor: FrameworkInstrumentor,
) -> Any:
    """Sync implementation shared by all framework decorators."""
    from rastir.context import (
        end_span, start_span, set_current_agent, reset_current_agent,
    )
    from rastir.queue import enqueue_span
    from rastir.spans import SpanStatus, SpanType

    span, token = start_span(agent_name, SpanType.AGENT)
    span.set_attribute(instrumentor.agent_attr_name, agent_name)
    agent_token = set_current_agent(agent_name)

    originals = instrumentor.create_originals()
    mcp_clients: list[Any] = []

    try:
        for obj in (*args, *kwargs.values()):
            if instrumentor.detect(obj):
                instrumentor.wrap(obj, originals)
                instrumentor.discover_extra_mcp_clients(obj, mcp_clients)
            mc = discover_mcp_client(obj)
            if mc is not None:
                mcp_clients.append(mc)

        walk_func_for_mcp_clients(fn, mcp_clients)
        inject_traceparent_into_mcp_clients(mcp_clients)

        result = fn(*args, **kwargs)
        span.finish(SpanStatus.OK)
        return result
    except BaseException as exc:
        span.record_error(exc)
        span.finish(SpanStatus.ERROR)
        raise
    finally:
        instrumentor.restore(originals)
        reset_current_agent(agent_token)
        end_span(token)
        enqueue_span(span)


async def _framework_impl_async(
    fn: Callable,
    agent_name: str,
    args: tuple,
    kwargs: dict,
    instrumentor: FrameworkInstrumentor,
) -> Any:
    """Async implementation shared by all framework decorators."""
    from rastir.context import (
        end_span, start_span, set_current_agent, reset_current_agent,
    )
    from rastir.queue import enqueue_span
    from rastir.spans import SpanStatus, SpanType

    span, token = start_span(agent_name, SpanType.AGENT)
    span.set_attribute(instrumentor.agent_attr_name, agent_name)
    agent_token = set_current_agent(agent_name)

    originals = instrumentor.create_originals()
    mcp_clients: list[Any] = []

    try:
        for obj in (*args, *kwargs.values()):
            if instrumentor.detect(obj):
                instrumentor.wrap(obj, originals)
                instrumentor.discover_extra_mcp_clients(obj, mcp_clients)
            mc = discover_mcp_client(obj)
            if mc is not None:
                mcp_clients.append(mc)

        walk_func_for_mcp_clients(fn, mcp_clients)
        inject_traceparent_into_mcp_clients(mcp_clients)

        result = await fn(*args, **kwargs)
        span.finish(SpanStatus.OK)
        return result
    except BaseException as exc:
        span.record_error(exc)
        span.finish(SpanStatus.ERROR)
        raise
    finally:
        instrumentor.restore(originals)
        reset_current_agent(agent_token)
        end_span(token)
        enqueue_span(span)


# ---------------------------------------------------------------------------
# Decorator factory
# ---------------------------------------------------------------------------

def make_framework_decorator(
    instrumentor: FrameworkInstrumentor,
) -> Callable:
    """Build a ``@framework_agent``-style decorator from an instrumentor.

    Returns a decorator factory that accepts ``func`` (bare usage) or
    ``agent_name`` keyword (parameterised usage).
    """

    def decorator_factory(
        func: F | None = None,
        *,
        agent_name: str | None = None,
    ) -> F | Callable[[F], F]:
        def decorator(fn: F) -> F:
            resolved_name = agent_name or fn.__name__

            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                return _framework_impl_sync(
                    fn, resolved_name, args, kwargs, instrumentor,
                )

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                return await _framework_impl_async(
                    fn, resolved_name, args, kwargs, instrumentor,
                )

            if asyncio.iscoroutinefunction(fn):
                return async_wrapper  # type: ignore[return-value]
            return wrapper  # type: ignore[return-value]

        if func is not None:
            return decorator(func)
        return decorator  # type: ignore[return-value]

    # Copy instrumentor docstring to the decorator for discoverability
    decorator_factory.__doc__ = (
        f"Decorator that instruments a {type(instrumentor).__name__} call.\n\n"
        "See ``FrameworkInstrumentor`` for details."
    )
    return decorator_factory


# ---------------------------------------------------------------------------
# Auto-detecting ``framework_agent`` decorator
# ---------------------------------------------------------------------------

def framework_agent(
    func: F | None = None,
    *,
    agent_name: str | None = None,
) -> F | Callable[[F], F]:
    """Auto-detecting framework agent decorator.

    Scans function arguments at call time and delegates to the first
    registered ``FrameworkInstrumentor`` whose ``detect`` matches.
    If no framework object is found, falls back to a plain ``@agent``
    span (same as ``rastir.agent``).

    Usage::

        from rastir import framework_agent

        @framework_agent(agent_name="my_agent")
        def run(graph_or_agent, prompt):
            return graph_or_agent.invoke(prompt)
    """

    def decorator(fn: F) -> F:
        resolved_name = agent_name or fn.__name__

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            inst = _find_instrumentor(args, kwargs)
            if inst is not None:
                return _framework_impl_sync(
                    fn, resolved_name, args, kwargs, inst,
                )
            return _plain_agent_sync(fn, resolved_name, args, kwargs)

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            inst = _find_instrumentor(args, kwargs)
            if inst is not None:
                return await _framework_impl_async(
                    fn, resolved_name, args, kwargs, inst,
                )
            return await _plain_agent_async(fn, resolved_name, args, kwargs)

        if asyncio.iscoroutinefunction(fn):
            return async_wrapper  # type: ignore[return-value]
        return wrapper  # type: ignore[return-value]

    if func is not None:
        return decorator(func)
    return decorator  # type: ignore[return-value]


def _find_instrumentor(
    args: tuple, kwargs: dict,
) -> FrameworkInstrumentor | None:
    """Scan args for a registered instrumentor match."""
    for obj in (*args, *kwargs.values()):
        for inst in _instrumentor_registry:
            if inst.detect(obj):
                return inst
    return None


def _plain_agent_sync(
    fn: Callable, agent_name: str, args: tuple, kwargs: dict,
) -> Any:
    """Fallback: plain @agent span when no framework is detected."""
    from rastir.context import (
        end_span, start_span, set_current_agent, reset_current_agent,
    )
    from rastir.queue import enqueue_span
    from rastir.spans import SpanStatus, SpanType

    span, token = start_span(agent_name, SpanType.AGENT)
    span.set_attribute("agent_name", agent_name)
    agent_token = set_current_agent(agent_name)

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
        end_span(token)
        enqueue_span(span)


async def _plain_agent_async(
    fn: Callable, agent_name: str, args: tuple, kwargs: dict,
) -> Any:
    """Fallback: plain @agent span when no framework is detected (async)."""
    from rastir.context import (
        end_span, start_span, set_current_agent, reset_current_agent,
    )
    from rastir.queue import enqueue_span
    from rastir.spans import SpanStatus, SpanType

    span, token = start_span(agent_name, SpanType.AGENT)
    span.set_attribute("agent_name", agent_name)
    agent_token = set_current_agent(agent_name)

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
        end_span(token)
        enqueue_span(span)
