"""Unit tests for rastir.llamaindex_support — llamaindex_agent decorator.

Tests cover:
  - _is_llamaindex_agent detection helpers
  - llamaindex_agent decorator: bare and parameterized usage
  - LLM and tool wrapping on agents
  - Restore of originals after execution
  - Agent span emission (name, type, status)
  - Error handling (span records error, re-raises)
  - Async variant

Uses mock Agent classes that mimic LlamaIndex's class-name / module
structure so we can test without requiring llama-index to be installed.
"""

from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rastir.llamaindex_support import (
    _is_llamaindex_agent,
    _get_agent_tools,
    _set_agent_tools,
    _wrap_agent_internals,
    _restore_originals,
    llamaindex_agent,
)
from rastir.spans import SpanType, SpanStatus


# ========================================================================
# Fake LlamaIndex-like classes for testing
# ========================================================================

def _agent_init(self, llm=None, tools=None):
    self._llm = llm
    self._tools = tools or []


_ReActAgentClass = type(
    "ReActAgent", (),
    {"__module__": "llama_index.core.agent", "__init__": _agent_init},
)

_FunctionCallingAgentClass = type(
    "FunctionCallingAgent", (),
    {"__module__": "llama_index.core.agent", "__init__": _agent_init},
)

_OpenAIAgentClass = type(
    "OpenAIAgent", (),
    {"__module__": "llama_index.agent.openai", "__init__": _agent_init},
)


def _make_agent(
    cls=None, llm=None, tools=None,
):
    """Create a mock LlamaIndex agent with the expected attributes."""
    klass = cls or _ReActAgentClass
    if llm is None:
        llm = MagicMock()
        llm._rastir_wrapped = False
    agent = klass(llm=llm, tools=tools or [])
    return agent


def _make_tool(name: str = "search_tool"):
    """Create a mock LlamaIndex tool with name and metadata."""
    tool = MagicMock()
    tool.metadata = MagicMock()
    tool.metadata.name = name
    tool._rastir_wrapped = False
    tool.call = MagicMock(return_value="tool result")
    tool.__call__ = MagicMock(return_value="tool result")
    return tool


# ========================================================================
# _is_llamaindex_agent tests
# ========================================================================


class TestIsLlamaindexAgent:
    def test_positive_react_agent(self):
        agent = _make_agent(cls=_ReActAgentClass)
        assert _is_llamaindex_agent(agent) is True

    def test_positive_function_calling_agent(self):
        agent = _make_agent(cls=_FunctionCallingAgentClass)
        assert _is_llamaindex_agent(agent) is True

    def test_positive_openai_agent(self):
        agent = _make_agent(cls=_OpenAIAgentClass)
        assert _is_llamaindex_agent(agent) is True

    def test_positive_subclass(self):
        """Subclass of ReActAgent is detected via MRO."""
        sub = type(
            "MyCustomAgent",
            (_ReActAgentClass,),
            {"__module__": "my_project.agents"},
        )
        agent = sub(llm=MagicMock(), tools=[])
        assert _is_llamaindex_agent(agent) is True

    def test_negative_wrong_name(self):
        cls = type("NotAnAgent", (), {"__module__": "llama_index.core.agent"})
        obj = cls()
        assert _is_llamaindex_agent(obj) is False

    def test_negative_wrong_module(self):
        cls = type("ReActAgent", (), {"__module__": "some_other.module"})
        obj = cls()
        assert _is_llamaindex_agent(obj) is False

    def test_negative_plain_object(self):
        assert _is_llamaindex_agent("hello") is False
        assert _is_llamaindex_agent(42) is False
        assert _is_llamaindex_agent(None) is False


# ========================================================================
# _get_agent_tools / _set_agent_tools tests
# ========================================================================


class TestAgentToolsAccessors:
    def test_get_tools_from_private_attr(self):
        agent = _make_agent(tools=[_make_tool("t1")])
        tools = _get_agent_tools(agent)
        assert len(tools) == 1

    def test_get_tools_returns_empty_for_none(self):
        agent = _make_agent()
        assert _get_agent_tools(agent) == []

    def test_set_tools_on_private_attr(self):
        agent = _make_agent()
        new_tools = [_make_tool("t2")]
        _set_agent_tools(agent, new_tools)
        assert agent._tools == new_tools

    def test_get_tools_from_public_attr(self):
        """Falls back to .tools if ._tools doesn't exist."""
        cls = type("AgentRunner", (), {
            "__module__": "llama_index.core.agent",
        })
        obj = cls()
        obj.tools = [_make_tool("pub")]
        assert len(_get_agent_tools(obj)) == 1

    def test_set_tools_on_public_attr(self):
        cls = type("AgentRunner", (), {
            "__module__": "llama_index.core.agent",
        })
        obj = cls()
        obj.tools = []
        new = [_make_tool()]
        _set_agent_tools(obj, new)
        assert obj.tools == new


