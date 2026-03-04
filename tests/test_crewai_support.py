"""Unit tests for rastir.crewai_support — crew_kickoff decorator.

Tests cover:
  - _is_crew / _get_agents detection helpers
  - crew_kickoff decorator: bare and parameterized usage
  - LLM and tool wrapping on agents
  - Restore of originals after execution
  - Agent span emission (name, type, status)
  - Error handling (span records error, re-raises)
  - Async variant

Uses mock Crew/Agent classes that mimic CrewAI's class-name / module
structure so we can test without requiring crewai to be installed.
"""

from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rastir.crewai_support import (
    _is_crew,
    _get_agents,
    _wrap_crew_internals,
    _restore_originals,
    crew_kickoff,
)
from rastir.spans import SpanType, SpanStatus


# ========================================================================
# Fake CrewAI-like classes for testing
# ========================================================================

# Dynamically create a class named "Crew" with __module__ = "crewai.crew"
# so that _is_crew() detection works correctly.
def _crew_init(self, agents=None):
    self.agents = agents or []

_CrewClass = type("Crew", (), {"__module__": "crewai.crew", "__init__": _crew_init})


def _make_agent(role: str = "researcher", llm: object = None, tools: list | None = None):
    """Create a mock Agent with the expected attributes."""
    agent = MagicMock()
    agent.role = role
    if llm is not None:
        agent.llm = llm
    else:
        default_llm = MagicMock()
        default_llm._rastir_wrapped = False
        agent.llm = default_llm
    agent.tools = tools if tools is not None else []
    return agent


def _make_crew(agents: list | None = None):
    """Create a Crew whose type().name == 'Crew' and module contains 'crewai'."""
    return _CrewClass(agents)


def _make_tool(name: str = "search_tool"):
    """Create a mock tool with name and run()."""
    tool = MagicMock()
    tool.name = name
    tool._rastir_wrapped = False
    tool._rastir_tool_patched = False
    tool.run = MagicMock(return_value="tool result")
    return tool


# ========================================================================
# _is_crew tests
# ========================================================================


class TestIsCrew:
    def test_positive_detection(self):
        crew = _make_crew()
        assert _is_crew(crew) is True
        assert type(crew).__name__ == "Crew"

    def test_negative_wrong_name(self):
        obj = MagicMock()
        obj.__class__ = type("NotCrew", (), {"__module__": "crewai.crew"})
        assert _is_crew(obj) is False

    def test_negative_wrong_module(self):
        obj = MagicMock()
        obj.__class__ = type("Crew", (), {"__module__": "some_other.module"})
        assert _is_crew(obj) is False

    def test_negative_plain_object(self):
        assert _is_crew("hello") is False
        assert _is_crew(42) is False
        assert _is_crew(None) is False


# ========================================================================
# _get_agents tests
# ========================================================================


class TestGetAgents:
    def test_returns_agents_list(self):
        agents = [_make_agent("a1"), _make_agent("a2")]
        crew = _make_crew(agents)
        result = _get_agents(crew)
        assert len(result) == 2

    def test_returns_empty_when_no_agents(self):
        crew = _make_crew([])
        assert _get_agents(crew) == []

    def test_handles_missing_agents_attr(self):
        obj = MagicMock(spec=[])  # no attributes at all
        assert _get_agents(obj) == []


# ========================================================================
# _wrap_crew_internals tests
# ========================================================================


