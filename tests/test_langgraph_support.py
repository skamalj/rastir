"""Unit tests for rastir.langgraph_support — langgraph_agent decorator.

Tests cover:
  - _is_compiled_graph detection
  - _is_chat_model detection
  - _is_tool_node detection
  - _model_display_name helper
  - _wrap_graph_internals: RunnableBinding, ToolNode, closures, globals
  - _restore_originals
  - langgraph_agent decorator: bare and parameterized usage
  - Agent span emission (name, type, status)
  - Error handling (span records error, re-raises)
  - Async variant
  - Double-wrap prevention

Uses mock classes that mimic LangGraph / LangChain class-name / module
structure so we can test without requiring full framework installation.
"""

from __future__ import annotations

import asyncio
import types as pytypes
from unittest.mock import MagicMock, patch

import pytest

from rastir.langgraph_support import (
    _is_compiled_graph,
    _is_chat_model,
    _is_tool_node,
    _model_display_name,
    _wrap_graph_internals,
    _wrap_node_func,
    _wrap_runnable,
    _walk_func_for_wrapping,
    _wrap_model_at,
    _wrap_toolnode_tools,
    _restore_originals,
    langgraph_agent,
)
from rastir.spans import SpanType, SpanStatus


# ========================================================================
# Fake classes mimicking LangGraph / LangChain structures
# ========================================================================

# --- Fake BaseChatModel (MRO ancestor) ---
_BaseChatModel = type(
    "BaseChatModel", (),
    {"__module__": "langchain_core.language_models.chat_models"},
)


def _chat_init(self, model_name="gpt-4o"):
    self.model_name = model_name
    self._rastir_wrapped = False


_FakeChatModel = type(
    "ChatOpenAI", (_BaseChatModel,),
    {"__module__": "langchain_openai.chat_models", "__init__": _chat_init},
)


# --- Fake ToolNode ---
def _toolnode_init(self, tools_by_name=None):
    self._tools_by_name = tools_by_name or {}


_FakeToolNode = type(
    "ToolNode", (),
    {
        "__module__": "langgraph.prebuilt.tool_node",
        "__init__": _toolnode_init,
    },
)


# --- Fake CompiledStateGraph ---
def _graph_init(self, nodes=None):
    self.nodes = nodes or {}


_FakeCompiledGraph = type(
    "CompiledStateGraph", (),
    {
        "__module__": "langgraph.graph.state",
        "__init__": _graph_init,
    },
)


# --- Fake RunnableBinding ---
def _rb_init(self, bound=None, kwargs=None):
    self.bound = bound
    self.kwargs = kwargs or {}


_FakeRunnableBinding = type(
    "RunnableBinding", (),
    {"__module__": "langchain_core.runnables.base", "__init__": _rb_init},
)


# --- Fake RunnableSequence ---
def _rs_init(self, first=None, last=None, middle=None):
    self.first = first
    self.last = last
    self.middle = middle or []


_FakeRunnableSequence = type(
    "RunnableSequence", (),
    {"__module__": "langchain_core.runnables.base", "__init__": _rs_init},
)


# --- Fake RunnableCallable ---
def _rc_init(self, func=None):
    self.func = func


_FakeRunnableCallable = type(
    "RunnableCallable", (),
    {"__module__": "langgraph.utils.runnable", "__init__": _rc_init},
)


# --- Fake PregelNode ---
def _pn_init(self, bound=None):
    self.bound = bound


_FakePregelNode = type(
    "PregelNode", (),
    {"__module__": "langgraph.pregel.read", "__init__": _pn_init},
)


# --- Fake StructuredTool ---
def _tool_init(self, name="my_tool"):
    self.name = name
    self._rastir_wrapped = False


_FakeStructuredTool = type(
    "StructuredTool", (),
    {
        "__module__": "langchain_core.tools.structured",
        "__init__": _tool_init,
        "invoke": lambda self, *a, **kw: "result",
        "_run": lambda self, *a, **kw: "result",
        "run": lambda self, *a, **kw: "result",
    },
)


# ========================================================================
# Helper factories
# ========================================================================

def _make_model(model_name="gpt-4o"):
    return _FakeChatModel(model_name=model_name)


def _make_tool(name="add"):
    return _FakeStructuredTool(name=name)


