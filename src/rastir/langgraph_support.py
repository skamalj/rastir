"""LangGraph integration for Rastir.

Provides ``langgraph_agent`` — a single decorator that instruments
LangGraph compiled-graph execution.  The decorator:

  1. Scans function arguments for a LangGraph CompiledGraph
  2. Wraps every graph node function with a ``TRACE`` span
     (``node:<name>``) so node execution is visible
  3. Walks graph nodes to find chat models (via Runnable chains,
     closures, globals) and wraps them for per-call tracing
  4. Wraps tools inside ToolNode instances for per-invocation tracing
  5. Creates an ``@agent`` span around the entire run

Resulting span hierarchy::

    react_agent (AGENT)
      ├── node:agent (TRACE)
      │   └── langgraph.llm.gpt-4o.invoke (LLM)
      ├── node:tools (TRACE)
      │   └── langgraph.tool.search.invoke (TOOL)
      └── node:agent (TRACE)
          └── langgraph.llm.gpt-4o.invoke (LLM)

Usage::

    from rastir import configure, langgraph_agent

    configure(service="my-app", push_url="http://localhost:8080")

    @langgraph_agent(agent_name="react_agent")
    def run(graph, query):
        return graph.invoke({"messages": [("user", query)]})

No LangGraph import is performed at module scope — detection uses
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

# Model attributes to extract a display name for the LLM span.
_MODEL_ATTRS = ("model_name", "model", "model_id", "modelId")

# LLM methods worth instrumenting on LangChain chat models.
_LLM_INCLUDE = [
    "invoke", "ainvoke",
    "stream", "astream",
    "generate", "agenerate",
    "batch", "abatch",
]

# Tool methods worth instrumenting on LangChain tools.
_TOOL_INCLUDE = ["invoke", "ainvoke", "_run", "_arun", "run", "arun"]


# ---------------------------------------------------------------------------
# Graph / model detection helpers
# ---------------------------------------------------------------------------

def _is_compiled_graph(obj: Any) -> bool:
    """True if *obj* looks like a LangGraph CompiledGraph."""
    module = getattr(type(obj), "__module__", "") or ""
    cls_name = type(obj).__name__
    return "langgraph" in module and "Compiled" in cls_name


def _is_chat_model(obj: Any) -> bool:
    """True if *obj* looks like a LangChain chat model.

    Walks the MRO looking for ``BaseChatModel``, ``BaseLLM``, or
    ``BaseLanguageModel`` from the ``langchain`` ecosystem.
    """
    for base in type(obj).__mro__:
        module = getattr(base, "__module__", "") or ""
        name = base.__name__
        if "langchain" in module and name in (
            "BaseChatModel", "BaseLLM", "BaseLanguageModel",
        ):
            return True
    return False


def _is_tool_node(obj: Any) -> bool:
    """True if *obj* looks like a LangGraph ``ToolNode``."""
    cls_name = type(obj).__name__
    module = getattr(type(obj), "__module__", "") or ""
    return cls_name == "ToolNode" and "langgraph" in module


def _model_display_name(model: Any) -> str:
    """Extract a human-readable name from a chat model."""
    for attr in _MODEL_ATTRS:
        val = getattr(model, attr, None)
        if val and isinstance(val, str):
            return val
    return type(model).__name__


# ---------------------------------------------------------------------------
# langgraph_agent decorator
# ---------------------------------------------------------------------------

def langgraph_agent(
    func: F | None = None,
    *,
    agent_name: str | None = None,
) -> F | Callable[[F], F]:
    """Decorator that instruments a LangGraph compiled-graph call.

    Wraps chat models and tools found inside the graph for per-call
    observability and creates an ``@agent`` span around execution.

    Args:
        agent_name: Name for the outer agent span.  Defaults to the
            function name.

    Usage::

        @langgraph_agent(agent_name="react")
        def run(graph, query):
            return graph.invoke({"messages": [("user", query)]})

        @langgraph_agent
        async def run(graph, query):
            return await graph.ainvoke({"messages": [("user", query)]})
    """

    def decorator(fn: F) -> F:
        resolved_name = agent_name or fn.__name__

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return _langgraph_agent_impl(fn, resolved_name, args, kwargs)

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            return await _async_langgraph_agent_impl(
                fn, resolved_name, args, kwargs,
            )

        if asyncio.iscoroutinefunction(fn):
            return async_wrapper  # type: ignore[return-value]
        return wrapper  # type: ignore[return-value]

    if func is not None:
        return decorator(func)
    return decorator  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Sync / async implementation
# ---------------------------------------------------------------------------

def _langgraph_agent_impl(
    fn: Callable,
    agent_name: str,
    args: tuple,
    kwargs: dict,
) -> Any:
    """Sync implementation of langgraph_agent."""
    from rastir.context import (
        end_span, start_span, set_current_agent, reset_current_agent,
    )
    from rastir.queue import enqueue_span
    from rastir.spans import SpanStatus, SpanType

    span, token = start_span(agent_name, SpanType.AGENT)
    span.set_attribute("agent_name", agent_name)
    agent_token = set_current_agent(agent_name)

    originals: list[tuple] = []

    try:
        for obj in (*args, *kwargs.values()):
            if _is_compiled_graph(obj):
                _wrap_graph_internals(obj, originals)

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


async def _async_langgraph_agent_impl(
    fn: Callable,
    agent_name: str,
    args: tuple,
    kwargs: dict,
) -> Any:
    """Async implementation of langgraph_agent."""
    from rastir.context import (
        end_span, start_span, set_current_agent, reset_current_agent,
    )
    from rastir.queue import enqueue_span
    from rastir.spans import SpanStatus, SpanType

    span, token = start_span(agent_name, SpanType.AGENT)
    span.set_attribute("agent_name", agent_name)
    agent_token = set_current_agent(agent_name)

    originals: list[tuple] = []

    try:
        for obj in (*args, *kwargs.values()):
            if _is_compiled_graph(obj):
                _wrap_graph_internals(obj, originals)

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
# Graph internals: walk & wrap
# ---------------------------------------------------------------------------

def _wrap_graph_internals(
    graph: Any, originals: list[tuple],
) -> None:
    """Walk compiled graph nodes, wrap node functions, chat models and tools."""
    nodes = getattr(graph, "nodes", None)
    if not isinstance(nodes, dict):
        return

    seen: set[int] = set()
    for name, node in nodes.items():
        if name == "__start__":
            continue
        bound = getattr(node, "bound", None)
        if bound is not None:
            # 1. Walk inside to wrap LLMs and tools (before node-func
            #    replacement, so closure/globals inspection sees originals)
            _wrap_runnable(bound, originals, seen)
            # 2. Wrap the node function itself with a trace span
            _wrap_node_func(bound, name, originals)


def _wrap_runnable(
    obj: Any, originals: list[tuple], seen: set[int],
) -> None:
    """Recursively walk a Runnable chain looking for models and tools.

    Tracks object ids in *seen* to avoid cycles.
    """
    obj_id = id(obj)
    if obj_id in seen:
        return
    seen.add(obj_id)

    type_name = type(obj).__name__

    # ---- ToolNode ---------------------------------------------------------
    if _is_tool_node(obj):
        _wrap_toolnode_tools(obj, originals)
        return

    # ---- RunnableBinding --------------------------------------------------
    if type_name == "RunnableBinding":
        inner = getattr(obj, "bound", None)
        if inner is not None:
            if _is_chat_model(inner):
                _wrap_model_at(obj, "bound", inner, originals)
            else:
                _wrap_runnable(inner, originals, seen)
        return

    # ---- RunnableSequence -------------------------------------------------
    if type_name == "RunnableSequence":
        for attr in ("first", "last"):
            inner = getattr(obj, attr, None)
            if inner is not None:
                _wrap_runnable(inner, originals, seen)
        middle = getattr(obj, "middle", None)
        if isinstance(middle, (list, tuple)):
            for m in middle:
                _wrap_runnable(m, originals, seen)
        return

    # ---- RunnableCallable / plain callable --------------------------------
    func = getattr(obj, "func", None)
    if func is not None and callable(func):
        _walk_func_for_wrapping(func, originals, seen)
        return

    # ---- Generic fallback: follow .bound, .func, .first ------------------
    for attr in ("bound", "first"):
        inner = getattr(obj, attr, None)
        if inner is not None:
            _wrap_runnable(inner, originals, seen)


def _walk_func_for_wrapping(
    func: Any, originals: list[tuple], seen: set[int],
) -> None:
    """Walk a function's closures and globals for chat models."""
    # 1. Closure cells
    closure = getattr(func, "__closure__", None)
    if closure:
        for cell in closure:
            try:
                val = cell.cell_contents
            except ValueError:
                continue
            if id(val) not in seen:
                _wrap_runnable(val, originals, seen)

    # 2. Global variables referenced by the function.
    #    Only inspect names actually used in the function body
    #    (co_names for global lookups) to avoid scanning the entire
    #    module namespace.
    code = getattr(func, "__code__", None)
    func_globals = getattr(func, "__globals__", None)
    if code is not None and func_globals is not None:
        for varname in code.co_names:
            val = func_globals.get(varname)
            if val is None or id(val) in seen:
                continue
            if _is_chat_model(val):
                seen.add(id(val))
                _wrap_model_at(func_globals, varname, val, originals,
                               kind="dict")


