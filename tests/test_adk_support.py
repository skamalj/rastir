"""Unit tests for rastir.adk_support — adk_agent decorator.

Tests cover:
  - _is_adk_agent / _is_adk_runner detection helpers
  - _get_adk_tools / _get_adk_model_name extraction
  - adk_agent decorator: bare and parameterized usage
  - Tool wrapping on agents
  - Restore of originals after execution
  - Agent span emission (name, type, status)
  - Error handling (span records error, re-raises)
  - Async variant

Uses mock ADK-like classes so we can test without google-adk installed.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from rastir.adk_support import (
    _is_adk_agent,
    _is_adk_runner,
    _get_adk_tools,
    _get_adk_model_name,
    _wrap_adk_agent_internals,
    _restore_originals,
    adk_agent,
)
from rastir.spans import SpanType, SpanStatus


# ========================================================================
# Fake ADK-like classes for testing
# ========================================================================

_BaseAgentClass = type("BaseAgent", (), {"__module__": "google.adk.agents.base_agent"})


def _make_adk_agent(name: str = "test_agent", model: str = "gemini-2.0-flash",
                     tools: list | None = None, sub_agents: list | None = None):
    """Create a mock ADK agent (subclass of BaseAgent)."""
    cls = type("LlmAgent", (_BaseAgentClass,), {"__module__": "google.adk.agents.llm_agent"})
    agent = cls()
    agent.name = name
    agent.model = model
    agent.tools = tools if tools is not None else []
    agent.sub_agents = sub_agents if sub_agents is not None else []
    return agent


def _make_adk_tool(name: str = "get_weather"):
    """Create a mock ADK tool (like FunctionTool)."""
    tool = MagicMock()
    tool.name = name
    tool._rastir_wrapped = False
    tool.run_async = MagicMock(return_value="tool result")
    return tool


_RunnerClass = type("Runner", (), {"__module__": "google.adk.runners"})


def _make_runner(agent=None):
    """Create a mock ADK Runner."""
    runner = _RunnerClass()
    runner.agent = agent
    return runner


# ========================================================================
# _is_adk_agent tests
# ========================================================================


class TestIsAdkAgent:
    def test_positive_detection_llmagent(self):
        agent = _make_adk_agent()
        assert _is_adk_agent(agent) is True

    def test_positive_detection_base_agent(self):
        agent = _BaseAgentClass()
        assert _is_adk_agent(agent) is True

    def test_negative_wrong_module(self):
        cls = type("BaseAgent", (), {"__module__": "other.module"})
        obj = cls()
        assert _is_adk_agent(obj) is False

    def test_negative_wrong_name(self):
        cls = type("NotAgent", (), {"__module__": "google.adk.agents"})
        obj = cls()
        assert _is_adk_agent(obj) is False

    def test_negative_plain_object(self):
        assert _is_adk_agent("hello") is False
        assert _is_adk_agent(42) is False
        assert _is_adk_agent(None) is False


# ========================================================================
# _is_adk_runner tests
# ========================================================================


class TestIsAdkRunner:
    def test_positive_detection(self):
        runner = _make_runner()
        assert _is_adk_runner(runner) is True

    def test_negative_wrong_name(self):
        cls = type("NotRunner", (), {"__module__": "google.adk.runners"})
        obj = cls()
        assert _is_adk_runner(obj) is False

    def test_negative_wrong_module(self):
        cls = type("Runner", (), {"__module__": "other.runners"})
        obj = cls()
        assert _is_adk_runner(obj) is False


# ========================================================================
# _get_adk_tools / _get_adk_model_name tests
# ========================================================================


class TestAdkHelpers:
    def test_get_tools_returns_list(self):
        tools = [_make_adk_tool("t1"), _make_adk_tool("t2")]
        agent = _make_adk_agent(tools=tools)
        assert len(_get_adk_tools(agent)) == 2

    def test_get_tools_returns_empty(self):
        agent = _make_adk_agent(tools=[])
        assert _get_adk_tools(agent) == []

    def test_get_model_name_string(self):
        agent = _make_adk_agent(model="gemini-2.0-flash")
        assert _get_adk_model_name(agent) == "gemini-2.0-flash"

    def test_get_model_name_object(self):
        model = MagicMock()
        model.model_name = "gemini-pro"
        agent = _make_adk_agent()
        agent.model = model
        assert _get_adk_model_name(agent) == "gemini-pro"

    def test_get_model_name_none(self):
        agent = _make_adk_agent()
        agent.model = None
        assert _get_adk_model_name(agent) == "unknown"


# ========================================================================
# _wrap_adk_agent_internals tests
# ========================================================================


class TestWrapAdkInternals:
    @patch("rastir.adk_support.wrap")
    def test_wraps_tools(self, mock_wrap):
        """Tools on agent get wrapped with include=['run_async', '__call__']."""
        mock_wrap.side_effect = lambda obj, **kw: MagicMock(_rastir_wrapped=True, _original=obj)

        tool = _make_adk_tool("search")
        agent = _make_adk_agent(tools=[tool])

        originals: dict = {}
        _wrap_adk_agent_internals(agent, originals)

        tool_wrap_calls = [c for c in mock_wrap.call_args_list if c[0][0] is tool]
        assert len(tool_wrap_calls) == 1
        kw = tool_wrap_calls[0][1]
        assert kw["span_type"] == "tool"
        assert "adk.tool.search" in kw["name"]

    @patch("rastir.adk_support.wrap")
    def test_stores_originals(self, mock_wrap):
        mock_wrap.side_effect = lambda obj, **kw: MagicMock(_rastir_wrapped=True)

        tool = _make_adk_tool()
        agent = _make_adk_agent(tools=[tool])

        originals: dict = {}
        _wrap_adk_agent_internals(agent, originals)

        agent_id = id(agent)
        assert agent_id in originals
        assert originals[agent_id]["_agent_ref"] is agent
        assert "tools" in originals[agent_id]

    @patch("rastir.adk_support.wrap")
    def test_wraps_sub_agents(self, mock_wrap):
        """Sub-agents' tools are also wrapped."""
        mock_wrap.side_effect = lambda obj, **kw: MagicMock(_rastir_wrapped=True)

        sub_tool = _make_adk_tool("sub_search")
        sub_agent = _make_adk_agent(name="sub", tools=[sub_tool])
        parent_agent = _make_adk_agent(name="parent", sub_agents=[sub_agent])

        originals: dict = {}
        _wrap_adk_agent_internals(parent_agent, originals)

        # Both parent and sub-agent should be in originals
        assert id(parent_agent) in originals
        assert id(sub_agent) in originals

    @patch("rastir.adk_support.wrap")
    def test_skips_already_wrapped(self, mock_wrap):
        """If agent was already wrapped (in originals), skip it."""
        tool = _make_adk_tool()
        agent = _make_adk_agent(tools=[tool])

        originals: dict = {id(agent): {"_agent_ref": agent}}
        _wrap_adk_agent_internals(agent, originals)

        mock_wrap.assert_not_called()