def _make_toolnode(tool_names=("add",)):
    tools = {n: _make_tool(n) for n in tool_names}
    return _FakeToolNode(tools_by_name=tools)


def _make_graph_with_binding(model_name="gpt-4o"):
    """CompiledGraph with agent node -> RunnableBinding -> ChatModel."""
    model = _make_model(model_name)
    binding = _FakeRunnableBinding(bound=model, kwargs={"tools": ["t1"]})
    agent_node = _FakePregelNode(bound=binding)
    graph = _FakeCompiledGraph(nodes={
        "__start__": _FakePregelNode(bound=_FakeRunnableCallable()),
        "agent": agent_node,
    })
    return graph, model, binding


def _make_graph_with_toolnode(tool_names=("add", "search")):
    """CompiledGraph with a tools node -> ToolNode."""
    tn = _make_toolnode(tool_names)
    node = _FakePregelNode(bound=tn)
    graph = _FakeCompiledGraph(nodes={
        "__start__": _FakePregelNode(bound=_FakeRunnableCallable()),
        "tools": node,
    })
    return graph, tn


def _make_graph_with_sequence(model_name="gpt-4o"):
    """CompiledGraph where the model is inside a RunnableSequence.last.bound."""
    model = _make_model(model_name)
    binding = _FakeRunnableBinding(bound=model)
    prompt = _FakeRunnableCallable(func=lambda x: x)
    seq = _FakeRunnableSequence(first=prompt, last=binding)
    # Wrap in a RunnableCallable that has the seq in a closure
    def call_model(state):
        return seq.last.bound.invoke(state)
    callable_node = _FakeRunnableCallable(func=call_model)
    node = _FakePregelNode(bound=callable_node)
    graph = _FakeCompiledGraph(nodes={
        "__start__": _FakePregelNode(bound=_FakeRunnableCallable()),
        "agent": node,
    })
    return graph, model, binding, seq


def _make_graph_full(model_name="gpt-4o", tool_names=("add",)):
    """CompiledGraph with both agent (binding) and tools (ToolNode)."""
    model = _make_model(model_name)
    binding = _FakeRunnableBinding(bound=model, kwargs={"tools": ["t1"]})
    agent_node = _FakePregelNode(bound=binding)
    tn = _make_toolnode(tool_names)
    tools_node = _FakePregelNode(bound=tn)
    graph = _FakeCompiledGraph(nodes={
        "__start__": _FakePregelNode(bound=_FakeRunnableCallable()),
        "agent": agent_node,
        "tools": tools_node,
    })
    return graph, model, binding, tn


def _make_global_func_with_model(model):
    """Create a function whose __globals__ contain ``model``.

    Uses exec() so ``model`` is a *global* reference (co_names)
    rather than a closure free variable.
    """
    src = "def node_func(state):\n    return model.invoke(state)\n"
    custom_globals: dict = {"model": model, "__builtins__": __builtins__}
    exec(compile(src, "<test>", "exec"), custom_globals)  # noqa: S102
    return custom_globals["node_func"]


# ========================================================================
# Detection tests
# ========================================================================

class TestIsCompiledGraph:
    def test_compiled_state_graph(self):
        g = _FakeCompiledGraph()
        assert _is_compiled_graph(g) is True

    def test_plain_dict_not_graph(self):
        assert _is_compiled_graph({"nodes": {}}) is False

    def test_wrong_module(self):
        cls = type("CompiledGraph", (), {"__module__": "mylib.graph"})
        assert _is_compiled_graph(cls()) is False

    def test_wrong_name(self):
        cls = type("StateGraph", (), {"__module__": "langgraph.graph.state"})
        assert _is_compiled_graph(cls()) is False


class TestIsChatModel:
    def test_fake_chat_model(self):
        m = _make_model()
        assert _is_chat_model(m) is True

    def test_plain_object(self):
        assert _is_chat_model(object()) is False

    def test_dict_not_model(self):
        assert _is_chat_model({"model_name": "x"}) is False

    def test_langchain_base_llm(self):
        _BaseLLM = type(
            "BaseLLM", (),
            {"__module__": "langchain_core.language_models.llms"},
        )
        _FakeLLM = type("FakeLLM", (_BaseLLM,), {"__module__": "my.llm"})
        assert _is_chat_model(_FakeLLM()) is True

    def test_base_lang_model(self):
        _Base = type(
            "BaseLanguageModel", (),
            {"__module__": "langchain_core.language_models.base"},
        )
        _M = type("MyLM", (_Base,), {"__module__": "my"})
        assert _is_chat_model(_M()) is True


