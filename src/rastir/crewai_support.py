"""CrewAI integration for Rastir.

Provides ``crew_kickoff`` — a single decorator that instruments an
entire CrewAI workflow.  The decorator:

  1. Scans function arguments for ``Crew`` objects
  2. Wraps each agent's LLM for per-call tracing
  3. Wraps each agent's tools for per-invocation tracing
  4. Creates an ``@agent`` span around the entire kickoff

MCP tools are handled natively by CrewAI — each agent can declare
``mcps=[...]`` directly.  Rastir auto-discovers and wraps those tools
just like any other tool on the agent.

Usage::

    from rastir import configure, crew_kickoff

    configure(service="my-app", push_url="http://localhost:8080")

    crew = Crew(agents=[researcher, writer], tasks=[...])

    @crew_kickoff(agent_name="research_crew")
    def run(crew):
        return crew.kickoff()

No CrewAI import is performed at module scope — detection uses
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
# Crew / Agent detection helpers
# ---------------------------------------------------------------------------

def _is_crew(obj: Any) -> bool:
    """True if ``obj`` looks like a CrewAI ``Crew`` instance."""
    cls_name = type(obj).__name__
    module = type(obj).__module__ or ""
    return cls_name == "Crew" and "crewai" in module


def _get_agents(crew: Any) -> list:
    """Return the list of agents from a Crew object."""
    agents = getattr(crew, "agents", None)
    return list(agents) if agents else []


# ---------------------------------------------------------------------------
# crew_kickoff decorator
# ---------------------------------------------------------------------------

def crew_kickoff(
    func: F | None = None,
    *,
    agent_name: str | None = None,
) -> F | Callable[[F], F]:
    """Decorator that instruments a CrewAI ``crew.kickoff()`` call.

    Wraps agent LLMs and tools for per-call observability, and creates
    an ``@agent`` span around execution.

    MCP tools are handled natively by CrewAI via the ``mcps=[]`` field
    on each agent.  Rastir auto-discovers and wraps those tools just
    like any other tool on the agent — no special Rastir MCP parameter
    is needed.

    Args:
        agent_name: Name for the outer agent span.  Defaults to the
            function name.

    Usage::

        @crew_kickoff(agent_name="my_crew")
        def run(crew):
            return crew.kickoff()
    """

    def decorator(fn: F) -> F:
        resolved_name = agent_name or fn.__name__

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return _crew_kickoff_impl(
                fn, resolved_name, args, kwargs
            )

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            return await _async_crew_kickoff_impl(
                fn, resolved_name, args, kwargs
            )

        if asyncio.iscoroutinefunction(fn):
            return async_wrapper  # type: ignore[return-value]
        return wrapper  # type: ignore[return-value]

    if func is not None:
        # Bare @crew_kickoff without parens
        return decorator(func)
    return decorator  # type: ignore[return-value]


def _crew_kickoff_impl(
    fn: Callable,
    agent_name: str,
    args: tuple,
    kwargs: dict,
) -> Any:
    """Sync implementation of crew_kickoff."""
    from rastir.context import end_span, start_span, set_current_agent, reset_current_agent
    from rastir.queue import enqueue_span
    from rastir.spans import SpanStatus, SpanType

    # 1. Start agent span
    span, token = start_span(agent_name, SpanType.AGENT)
    span.set_attribute("agent_name", agent_name)
    agent_token = set_current_agent(agent_name)

    originals: dict[int, dict[str, Any]] = {}
    mcp_clients: list[Any] = []

    try:
        # 2. Find Crew objects in args, wrap internals
        for obj in (*args, *kwargs.values()):
            if _is_crew(obj):
                _wrap_crew_internals(obj, originals)
                # Discover MCP server configs on agents
                _discover_crew_mcp_clients(obj, mcp_clients)

        # Inject traceparent header into discovered MCP clients
        inject_traceparent_into_mcp_clients(mcp_clients)

        # 3. Run the user function
        result = fn(*args, **kwargs)
        span.finish(SpanStatus.OK)
        return result
    except BaseException as exc:
        span.record_error(exc)
        span.finish(SpanStatus.ERROR)
        raise
    finally:
        # 4. Restore originals
        _restore_originals(originals)
        reset_current_agent(agent_token)
        end_span(token)
        enqueue_span(span)


async def _async_crew_kickoff_impl(
    fn: Callable,
    agent_name: str,
    args: tuple,
    kwargs: dict,
) -> Any:
    """Async implementation of crew_kickoff."""
    from rastir.context import end_span, start_span, set_current_agent, reset_current_agent
    from rastir.queue import enqueue_span
    from rastir.spans import SpanStatus, SpanType

    span, token = start_span(agent_name, SpanType.AGENT)
    span.set_attribute("agent_name", agent_name)
    agent_token = set_current_agent(agent_name)

    originals: dict[int, dict[str, Any]] = {}
    mcp_clients: list[Any] = []

    try:
        for obj in (*args, *kwargs.values()):
            if _is_crew(obj):
                _wrap_crew_internals(obj, originals)
                _discover_crew_mcp_clients(obj, mcp_clients)

        # Inject traceparent header into discovered MCP clients
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


def _wrap_crew_internals(
    crew: Any,
    originals: dict[int, dict[str, Any]],
) -> None:
    """Wrap LLMs and tools on all agents in a Crew.

    Stores original values + agent ref in ``originals`` for cleanup.
    """
    for ag in _get_agents(crew):
        agent_id = id(ag)
        originals[agent_id] = {"_agent_ref": ag}

        # --- Wrap LLM ---
        llm = getattr(ag, "llm", None)
        if llm is not None and not getattr(llm, "_rastir_wrapped", False):
            originals[agent_id]["llm"] = llm
            role = getattr(ag, "role", "agent") or "agent"
            ag.llm = wrap(
                llm,
                name=f"crewai.{role}.llm",
                span_type="llm",
                include=["call"],
            )

        # --- Wrap existing tools ---
        existing_tools = list(getattr(ag, "tools", []) or [])
        originals[agent_id]["tools"] = existing_tools
        wrapped_tools = []
        for t in existing_tools:
            if not getattr(t, "_rastir_wrapped", False):
                tool_name = getattr(t, "name", None) or "tool"
                wrapped_tools.append(
                    wrap(t, name=tool_name, span_type="tool", include=["run"])
                )
            else:
                wrapped_tools.append(t)

        ag.tools = wrapped_tools


def _restore_originals(originals: dict[int, dict[str, Any]]) -> None:
    """Restore original LLMs and tools on agents after execution."""
    for agent_id, saved in originals.items():
        ag = saved.get("_agent_ref")
        if ag is None:
            continue
        if "llm" in saved:
            ag.llm = saved["llm"]
        if "tools" in saved:
            ag.tools = saved["tools"]


def _discover_crew_mcp_clients(
    crew: Any, mcp_clients: list[Any],
) -> None:
    """Discover MCP server configs on agents' ``mcp_servers`` field."""
    for ag in _get_agents(crew):
        mcp_servers = getattr(ag, "mcp_servers", None)
        if not mcp_servers:
            continue
        for srv in mcp_servers:
            mc = discover_mcp_client(srv)
            if mc is not None:
                mcp_clients.append(mc)
