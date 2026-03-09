"""AWS Strands Agents integration for Rastir.

Provides ``strands_agent`` — a single decorator that instruments a
Strands Agent call.  The decorator:

  1. Scans function arguments for Strands ``Agent`` objects
  2. Wraps the agent's ``model.stream`` for LLM span tracing
  3. Wraps each tool's ``stream`` method for tool span tracing
  4. Creates an ``@agent`` span around the entire execution

Strands agents are invoked via ``agent(prompt)`` (sync) or
``agent.invoke_async(prompt)`` (async), using ``model.stream()``
for LLM calls and ``tool.stream()`` for tool invocations.

Usage::

    from rastir import configure, strands_agent

    configure(service="my-app", push_url="http://localhost:8080")

    @strands_agent(agent_name="my_strands_agent")
    def run(agent, prompt):
        return agent(prompt)

No Strands import is performed at module scope — detection uses
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
# Detection helpers
# ---------------------------------------------------------------------------

def _is_strands_agent(obj: Any) -> bool:
    """True if *obj* looks like a Strands ``Agent``."""
    cls = type(obj)
    for base in cls.__mro__:
        module = getattr(base, "__module__", "") or ""
        if base.__name__ in ("Agent", "AgentBase") and "strands" in module:
            return True
    return False


def _get_model_name(agent: Any) -> str:
    """Extract the model name from a Strands agent."""
    model = getattr(agent, "model", None)
    if model is None:
        return "unknown"
    if isinstance(model, str):
        return model
    # Check for model_id (BedrockModel) or model_name
    for attr in ("model_id", "model_name", "model"):
        val = getattr(model, attr, None)
        if val and isinstance(val, str):
            return val
    return type(model).__name__


# ---------------------------------------------------------------------------
# StrandsInstrumentor — FrameworkInstrumentor subclass
# ---------------------------------------------------------------------------

class StrandsInstrumentor(FrameworkInstrumentor):
    """Instrumentor for AWS Strands Agent calls."""

    def detect(self, obj: Any) -> bool:
        return _is_strands_agent(obj)

    def wrap(self, obj: Any, originals: Any) -> None:
        _wrap_strands_internals(obj, originals)

    def restore(self, originals: Any) -> None:
        _restore_originals(originals)


_strands_instrumentor = StrandsInstrumentor()
register_instrumentor(_strands_instrumentor)

strands_agent = make_framework_decorator(_strands_instrumentor)
strands_agent.__doc__ = """Decorator that instruments a Strands Agent call.

Wraps the agent's model and tools for per-call observability, and
creates an ``@agent`` span around execution.

Args:
    agent_name: Name for the outer agent span.  Defaults to the
        function name.

Usage::

    @strands_agent(agent_name="researcher")
    def run(agent, prompt):
        return agent(prompt)
"""


# ---------------------------------------------------------------------------
# Internal wrapping
# ---------------------------------------------------------------------------

def _wrap_strands_internals(
    agent: Any,
    originals: dict[int, dict[str, Any]],
) -> None:
    """Wrap model and tools on a Strands agent for observability."""
    agent_id = id(agent)
    if agent_id in originals:
        return
    originals[agent_id] = {"_agent_ref": agent}

    # --- Wrap model (LLM tracing) ---
    model = getattr(agent, "model", None)
    if model is not None and not getattr(model, "_rastir_wrapped", False):
        originals[agent_id]["model"] = model
        model_name = _get_model_name(agent)
        agent.model = wrap(
            model,
            name=f"strands.{model_name}",
            span_type="llm",
            include=["stream"],
        )

    # --- Wrap tools (tool tracing) ---
    # Tools live in agent.tool_registry.registry: dict[str, AgentTool]
    registry = getattr(getattr(agent, "tool_registry", None), "registry", None)
    if registry:
        from rastir.wrapper import _make_sync_wrapper
        from rastir.spans import SpanType

        patched_tools: list[tuple[Any, str, Any]] = []  # (tool, attr_name, orig)
        for tool_name, tool in registry.items():
            if getattr(tool, "_rastir_tool_patched", False):
                continue
            # Patch tool.stream in-place via __dict__ to bypass ABC
            original_stream = tool.stream
            span_name = f"strands.tool.{tool_name}"
            wrapped_stream = _make_sync_wrapper(
                original_stream, span_name, SpanType.TOOL, wrapped_obj=tool,
            )
            tool.__dict__["stream"] = wrapped_stream
            tool.__dict__["_rastir_tool_patched"] = True
            patched_tools.append((tool, "stream", original_stream))
        originals[agent_id]["_patched_tools"] = patched_tools


def _restore_originals(originals: dict[int, dict[str, Any]]) -> None:
    """Restore original model and tools on agents after execution."""
    for _id, saved in originals.items():
        ag = saved.get("_agent_ref")
        if ag is None:
            continue
        if "model" in saved:
            ag.model = saved["model"]
        # Restore patched tool methods
        for tool, attr_name, _orig in saved.get("_patched_tools", []):
            tool.__dict__.pop(attr_name, None)
            tool.__dict__.pop("_rastir_tool_patched", None)

