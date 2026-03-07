"""Unit tests for rastir.strands_support — strands_agent decorator.

Tests cover:
  - _is_strands_agent detection helper
  - _get_model_name extraction
  - strands_agent decorator: bare and parameterized usage
  - Model and tool wrapping on agents
  - Restore of originals after execution
  - Agent span emission (name, type, status)
  - Error handling (span records error, re-raises)
  - Async variant

Uses mock Strands-like classes so we can test without strands-agents installed.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from rastir.strands_support import (
    _is_strands_agent,
    _get_model_name,
    _wrap_strands_internals,
    _restore_originals,
    strands_agent,
)
from rastir.spans import SpanType, SpanStatus


# ========================================================================
# Fake Strands-like classes for testing
# ========================================================================

_AgentBaseClass = type("AgentBase", (), {"__module__": "strands.agent.agent"})
_AgentClass = type("Agent", (_AgentBaseClass,), {"__module__": "strands.agent.agent"})


def _make_strands_agent(name: str = "test_agent", model=None, tools: dict | None = None):
    """Create a mock Strands agent."""
    agent = _AgentClass()
    agent.name = name

    if model is None:
        model = MagicMock()
        model._rastir_wrapped = False
        model.model_id = "us.anthropic.claude-sonnet-4-20250514-v1:0"
    agent.model = model

    # Strands stores tools in agent.tool_registry.registry (dict)
    registry = MagicMock()
    if tools is not None:
        registry.registry = dict(tools)
    else:
        registry.registry = {}
    agent.tool_registry = registry

    return agent


def _make_strands_tool(name: str = "get_weather"):
    """Create a mock Strands AgentTool."""
    tool = MagicMock()
    tool.tool_name = name
    tool._rastir_wrapped = False
    tool._rastir_tool_patched = False
    tool.stream = MagicMock(return_value=iter(["result"]))
    return tool


# ========================================================================
# _is_strands_agent tests
# ========================================================================


class TestIsStrandsAgent:
    def test_positive_detection(self):
        agent = _make_strands_agent()
        assert _is_strands_agent(agent) is True

    def test_positive_detection_base(self):
        agent = _AgentBaseClass()
        assert _is_strands_agent(agent) is True

    def test_negative_wrong_module(self):
        cls = type("Agent", (), {"__module__": "other.module"})
        obj = cls()
        assert _is_strands_agent(obj) is False

    def test_negative_wrong_name(self):
        cls = type("NotAgent", (), {"__module__": "strands.agent.agent"})
        obj = cls()
        assert _is_strands_agent(obj) is False

    def test_negative_plain_object(self):
        assert _is_strands_agent("hello") is False
        assert _is_strands_agent(42) is False
        assert _is_strands_agent(None) is False


# ========================================================================
# _get_model_name tests
# ========================================================================


class TestGetModelName:
    def test_model_id(self):
        agent = _make_strands_agent()
        result = _get_model_name(agent)
        assert result == "us.anthropic.claude-sonnet-4-20250514-v1:0"

    def test_string_model(self):
        agent = _make_strands_agent()
        agent.model = "claude-sonnet"  # string model
        assert _get_model_name(agent) == "claude-sonnet"

    def test_no_model(self):
        agent = _make_strands_agent()
        agent.model = None
        assert _get_model_name(agent) == "unknown"


# ========================================================================
# _wrap_strands_internals tests
# ========================================================================


class TestWrapStrandsInternals:
    @patch("rastir.strands_support.wrap")
    def test_wraps_model(self, mock_wrap):
        """Model on agent gets wrapped with include=['stream']."""
        mock_wrap.side_effect = lambda obj, **kw: MagicMock(_rastir_wrapped=True, _original=obj)

        model = MagicMock()
        model._rastir_wrapped = False
        model.model_id = "claude-sonnet"
        agent = _make_strands_agent(model=model)

        originals: dict = {}
        _wrap_strands_internals(agent, originals)

        model_calls = [c for c in mock_wrap.call_args_list if c[0][0] is model]
        assert len(model_calls) == 1
        kw = model_calls[0][1]
        assert kw["include"] == ["stream"]
        assert kw["span_type"] == "llm"

    @patch("rastir.strands_support.wrap")
    def test_wraps_tools(self, mock_wrap):
        """Each tool in registry gets its .stream patched in-place."""
        mock_wrap.side_effect = lambda obj, **kw: MagicMock(_rastir_wrapped=True)

        tool = _make_strands_tool("search")
        original_stream = tool.stream
        agent = _make_strands_agent(tools={"search": tool})

        originals: dict = {}
        _wrap_strands_internals(agent, originals)

        # .stream is patched in the tool's __dict__
        assert "stream" in tool.__dict__
        assert tool.__dict__.get("_rastir_tool_patched") is True
        # Original stream is saved for restoration
        patched = originals[id(agent)].get("_patched_tools", [])
        assert len(patched) == 1
        assert patched[0][0] is tool
        assert patched[0][2] is original_stream

    @patch("rastir.strands_support.wrap")
    def test_stores_originals(self, mock_wrap):
        mock_wrap.side_effect = lambda obj, **kw: MagicMock(_rastir_wrapped=True)

        model = MagicMock()
        model._rastir_wrapped = False
        model.model_id = "test"
        agent = _make_strands_agent(model=model)

        originals: dict = {}
        _wrap_strands_internals(agent, originals)

        agent_id = id(agent)
        assert agent_id in originals
        assert originals[agent_id]["_agent_ref"] is agent
        assert "model" in originals[agent_id]

    @patch("rastir.strands_support.wrap")
    def test_skips_already_wrapped_model(self, mock_wrap):
        model = MagicMock()
        model._rastir_wrapped = True
        agent = _make_strands_agent(model=model)

        originals: dict = {}
        _wrap_strands_internals(agent, originals)

        model_calls = [c for c in mock_wrap.call_args_list if c[0][0] is model]
        assert len(model_calls) == 0

    @patch("rastir.strands_support.wrap")
    def test_skips_already_patched_tool(self, mock_wrap):
        mock_wrap.side_effect = lambda obj, **kw: MagicMock(_rastir_wrapped=True)

        tool = _make_strands_tool("search")
        tool._rastir_tool_patched = True
        agent = _make_strands_agent(tools={"search": tool})

        originals: dict = {}
        _wrap_strands_internals(agent, originals)

        patched = originals[id(agent)].get("_patched_tools", [])
        assert len(patched) == 0

    @patch("rastir.strands_support.wrap")
    def test_skips_duplicate_wrap(self, mock_wrap):
        """If agent already in originals, skip it."""
        agent = _make_strands_agent()
        originals: dict = {id(agent): {"_agent_ref": agent}}
        _wrap_strands_internals(agent, originals)
        mock_wrap.assert_not_called()


# ========================================================================
# _restore_originals tests
# ========================================================================


class TestRestoreOriginals:
    def test_restores_model_and_tools(self):
        original_model = MagicMock()
        tool = _make_strands_tool()
        original_stream = tool.stream
        tool.__dict__["stream"] = MagicMock()
        tool.__dict__["_rastir_tool_patched"] = True
        agent = _make_strands_agent(model=MagicMock())

        originals = {
            id(agent): {
                "_agent_ref": agent,
                "model": original_model,
                "_patched_tools": [(tool, "stream", original_stream)],
            }
        }

        _restore_originals(originals)

        assert agent.model is original_model
        assert "stream" not in tool.__dict__
        assert "_rastir_tool_patched" not in tool.__dict__

    def test_handles_missing_agent_ref(self):
        originals = {123: {"model": MagicMock()}}
        _restore_originals(originals)  # Should not raise


# ========================================================================
# strands_agent decorator tests
# ========================================================================


class TestStrandsAgent:
    @patch("rastir.queue.enqueue_span")
    def test_bare_decorator(self, mock_enqueue):
        agent = _make_strands_agent()

        @strands_agent
        def run(a):
            return "done"

        with patch("rastir.strands_support.wrap") as mw:
            mw.side_effect = lambda obj, **kw: obj
            result = run(agent)

        assert result == "done"

    @patch("rastir.queue.enqueue_span")
    def test_parameterized_decorator(self, mock_enqueue):
        agent = _make_strands_agent()

        @strands_agent(agent_name="my_agent")
        def run(a):
            return "ok"

        with patch("rastir.strands_support.wrap") as mw:
            mw.side_effect = lambda obj, **kw: obj
            result = run(agent)

        assert result == "ok"

    @patch("rastir.queue.enqueue_span")
    def test_agent_span_emitted(self, mock_enqueue):
        agent = _make_strands_agent()

        @strands_agent(agent_name="test_strands")
        def run(a):
            return None

        with patch("rastir.strands_support.wrap") as mw:
            mw.side_effect = lambda obj, **kw: obj
            run(agent)

        assert mock_enqueue.call_count == 1
        span = mock_enqueue.call_args[0][0]
        assert span.name == "test_strands"
        assert span.span_type == SpanType.AGENT
        assert span.status == SpanStatus.OK
        assert span.attributes["agent_name"] == "test_strands"

    @patch("rastir.queue.enqueue_span")
    def test_defaults_to_func_name(self, mock_enqueue):
        @strands_agent
        def my_strands_workflow():
            return None

        my_strands_workflow()
        span = mock_enqueue.call_args[0][0]
        assert span.name == "my_strands_workflow"

    @patch("rastir.queue.enqueue_span")
    def test_error_records_on_span(self, mock_enqueue):
        @strands_agent(agent_name="error_strands")
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
        original_model = MagicMock()
        original_model._rastir_wrapped = False
        original_model.model_id = "test"
        agent = _make_strands_agent(model=original_model)

        @strands_agent(agent_name="restore_test")
        def run(a):
            return "done"

        with patch("rastir.strands_support.wrap") as mw:
            mw.side_effect = lambda obj, **kw: MagicMock(_rastir_wrapped=True)
            run(agent)

        assert agent.model is original_model

    @patch("rastir.queue.enqueue_span")
    def test_originals_restored_after_error(self, mock_enqueue):
        original_model = MagicMock()
        original_model._rastir_wrapped = False
        original_model.model_id = "test"
        agent = _make_strands_agent(model=original_model)

        @strands_agent(agent_name="error_restore")
        def run(a):
            raise RuntimeError("fail")

        with patch("rastir.strands_support.wrap") as mw:
            mw.side_effect = lambda obj, **kw: MagicMock(_rastir_wrapped=True)
            with pytest.raises(RuntimeError):
                run(agent)

        assert agent.model is original_model

    @patch("rastir.queue.enqueue_span")
    def test_non_strands_args_ignored(self, mock_enqueue):
        @strands_agent(agent_name="safe")
        def run(x, y):
            return x + y

        result = run(1, 2)
        assert result == 3


class TestStrandsAgentAsync:
    @patch("rastir.queue.enqueue_span")
    def test_async_decorator(self, mock_enqueue):
        @strands_agent(agent_name="async_strands")
        async def run():
            return "async_done"

        result = asyncio.run(run())
        assert result == "async_done"

    @patch("rastir.queue.enqueue_span")
    def test_async_error(self, mock_enqueue):
        @strands_agent(agent_name="async_error")
        async def run():
            raise RuntimeError("async_fail")

        with pytest.raises(RuntimeError, match="async_fail"):
            asyncio.run(run())

        span = mock_enqueue.call_args[0][0]
        assert span.status == SpanStatus.ERROR
