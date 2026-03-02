"""Unit tests for rastir.crewai_support — crew_kickoff decorator.

Tests cover:
  - _is_crew / _get_agents detection helpers
  - crew_kickoff decorator: bare and parameterized usage
  - LLM and tool wrapping on agents
  - MCP tool bridge (_build_crewai_tools_from_mcp)
  - Restore of originals after execution
  - Agent span emission (name, type, status)
  - Error handling (span records error, re-raises)
  - Async variant
  - mcp= parameter (single session, list, dict by role)

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
    _mcp_schema_to_python_type,
    _wrap_crew_internals,
    _restore_originals,
    _resolve_mcp_tools,
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
# _mcp_schema_to_python_type tests
# ========================================================================


class TestMcpSchemaType:
    def test_string(self):
        assert _mcp_schema_to_python_type({"type": "string"}) is str

    def test_integer(self):
        assert _mcp_schema_to_python_type({"type": "integer"}) is int

    def test_number(self):
        assert _mcp_schema_to_python_type({"type": "number"}) is float

    def test_boolean(self):
        assert _mcp_schema_to_python_type({"type": "boolean"}) is bool

    def test_array(self):
        assert _mcp_schema_to_python_type({"type": "array"}) is list

    def test_object(self):
        assert _mcp_schema_to_python_type({"type": "object"}) is dict

    def test_unknown_defaults_to_str(self):
        assert _mcp_schema_to_python_type({"type": "unknown"}) is str

    def test_missing_type_defaults_to_str(self):
        assert _mcp_schema_to_python_type({}) is str


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
        _wrap_crew_internals(crew, None, originals)

        # wrap() was called for the LLM
        llm_wrap_calls = [c for c in mock_wrap.call_args_list if c[0][0] is llm]
        assert len(llm_wrap_calls) == 1
        call_kwargs = llm_wrap_calls[0][1]
        assert call_kwargs["include"] == ["call"]
        assert call_kwargs["span_type"] == "llm"
        assert "crewai.researcher.llm" in call_kwargs["name"]

    @patch("rastir.crewai_support.wrap")
    def test_wraps_agent_tools(self, mock_wrap):
        """Each tool on agents gets wrapped with include=['run']."""
        mock_wrap.side_effect = lambda obj, **kw: MagicMock(
            _rastir_wrapped=True, name=getattr(obj, "name", "t")
        )

        tool = _make_tool("web_search")
        agent = _make_agent("researcher", tools=[tool])
        crew = _make_crew([agent])

        originals: dict = {}
        _wrap_crew_internals(crew, None, originals)

        tool_wrap_calls = [c for c in mock_wrap.call_args_list if c[0][0] is tool]
        assert len(tool_wrap_calls) == 1
        call_kwargs = tool_wrap_calls[0][1]
        assert call_kwargs["include"] == ["run"]
        assert call_kwargs["span_type"] == "tool"

    @patch("rastir.crewai_support.wrap")
    def test_stores_originals(self, mock_wrap):
        """Originals dict stores agent ref, original LLM and tools."""
        mock_wrap.side_effect = lambda obj, **kw: MagicMock(_rastir_wrapped=True)

        llm = MagicMock()
        llm._rastir_wrapped = False
        tool = _make_tool()
        agent = _make_agent("dev", llm=llm, tools=[tool])
        crew = _make_crew([agent])

        originals: dict = {}
        _wrap_crew_internals(crew, None, originals)

        agent_id = id(agent)
        assert agent_id in originals
        assert originals[agent_id]["_agent_ref"] is agent
        assert originals[agent_id]["llm"] is llm
        assert originals[agent_id]["tools"] == [tool]

    @patch("rastir.crewai_support.wrap")
    def test_skips_already_wrapped_llm(self, mock_wrap):
        """If LLM is already wrapped, don't re-wrap it."""
        llm = MagicMock()
        llm._rastir_wrapped = True  # already wrapped
        agent = _make_agent("researcher")
        agent.llm = llm  # set after make_agent to keep _rastir_wrapped
        crew = _make_crew([agent])

        originals: dict = {}
        _wrap_crew_internals(crew, None, originals)

        # wrap() should NOT be called for the LLM
        llm_wrap_calls = [c for c in mock_wrap.call_args_list if c[0][0] is llm]
        assert len(llm_wrap_calls) == 0

    @patch("rastir.crewai_support.wrap")
    def test_skips_already_wrapped_tool(self, mock_wrap):
        """If a tool is already wrapped, keep it as-is."""
        mock_wrap.side_effect = lambda obj, **kw: MagicMock(_rastir_wrapped=True)

        tool = _make_tool("search")
        tool._rastir_wrapped = True
        agent = _make_agent("dev", tools=[tool])
        crew = _make_crew([agent])

        originals: dict = {}
        _wrap_crew_internals(crew, None, originals)

        # wrap() should NOT be called for the tool
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
        agent = _make_agent("dev", llm=MagicMock(), tools=[MagicMock()])

        originals = {
            id(agent): {
                "_agent_ref": agent,
                "llm": original_llm,
                "tools": original_tools,
            }
        }

        _restore_originals(originals)

        assert agent.llm is original_llm
        assert agent.tools is original_tools

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
# _resolve_mcp_tools tests
# ========================================================================


