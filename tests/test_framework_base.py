"""Unit tests for rastir.framework_base — FrameworkInstrumentor, make_framework_decorator, framework_agent.

Tests cover:
  - FrameworkInstrumentor ABC enforcement
  - make_framework_decorator: bare and parameterised usage, sync + async
  - framework_agent auto-detection: finds the correct instrumentor
  - framework_agent fallback: plain @agent span when no framework detected
  - walk_func_for_mcp_clients: closure and globals scanning
  - Instrumentor registration and registry ordering
  - Error handling (span records error, re-raises)
  - Restore is always called (even on error)
  - Agent name defaults to function name when not supplied

Uses mock instrumentors — no real framework imports needed.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from rastir.framework_base import (
    FrameworkInstrumentor,
    _instrumentor_registry,
    framework_agent,
    make_framework_decorator,
    register_instrumentor,
    walk_func_for_mcp_clients,
)
from rastir.spans import SpanType


# ========================================================================
# Test instrumentor implementations
# ========================================================================

class DummyInstrumentor(FrameworkInstrumentor):
    """Instrumentor that detects objects with _dummy_marker=True."""

    def __init__(self):
        self.wrap_calls: list = []
        self.restore_calls: list = []

    def detect(self, obj):
        return getattr(obj, "_dummy_marker", False)

    def wrap(self, obj, originals):
        self.wrap_calls.append(obj)
        originals["wrapped"] = True

    def restore(self, originals):
        self.restore_calls.append(originals)


class ListOriginalsInstrumentor(FrameworkInstrumentor):
    """Instrumentor that uses list-based originals (like LangGraph)."""

    def detect(self, obj):
        return getattr(obj, "_list_marker", False)

    def wrap(self, obj, originals):
        originals.append(("ref", obj))

    def restore(self, originals):
        originals.clear()

    def create_originals(self):
        return []

    @property
    def agent_attr_name(self):
        return "agent"


class ExtraMCPInstrumentor(FrameworkInstrumentor):
    """Instrumentor that discovers extra MCP clients."""

    def __init__(self):
        self.extra_discovered: list = []

    def detect(self, obj):
        return getattr(obj, "_extra_marker", False)

    def wrap(self, obj, originals):
        pass

    def restore(self, originals):
        pass

    def discover_extra_mcp_clients(self, obj, mcp_clients):
        extra = getattr(obj, "_mcp_clients", [])
        mcp_clients.extend(extra)
        self.extra_discovered.extend(extra)


# ========================================================================
# ABC enforcement
# ========================================================================

class TestFrameworkInstrumentorABC:
    def test_cannot_instantiate_abc(self):
        with pytest.raises(TypeError):
            FrameworkInstrumentor()

    def test_must_implement_detect(self):
        class Missing(FrameworkInstrumentor):
            def wrap(self, obj, originals): pass
            def restore(self, originals): pass
        with pytest.raises(TypeError):
            Missing()

    def test_must_implement_wrap(self):
        class Missing(FrameworkInstrumentor):
            def detect(self, obj): return False
            def restore(self, originals): pass
        with pytest.raises(TypeError):
            Missing()

    def test_must_implement_restore(self):
        class Missing(FrameworkInstrumentor):
            def detect(self, obj): return False
            def wrap(self, obj, originals): pass
        with pytest.raises(TypeError):
            Missing()

    def test_defaults(self):
        inst = DummyInstrumentor()
        assert inst.agent_attr_name == "agent_name"
        assert isinstance(inst.create_originals(), dict)
        clients: list = []
        inst.discover_extra_mcp_clients(object(), clients)
        assert clients == []


# ========================================================================
# make_framework_decorator
# ========================================================================

class TestMakeFrameworkDecorator:
    def setup_method(self):
        self.inst = DummyInstrumentor()

    @patch("rastir.queue.enqueue_span")
    def test_bare_decorator(self, mock_enqueue):
        """@dec applied without parentheses."""
        dec = make_framework_decorator(self.inst)

        @dec
        def my_func():
            return "ok"

        result = my_func()
        assert result == "ok"
        mock_enqueue.assert_called_once()
        span = mock_enqueue.call_args[0][0]
        assert span.span_type == SpanType.AGENT
        assert span.attributes.get("agent_name") == "my_func"

    @patch("rastir.queue.enqueue_span")
    def test_parameterised_decorator(self, mock_enqueue):
        """@dec(agent_name=...) works."""
        dec = make_framework_decorator(self.inst)

        @dec(agent_name="custom")
        def my_func():
            return "ok"

        result = my_func()
        assert result == "ok"
        span = mock_enqueue.call_args[0][0]
        assert span.attributes.get("agent_name") == "custom"

    @patch("rastir.queue.enqueue_span")
    def test_wraps_detected_objects(self, mock_enqueue):
        """Framework objects in args get wrapped."""
        dec = make_framework_decorator(self.inst)
        obj = MagicMock(_dummy_marker=True)

        @dec(agent_name="test")
        def my_func(framework_obj):
            return "done"

        my_func(obj)
        assert obj in self.inst.wrap_calls
        assert len(self.inst.restore_calls) == 1

    @patch("rastir.queue.enqueue_span")
    def test_error_handling(self, mock_enqueue):
        """Errors are recorded on span and re-raised."""
        dec = make_framework_decorator(self.inst)

        @dec(agent_name="test")
        def my_func():
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            my_func()

        span = mock_enqueue.call_args[0][0]
        assert span.attributes.get("error.type") is not None

    @patch("rastir.queue.enqueue_span")
    def test_restore_called_on_error(self, mock_enqueue):
        """Restore is always called, even on error."""
        self.inst = DummyInstrumentor()
        dec = make_framework_decorator(self.inst)
        obj = MagicMock(_dummy_marker=True)

        @dec(agent_name="test")
        def my_func(framework_obj):
            raise RuntimeError("fail")

        with pytest.raises(RuntimeError):
            my_func(obj)

        assert len(self.inst.wrap_calls) == 1
        assert len(self.inst.restore_calls) == 1

    @patch("rastir.queue.enqueue_span")
    def test_list_originals(self, mock_enqueue):
        """Instrumentor with list-based originals works correctly."""
        inst = ListOriginalsInstrumentor()
        dec = make_framework_decorator(inst)
        obj = MagicMock(_list_marker=True)

        @dec(agent_name="test")
        def my_func(framework_obj):
            return "done"

        my_func(obj)
        span = mock_enqueue.call_args[0][0]
        assert span.attributes.get("agent") == "test"

    @patch("rastir.queue.enqueue_span")
    def test_async_decorator(self, mock_enqueue):
        """Async functions are correctly handled."""
        dec = make_framework_decorator(self.inst)

        @dec(agent_name="async_test")
        async def my_func():
            return "async_ok"

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(my_func())
        finally:
            loop.close()

        assert result == "async_ok"
        span = mock_enqueue.call_args[0][0]
        assert span.attributes.get("agent_name") == "async_test"

    @patch("rastir.queue.enqueue_span")
    def test_async_error(self, mock_enqueue):
        """Async errors are recorded and re-raised."""
        dec = make_framework_decorator(self.inst)

        @dec(agent_name="async_err")
        async def my_func():
            raise ValueError("async_boom")

        loop = asyncio.new_event_loop()
        try:
            with pytest.raises(ValueError, match="async_boom"):
                loop.run_until_complete(my_func())
        finally:
            loop.close()

        span = mock_enqueue.call_args[0][0]
        assert span.attributes.get("error.type") is not None


# ========================================================================
# framework_agent auto-detection
# ========================================================================

class TestFrameworkAgent:
    def setup_method(self):
        self._saved_registry = list(_instrumentor_registry)
        _instrumentor_registry.clear()
        self.dummy_inst = DummyInstrumentor()
        self.list_inst = ListOriginalsInstrumentor()
        register_instrumentor(self.dummy_inst)
        register_instrumentor(self.list_inst)

    def teardown_method(self):
        _instrumentor_registry.clear()
        _instrumentor_registry.extend(self._saved_registry)

    @patch("rastir.queue.enqueue_span")
    def test_autodetects_dummy(self, mock_enqueue):
        """framework_agent detects _dummy_marker objects."""
        obj = MagicMock(_dummy_marker=True)

        @framework_agent(agent_name="auto")
        def run(fw_obj):
            return "detected"

        result = run(obj)
        assert result == "detected"
        assert obj in self.dummy_inst.wrap_calls
        span = mock_enqueue.call_args[0][0]
        assert span.attributes.get("agent_name") == "auto"

    @patch("rastir.queue.enqueue_span")
    def test_autodetects_list_instrumentor(self, mock_enqueue):
        """framework_agent picks the correct instrumentor from registry."""
        obj = MagicMock(_list_marker=True, _dummy_marker=False)

        @framework_agent(agent_name="auto_list")
        def run(fw_obj):
            return "list_ok"

        result = run(obj)
        assert result == "list_ok"
        span = mock_enqueue.call_args[0][0]
        assert span.attributes.get("agent") == "auto_list"

    @patch("rastir.queue.enqueue_span")
    def test_fallback_no_framework(self, mock_enqueue):
        """framework_agent falls back to plain @agent span."""
        @framework_agent(agent_name="fallback")
        def run(query):
            return query.upper()

        result = run("hello")
        assert result == "HELLO"
        span = mock_enqueue.call_args[0][0]
        assert span.attributes.get("agent_name") == "fallback"
        assert len(self.dummy_inst.wrap_calls) == 0

    @patch("rastir.queue.enqueue_span")
    def test_bare_usage(self, mock_enqueue):
        """@framework_agent without parentheses."""
        @framework_agent
        def run():
            return "bare"

        result = run()
        assert result == "bare"
        span = mock_enqueue.call_args[0][0]
        assert span.attributes.get("agent_name") == "run"

    @patch("rastir.queue.enqueue_span")
    def test_async_auto_detection(self, mock_enqueue):
        """framework_agent works with async functions."""
        obj = MagicMock(_dummy_marker=True)

        @framework_agent(agent_name="async_auto")
        async def run(fw_obj):
            return "async_detected"

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(run(obj))
        finally:
            loop.close()

        assert result == "async_detected"

    @patch("rastir.queue.enqueue_span")
    def test_async_fallback(self, mock_enqueue):
        """framework_agent async fallback works."""
        @framework_agent(agent_name="async_fb")
        async def run():
            return "async_fallback"

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(run())
        finally:
            loop.close()

        assert result == "async_fallback"

    @patch("rastir.queue.enqueue_span")
    def test_kwargs_detection(self, mock_enqueue):
        """framework_agent detects framework objects in kwargs."""
        obj = MagicMock(_dummy_marker=True)

        @framework_agent(agent_name="kw")
        def run(query, fw_obj=None):
            return "kwargs"

        result = run("hello", fw_obj=obj)
        assert result == "kwargs"
        assert obj in self.dummy_inst.wrap_calls

    @patch("rastir.queue.enqueue_span")
    def test_error_in_auto_detected(self, mock_enqueue):
        """Errors are handled correctly with auto-detection."""
        obj = MagicMock(_dummy_marker=True)

        @framework_agent(agent_name="err")
        def run(fw_obj):
            raise RuntimeError("auto_error")

        with pytest.raises(RuntimeError, match="auto_error"):
            run(obj)

        assert len(self.dummy_inst.restore_calls) == 1


# ========================================================================
# walk_func_for_mcp_clients
# ========================================================================

class TestWalkFuncForMCPClients:
    @patch("rastir.framework_base.discover_mcp_client")
    def test_discovers_from_closure(self, mock_discover):
        """Discovers MCP clients from function closures."""
        mock_client = MagicMock()
        mock_discover.return_value = mock_client

        captured = "some_value"

        def func():
            return captured

        clients: list = []
        walk_func_for_mcp_clients(func, clients)
        assert len(clients) == 1
        assert clients[0] is mock_client

    @patch("rastir.framework_base.discover_mcp_client")
    def test_skips_non_mcp_objects(self, mock_discover):
        """Non-MCP objects are skipped."""
        mock_discover.return_value = None

        val = "not_mcp"

        def func():
            return val

        clients: list = []
        walk_func_for_mcp_clients(func, clients)
        assert len(clients) == 0

    def test_handles_no_closure(self):
        """Functions without closures don't crash."""
        def func():
            return "plain"

        clients: list = []
        walk_func_for_mcp_clients(func, clients)
        assert len(clients) == 0