class TestWrapCrewInternals:
    @patch("rastir.crewai_support.wrap")
    def test_wraps_agent_llm(self, mock_wrap):
        """LLM on each agent gets wrapped with include=['call']."""
        mock_wrap.side_effect = lambda obj, **kw: MagicMock(_rastir_wrapped=True, _original=obj)

        llm = MagicMock()
        llm._rastir_wrapped = False
        agent = _make_agent("researcher", llm=llm)
        crew = _make_crew([agent])

        originals: dict = {}
        _wrap_crew_internals(crew, originals)

        # wrap() was called for the LLM
        llm_wrap_calls = [c for c in mock_wrap.call_args_list if c[0][0] is llm]
        assert len(llm_wrap_calls) == 1
        call_kwargs = llm_wrap_calls[0][1]
        assert call_kwargs["include"] == ["call"]
        assert call_kwargs["span_type"] == "llm"
        assert "crewai.researcher.llm" in call_kwargs["name"]

    @patch("rastir.crewai_support.wrap")
    def test_wraps_agent_tools(self, mock_wrap):
        """Each tool on agents gets its .run patched in-place."""
        mock_wrap.side_effect = lambda obj, **kw: MagicMock(_rastir_wrapped=True)

        tool = _make_tool("web_search")
        original_run = tool.run
        agent = _make_agent("researcher", tools=[tool])
        crew = _make_crew([agent])

        originals: dict = {}
        _wrap_crew_internals(crew, originals)

        # .run is now patched in the tool's __dict__
        assert "run" in tool.__dict__, "tool.run should be patched in instance __dict__"
        assert tool.__dict__.get("_rastir_tool_patched") is True
        # Original run is saved in originals for restoration
        patched = originals[id(agent)].get("_patched_tool_runs", [])
        assert len(patched) == 1
        assert patched[0][0] is tool
        assert patched[0][1] is original_run

    @patch("rastir.crewai_support.wrap")
    def test_stores_originals(self, mock_wrap):
        """Originals dict stores agent ref, original LLM and patched tool runs."""
        mock_wrap.side_effect = lambda obj, **kw: MagicMock(_rastir_wrapped=True)

        llm = MagicMock()
        llm._rastir_wrapped = False
        tool = _make_tool()
        agent = _make_agent("dev", llm=llm, tools=[tool])
        crew = _make_crew([agent])

        originals: dict = {}
        _wrap_crew_internals(crew, originals)

        agent_id = id(agent)
        assert agent_id in originals
        assert originals[agent_id]["_agent_ref"] is agent
        assert originals[agent_id]["llm"] is llm
        # Tool runs are patched in-place, stored as (tool, original_run) pairs
        patched = originals[agent_id]["_patched_tool_runs"]
        assert len(patched) == 1
        assert patched[0][0] is tool  # tool ref

    @patch("rastir.crewai_support.wrap")
    def test_skips_already_wrapped_llm(self, mock_wrap):
        """If LLM is already wrapped, don't re-wrap it."""
        llm = MagicMock()
        llm._rastir_wrapped = True  # already wrapped
        agent = _make_agent("researcher")
        agent.llm = llm  # set after make_agent to keep _rastir_wrapped
        crew = _make_crew([agent])

        originals: dict = {}
        _wrap_crew_internals(crew, originals)

        # wrap() should NOT be called for the LLM
        llm_wrap_calls = [c for c in mock_wrap.call_args_list if c[0][0] is llm]
        assert len(llm_wrap_calls) == 0

    @patch("rastir.crewai_support.wrap")
    def test_skips_already_patched_tool(self, mock_wrap):
        """If a tool is already patched, don't re-patch it."""
        mock_wrap.side_effect = lambda obj, **kw: MagicMock(_rastir_wrapped=True)

        tool = _make_tool("search")
        tool._rastir_tool_patched = True
        agent = _make_agent("dev", tools=[tool])
        crew = _make_crew([agent])

        originals: dict = {}
        _wrap_crew_internals(crew, originals)

        # Should have 0 patched tool runs since already patched
        patched = originals[id(agent)].get("_patched_tool_runs", [])
        assert len(patched) == 0


# ========================================================================
# _restore_originals tests
# ========================================================================


class TestRestoreOriginals:
    def test_restores_llm_and_tools(self):
        """After restore, agent has its original LLM and tool .run is unpatched."""
        original_llm = MagicMock()
        tool = _make_tool()
        original_run = tool.run
        # Simulate patching
        tool.__dict__["run"] = MagicMock()
        tool.__dict__["_rastir_tool_patched"] = True
        agent = _make_agent("dev", llm=MagicMock(), tools=[tool])

        originals = {
            id(agent): {
                "_agent_ref": agent,
                "llm": original_llm,
                "_patched_tool_runs": [(tool, original_run)],
            }
        }

        _restore_originals(originals)

        assert agent.llm is original_llm
        # Tool's __dict__ entries should be removed (restoring class method)
        assert "run" not in tool.__dict__
        assert "_rastir_tool_patched" not in tool.__dict__

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
            }
        }

        _restore_originals(originals)
        assert agent.llm is original_llm