# ========================================================================
# _restore_originals tests
# ========================================================================


class TestRestoreOriginals:
    def test_restores_tools(self):
        original_tools = [_make_adk_tool("t1")]
        agent = _make_adk_agent(tools=[MagicMock()])  # wrapped tool

        originals = {
            id(agent): {
                "_agent_ref": agent,
                "tools": original_tools,
            }
        }

        _restore_originals(originals)
        assert agent.tools is original_tools

    def test_handles_missing_agent_ref(self):
        originals = {123: {"tools": []}}
        _restore_originals(originals)  # Should not raise


# ========================================================================
# adk_agent decorator tests
# ========================================================================


class TestAdkAgent:
    @patch("rastir.queue.enqueue_span")
    def test_bare_decorator(self, mock_enqueue):
        agent = _make_adk_agent()

        @adk_agent
        def run(a):
            return "done"

        result = run(agent)
        assert result == "done"

    @patch("rastir.queue.enqueue_span")
    def test_parameterized_decorator(self, mock_enqueue):
        agent = _make_adk_agent()

        @adk_agent(agent_name="my_agent")
        def run(a):
            return "ok"

        result = run(agent)
        assert result == "ok"

    @patch("rastir.queue.enqueue_span")
    def test_agent_span_emitted(self, mock_enqueue):
        agent = _make_adk_agent()

        @adk_agent(agent_name="test_adk")
        def run(a):
            return None

        run(agent)

        assert mock_enqueue.call_count == 1
        span = mock_enqueue.call_args[0][0]
        assert span.name == "test_adk"
        assert span.span_type == SpanType.AGENT
        assert span.status == SpanStatus.OK
        assert span.attributes["agent_name"] == "test_adk"

    @patch("rastir.queue.enqueue_span")
    def test_defaults_to_func_name(self, mock_enqueue):
        @adk_agent
        def my_adk_workflow():
            return None

        my_adk_workflow()
        span = mock_enqueue.call_args[0][0]
        assert span.name == "my_adk_workflow"

    @patch("rastir.queue.enqueue_span")
    def test_error_records_on_span(self, mock_enqueue):
        @adk_agent(agent_name="error_adk")
        def run():
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            run()

        span = mock_enqueue.call_args[0][0]
        assert span.status == SpanStatus.ERROR
        assert len(span.events) == 1
        assert "boom" in span.events[0]["attributes"]["exception.message"]

    @patch("rastir.queue.enqueue_span")
    def test_originals_restored_after_success(self, mock_enqueue):
        original_tools = [_make_adk_tool("search")]
        agent = _make_adk_agent(tools=list(original_tools))

        @adk_agent(agent_name="restore_test")
        def run(a):
            return "done"

        with patch("rastir.adk_support.wrap") as mw:
            mw.side_effect = lambda obj, **kw: MagicMock(_rastir_wrapped=True)
            run(agent)

        # After completion, tools should be restored
        assert len(agent.tools) == len(original_tools)

    @patch("rastir.queue.enqueue_span")
    def test_runner_detection(self, mock_enqueue):
        """Runner passed as arg triggers agent wrapping."""
        agent = _make_adk_agent()
        runner = _make_runner(agent=agent)

        @adk_agent(agent_name="runner_test")
        def run(r):
            return "done"

        with patch("rastir.adk_support.wrap") as mw:
            mw.side_effect = lambda obj, **kw: MagicMock(_rastir_wrapped=True)
            result = run(runner)

        assert result == "done"
        assert mock_enqueue.call_count == 1

    @patch("rastir.queue.enqueue_span")
    def test_non_adk_args_ignored(self, mock_enqueue):
        @adk_agent(agent_name="safe")
        def run(x, y):
            return x + y

        result = run(1, 2)
        assert result == 3


class TestAdkAgentAsync:
    @patch("rastir.queue.enqueue_span")
    def test_async_decorator(self, mock_enqueue):
        @adk_agent(agent_name="async_adk")
        async def run():
            return "async_done"

        result = asyncio.run(run())
        assert result == "async_done"

    @patch("rastir.queue.enqueue_span")
    def test_async_error(self, mock_enqueue):
        @adk_agent(agent_name="async_error")
        async def run():
            raise RuntimeError("async_fail")

        with pytest.raises(RuntimeError, match="async_fail"):
            asyncio.run(run())

        span = mock_enqueue.call_args[0][0]
        assert span.status == SpanStatus.ERROR