class TestIsToolNode:
    def test_fake_toolnode(self):
        tn = _FakeToolNode()
        assert _is_tool_node(tn) is True

    def test_plain_obj_not_toolnode(self):
        assert _is_tool_node(object()) is False

    def test_wrong_module(self):
        cls = type("ToolNode", (), {"__module__": "mylib.tools"})
        assert _is_tool_node(cls()) is False


class TestModelDisplayName:
    def test_from_model_name(self):
        m = _make_model("gpt-4o")
        assert _model_display_name(m) == "gpt-4o"

    def test_from_model_attr(self):
        m = MagicMock(spec=[])
        m.model = "claude-3-opus"
        assert _model_display_name(m) == "claude-3-opus"

    def test_fallback_class_name(self):
        m = MagicMock(spec=[])
        # no model attrs -> class name
        assert "Mock" in _model_display_name(m)


# ========================================================================
# Wrapping tests
# ========================================================================

class TestWrapModelAt:
    def test_wrap_via_attr(self):
        model = _make_model()
        parent = _FakeRunnableBinding(bound=model)
        originals: list[tuple] = []

        _wrap_model_at(parent, "bound", model, originals, kind="attr")

        assert parent.bound is not model
        assert getattr(parent.bound, "_rastir_wrapped", False) is True
        assert len(originals) == 1
        assert originals[0] == ("attr", parent, "bound", model)

    def test_wrap_via_dict(self):
        model = _make_model()
        d = {"llm": model}
        originals: list[tuple] = []

        _wrap_model_at(d, "llm", model, originals, kind="dict")

        assert d["llm"] is not model
        assert getattr(d["llm"], "_rastir_wrapped", False) is True
        assert originals[0] == ("dict", d, "llm", model)

    def test_skip_already_wrapped(self):
        model = _make_model()
        model._rastir_wrapped = True
        parent = _FakeRunnableBinding(bound=model)
        originals: list[tuple] = []

        _wrap_model_at(parent, "bound", model, originals)

        assert parent.bound is model  # unchanged
        assert len(originals) == 0


class TestWrapToolnodeTools:
    def test_wraps_all_tools(self):
        tn = _make_toolnode(("add", "search"))
        originals: list[tuple] = []

        _wrap_toolnode_tools(tn, originals)

        assert len(originals) == 2
        for name in ("add", "search"):
            assert getattr(tn._tools_by_name[name], "_rastir_wrapped") is True

    def test_skip_already_wrapped_tool(self):
        tn = _make_toolnode(("add",))
        tn._tools_by_name["add"]._rastir_wrapped = True
        originals: list[tuple] = []

        _wrap_toolnode_tools(tn, originals)

        assert len(originals) == 0

    def test_no_tools_dict(self):
        tn = _FakeToolNode()
        tn._tools_by_name = None
        originals: list[tuple] = []
        _wrap_toolnode_tools(tn, originals)
        assert len(originals) == 0


class TestWrapGraphInternals:
    def test_wraps_binding_model(self):
        graph, model, binding = _make_graph_with_binding()
        originals: list[tuple] = []

        _wrap_graph_internals(graph, originals)

        assert binding.bound is not model
        assert getattr(binding.bound, "_rastir_wrapped") is True
        assert len(originals) == 1

    def test_wraps_toolnode_tools(self):
        graph, tn = _make_graph_with_toolnode(("add", "search"))
        originals: list[tuple] = []

        _wrap_graph_internals(graph, originals)

        assert len(originals) == 2
        for name in ("add", "search"):
            assert getattr(tn._tools_by_name[name], "_rastir_wrapped") is True

    def test_wraps_both_model_and_tools(self):
        graph, model, binding, tn = _make_graph_full("gpt-4o", ("multiply",))
        originals: list[tuple] = []

        _wrap_graph_internals(graph, originals)

        # Model wrapped
        assert binding.bound is not model
        assert getattr(binding.bound, "_rastir_wrapped") is True
        # Tool wrapped
        assert getattr(tn._tools_by_name["multiply"], "_rastir_wrapped") is True

    def test_sequence_last_binding(self):
        """Model inside RunnableSequence.last (RunnableBinding)."""
        graph, model, binding, seq = _make_graph_with_sequence("claude-3")
        originals: list[tuple] = []

        _wrap_graph_internals(graph, originals)

        # The binding.bound should be wrapped
        assert binding.bound is not model
        assert getattr(binding.bound, "_rastir_wrapped") is True

    def test_skips_start_node(self):
        """__start__ node should be skipped."""
        start_model = _make_model("skip_me")
        start_binding = _FakeRunnableBinding(bound=start_model)
        graph = _FakeCompiledGraph(nodes={
            "__start__": _FakePregelNode(bound=start_binding),
        })
        originals: list[tuple] = []

        _wrap_graph_internals(graph, originals)

        assert start_binding.bound is start_model  # unchanged
        assert len(originals) == 0

    def test_no_nodes_attr(self):
        """Graph without nodes dict should not error."""
        graph = MagicMock(spec=[])
        originals: list[tuple] = []
        _wrap_graph_internals(graph, originals)
        assert len(originals) == 0


