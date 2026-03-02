"""CrewAI integration for Rastir.

Provides ``crew_kickoff`` — a single decorator that instruments an
entire CrewAI workflow.  The decorator:

  1. Scans function arguments for ``Crew`` objects
  2. Wraps each agent's LLM for per-call tracing
  3. Wraps each agent's tools for per-invocation tracing
  4. Optionally injects MCP tools (converted to CrewAI ``BaseTool``
     subclasses) into agents
  5. Creates an ``@agent`` span around the entire kickoff

Usage::

    from rastir import configure, crew_kickoff, wrap_mcp

    configure(service="my-app", push_url="http://localhost:8080")

    crew = Crew(agents=[researcher, writer], tasks=[...])

    # Without MCP:
    @crew_kickoff(agent_name="research_crew")
    def run(crew):
        return crew.kickoff()

    # With MCP:
    session = wrap_mcp(mcp_session)

    @crew_kickoff(agent_name="research_crew", mcp=session)
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

from rastir.wrapper import wrap

logger = logging.getLogger("rastir")

F = TypeVar("F", bound=Callable[..., Any])

# MCP JSON Schema type → Python type mapping
_JSON_TYPE_MAP: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


# ---------------------------------------------------------------------------
# MCP-to-CrewAI tool bridge
# ---------------------------------------------------------------------------

def _mcp_schema_to_python_type(prop: dict) -> type:
    """Map a JSON Schema property to a Python type."""
    json_type = prop.get("type", "string")
    return _JSON_TYPE_MAP.get(json_type, str)


def _build_crewai_tools_from_mcp(session: Any, tools: list) -> list:
    """Convert MCP tool descriptors into CrewAI ``BaseTool`` subclasses.

    Each generated tool's ``_run`` method calls
    ``session.call_tool(name, args)`` — which, if the session is a
    ``wrap_mcp`` proxy, automatically injects trace context.

    Args:
        session: An MCP ``ClientSession`` (ideally wrapped with
            ``wrap_mcp``).
        tools: List of MCP ``Tool`` objects from ``session.list_tools()``.

    Returns:
        List of ``BaseTool`` instances ready for ``Agent(tools=[...])``.
    """
    try:
        from crewai.tools.base_tool import BaseTool      # noqa: F811
        from pydantic import BaseModel, create_model
    except ImportError:
        logger.warning(
            "crewai not installed — cannot convert MCP tools to CrewAI tools"
        )
        return []

    crewai_tools: list[Any] = []

    for mcp_tool in tools:
        tool_name = getattr(mcp_tool, "name", None) or str(mcp_tool)
        tool_desc = getattr(mcp_tool, "description", None) or tool_name
        input_schema = getattr(mcp_tool, "inputSchema", None) or {}

        # Build Pydantic args model from MCP inputSchema
        properties = input_schema.get("properties", {})
        required = set(input_schema.get("required", []))
        fields: dict[str, Any] = {}
        for prop_name, prop_info in properties.items():
            py_type = _mcp_schema_to_python_type(prop_info)
            default = ... if prop_name in required else None
            fields[prop_name] = (py_type, default)

        if fields:
            ArgsModel = create_model(f"{tool_name}_args", **fields)
        else:
            ArgsModel = create_model(f"{tool_name}_args")

        # Create _run that calls session.call_tool()
        def _make_run(sess: Any, tname: str) -> Callable:
            def _run(self: Any, **kwargs: Any) -> Any:
                coro = sess.call_tool(tname, kwargs)
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None

                if loop and loop.is_running():
                    # Already in async context — use nest_asyncio or
                    # a thread.  CrewAI typically runs sync, so this
                    # path is rare.
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor(1) as pool:
                        result = pool.submit(asyncio.run, coro).result()
                else:
                    result = asyncio.run(coro)

                # Extract text content from MCP CallToolResult
                if hasattr(result, "content"):
                    parts = []
                    for item in result.content:
                        text = getattr(item, "text", None)
                        if text is not None:
                            parts.append(text)
                    return "\n".join(parts) if parts else str(result)
                return str(result)

            return _run

        ToolCls = type(
            f"MCP_{tool_name}",
            (BaseTool,),
            {
                "__module__": __name__,
                "__annotations__": {
                    "name": str,
                    "description": str,
                    "args_schema": type[BaseModel],
                },
                "name": tool_name,
                "description": tool_desc,
                "args_schema": ArgsModel,
                "_run": _make_run(session, tool_name),
            },
        )

        crewai_tools.append(ToolCls())

    return crewai_tools


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
    mcp: Any | None = None,
) -> F | Callable[[F], F]:
    """Decorator that instruments a CrewAI ``crew.kickoff()`` call.

    Wraps agent LLMs and tools for per-call observability, optionally
    injects MCP tools, and creates an ``@agent`` span around execution.

    Args:
        agent_name: Name for the outer agent span.  Defaults to the
            function name.
        mcp: MCP session(s) to convert into CrewAI tools and inject
            into agents.  Accepts:

            - A single session → tools injected into **all** agents
            - A list of sessions → tools from all sessions injected
              into all agents
            - A dict mapping agent role (str) to session → tools
              injected into the matching agent only

    Usage::

        @crew_kickoff(agent_name="my_crew")
        def run(crew):
            return crew.kickoff()

        @crew_kickoff(agent_name="my_crew", mcp=wrapped_session)
        def run(crew):
            return crew.kickoff()
    """

    def decorator(fn: F) -> F:
        resolved_name = agent_name or fn.__name__

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return _crew_kickoff_impl(
                fn, resolved_name, mcp, args, kwargs
            )

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            return await _async_crew_kickoff_impl(
                fn, resolved_name, mcp, args, kwargs
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
    mcp: Any | None,
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

    try:
        # 2. Find Crew objects in args, wrap internals
        for obj in (*args, *kwargs.values()):
            if _is_crew(obj):
                _wrap_crew_internals(obj, mcp, originals)

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
    mcp: Any | None,
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

    try:
        for obj in (*args, *kwargs.values()):
            if _is_crew(obj):
                _wrap_crew_internals(obj, mcp, originals)

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


def _resolve_mcp_tools(
    agent: Any,
    mcp: Any | None,
    cache: dict[int, list],
) -> list:
    """Resolve MCP tools for a specific agent.

    Handles session, list-of-sessions, and role→session dict.
    """
    if mcp is None:
        return []

    sessions: list[Any] = []
    role = getattr(agent, "role", None) or ""

    if isinstance(mcp, dict):
        # {"Agent Role": session}
        session = mcp.get(role)
        if session is not None:
            sessions = [session]
    elif isinstance(mcp, (list, tuple)):
        sessions = list(mcp)
    else:
        # Single session
        sessions = [mcp]

    all_tools: list[Any] = []
    for session in sessions:
        sid = id(session)
        if sid not in cache:
            # list_tools() is async — run it sync
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop and loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(1) as pool:
                    tools_result = pool.submit(
                        asyncio.run, session.list_tools()
                    ).result()
            else:
                tools_result = asyncio.run(session.list_tools())

            # list_tools() returns ListToolsResult with .tools
            raw_tools = getattr(tools_result, "tools", tools_result)
            if not isinstance(raw_tools, (list, tuple)):
                raw_tools = list(raw_tools) if raw_tools else []

            cache[sid] = _build_crewai_tools_from_mcp(session, raw_tools)

        all_tools.extend(cache[sid])

    return all_tools


def _wrap_crew_internals(
    crew: Any,
    mcp: Any | None,
    originals: dict[int, dict[str, Any]],
) -> None:
    """Wrap LLMs and tools on all agents in a Crew.

    Also injects MCP tools if ``mcp`` is provided.
    Stores original values + agent ref in ``originals`` for cleanup.
    """
    mcp_tools_cache: dict[int, list] = {}

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

        # --- Inject MCP tools ---
        mcp_tools = _resolve_mcp_tools(ag, mcp, mcp_tools_cache)
        wrapped_tools.extend(mcp_tools)

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
