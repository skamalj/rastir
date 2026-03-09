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

import logging
import time
from typing import Any, Callable, TypeVar

from rastir.framework_base import (
    FrameworkInstrumentor,
    make_framework_decorator,
    register_instrumentor,
)
from rastir.remote import discover_mcp_client

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
# ADKInstrumentor — FrameworkInstrumentor subclass
# ---------------------------------------------------------------------------

class ADKInstrumentor(FrameworkInstrumentor):
    """Instrumentor for Google ADK agent workflows."""

    def detect(self, obj: Any) -> bool:
        return _is_adk_runner(obj) or _is_adk_agent(obj)

    def wrap(self, obj: Any, originals: Any) -> None:
        if _is_adk_runner(obj):
            _wrap_runner_internals(obj, originals)
        elif _is_adk_agent(obj):
            _install_adk_callbacks(obj, originals)

    def restore(self, originals: Any) -> None:
        _restore_originals(originals)


_adk_instrumentor = ADKInstrumentor()
register_instrumentor(_adk_instrumentor)

adk_agent = make_framework_decorator(_adk_instrumentor)
adk_agent.__doc__ = """Decorator that instruments a Google ADK agent call.

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

    # The Runner has an agent attribute — install callbacks on it
    agent = getattr(runner, "agent", None) or getattr(runner, "_agent", None)
    if agent is not None and _is_adk_agent(agent):
        _install_adk_callbacks(agent, originals)


def _install_adk_callbacks(
    agent: Any,
    originals: dict[int, dict[str, Any]],
) -> None:
    """Install before/after model and tool callbacks on an ADK agent.

    Uses ADK's official callback system to create LLM and tool spans
    without replacing tool objects (which are Pydantic models).
    """
    from rastir.context import start_span, end_span
    from rastir.queue import enqueue_span
    from rastir.spans import SpanStatus, SpanType

    agent_id = id(agent)
    if agent_id in originals:
        return
    originals[agent_id] = {
        "_agent_ref": agent,
        "before_model_callback": getattr(agent, "before_model_callback", None),
        "after_model_callback": getattr(agent, "after_model_callback", None),
        "on_model_error_callback": getattr(agent, "on_model_error_callback", None),
        "before_tool_callback": getattr(agent, "before_tool_callback", None),
        "after_tool_callback": getattr(agent, "after_tool_callback", None),
        "on_tool_error_callback": getattr(agent, "on_tool_error_callback", None),
    }

    # Span storage keyed by ADK context invocation_id to handle concurrency
    _active_llm_spans: dict[str, tuple] = {}   # inv_id -> (span, token)
    _active_tool_spans: dict[str, tuple] = {}   # tool_call_key -> (span, token)

    def _inv_id(ctx: Any) -> str:
        return getattr(ctx, "invocation_id", "") or str(id(ctx))

    # --- LLM callbacks ---

    async def _before_model(*, callback_context: Any, llm_request: Any, **kwargs: Any) -> None:
        model_name = getattr(llm_request, "model", None) or _get_adk_model_name(agent)
        span, token = start_span(f"adk.llm.{model_name}", SpanType.LLM)
        span.set_attribute("model", str(model_name))
        span.set_attribute("provider", "google")
        _active_llm_spans[_inv_id(callback_context)] = (span, token)
        return None  # Let the LLM call proceed

    async def _after_model(*, callback_context: Any, llm_response: Any, **kwargs: Any) -> None:
        key = _inv_id(callback_context)
        entry = _active_llm_spans.pop(key, None)
        if entry is None:
            return None
        span, token = entry
        # Extract token usage from response
        usage = getattr(llm_response, "usage_metadata", None)
        if usage is not None:
            prompt_tokens = getattr(usage, "prompt_token_count", None)
            completion_tokens = getattr(usage, "candidates_token_count", None)
            if prompt_tokens is not None:
                span.set_attribute("prompt_tokens", prompt_tokens)
            if completion_tokens is not None:
                span.set_attribute("completion_tokens", completion_tokens)
            total = getattr(usage, "total_token_count", None)
            if total is not None:
                span.set_attribute("total_tokens", total)
        span.finish(SpanStatus.OK)
        end_span(token)
        enqueue_span(span)
        return None

    async def _on_model_error(*, callback_context: Any, llm_request: Any, error: Exception, **kwargs: Any) -> None:
        key = _inv_id(callback_context)
        entry = _active_llm_spans.pop(key, None)
        if entry is None:
            return None
        span, token = entry
        span.record_error(error)
        span.finish(SpanStatus.ERROR)
        end_span(token)
        enqueue_span(span)
        return None

    # --- Tool callbacks ---

    def _tool_key(tool: Any, tool_ctx: Any) -> str:
        fcid = getattr(tool_ctx, "function_call_id", "") or ""
        return f"{_inv_id(tool_ctx)}:{fcid}"

    async def _before_tool(*, tool: Any, args: dict, tool_context: Any, **kwargs: Any) -> None:
        tool_name = getattr(tool, "name", None) or type(tool).__name__
        span, token = start_span(f"adk.tool.{tool_name}", SpanType.TOOL)
        span.set_attribute("tool_name", tool_name)
        _active_tool_spans[_tool_key(tool, tool_context)] = (span, token)
        return None  # Let the tool call proceed

    async def _after_tool(*, tool: Any, args: dict, tool_context: Any, tool_response: dict, **kwargs: Any) -> None:
        key = _tool_key(tool, tool_context)
        entry = _active_tool_spans.pop(key, None)
        if entry is None:
            return None
        span, token = entry
        span.finish(SpanStatus.OK)
        end_span(token)
        enqueue_span(span)
        return None

    async def _on_tool_error(*, tool: Any, args: dict, tool_context: Any, error: Exception, **kwargs: Any) -> None:
        key = _tool_key(tool, tool_context)
        entry = _active_tool_spans.pop(key, None)
        if entry is None:
            return None
        span, token = entry
        span.record_error(error)
        span.finish(SpanStatus.ERROR)
        end_span(token)
        enqueue_span(span)
        return None

    # Install callbacks (ADK supports lists — prepend ours)
    agent.before_model_callback = _prepend_callback(
        getattr(agent, "before_model_callback", None), _before_model
    )
    agent.after_model_callback = _prepend_callback(
        getattr(agent, "after_model_callback", None), _after_model
    )
    agent.on_model_error_callback = _prepend_callback(
        getattr(agent, "on_model_error_callback", None), _on_model_error
    )
    agent.before_tool_callback = _prepend_callback(
        getattr(agent, "before_tool_callback", None), _before_tool
    )
    agent.after_tool_callback = _prepend_callback(
        getattr(agent, "after_tool_callback", None), _after_tool
    )
    agent.on_tool_error_callback = _prepend_callback(
        getattr(agent, "on_tool_error_callback", None), _on_tool_error
    )

    # --- Recurse into sub-agents ---
    sub_agents = getattr(agent, "sub_agents", None)
    if sub_agents:
        for sub in sub_agents:
            if _is_adk_agent(sub):
                _install_adk_callbacks(sub, originals)


def _prepend_callback(existing: Any, new_cb: Callable) -> list:
    """Prepend a new callback to an existing callback (or list of callbacks)."""
    if existing is None:
        return new_cb
    if isinstance(existing, list):
        return [new_cb] + existing
    return [new_cb, existing]


def _restore_originals(originals: dict[int, dict[str, Any]]) -> None:
    """Restore original callbacks on agents after execution."""
    for _id, saved in originals.items():
        ag = saved.get("_agent_ref")
        if ag is None:
            continue
        for attr in ("before_model_callback", "after_model_callback",
                     "on_model_error_callback", "before_tool_callback",
                     "after_tool_callback", "on_tool_error_callback"):
            if attr in saved:
                try:
                    setattr(ag, attr, saved[attr])
                except Exception:
                    pass

