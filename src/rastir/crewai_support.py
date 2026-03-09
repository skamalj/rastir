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

import logging
from typing import Any, Callable, TypeVar

from rastir.framework_base import (
    FrameworkInstrumentor,
    make_framework_decorator,
    register_instrumentor,
)
from rastir.remote import discover_mcp_client
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
# CrewAIInstrumentor — FrameworkInstrumentor subclass
# ---------------------------------------------------------------------------

class CrewAIInstrumentor(FrameworkInstrumentor):
    """Instrumentor for CrewAI crew.kickoff() calls."""

    def detect(self, obj: Any) -> bool:
        return _is_crew(obj)

    def wrap(self, obj: Any, originals: Any) -> None:
        _wrap_crew_internals(obj, originals)

    def restore(self, originals: Any) -> None:
        _restore_originals(originals)

    def discover_extra_mcp_clients(
        self, obj: Any, mcp_clients: list[Any],
    ) -> None:
        _discover_crew_mcp_clients(obj, mcp_clients)


_crewai_instrumentor = CrewAIInstrumentor()
register_instrumentor(_crewai_instrumentor)

crew_kickoff = make_framework_decorator(_crewai_instrumentor)
crew_kickoff.__doc__ = """Decorator that instruments a CrewAI ``crew.kickoff()`` call.

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
