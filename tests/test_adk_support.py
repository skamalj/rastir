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
    _install_adk_callbacks,
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
# _install_adk_callbacks tests
# ========================================================================


class TestInstallAdkCallbacks:
    def test_installs_model_callbacks(self):
        """Model callbacks get installed on the agent."""
        agent = _make_adk_agent()
        agent.before_model_callback = None
        agent.after_model_callback = None
        agent.on_model_error_callback = None
        agent.before_tool_callback = None
        agent.after_tool_callback = None
        agent.on_tool_error_callback = None

        originals: dict = {}
        _install_adk_callbacks(agent, originals)

        assert agent.before_model_callback is not None
        assert agent.after_model_callback is not None
        assert agent.on_model_error_callback is not None

    def test_installs_tool_callbacks(self):
        """Tool callbacks get installed on the agent."""
        agent = _make_adk_agent()
        agent.before_model_callback = None
        agent.after_model_callback = None
        agent.on_model_error_callback = None
        agent.before_tool_callback = None
        agent.after_tool_callback = None
        agent.on_tool_error_callback = None

        originals: dict = {}
        _install_adk_callbacks(agent, originals)

        assert agent.before_tool_callback is not None
        assert agent.after_tool_callback is not None
        assert agent.on_tool_error_callback is not None

    def test_stores_originals(self):
        """Original callbacks are saved in originals dict."""
        agent = _make_adk_agent()
        original_before = MagicMock()
        agent.before_model_callback = original_before
        agent.after_model_callback = None
        agent.on_model_error_callback = None
        agent.before_tool_callback = None
        agent.after_tool_callback = None
        agent.on_tool_error_callback = None

        originals: dict = {}
        _install_adk_callbacks(agent, originals)

        agent_id = id(agent)
        assert agent_id in originals
        assert originals[agent_id]["_agent_ref"] is agent
        assert originals[agent_id]["before_model_callback"] is original_before

    def test_prepends_to_existing_callbacks(self):
        """If agent already has callbacks, rastir's are prepended."""
        agent = _make_adk_agent()
        existing_cb = MagicMock()
        agent.before_model_callback = existing_cb
        agent.after_model_callback = None
        agent.on_model_error_callback = None
        agent.before_tool_callback = None
        agent.after_tool_callback = None
        agent.on_tool_error_callback = None

        originals: dict = {}
        _install_adk_callbacks(agent, originals)

        # Should be a list with rastir's cb first, then the existing one
        assert isinstance(agent.before_model_callback, list)
        assert len(agent.before_model_callback) == 2
        assert agent.before_model_callback[1] is existing_cb

    def test_recurses_sub_agents(self):
        """Sub-agents' callbacks are also installed."""
        sub_agent = _make_adk_agent(name="sub")
        sub_agent.before_model_callback = None
        sub_agent.after_model_callback = None
        sub_agent.on_model_error_callback = None
        sub_agent.before_tool_callback = None
        sub_agent.after_tool_callback = None
        sub_agent.on_tool_error_callback = None

        parent_agent = _make_adk_agent(name="parent", sub_agents=[sub_agent])
        parent_agent.before_model_callback = None
        parent_agent.after_model_callback = None
        parent_agent.on_model_error_callback = None
        parent_agent.before_tool_callback = None
        parent_agent.after_tool_callback = None
        parent_agent.on_tool_error_callback = None

        originals: dict = {}
        _install_adk_callbacks(parent_agent, originals)

        # Both parent and sub-agent should be in originals
        assert id(parent_agent) in originals
        assert id(sub_agent) in originals

    def test_skips_already_installed(self):
        """If agent was already processed (in originals), skip it."""
        agent = _make_adk_agent()
        agent.before_model_callback = None
        agent.after_model_callback = None
        agent.on_model_error_callback = None
        agent.before_tool_callback = None
        agent.after_tool_callback = None
        agent.on_tool_error_callback = None

        originals: dict = {id(agent): {"_agent_ref": agent}}
        _install_adk_callbacks(agent, originals)

        # Callbacks should not have changed
        assert agent.before_model_callback is None


# ========================================================================
# _restore_originals tests
# ========================================================================


class TestRestoreOriginals:
    def test_restores_callbacks(self):
        original_before = MagicMock()
        agent = _make_adk_agent()
        agent.before_model_callback = [MagicMock(), original_before]

        originals = {
            id(agent): {
                "_agent_ref": agent,
                "before_model_callback": original_before,
                "after_model_callback": None,
                "on_model_error_callback": None,
                "before_tool_callback": None,
                "after_tool_callback": None,
                "on_tool_error_callback": None,
            }
        }

        _restore_originals(originals)
        assert agent.before_model_callback is original_before

    def test_handles_missing_agent_ref(self):
        originals = {123: {"before_model_callback": None}}
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
        agent = _make_adk_agent()
        agent.before_model_callback = None
        agent.after_model_callback = None
        agent.on_model_error_callback = None
        agent.before_tool_callback = None
        agent.after_tool_callback = None
        agent.on_tool_error_callback = None

        @adk_agent(agent_name="restore_test")
        def run(a):
            return "done"

        run(agent)

        # After completion, callbacks should be restored to None
        assert agent.before_model_callback is None
        assert agent.after_model_callback is None

    @patch("rastir.queue.enqueue_span")
    def test_runner_detection(self, mock_enqueue):
        """Runner passed as arg triggers callback installation."""
        agent = _make_adk_agent()
        agent.before_model_callback = None
        agent.after_model_callback = None
        agent.on_model_error_callback = None
        agent.before_tool_callback = None
        agent.after_tool_callback = None
        agent.on_tool_error_callback = None
        runner = _make_runner(agent=agent)

        @adk_agent(agent_name="runner_test")
        def run(r):
            return "done"

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