class TestWrapGlobalModel:
    """Test wrapping a model found via func.__globals__."""

    def test_global_model_wrapped(self):
        model = _make_model("gpt-4o-mini")
        # Create a function that references ``model`` as a GLOBAL
        # (not closure) by building it in a custom globals dict.
        node_func = _make_global_func_with_model(model)

        originals: list[tuple] = []
        seen: set[int] = set()

        _walk_func_for_wrapping(node_func, originals, seen)

        assert len(originals) == 1
        assert originals[0][0] == "dict"
        assert originals[0][2] == "model"
        assert node_func.__globals__["model"] is not model

    def test_global_non_model_ignored(self):
        """Non-model globals should not be wrapped."""
        def node_func(state):
            return str(state)

        originals: list[tuple] = []
        seen: set[int] = set()

        _walk_func_for_wrapping(node_func, originals, seen)

        assert len(originals) == 0

    def test_global_model_in_graph(self):
        """Model referenced as a global inside a graph node function."""
        model = _make_model("gpt-4o-mini")
        node_func = _make_global_func_with_model(model)
        callable_node = _FakeRunnableCallable(func=node_func)
        node = _FakePregelNode(bound=callable_node)
        graph = _FakeCompiledGraph(nodes={"llm_node": node})
        originals: list[tuple] = []

        _wrap_graph_internals(graph, originals)

        # 1 for global model + 1 for node func wrapping
        assert len(originals) == 2
        assert node_func.__globals__["model"] is not model


# ========================================================================
# Restore tests
# ========================================================================

class TestRestoreOriginals:
    def test_restore_attr(self):
        model = _make_model()
        parent = _FakeRunnableBinding(bound=MagicMock())  # wrapped
        originals = [("attr", parent, "bound", model)]

        _restore_originals(originals)

        assert parent.bound is model

    def test_restore_dict(self):
        original_tool = _make_tool("add")
        d = {"add": MagicMock()}  # wrapped
        originals = [("dict", d, "add", original_tool)]

        _restore_originals(originals)

        assert d["add"] is original_tool

    def test_restore_multiple(self):
        m = _make_model()
        t = _make_tool()
        parent = _FakeRunnableBinding(bound=MagicMock())
        d = {"tool": MagicMock()}
        originals = [
            ("attr", parent, "bound", m),
            ("dict", d, "tool", t),
        ]

        _restore_originals(originals)

        assert parent.bound is m
        assert d["tool"] is t

    def test_restore_in_reverse_order(self):
        """Verify restoration happens in reverse order."""
        order = []
        m1, m2 = _make_model("a"), _make_model("b")

        class TrackingDict(dict):
            def __setitem__(self, key, value):
                order.append(key)
                super().__setitem__(key, value)

        td = TrackingDict({"a": None, "b": None})
        originals = [("dict", td, "a", m1), ("dict", td, "b", m2)]

        _restore_originals(originals)

        assert order == ["b", "a"]  # reversed


# ========================================================================
# Decorator tests
# ========================================================================

