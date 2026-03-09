"""LlamaIndex integration for Rastir.

Provides ``llamaindex_agent`` — a single decorator that instruments a
LlamaIndex agent workflow.  The decorator:

  1. Scans function arguments for LlamaIndex agent objects
  2. Wraps the agent's LLM for per-call tracing
  3. Wraps existing tools (local or MCP — doesn't matter) for
     per-invocation tracing
  4. Creates an ``@agent`` span around the entire run

MCP tools are handled natively by LlamaIndex via ``llama-index-tools-mcp``
(``McpToolSpec.to_tool_list_async()``).  By the time the agent is created,
MCP tools are already regular ``FunctionTool`` objects — Rastir wraps them
for observability like any other tool.

Usage::

    from rastir import configure, llamaindex_agent

    configure(service="my-app", push_url="http://localhost:8080")

    @llamaindex_agent(agent_name="qa_agent")
    def run(agent):
        return agent.chat("Hello")

No LlamaIndex import is performed at module scope — detection uses
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
from rastir.wrapper import wrap

logger = logging.getLogger("rastir")

F = TypeVar("F", bound=Callable[..., Any])


# ---------------------------------------------------------------------------
# Agent detection helpers
# ---------------------------------------------------------------------------

# LlamaIndex agent class names we recognise
_LI_AGENT_CLASS_NAMES = frozenset({
    "ReActAgent",
    "OpenAIAgent",
    "FunctionAgent",
    "FunctionCallingAgent",
    "StructuredPlannerAgent",
    "AgentRunner",
    "BaseAgent",
})


def _is_llamaindex_agent(obj: Any) -> bool:
    """True if ``obj`` looks like a LlamaIndex agent instance."""
    cls = type(obj)
    module = getattr(cls, "__module__", "") or ""
    name = cls.__name__

    # Direct match on known agent classes
    if name in _LI_AGENT_CLASS_NAMES and "llama_index" in module:
        return True

    # Walk MRO to catch subclasses of BaseAgent
    for base in cls.__mro__:
        base_mod = getattr(base, "__module__", "") or ""
        if base.__name__ in _LI_AGENT_CLASS_NAMES and "llama_index" in base_mod:
            return True

    return False


def _get_agent_tools(agent: Any) -> list:
    """Extract tools from a LlamaIndex agent."""
    # ReActAgent / FunctionCallingAgent keep tools in _tools or tools
    tools = getattr(agent, "_tools", None)
    if tools is None:
        tools = getattr(agent, "tools", None)
    return list(tools) if tools else []


def _set_agent_tools(agent: Any, tools: list) -> None:
    """Set tools on a LlamaIndex agent."""
    if hasattr(agent, "_tools"):
        agent._tools = tools
    elif hasattr(agent, "tools"):
        agent.tools = tools


# ---------------------------------------------------------------------------
# LlamaIndexInstrumentor — FrameworkInstrumentor subclass
# ---------------------------------------------------------------------------

class LlamaIndexInstrumentor(FrameworkInstrumentor):
    """Instrumentor for LlamaIndex agent calls."""

    def detect(self, obj: Any) -> bool:
        return _is_llamaindex_agent(obj)

    def wrap(self, obj: Any, originals: Any) -> None:
        _wrap_agent_internals(obj, originals)

    def restore(self, originals: Any) -> None:
        _restore_originals(originals)


_llamaindex_instrumentor = LlamaIndexInstrumentor()
register_instrumentor(_llamaindex_instrumentor)

llamaindex_agent = make_framework_decorator(_llamaindex_instrumentor)
llamaindex_agent.__doc__ = """Decorator that instruments a LlamaIndex agent call.

Wraps agent LLMs and tools for per-call observability and creates
an ``@agent`` span around execution.  MCP tools are handled
natively by LlamaIndex (``McpToolSpec.to_tool_list_async()``) and
are wrapped the same as any local tool.

Args:
    agent_name: Name for the outer agent span.  Defaults to the
        function name.

Usage::

    @llamaindex_agent(agent_name="research")
    def run(agent):
        return agent.chat("What is 2+2?")

    @llamaindex_agent
    async def run(agent):
        return await agent.arun("List files in /tmp")
"""


# ---------------------------------------------------------------------------
# Internal wrapping / restore
# ---------------------------------------------------------------------------

def _wrap_agent_internals(
    agent: Any,
    originals: dict[int, dict[str, Any]],
) -> None:
    """Wrap LLM and tools on a LlamaIndex agent.

    Stores original values for cleanup in ``originals``.
    """
    agent_id = id(agent)
    originals[agent_id] = {"_agent_ref": agent}

    # --- Wrap LLM ---
    llm = getattr(agent, "_llm", None) or getattr(agent, "llm", None)
    if llm is not None and not getattr(llm, "_rastir_wrapped", False):
        originals[agent_id]["llm"] = llm
        originals[agent_id]["llm_attr"] = (
            "_llm" if hasattr(agent, "_llm") else "llm"
        )
        wrapped_llm = wrap(
            llm,
            name=f"llamaindex.{type(agent).__name__}.llm",
            span_type="llm",
            include=["chat", "complete", "achat", "acomplete",
                     "stream_chat", "stream_complete",
                     "astream_chat", "astream_complete",
                     "chat_with_tools", "achat_with_tools",
                     "stream_chat_with_tools", "astream_chat_with_tools"],
        )
        setattr(agent, originals[agent_id]["llm_attr"], wrapped_llm)

    # --- Wrap existing tools (local or MCP — all are FunctionTool) ---
    existing_tools = _get_agent_tools(agent)
    originals[agent_id]["tools"] = existing_tools
    wrapped_tools = []
    for t in existing_tools:
        if not getattr(t, "_rastir_wrapped", False):
            tool_name = getattr(t, "metadata", None)
            if tool_name and hasattr(tool_name, "name"):
                tool_name = tool_name.name
            else:
                tool_name = getattr(t, "name", None) or "tool"
            wrapped_tools.append(
                wrap(t, name=tool_name, span_type="tool",
                     include=["call", "__call__", "acall"])
            )
        else:
            wrapped_tools.append(t)

    _set_agent_tools(agent, wrapped_tools)


def _restore_originals(originals: dict[int, dict[str, Any]]) -> None:
    """Restore original LLMs and tools on agents after execution."""
    for _agent_id, saved in originals.items():
        ag = saved.get("_agent_ref")
        if ag is None:
            continue
        if "llm" in saved:
            attr = saved.get("llm_attr", "llm")
            setattr(ag, attr, saved["llm"])
        if "tools" in saved:
            _set_agent_tools(ag, saved["tools"])