# ========================================================================
# _wrap_agent_internals tests
# ========================================================================


class TestWrapAgentInternals:
    @patch("rastir.llamaindex_support.wrap")
    def test_wraps_agent_llm(self, mock_wrap):
        """LLM on agent gets wrapped with include=[chat, complete, ...]."""
        mock_wrap.side_effect = lambda obj, **kw: MagicMock(
            _rastir_wrapped=True, _original=obj
        )

        llm = MagicMock()
        llm._rastir_wrapped = False
        agent = _make_agent(llm=llm)

        originals: dict = {}
        _wrap_agent_internals(agent, originals)

        llm_wrap_calls = [c for c in mock_wrap.call_args_list if c[0][0] is llm]
        assert len(llm_wrap_calls) == 1
        call_kwargs = llm_wrap_calls[0][1]
        assert "chat" in call_kwargs["include"]
        assert "complete" in call_kwargs["include"]
        assert call_kwargs["span_type"] == "llm"

    @patch("rastir.llamaindex_support.wrap")
    def test_wraps_agent_tools(self, mock_wrap):
        """Each tool on agent gets wrapped with include=['call', '__call__']."""
        mock_wrap.side_effect = lambda obj, **kw: MagicMock(
            _rastir_wrapped=True,
            metadata=MagicMock(name=getattr(obj, "name", "t")),
        )

        tool = _make_tool("web_search")
        agent = _make_agent(tools=[tool])

        originals: dict = {}
        _wrap_agent_internals(agent, originals)

        tool_wrap_calls = [c for c in mock_wrap.call_args_list if c[0][0] is tool]
        assert len(tool_wrap_calls) == 1
        call_kwargs = tool_wrap_calls[0][1]
        assert "call" in call_kwargs["include"]
        assert call_kwargs["span_type"] == "tool"

    @patch("rastir.llamaindex_support.wrap")
    def test_stores_originals(self, mock_wrap):
        """Originals dict stores agent ref, original LLM and tools."""
        mock_wrap.side_effect = lambda obj, **kw: MagicMock(_rastir_wrapped=True)

        llm = MagicMock()
        llm._rastir_wrapped = False
        tool = _make_tool()
        agent = _make_agent(llm=llm, tools=[tool])

        originals: dict = {}
        _wrap_agent_internals(agent, originals)

        agent_id = id(agent)
        assert agent_id in originals
        assert originals[agent_id]["_agent_ref"] is agent
        assert originals[agent_id]["llm"] is llm
        assert originals[agent_id]["tools"] == [tool]

    @patch("rastir.llamaindex_support.wrap")
    def test_skips_already_wrapped_llm(self, mock_wrap):
        """If LLM is already wrapped, don't re-wrap it."""
        llm = MagicMock()
        llm._rastir_wrapped = True
        agent = _make_agent(llm=llm)

        originals: dict = {}
        _wrap_agent_internals(agent, originals)

        llm_wrap_calls = [c for c in mock_wrap.call_args_list if c[0][0] is llm]
        assert len(llm_wrap_calls) == 0

    @patch("rastir.llamaindex_support.wrap")
    def test_skips_already_wrapped_tool(self, mock_wrap):
        """If a tool is already wrapped, keep it as-is."""
        mock_wrap.side_effect = lambda obj, **kw: MagicMock(_rastir_wrapped=True)

        tool = _make_tool("search")
        tool._rastir_wrapped = True
        agent = _make_agent(tools=[tool])

        originals: dict = {}
        _wrap_agent_internals(agent, originals)

        tool_wrap_calls = [c for c in mock_wrap.call_args_list if c[0][0] is tool]
        assert len(tool_wrap_calls) == 0


# ========================================================================
# _restore_originals tests
# ========================================================================