# ========================================================================
# Extra MCP discovery
# ========================================================================

class TestExtraMCPDiscovery:
    def setup_method(self):
        self._saved_registry = list(_instrumentor_registry)
        _instrumentor_registry.clear()
        self.inst = ExtraMCPInstrumentor()
        register_instrumentor(self.inst)

    def teardown_method(self):
        _instrumentor_registry.clear()
        _instrumentor_registry.extend(self._saved_registry)

    @patch("rastir.framework_base.inject_traceparent_into_mcp_clients")
    @patch("rastir.queue.enqueue_span")
    def test_extra_mcp_clients_discovered(self, mock_enqueue, mock_inject):
        """discover_extra_mcp_clients is called on detected objects."""
        mock_mcp = MagicMock()
        obj = MagicMock(_extra_marker=True, _mcp_clients=[mock_mcp])

        @framework_agent(agent_name="mcp_test")
        def run(fw_obj):
            return "mcp_ok"

        run(obj)
        assert mock_mcp in self.inst.extra_discovered
        inject_call_args = mock_inject.call_args[0][0]
        assert mock_mcp in inject_call_args


# ========================================================================
# Instrumentor registration
# ========================================================================

class TestInstrumentorRegistry:
    def setup_method(self):
        self._saved_registry = list(_instrumentor_registry)

    def teardown_method(self):
        _instrumentor_registry.clear()
        _instrumentor_registry.extend(self._saved_registry)

    def test_real_instrumentors_registered(self):
        """All 5 framework instrumentors are registered."""
        from rastir.langgraph_support import LangGraphInstrumentor
        from rastir.crewai_support import CrewAIInstrumentor
        from rastir.llamaindex_support import LlamaIndexInstrumentor
        from rastir.adk_support import ADKInstrumentor
        from rastir.strands_support import StrandsInstrumentor

        types_in_registry = {type(inst) for inst in _instrumentor_registry}
        assert LangGraphInstrumentor in types_in_registry
        assert CrewAIInstrumentor in types_in_registry
        assert LlamaIndexInstrumentor in types_in_registry
        assert ADKInstrumentor in types_in_registry
        assert StrandsInstrumentor in types_in_registry