# ---------------------------------------------------------------------------
# Node-level wrapping
# ---------------------------------------------------------------------------

def _wrap_node_func(
    bound: Any, node_name: str, originals: list[tuple],
) -> None:
    """Replace ``bound.func`` (and ``bound.afunc``) with traced wrappers.

    Each node invocation will emit a ``TRACE`` span named
    ``node:<node_name>``.
    """
    from rastir.context import end_span, start_span
    from rastir.queue import enqueue_span
    from rastir.spans import SpanStatus, SpanType

    span_name = f"node:{node_name}"

    # --- sync func ---
    orig_func = getattr(bound, "func", None)
    if orig_func is not None and callable(orig_func):
        if getattr(orig_func, "_rastir_node_traced", False):
            return  # already wrapped

        @functools.wraps(orig_func)
        def traced_func(*args: Any, **kwargs: Any) -> Any:
            span, token = start_span(span_name, SpanType.TRACE)
            span.set_attribute("langgraph.node", node_name)
            try:
                result = orig_func(*args, **kwargs)
                span.finish(SpanStatus.OK)
                return result
            except BaseException as exc:
                span.record_error(exc)
                span.finish(SpanStatus.ERROR)
                raise
            finally:
                end_span(token)
                enqueue_span(span)

        traced_func._rastir_node_traced = True  # type: ignore[attr-defined]
        originals.append(("attr", bound, "func", orig_func))
        bound.func = traced_func

    # --- async afunc ---
    orig_afunc = getattr(bound, "afunc", None)
    if orig_afunc is not None and callable(orig_afunc):
        if getattr(orig_afunc, "_rastir_node_traced", False):
            return

        @functools.wraps(orig_afunc)
        async def traced_afunc(*args: Any, **kwargs: Any) -> Any:
            span, token = start_span(span_name, SpanType.TRACE)
            span.set_attribute("langgraph.node", node_name)
            try:
                result = await orig_afunc(*args, **kwargs)
                span.finish(SpanStatus.OK)
                return result
            except BaseException as exc:
                span.record_error(exc)
                span.finish(SpanStatus.ERROR)
                raise
            finally:
                end_span(token)
                enqueue_span(span)

        traced_afunc._rastir_node_traced = True  # type: ignore[attr-defined]
        originals.append(("attr", bound, "afunc", orig_afunc))
        bound.afunc = traced_afunc