# ========================================================================
# crew_kickoff decorator tests
# ========================================================================


class TestCrewKickoff:
    @patch("rastir.crewai_support.enqueue_span", create=True)
    @patch("rastir.queue.enqueue_span")
    def test_bare_decorator(self, mock_enqueue_queue, mock_enqueue_local):
        """@crew_kickoff without parens works."""
        crew = _make_crew([_make_agent()])

        @crew_kickoff
        def run(c):
            return "done"

        with patch("rastir.crewai_support.wrap") as mw:
            mw.side_effect = lambda obj, **kw: obj
            result = run(crew)

        assert result == "done"

    @patch("rastir.queue.enqueue_span")
    def test_parameterized_decorator(self, mock_enqueue):
        """@crew_kickoff(agent_name=...) works."""
        crew = _make_crew([_make_agent()])

        @crew_kickoff(agent_name="my_crew")
        def run(c):
            return "ok"

        with patch("rastir.crewai_support.wrap") as mw:
            mw.side_effect = lambda obj, **kw: obj
            result = run(crew)

        assert result == "ok"

    @patch("rastir.queue.enqueue_span")
    def test_agent_span_emitted(self, mock_enqueue):
        """Decorator creates an AGENT span with correct name."""
        crew = _make_crew([])

        @crew_kickoff(agent_name="test_crew")
        def run(c):
            return None

        run(crew)

        assert mock_enqueue.call_count == 1
        span = mock_enqueue.call_args[0][0]
        assert span.name == "test_crew"
        assert span.span_type == SpanType.AGENT
        assert span.status == SpanStatus.OK
        assert span.attributes["agent_name"] == "test_crew"

    @patch("rastir.queue.enqueue_span")
    def test_agent_span_name_defaults_to_func(self, mock_enqueue):
        """Without agent_name, span name defaults to function name."""
        crew = _make_crew([])

        @crew_kickoff
        def my_workflow(c):
            return None

        my_workflow(crew)

        span = mock_enqueue.call_args[0][0]
        assert span.name == "my_workflow"

    @patch("rastir.queue.enqueue_span")
    def test_error_records_on_span(self, mock_enqueue):
        """On exception, span records error and re-raises."""
        crew = _make_crew([])

        @crew_kickoff(agent_name="error_crew")
        def run(c):
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            run(crew)

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
        agent = _make_agent("dev", llm=original_llm, tools=[original_tool])
        crew = _make_crew([agent])

        @crew_kickoff(agent_name="restore_test")
        def run(c):
            # During execution, agent.llm should be wrapped
            return "done"

        with patch("rastir.crewai_support.wrap") as mw:
            mw.side_effect = lambda obj, **kw: MagicMock(_rastir_wrapped=True)
            run(crew)

        # After completion, originals are restored
        assert agent.llm is original_llm
        assert agent.tools == [original_tool]

    @patch("rastir.queue.enqueue_span")
    def test_originals_restored_after_error(self, mock_enqueue):
        """After error, agent LLM and tools are still restored."""
        original_llm = MagicMock()
        original_llm._rastir_wrapped = False
        agent = _make_agent("dev", llm=original_llm)
        crew = _make_crew([agent])

        @crew_kickoff(agent_name="error_restore")
        def run(c):
            raise RuntimeError("fail")

        with patch("rastir.crewai_support.wrap") as mw:
            mw.side_effect = lambda obj, **kw: MagicMock(_rastir_wrapped=True)
            with pytest.raises(RuntimeError):
                run(crew)

        assert agent.llm is original_llm

    @patch("rastir.queue.enqueue_span")
    def test_crew_in_kwargs(self, mock_enqueue):
        """Crew passed as kwarg is also detected and wrapped."""
        crew = _make_crew([_make_agent()])

        @crew_kickoff(agent_name="kw_crew")
        def run(*, my_crew):
            return "ok"

        with patch("rastir.crewai_support.wrap") as mw:
            mw.side_effect = lambda obj, **kw: obj
            result = run(my_crew=crew)

        assert result == "ok"
        # Span was enqueued
        assert mock_enqueue.call_count == 1

    @patch("rastir.queue.enqueue_span")
    def test_non_crew_args_ignored(self, mock_enqueue):
        """Non-Crew args don't cause errors."""

        @crew_kickoff(agent_name="safe")
        def run(x, y):
            return x + y

        result = run(1, 2)
        assert result == 3