class TestResolveMcpTools:
    def test_none_mcp_returns_empty(self):
        agent = _make_agent()
        assert _resolve_mcp_tools(agent, None, {}) == []

    @patch("rastir.crewai_support._build_crewai_tools_from_mcp")
    def test_single_session(self, mock_build):
        """Single session → tools are built for all agents."""
        mock_build.return_value = ["tool1", "tool2"]
        session = MagicMock()
        session.list_tools = AsyncMock(return_value=MagicMock(tools=["raw1"]))

        agent = _make_agent()
        cache: dict = {}
        result = _resolve_mcp_tools(agent, session, cache)

        assert result == ["tool1", "tool2"]
        mock_build.assert_called_once()

    @patch("rastir.crewai_support._build_crewai_tools_from_mcp")
    def test_list_of_sessions(self, mock_build):
        """List of sessions → tools from all sessions combined."""
        s1 = MagicMock()
        s1.list_tools = AsyncMock(return_value=MagicMock(tools=["a"]))
        s2 = MagicMock()
        s2.list_tools = AsyncMock(return_value=MagicMock(tools=["b"]))
        mock_build.side_effect = [["t1"], ["t2"]]

        agent = _make_agent()
        cache: dict = {}
        result = _resolve_mcp_tools(agent, [s1, s2], cache)

        assert result == ["t1", "t2"]
        assert mock_build.call_count == 2

    @patch("rastir.crewai_support._build_crewai_tools_from_mcp")
    def test_dict_session_matching_role(self, mock_build):
        """Dict mapping: matching role gets tools."""
        session = MagicMock()
        session.list_tools = AsyncMock(return_value=MagicMock(tools=["x"]))
        mock_build.return_value = ["mt1"]

        agent = _make_agent(role="researcher")
        cache: dict = {}
        result = _resolve_mcp_tools(agent, {"researcher": session}, cache)

        assert result == ["mt1"]

    @patch("rastir.crewai_support._build_crewai_tools_from_mcp")
    def test_dict_session_non_matching_role(self, mock_build):
        """Dict mapping: non-matching role gets no tools."""
        session = MagicMock()
        agent = _make_agent(role="writer")
        cache: dict = {}
        result = _resolve_mcp_tools(agent, {"researcher": session}, cache)

        assert result == []
        mock_build.assert_not_called()

    @patch("rastir.crewai_support._build_crewai_tools_from_mcp")
    def test_caches_by_session_id(self, mock_build):
        """Same session used twice → list_tools called only once."""
        session = MagicMock()
        session.list_tools = AsyncMock(return_value=MagicMock(tools=["a"]))
        mock_build.return_value = ["t1"]

        a1 = _make_agent(role="r1")
        a2 = _make_agent(role="r2")
        cache: dict = {}

        _resolve_mcp_tools(a1, session, cache)
        _resolve_mcp_tools(a2, session, cache)

        # list_tools called once, build called once
        session.list_tools.assert_called_once()
        mock_build.assert_called_once()


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
# _build_crewai_tools_from_mcp tests (requires crewai installed)
# ========================================================================