# ---------------------------------------------------------------------------
# Wrapping actions
# ---------------------------------------------------------------------------

def _wrap_model_at(
    parent: Any,
    attr: str,
    model: Any,
    originals: list[tuple],
    *,
    kind: str = "attr",
) -> None:
    """Replace a chat model with a wrapped version.

    Args:
        parent: Object (or dict) that holds the model reference.
        attr: Attribute name (or dict key) to replace.
        model: The original chat model object.
        originals: List to append restore info to.
        kind: ``"attr"`` for ``setattr`` or ``"dict"`` for dict
            key assignment.
    """
    if getattr(model, "_rastir_wrapped", False):
        return

    display = _model_display_name(model)
    wrapped = wrap(
        model,
        name=f"langgraph.llm.{display}",
        span_type="llm",
        include=_LLM_INCLUDE,
    )

    if kind == "dict":
        originals.append(("dict", parent, attr, model))
        parent[attr] = wrapped
    else:
        originals.append(("attr", parent, attr, model))
        setattr(parent, attr, wrapped)


def _wrap_toolnode_tools(toolnode: Any, originals: list[tuple]) -> None:
    """Wrap every tool inside a ``ToolNode``."""
    tools_dict = getattr(toolnode, "_tools_by_name", None)
    if not isinstance(tools_dict, dict):
        return

    for name, tool_obj in list(tools_dict.items()):
        if getattr(tool_obj, "_rastir_wrapped", False):
            continue
        originals.append(("dict", tools_dict, name, tool_obj))
        tools_dict[name] = wrap(
            tool_obj,
            name=f"langgraph.tool.{name}",
            span_type="tool",
            include=_TOOL_INCLUDE,
        )


# ---------------------------------------------------------------------------
# Restore originals
# ---------------------------------------------------------------------------

def _restore_originals(originals: list[tuple]) -> None:
    """Undo all wrapping by restoring original objects."""
    for entry in reversed(originals):
        kind = entry[0]
        if kind == "dict":
            _, d, key, original = entry
            d[key] = original
        elif kind == "attr":
            _, obj, attr, original = entry
            setattr(obj, attr, original)