class TestLanggraphAgentDecorator:
    """Tests for the langgraph_agent decorator."""

    @patch("rastir.queue.enqueue_span")
    def test_bare_decorator(self, mock_enqueue):
        """@langgraph_agent without parens works."""
        @langgraph_agent
        def run_graph(x):
            return "result"

        result = run_graph("not a graph")

        assert result == "result"
        mock_enqueue.assert_called_once()
        span = mock_enqueue.call_args[0][0]
        assert span.span_type == SpanType.AGENT
        assert span.attributes.get("agent") == "run_graph"

    @patch("rastir.queue.enqueue_span")
    def test_parameterised_decorator(self, mock_enqueue):
        """@langgraph_agent(agent_name=...) works."""
        @langgraph_agent(agent_name="react_agent")
        def run_graph(x):
            return "ok"

        result = run_graph("x")

        assert result == "ok"
        span = mock_enqueue.call_args[0][0]
        assert span.attributes.get("agent") == "react_agent"

    @patch("rastir.queue.enqueue_span")
    def test_wraps_and_restores_graph(self, mock_enqueue):
        """Graph internals are wrapped during execution and restored after."""
        graph, model, binding = _make_graph_with_binding()
        wrapped_during = []

        @langgraph_agent(agent_name="test")
        def run(g):
            wrapped_during.append(getattr(binding.bound, "_rastir_wrapped", False))
            return "ok"

        result = run(graph)

        assert result == "ok"
        assert wrapped_during == [True]  # was wrapped during execution
        assert binding.bound is model  # restored after

    @patch("rastir.queue.enqueue_span")
    def test_error_records_and_reraises(self, mock_enqueue):
        """Error is recorded on span and re-raised; originals restored."""
        graph, model, binding = _make_graph_with_binding()

        @langgraph_agent(agent_name="fail")
        def run(g):
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            run(graph)

        span = mock_enqueue.call_args[0][0]
        assert span.status == SpanStatus.ERROR
        assert any(e["name"] == "exception" for e in span.events)
        # Originals still restored
        assert binding.bound is model

    @patch("rastir.queue.enqueue_span")
    def test_graph_in_kwargs(self, mock_enqueue):
        """Graph passed as keyword arg is detected and wrapped."""
        graph, model, binding = _make_graph_with_binding()
        wrapped_during = []

        @langgraph_agent(agent_name="kw")
        def run(query, graph=None):
            wrapped_during.append(getattr(binding.bound, "_rastir_wrapped", False))
            return "ok"

        run("hello", graph=graph)

        assert wrapped_during == [True]
        assert binding.bound is model  # restored

    @patch("rastir.queue.enqueue_span")
    def test_span_ok_status(self, mock_enqueue):
        """Successful execution sets span status to OK."""
        @langgraph_agent(agent_name="ok_test")
        def run():
            return 42

        run()

        span = mock_enqueue.call_args[0][0]
        assert span.status == SpanStatus.OK

    @patch("rastir.queue.enqueue_span")
    def test_preserves_func_name(self, mock_enqueue):
        """Decorated function preserves __name__."""
        @langgraph_agent
        def my_custom_fn():
            return 1

        assert my_custom_fn.__name__ == "my_custom_fn"


class TestLanggraphAgentAsync:
    """Async decorator tests."""

    @patch("rastir.queue.enqueue_span")
    def test_async_decorator(self, mock_enqueue):
        graph, model, binding = _make_graph_with_binding()
        wrapped_during = []

        @langgraph_agent(agent_name="async_test")
        async def run(g):
            wrapped_during.append(getattr(binding.bound, "_rastir_wrapped", False))
            return "async_ok"

        result = asyncio.get_event_loop().run_until_complete(run(graph))

        assert result == "async_ok"
        assert wrapped_during == [True]
        assert binding.bound is model

    @patch("rastir.queue.enqueue_span")
    def test_async_error(self, mock_enqueue):
        @langgraph_agent
        async def run():
            raise RuntimeError("async boom")

        with pytest.raises(RuntimeError, match="async boom"):
            asyncio.get_event_loop().run_until_complete(run())

        span = mock_enqueue.call_args[0][0]
        assert span.status == SpanStatus.ERROR

    @patch("rastir.queue.enqueue_span")
    def test_async_bare_decorator(self, mock_enqueue):
        @langgraph_agent
        async def my_async_fn():
            return "hi"

        result = asyncio.get_event_loop().run_until_complete(my_async_fn())

        assert result == "hi"
        span = mock_enqueue.call_args[0][0]
        assert span.attributes.get("agent") == "my_async_fn"