class TestRestoreOriginals:
    def test_restores_llm_and_tools(self):
        """After restore, agent has its original LLM and tools."""
        original_llm = MagicMock()
        original_tools = [_make_tool()]
        agent = _make_agent(llm=MagicMock(), tools=[MagicMock()])

        originals = {
            id(agent): {
                "_agent_ref": agent,
                "llm": original_llm,
                "llm_attr": "_llm",
                "tools": original_tools,
            }
        }

        _restore_originals(originals)

        assert agent._llm is original_llm
        assert agent._tools is original_tools

    def test_handles_missing_agent_ref(self):
        """No error if _agent_ref is missing."""
        originals = {123: {"llm": MagicMock()}}
        _restore_originals(originals)  # Should not raise

    def test_partial_restore(self):
        """If only LLM was saved (no tools), only LLM is restored."""
        original_llm = MagicMock()
        agent = _make_agent()

        originals = {
            id(agent): {
                "_agent_ref": agent,
                "llm": original_llm,
                "llm_attr": "_llm",
            }
        }

        _restore_originals(originals)
        assert agent._llm is original_llm


# ========================================================================
# llamaindex_agent decorator tests
# ========================================================================


class TestLlamaindexAgent:
    @patch("rastir.llamaindex_support.enqueue_span", create=True)
    @patch("rastir.queue.enqueue_span")
    def test_bare_decorator(self, mock_enqueue_queue, mock_enqueue_local):
        """@llamaindex_agent without parens works."""
        agent = _make_agent()

        @llamaindex_agent
        def run(a):
            return "done"

        with patch("rastir.llamaindex_support.wrap") as mw:
            mw.side_effect = lambda obj, **kw: obj
            result = run(agent)

        assert result == "done"

    @patch("rastir.queue.enqueue_span")
    def test_parameterized_decorator(self, mock_enqueue):
        """@llamaindex_agent(agent_name=...) works."""
        agent = _make_agent()

        @llamaindex_agent(agent_name="my_agent")
        def run(a):
            return "ok"

        with patch("rastir.llamaindex_support.wrap") as mw:
            mw.side_effect = lambda obj, **kw: obj
            result = run(agent)

        assert result == "ok"

    @patch("rastir.queue.enqueue_span")
    def test_agent_span_emitted(self, mock_enqueue):
        """Decorator creates an AGENT span with correct name."""
        agent = _make_agent()

        @llamaindex_agent(agent_name="test_agent")
        def run(a):
            return None

        with patch("rastir.llamaindex_support.wrap") as mw:
            mw.side_effect = lambda obj, **kw: obj
            run(agent)

        assert mock_enqueue.call_count == 1
        span = mock_enqueue.call_args[0][0]
        assert span.name == "test_agent"
        assert span.span_type == SpanType.AGENT
        assert span.status == SpanStatus.OK
        assert span.attributes["agent_name"] == "test_agent"

    @patch("rastir.queue.enqueue_span")
    def test_agent_span_name_defaults_to_func(self, mock_enqueue):
        """Without agent_name, span name defaults to function name."""

        @llamaindex_agent
        def my_workflow(a):
            return None

        with patch("rastir.llamaindex_support.wrap") as mw:
            mw.side_effect = lambda obj, **kw: obj
            my_workflow("not_an_agent")

        span = mock_enqueue.call_args[0][0]
        assert span.name == "my_workflow"

    @patch("rastir.queue.enqueue_span")
    def test_error_records_on_span(self, mock_enqueue):
        """On exception, span records error and re-raises."""
        agent = _make_agent()

        @llamaindex_agent(agent_name="error_agent")
        def run(a):
            raise ValueError("boom")

        with patch("rastir.llamaindex_support.wrap") as mw:
            mw.side_effect = lambda obj, **kw: obj
            with pytest.raises(ValueError, match="boom"):
                run(agent)

        span = mock_enqueue.call_args[0][0]
        assert span.status == SpanStatus.ERROR
        assert len(span.events) == 1
        assert span.events[0]["name"] == "exception"
        assert "boom" in span.events[0]["attributes"]["exception.message"]

    @patch("rastir.queue.enqueue_span")
    def test_originals_restored_after_success(self, mock_enqueue):
        """After successful execution, agent LLM and tools are restored."""
        original_llm = MagicMock()
        original_llm._rastir_wrapped = False
        original_tool = _make_tool("search")
        agent = _make_agent(llm=original_llm, tools=[original_tool])

        @llamaindex_agent(agent_name="restore_test")
        def run(a):
            return "done"

        with patch("rastir.llamaindex_support.wrap") as mw:
            mw.side_effect = lambda obj, **kw: MagicMock(_rastir_wrapped=True)
            run(agent)

        assert agent._llm is original_llm
        assert agent._tools == [original_tool]

    @patch("rastir.queue.enqueue_span")
    def test_originals_restored_after_error(self, mock_enqueue):
        """After error, agent LLM and tools are still restored."""
        original_llm = MagicMock()
        original_llm._rastir_wrapped = False
        agent = _make_agent(llm=original_llm)

        @llamaindex_agent(agent_name="error_restore")
        def run(a):
            raise RuntimeError("fail")

        with patch("rastir.llamaindex_support.wrap") as mw:
            mw.side_effect = lambda obj, **kw: MagicMock(_rastir_wrapped=True)
            with pytest.raises(RuntimeError):
                run(agent)

        assert agent._llm is original_llm

    @patch("rastir.queue.enqueue_span")
    def test_agent_in_kwargs(self, mock_enqueue):
        """Agent passed as kwarg is also detected and wrapped."""
        agent = _make_agent()

        @llamaindex_agent(agent_name="kw_agent")
        def run(*, my_agent):
            return "ok"

        with patch("rastir.llamaindex_support.wrap") as mw:
            mw.side_effect = lambda obj, **kw: obj
            result = run(my_agent=agent)

        assert result == "ok"
        assert mock_enqueue.call_count == 1

    @patch("rastir.queue.enqueue_span")
    def test_non_agent_args_ignored(self, mock_enqueue):
        """Non-agent args don't cause errors."""

        @llamaindex_agent(agent_name="safe")
        def run(x, y):
            return x + y

        result = run(1, 2)
        assert result == 3


