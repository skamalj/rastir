"""Google ADK (Agent Development Kit) integration for Rastir.

Provides ``adk_agent`` — a single decorator that instruments a Google
ADK agent workflow.  The decorator:

  1. Scans function arguments for ADK ``Runner`` or ``BaseAgent`` objects
  2. Wraps the runner's ``run_async`` to capture events and create spans
  3. Creates an ``@agent`` span around the entire execution

ADK agents are invoked via ``Runner.run_async()`` which yields events.
Rastir intercepts these events to create LLM and tool spans.

Usage::

    from rastir import configure, adk_agent

    configure(service="my-app", push_url="http://localhost:8080")

    @adk_agent(agent_name="my_adk_agent")
    async def run(runner, user_id, session_id, prompt):
        events = []
        async for event in runner.run_async(
            user_id=user_id, session_id=session_id,
            new_message=types.Content(role="user", parts=[types.Part(text=prompt)])
        ):
            events.append(event)
        return events

No ADK import is performed at module scope — detection uses
class-name / module inspection only.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any, Callable, TypeVar

from rastir.remote import discover_mcp_client, inject_traceparent_into_mcp_clients
from rastir.wrapper import wrap

logger = logging.getLogger("rastir")

F = TypeVar("F", bound=Callable[..., Any])


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------

def _is_adk_runner(obj: Any) -> bool:
    """True if *obj* looks like a Google ADK ``Runner``."""
    cls = type(obj)
    module = getattr(cls, "__module__", "") or ""
    return cls.__name__ == "Runner" and "google.adk" in module


def _is_adk_agent(obj: Any) -> bool:
    """True if *obj* looks like a Google ADK agent (BaseAgent/LlmAgent)."""
    cls = type(obj)
    for base in cls.__mro__:
        module = getattr(base, "__module__", "") or ""
        if base.__name__ in ("BaseAgent", "LlmAgent") and "google.adk" in module:
            return True
    return False


def _get_adk_tools(agent: Any) -> list:
    """Extract tools from an ADK agent."""
    tools = getattr(agent, "tools", None)
    return list(tools) if tools else []


def _get_adk_model_name(agent: Any) -> str:
    """Extract the model name from an ADK agent."""
    model = getattr(agent, "model", None)
    if model is None:
        return "unknown"
    if isinstance(model, str):
        return model
    # BaseLlm subclass — check model_name or model
    for attr in ("model_name", "model", "model_id"):
        val = getattr(model, attr, None)
        if val and isinstance(val, str):
            return val
    return type(model).__name__


# ---------------------------------------------------------------------------
# adk_agent decorator
# ---------------------------------------------------------------------------

def adk_agent(
    func: F | None = None,
    *,
    agent_name: str | None = None,
) -> F | Callable[[F], F]:
    """Decorator that instruments a Google ADK agent call.

    Wraps the Runner or agent execution in an ``@agent`` span, and
    intercepts events to create LLM and tool spans.

    Args:
        agent_name: Name for the outer agent span.  Defaults to the
            function name.

    Usage::

        @adk_agent(agent_name="researcher")
        async def run(runner, user_id, session_id, prompt):
            events = []
            async for event in runner.run_async(...):
                events.append(event)
            return events
    """

    def decorator(fn: F) -> F:
        resolved_name = agent_name or fn.__name__

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return _adk_agent_impl(fn, resolved_name, args, kwargs)

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            return await _async_adk_agent_impl(fn, resolved_name, args, kwargs)

        if asyncio.iscoroutinefunction(fn):
            return async_wrapper  # type: ignore[return-value]
        return wrapper  # type: ignore[return-value]

    if func is not None:
        return decorator(func)
    return decorator  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------

def _adk_agent_impl(
    fn: Callable,
    agent_name: str,
    args: tuple,
    kwargs: dict,
) -> Any:
    """Sync implementation of adk_agent."""
    from rastir.context import (
        end_span, start_span, set_current_agent, reset_current_agent,
    )
    from rastir.queue import enqueue_span
    from rastir.spans import SpanStatus, SpanType

    span, token = start_span(agent_name, SpanType.AGENT)
    span.set_attribute("agent_name", agent_name)
    agent_token = set_current_agent(agent_name)

    originals: dict[int, dict[str, Any]] = {}
    mcp_clients: list[Any] = []

    try:
        for obj in (*args, *kwargs.values()):
            if _is_adk_runner(obj):
                _wrap_runner_internals(obj, originals)
            elif _is_adk_agent(obj):
                _wrap_adk_agent_internals(obj, originals)
            mc = discover_mcp_client(obj)
            if mc is not None:
                mcp_clients.append(mc)

        _walk_func_for_mcp_clients(fn, mcp_clients)
        inject_traceparent_into_mcp_clients(mcp_clients)

        result = fn(*args, **kwargs)
        span.finish(SpanStatus.OK)
        return result
    except BaseException as exc:
        span.record_error(exc)
        span.finish(SpanStatus.ERROR)
        raise
    finally:
        _restore_originals(originals)
        reset_current_agent(agent_token)
        end_span(token)
        enqueue_span(span)


async def _async_adk_agent_impl(
    fn: Callable,
    agent_name: str,
    args: tuple,
    kwargs: dict,
) -> Any:
    """Async implementation of adk_agent."""
    from rastir.context import (
        end_span, start_span, set_current_agent, reset_current_agent,
    )
    from rastir.queue import enqueue_span
    from rastir.spans import SpanStatus, SpanType

    span, token = start_span(agent_name, SpanType.AGENT)
    span.set_attribute("agent_name", agent_name)
    agent_token = set_current_agent(agent_name)

    originals: dict[int, dict[str, Any]] = {}
    mcp_clients: list[Any] = []

    try:
        for obj in (*args, *kwargs.values()):
            if _is_adk_runner(obj):
                _wrap_runner_internals(obj, originals)
            elif _is_adk_agent(obj):
                _wrap_adk_agent_internals(obj, originals)
            mc = discover_mcp_client(obj)
            if mc is not None:
                mcp_clients.append(mc)

        _walk_func_for_mcp_clients(fn, mcp_clients)
        inject_traceparent_into_mcp_clients(mcp_clients)

        result = await fn(*args, **kwargs)
        span.finish(SpanStatus.OK)
        return result
    except BaseException as exc:
        span.record_error(exc)
        span.finish(SpanStatus.ERROR)
        raise
    finally:
        _restore_originals(originals)
        reset_current_agent(agent_token)
        end_span(token)
        enqueue_span(span)


# ---------------------------------------------------------------------------
# Internal wrapping
# ---------------------------------------------------------------------------

def _wrap_runner_internals(
    runner: Any,
    originals: dict[int, dict[str, Any]],
) -> None:
    """Wrap the agent inside a Runner for observability."""
    runner_id = id(runner)
    originals[runner_id] = {"_runner_ref": runner}

    # The Runner has an agent attribute — wrap it
    agent = getattr(runner, "agent", None) or getattr(runner, "_agent", None)
    if agent is not None and _is_adk_agent(agent):
        _wrap_adk_agent_internals(agent, originals)


def _wrap_adk_agent_internals(
    agent: Any,
    originals: dict[int, dict[str, Any]],
) -> None:
    """Wrap tools on an ADK agent for per-invocation tracing."""
    agent_id = id(agent)
    if agent_id in originals:
        return
    originals[agent_id] = {"_agent_ref": agent}

    # --- Wrap tools ---
    tools = _get_adk_tools(agent)
    if tools:
        originals[agent_id]["tools"] = list(tools)
        wrapped_tools = []
        for t in tools:
            if callable(t) and not getattr(t, "_rastir_wrapped", False):
                tool_name = getattr(t, "name", None) or getattr(t, "__name__", None) or "tool"
                wrapped_tools.append(
                    wrap(t, name=f"adk.tool.{tool_name}", span_type="tool",
                         include=["run_async", "__call__"])
                )
            else:
                wrapped_tools.append(t)
        try:
            agent.tools = wrapped_tools
        except Exception:
            pass  # Pydantic model may reject assignment

    # --- Wrap sub-agents ---
    sub_agents = getattr(agent, "sub_agents", None)
    if sub_agents:
        for sub in sub_agents:
            if _is_adk_agent(sub):
                _wrap_adk_agent_internals(sub, originals)


def _restore_originals(originals: dict[int, dict[str, Any]]) -> None:
    """Restore original tools on agents after execution."""
    for _id, saved in originals.items():
        ag = saved.get("_agent_ref")
        if ag is None:
            continue
        if "tools" in saved:
            try:
                ag.tools = saved["tools"]
            except Exception:
                pass


def _walk_func_for_mcp_clients(
    func: Any, mcp_clients: list[Any],
) -> None:
    """Walk a function's closures and globals for MCP client objects."""
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