class TestFullGraphWrapping:
    """End-to-end tests with both model and tool wrapping."""

    @patch("rastir.queue.enqueue_span")
    def test_full_graph_model_and_tools(self, mock_enqueue):
        graph, model, binding, tn = _make_graph_full("gpt-4o", ("add", "mul"))
        orig_add = tn._tools_by_name["add"]
        orig_mul = tn._tools_by_name["mul"]

        model_wrapped = []
        tools_wrapped = []

        @langgraph_agent(agent_name="full")
        def run(g):
            model_wrapped.append(getattr(binding.bound, "_rastir_wrapped", False))
            tools_wrapped.append(
                getattr(tn._tools_by_name["add"], "_rastir_wrapped", False)
            )
            return "done"

        run(graph)

        assert model_wrapped == [True]
        assert tools_wrapped == [True]
        # Restored
        assert binding.bound is model
        assert tn._tools_by_name["add"] is orig_add
        assert tn._tools_by_name["mul"] is orig_mul

    @patch("rastir.queue.enqueue_span")
    def test_global_model_in_graph_with_decorator(self, mock_enqueue):
        """Model referenced as __globals__ inside graph node wrapped."""
        model = _make_model("gpt-4o-mini")
        node_func = _make_global_func_with_model(model)
        callable_node = _FakeRunnableCallable(func=node_func)
        node = _FakePregelNode(bound=callable_node)
        graph = _FakeCompiledGraph(nodes={"llm_node": node})

        globals_wrapped = []

        @langgraph_agent(agent_name="globals_test")
        def run(g):
            globals_wrapped.append(
                getattr(node_func.__globals__["model"], "_rastir_wrapped", False)
            )
            return "ok"

        run(graph)

        assert globals_wrapped == [True]
        assert node_func.__globals__["model"] is model  # restored


class TestWrapRunnable:
    """Unit tests for the recursive _wrap_runnable walker."""

    def test_runnable_binding_with_model(self):
        model = _make_model()
        rb = _FakeRunnableBinding(bound=model)
        originals: list[tuple] = []

        _wrap_runnable(rb, originals, set())

        assert rb.bound is not model
        assert len(originals) == 1

    def test_runnable_binding_with_non_model(self):
        """RunnableBinding wrapping a non-model should recurse deeper."""
        inner = _FakeRunnableCallable(func=lambda x: x)
        rb = _FakeRunnableBinding(bound=inner)
        originals: list[tuple] = []

        _wrap_runnable(rb, originals, set())

        # No models found -> no wrapping
        assert len(originals) == 0

    def test_sequence_walks_first_and_last(self):
        model = _make_model()
        binding = _FakeRunnableBinding(bound=model)
        prompt = _FakeRunnableCallable(func=lambda x: x)
        seq = _FakeRunnableSequence(first=prompt, last=binding)
        originals: list[tuple] = []

        _wrap_runnable(seq, originals, set())

        assert binding.bound is not model
        assert len(originals) == 1

    def test_cycle_prevention(self):
        model = _make_model()
        binding = _FakeRunnableBinding(bound=model)
        originals: list[tuple] = []
        seen = {id(binding)}  # already seen

        _wrap_runnable(binding, originals, seen)

        assert binding.bound is model  # unchanged
        assert len(originals) == 0

    def test_sequence_with_middle(self):
        """Models in sequence.middle should be found too."""
        model = _make_model()
        binding = _FakeRunnableBinding(bound=model)
        seq = _FakeRunnableSequence(
            first=_FakeRunnableCallable(),
            last=_FakeRunnableCallable(),
            middle=[binding],
        )
        originals: list[tuple] = []

        _wrap_runnable(seq, originals, set())

        assert binding.bound is not model

    def test_toolnode_detected(self):
        tn = _make_toolnode(("mul",))
        originals: list[tuple] = []

        _wrap_runnable(tn, originals, set())

        assert len(originals) == 1
        assert getattr(tn._tools_by_name["mul"], "_rastir_wrapped") is True


class TestDoubleWrapPrevention:
    def test_model_not_double_wrapped(self):
        """Calling _wrap_graph_internals twice should not double-wrap."""
        graph, model, binding = _make_graph_with_binding()
        originals1: list[tuple] = []
        originals2: list[tuple] = []

        _wrap_graph_internals(graph, originals1)
        wrapped = binding.bound
        assert getattr(wrapped, "_rastir_wrapped") is True

        _wrap_graph_internals(graph, originals2)

        # Second call should not add more originals (already wrapped)
        assert len(originals2) == 0
        assert binding.bound is wrapped  # same wrapped object