class TestLlamaindexAgentAsync:
    @patch("rastir.queue.enqueue_span")
    def test_async_decorator(self, mock_enqueue):
        """@llamaindex_agent works with async functions."""
        agent = _make_agent()

        @llamaindex_agent(agent_name="async_agent")
        async def run(a):
            return "async_done"

        with patch("rastir.llamaindex_support.wrap") as mw:
            mw.side_effect = lambda obj, **kw: obj
            result = asyncio.run(run(agent))

        assert result == "async_done"

        span = mock_enqueue.call_args[0][0]
        assert span.name == "async_agent"
        assert span.span_type == SpanType.AGENT
        assert span.status == SpanStatus.OK

    @patch("rastir.queue.enqueue_span")
    def test_async_error_handling(self, mock_enqueue):
        """Async path records errors on span."""
        agent = _make_agent()

        @llamaindex_agent(agent_name="async_err")
        async def run(a):
            raise TypeError("async boom")

        with patch("rastir.llamaindex_support.wrap") as mw:
            mw.side_effect = lambda obj, **kw: obj
            with pytest.raises(TypeError, match="async boom"):
                asyncio.run(run(agent))

        span = mock_enqueue.call_args[0][0]
        assert span.status == SpanStatus.ERROR


# ========================================================================
# Integration-style: wrapping flows through the decorator
# ========================================================================


class TestLlamaindexWrapping:
    """End-to-end: decorator wraps LLMs and tools on agents."""

    @patch("rastir.queue.enqueue_span")
    @patch("rastir.llamaindex_support.wrap")
    def test_llms_wrapped_during_execution(self, mock_wrap, mock_enqueue):
        """During execution, agent LLMs are wrapped."""
        sentinel = MagicMock(_rastir_wrapped=True, _is_wrapped_llm=True)
        mock_wrap.side_effect = lambda obj, **kw: sentinel

        llm = MagicMock()
        llm._rastir_wrapped = False
        agent = _make_agent(llm=llm, tools=[])

        wrapped_during: list = []

        @llamaindex_agent(agent_name="int_test")
        def run(a):
            wrapped_during.append(a._llm)
            return "ok"

        run(agent)

        assert len(wrapped_during) == 1
        assert wrapped_during[0]._is_wrapped_llm is True

        # After execution, original is restored
        assert agent._llm is llm

    @patch("rastir.queue.enqueue_span")
    @patch("rastir.llamaindex_support.wrap")
    def test_tools_wrapped_during_execution(self, mock_wrap, mock_enqueue):
        """During execution, agent tools are wrapped."""
        sentinel = MagicMock(_rastir_wrapped=True, _is_wrapped_tool=True)
        mock_wrap.side_effect = lambda obj, **kw: sentinel

        tool = _make_tool("scraper")
        agent = _make_agent(tools=[tool])

        wrapped_during: list = []

        @llamaindex_agent(agent_name="tool_test")
        def run(a):
            wrapped_during.extend(a._tools)
            return "ok"

        run(agent)

        assert len(wrapped_during) == 1
        assert wrapped_during[0]._is_wrapped_tool is True

        # After execution, original is restored
        assert agent._tools == [tool]