class TestBuildCrewaiToolsFromMcp:
    """Tests for MCP → CrewAI BaseTool conversion.

    These tests only run if crewai is installed.
    """

    @pytest.fixture(autouse=True)
    def _skip_if_no_crewai(self):
        try:
            from crewai.tools.base_tool import BaseTool  # noqa: F401
        except ImportError:
            pytest.skip("crewai not installed")

    def _make_mcp_tool(self, name: str, description: str, schema: dict | None = None):
        """Create a mock MCP Tool descriptor."""
        tool = MagicMock()
        tool.name = name
        tool.description = description
        tool.inputSchema = schema or {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
            },
            "required": ["query"],
        }
        return tool

    def test_creates_basetool_instances(self):
        from crewai.tools.base_tool import BaseTool
        from rastir.crewai_support import _build_crewai_tools_from_mcp

        session = MagicMock()
        mcp_tool = self._make_mcp_tool("web_search", "Search the web")
        tools = _build_crewai_tools_from_mcp(session, [mcp_tool])

        assert len(tools) == 1
        assert isinstance(tools[0], BaseTool)
        assert tools[0].name == "web_search"
        # CrewAI may augment the description with tool signature info
        assert "Search the web" in tools[0].description

    def test_args_schema_has_required_fields(self):
        from rastir.crewai_support import _build_crewai_tools_from_mcp

        session = MagicMock()
        mcp_tool = self._make_mcp_tool(
            "calc", "Calculator",
            {
                "type": "object",
                "properties": {
                    "expression": {"type": "string"},
                    "precision": {"type": "integer"},
                },
                "required": ["expression"],
            },
        )
        tools = _build_crewai_tools_from_mcp(session, [mcp_tool])
        schema = tools[0].args_schema
        fields = schema.model_fields
        assert "expression" in fields
        assert "precision" in fields
        # expression is required
        assert fields["expression"].is_required()
        # precision is optional
        assert not fields["precision"].is_required()

    def test_run_calls_session(self):
        from rastir.crewai_support import _build_crewai_tools_from_mcp

        session = MagicMock()
        # call_tool is async
        call_result = MagicMock()
        call_result.content = [MagicMock(text="result text")]
        session.call_tool = AsyncMock(return_value=call_result)

        mcp_tool = self._make_mcp_tool("search", "Search")
        tools = _build_crewai_tools_from_mcp(session, [mcp_tool])

        result = tools[0]._run(query="test")
        session.call_tool.assert_called_once_with("search", {"query": "test"})
        assert result == "result text"

    def test_empty_tools_list(self):
        from rastir.crewai_support import _build_crewai_tools_from_mcp

        session = MagicMock()
        tools = _build_crewai_tools_from_mcp(session, [])
        assert tools == []

    def test_no_input_schema(self):
        from rastir.crewai_support import _build_crewai_tools_from_mcp

        session = MagicMock()
        mcp_tool = self._make_mcp_tool("ping", "Ping tool", None)
        mcp_tool.inputSchema = None
        tools = _build_crewai_tools_from_mcp(session, [mcp_tool])
        assert len(tools) == 1
        assert tools[0].name == "ping"

    def test_multiple_tools(self):
        from rastir.crewai_support import _build_crewai_tools_from_mcp

        session = MagicMock()
        tools_in = [
            self._make_mcp_tool("t1", "Tool 1"),
            self._make_mcp_tool("t2", "Tool 2"),
            self._make_mcp_tool("t3", "Tool 3"),
        ]
        tools_out = _build_crewai_tools_from_mcp(session, tools_in)
        assert len(tools_out) == 3
        names = {t.name for t in tools_out}
        assert names == {"t1", "t2", "t3"}


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
        """During execution, agent tools are wrapped."""
        sentinel = MagicMock(_rastir_wrapped=True, _is_wrapped_tool=True)
        mock_wrap.side_effect = lambda obj, **kw: sentinel

        tool = _make_tool("scraper")
        agent = _make_agent("dev", tools=[tool])
        crew = _make_crew([agent])

        wrapped_during: list = []

        @crew_kickoff(agent_name="tool_test")
        def run(c):
            wrapped_during.extend(agent.tools)
            return "ok"

        run(crew)

        assert len(wrapped_during) == 1
        assert wrapped_during[0]._is_wrapped_tool is True

        # After execution, original is restored
        assert agent.tools == [tool]

    @patch("rastir.queue.enqueue_span")
    def test_mcp_tools_injected(self, mock_enqueue):
        """With mcp= param, MCP tools are injected into agents."""
        agent = _make_agent("dev", tools=[])
        crew = _make_crew([agent])

        session = MagicMock()
        session.list_tools = AsyncMock(return_value=MagicMock(tools=[]))

        injected_tools: list = []

        @crew_kickoff(agent_name="mcp_test", mcp=session)
        def run(c):
            injected_tools.extend(agent.tools)
            return "ok"

        with patch("rastir.crewai_support.wrap") as mw:
            mw.side_effect = lambda obj, **kw: obj
            with patch("rastir.crewai_support._build_crewai_tools_from_mcp") as mb:
                mb.return_value = [MagicMock(name="mcp_tool")]
                run(crew)

        # MCP tools were injected
        assert len(injected_tools) == 1

        # After execution, agent tools restored to empty
        assert agent.tools == []