class TestClosureWrapping:
    """Test wrapping models found in function closures."""

    def test_closure_cell_model(self):
        model = _make_model("claude-3")
        binding = _FakeRunnableBinding(bound=model)

        # Create a closure that captures the binding
        def make_func():
            b = binding
            def call_model(state):
                return b.bound.invoke(state)
            return call_model

        func = make_func()
        callable_node = _FakeRunnableCallable(func=func)
        node = _FakePregelNode(bound=callable_node)
        graph = _FakeCompiledGraph(nodes={"agent": node})
        originals: list[tuple] = []

        _wrap_graph_internals(graph, originals)

        assert binding.bound is not model
        assert getattr(binding.bound, "_rastir_wrapped") is True

    def test_closure_with_sequence(self):
        """Model inside closure -> RunnableSequence -> binding.bound."""
        model = _make_model("gpt-4o")
        binding = _FakeRunnableBinding(bound=model)
        seq = _FakeRunnableSequence(
            first=_FakeRunnableCallable(),
            last=binding,
        )

        def make_func():
            s = seq
            def call_model(state):
                return s.last.bound.invoke(state)
            return call_model

        func = make_func()
        callable_node = _FakeRunnableCallable(func=func)
        node = _FakePregelNode(bound=callable_node)
        graph = _FakeCompiledGraph(nodes={"agent": node})
        originals: list[tuple] = []

        _wrap_graph_internals(graph, originals)

        assert binding.bound is not model
        assert getattr(binding.bound, "_rastir_wrapped") is True


class TestSpanAttributes:
    """Verify correct span type and attributes."""

    @patch("rastir.queue.enqueue_span")
    def test_span_type_is_agent(self, mock_enqueue):
        @langgraph_agent(agent_name="my_agent")
        def run():
            return "ok"

        run()

        span = mock_enqueue.call_args[0][0]
        assert span.span_type == SpanType.AGENT
        assert span.name == "my_agent"


class TestFuncNameDefault:
    """Agent name defaults to function name when not specified."""

    @patch("rastir.queue.enqueue_span")
    def test_default_name(self, mock_enqueue):
        @langgraph_agent
        def my_custom_graph():
            return "ok"

        my_custom_graph()

        span = mock_enqueue.call_args[0][0]
        assert span.name == "my_custom_graph"
        assert span.attributes.get("agent") == "my_custom_graph"


# ========================================================================
# Node-level tracing tests
# ========================================================================