class TestCrewKickoffAsync:
    @patch("rastir.queue.enqueue_span")
    def test_async_decorator(self, mock_enqueue):
        """@crew_kickoff works with async functions."""
        crew = _make_crew([])

        @crew_kickoff(agent_name="async_crew")
        async def run(c):
            return "async_done"

        result = asyncio.run(run(crew))
        assert result == "async_done"

        span = mock_enqueue.call_args[0][0]
        assert span.name == "async_crew"
        assert span.span_type == SpanType.AGENT
        assert span.status == SpanStatus.OK

    @patch("rastir.queue.enqueue_span")
    def test_async_error_handling(self, mock_enqueue):
        """Async path records errors on span."""
        crew = _make_crew([])

        @crew_kickoff(agent_name="async_err")
        async def run(c):
            raise TypeError("async boom")

        with pytest.raises(TypeError, match="async boom"):
            asyncio.run(run(crew))

        span = mock_enqueue.call_args[0][0]
        assert span.status == SpanStatus.ERROR


# ========================================================================
# Integration-style: wrapping flows through the decorator
# ========================================================================


class TestCrewKickoffWrapping:
    """End-to-end: decorator wraps LLMs and tools on agents."""

    @patch("rastir.queue.enqueue_span")
    @patch("rastir.crewai_support.wrap")
    def test_llms_wrapped_during_execution(self, mock_wrap, mock_enqueue):
        """During execution, agent LLMs are wrapped."""
        sentinel = MagicMock(_rastir_wrapped=True, _is_wrapped_llm=True)
        mock_wrap.side_effect = lambda obj, **kw: sentinel

        llm = MagicMock()
        llm._rastir_wrapped = False
        agent = _make_agent("analyst", llm=llm, tools=[])
        crew = _make_crew([agent])

        wrapped_during: list = []

        @crew_kickoff(agent_name="int_test")
        def run(c):
            # agent is the same object since _FakeCrew stores list ref
            wrapped_during.append(agent.llm)
            return "ok"

        run(crew)

        # During execution, llm was replaced with wrapped version
        assert len(wrapped_during) == 1
        assert wrapped_during[0]._is_wrapped_llm is True

        # After execution, original is restored
        assert agent.llm is llm

    @patch("rastir.queue.enqueue_span")
    @patch("rastir.crewai_support.wrap")
    def test_tools_wrapped_during_execution(self, mock_wrap, mock_enqueue):
        """During execution, agent tools have .run patched in-place."""
        mock_wrap.side_effect = lambda obj, **kw: MagicMock(_rastir_wrapped=True)

        tool = _make_tool("scraper")
        original_run = tool.run
        agent = _make_agent("dev", tools=[tool])
        crew = _make_crew([agent])

        patched_during: list = []

        @crew_kickoff(agent_name="tool_test")
        def run(c):
            # During execution, tool should have .run patched via __dict__
            patched_during.append("run" in tool.__dict__)
            patched_during.append(tool.__dict__.get("_rastir_tool_patched", False))
            return "ok"

        run(crew)

        assert patched_during[0] is True, "tool.run should be patched in __dict__"
        assert patched_during[1] is True, "tool._rastir_tool_patched should be set"

        # After execution, original is restored
        assert "run" not in tool.__dict__, "tool.run should be unpatched after execution"


