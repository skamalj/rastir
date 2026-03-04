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
            # Also scan args for MCP clients (e.g. MultiServerMCPClient)
            mc = discover_mcp_client(obj)
            if mc is not None:
                mcp_clients.append(mc)

        # Walk function closures/globals for MCP clients
        _walk_func_for_mcp_clients(fn, mcp_clients)

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
            # Also scan args for MCP clients (e.g. MultiServerMCPClient)
            mc = discover_mcp_client(obj)
            if mc is not None:
                mcp_clients.append(mc)

        # Walk function closures/globals for MCP clients
        _walk_func_for_mcp_clients(fn, mcp_clients)

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

        # --- Wrap existing tools (in-place method patching) ---
        # CrewAI's Agent is a Pydantic model whose ``tools`` field
        # validates entries as ``BaseTool``.  Proxy wrappers are
        # stripped by this validation, so we monkey-patch each tool's
        # ``.run`` method directly via ``__dict__`` (instance dict),
        # which bypasses Pydantic's ``__setattr__`` restriction.
        existing_tools = list(getattr(ag, "tools", []) or [])
        patched_runs: list[tuple[Any, Any]] = []  # (tool, original_run)
        role = getattr(ag, "role", "agent") or "agent"
        for t in existing_tools:
            if getattr(t, "_rastir_tool_patched", False):
                continue
            tool_name = getattr(t, "name", None) or "tool"
            original_run = t.run  # bound method
            from rastir.wrapper import _make_sync_wrapper
            from rastir.spans import SpanType
            span_name = f"crewai.{role}.tool.{tool_name}"
            wrapped_run = _make_sync_wrapper(
                original_run, span_name, SpanType.TOOL, wrapped_obj=t,
            )
            t.__dict__["run"] = wrapped_run
            t.__dict__["_rastir_tool_patched"] = True
            patched_runs.append((t, original_run))
        originals[agent_id]["_patched_tool_runs"] = patched_runs


def _restore_originals(originals: dict[int, dict[str, Any]]) -> None:
    """Restore original LLMs and tools on agents after execution."""
    for agent_id, saved in originals.items():
        ag = saved.get("_agent_ref")
        if ag is None:
            continue
        if "llm" in saved:
            ag.llm = saved["llm"]
        # Restore in-place patched tool .run methods
        for tool, original_run in saved.get("_patched_tool_runs", []):
            tool.__dict__.pop("run", None)
            tool.__dict__.pop("_rastir_tool_patched", None)


def _walk_func_for_mcp_clients(
    func: Any, mcp_clients: list[Any],
) -> None:
    """Walk a function's closures and globals for MCP client objects."""
    seen: set[int] = set()

    # 1. Closure cells
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

    # 2. Global variables referenced by the function
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


def _discover_crew_mcp_clients(
    crew: Any, mcp_clients: list[Any],
) -> None:
    """Discover MCP server configs on agents' ``mcps`` field."""
    for ag in _get_agents(crew):
        # CrewAI 1.9+ uses 'mcps'; older versions may use 'mcp_servers'
        mcp_servers = getattr(ag, "mcps", None) or getattr(ag, "mcp_servers", None)
        if not mcp_servers:
            continue
        for srv in mcp_servers:
            mc = discover_mcp_client(srv)
            if mc is not None:
                mcp_clients.append(mc)