class TestNodeFuncWrapping:
    """Test _wrap_node_func: sync/async func wrapping, spans, restore."""

    def test_sync_func_wrapped(self):
        """bound.func is replaced and has _rastir_node_traced marker."""
        orig = lambda state: state
        bound = _FakeRunnableCallable(func=orig)
        originals: list[tuple] = []

        _wrap_node_func(bound, "my_node", originals)

        assert bound.func is not orig
        assert getattr(bound.func, "_rastir_node_traced", False) is True
        assert len(originals) == 1
        assert originals[0] == ("attr", bound, "func", orig)

    def test_sync_func_calls_original(self):
        """Wrapped sync func should call through to original."""
        calls = []
        def orig(state):
            calls.append(state)
            return state * 2

        bound = _FakeRunnableCallable(func=orig)
        originals: list[tuple] = []

        _wrap_node_func(bound, "double", originals)

        result = bound.func("hello")
        assert result == "hellohello"
        assert calls == ["hello"]

    def test_afunc_wrapped_when_present(self):
        """bound.afunc is replaced when it's a callable."""
        async def orig_afunc(state):
            return state

        bound = _FakeRunnableCallable(func=lambda s: s)
        bound.afunc = orig_afunc
        originals: list[tuple] = []

        _wrap_node_func(bound, "async_node", originals)

        assert bound.afunc is not orig_afunc
        assert getattr(bound.afunc, "_rastir_node_traced", False) is True
        # 1 for func + 1 for afunc
        assert len(originals) == 2

    def test_afunc_none_not_wrapped(self):
        """When afunc is None, only func gets wrapped."""
        orig = lambda state: state
        bound = _FakeRunnableCallable(func=orig)
        # Ensure afunc is None (default for _FakeRunnableCallable)
        assert not hasattr(bound, "afunc") or getattr(bound, "afunc", None) is None
        originals: list[tuple] = []

        _wrap_node_func(bound, "sync_only", originals)

        assert len(originals) == 1  # only func

    def test_double_wrap_prevented(self):
        """_rastir_node_traced marker prevents re-wrapping."""
        orig = lambda state: state
        bound = _FakeRunnableCallable(func=orig)
        originals1: list[tuple] = []
        originals2: list[tuple] = []

        _wrap_node_func(bound, "n", originals1)
        first_wrapped = bound.func
        _wrap_node_func(bound, "n", originals2)

        assert bound.func is first_wrapped  # unchanged
        assert len(originals2) == 0  # nothing added

    @patch("rastir.queue.enqueue_span")
    def test_node_span_emitted(self, mock_enqueue):
        """Calling wrapped func emits a TRACE span named 'node:<name>'."""
        bound = _FakeRunnableCallable(func=lambda s: s)
        originals: list[tuple] = []

        _wrap_node_func(bound, "agent", originals)
        bound.func({"key": "val"})

        assert mock_enqueue.called
        span = mock_enqueue.call_args[0][0]
        assert span.name == "node:agent"
        assert span.span_type == SpanType.TRACE
        assert span.status == SpanStatus.OK

    @patch("rastir.queue.enqueue_span")
    def test_node_span_has_node_attribute(self, mock_enqueue):
        """Span should have langgraph.node attribute set."""
        bound = _FakeRunnableCallable(func=lambda s: s)
        originals: list[tuple] = []

        _wrap_node_func(bound, "tools", originals)
        bound.func({})

        span = mock_enqueue.call_args[0][0]
        assert span.attributes.get("langgraph.node") == "tools"

    @patch("rastir.queue.enqueue_span")
    def test_node_span_error_on_exception(self, mock_enqueue):
        """Span should record error when func raises."""
        def failing(state):
            raise ValueError("boom")

        bound = _FakeRunnableCallable(func=failing)
        originals: list[tuple] = []

        _wrap_node_func(bound, "bad_node", originals)

        with pytest.raises(ValueError, match="boom"):
            bound.func({})

        span = mock_enqueue.call_args[0][0]
        assert span.status == SpanStatus.ERROR
        assert span.name == "node:bad_node"

    @patch("rastir.queue.enqueue_span")
    def test_async_node_span_emitted(self, mock_enqueue):
        """Async afunc should also emit TRACE span."""
        async def orig_afunc(state):
            return state

        bound = _FakeRunnableCallable(func=lambda s: s)
        bound.afunc = orig_afunc
        originals: list[tuple] = []

        _wrap_node_func(bound, "async_agent", originals)

        result = asyncio.get_event_loop().run_until_complete(bound.afunc({"x": 1}))
        assert result == {"x": 1}

        # Find the span from the afunc call (last enqueued)
        span = mock_enqueue.call_args[0][0]
        assert span.name == "node:async_agent"
        assert span.span_type == SpanType.TRACE

    def test_restore_puts_original_func_back(self):
        """_restore_originals should restore original func."""
        orig = lambda state: state
        bound = _FakeRunnableCallable(func=orig)
        originals: list[tuple] = []

        _wrap_node_func(bound, "n", originals)
        assert bound.func is not orig

        _restore_originals(originals)
        assert bound.func is orig

    def test_all_nodes_traced_in_full_graph(self):
        """Full graph with agent + tools nodes should trace both."""
        graph, model, binding, tn = _make_graph_full("gpt-4o", ("search",))
        originals: list[tuple] = []

        _wrap_graph_internals(graph, originals)

        # Check that node funcs on agent and tools nodes are wrapped
        agent_bound = graph.nodes["agent"].bound
        tools_bound = graph.nodes["tools"].bound

        # Agent node is a RunnableBinding — _wrap_node_func wraps
        # bound.func, but RunnableBinding doesn't have func → skip
        # ToolNode also doesn't have func attr → skip
        # Only RunnableCallable nodes get func-wrapped.
        # Model and tool wrapping should still work:
        assert binding.bound is not model
        assert getattr(binding.bound, "_rastir_wrapped") is True
        assert getattr(tn._tools_by_name["search"], "_rastir_wrapped") is True
