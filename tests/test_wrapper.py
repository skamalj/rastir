"""Tests for rastir.wrap() — generic object wrapper.

Verifies:
  - Method calls produce spans with correct type and attributes
  - Sync and async methods both work
  - include/exclude filtering
  - Double-wrap prevention
  - isinstance preservation
  - Error handling
  - Custom name and span_type
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from rastir.wrapper import wrap
from rastir.spans import SpanType


# ========================================================================
# Test fixtures — sample objects to wrap
# ========================================================================


class SampleCache:
    """A simple cache-like object for testing."""

    def __init__(self):
        self.store = {}

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def set(self, key: str, value: str) -> None:
        self.store[key] = value

    def delete(self, key: str) -> bool:
        return self.store.pop(key, None) is not None

    def _internal(self):
        """Private method — should NOT be wrapped."""
        return "internal"


class AsyncService:
    """An async service for testing async wrapping."""

    async def fetch(self, url: str) -> str:
        return f"response from {url}"

    async def post(self, url: str, data: dict) -> dict:
        return {"url": url, "posted": True}

    def sync_method(self) -> str:
        return "sync"


class ErrorService:
    """Service that raises errors."""

    def fail(self):
        raise RuntimeError("Something went wrong")

    async def async_fail(self):
        raise ValueError("Async error")


# ========================================================================
# Tests
# ========================================================================


class TestWrapBasic:
    def test_wrap_returns_proxy(self):
        """wrap() returns a proxy, not the original object."""
        cache = SampleCache()
        wrapped = wrap(cache)
        assert wrapped is not cache

    def test_wrapped_method_returns_correct_result(self):
        """Wrapped methods still return the correct result."""
        cache = SampleCache()
        wrapped = wrap(cache)
        wrapped.set("key", "value")
        assert wrapped.get("key") == "value"

    def test_isinstance_preservation(self):
        """isinstance() still works on the wrapped object."""
        cache = SampleCache()
        wrapped = wrap(cache)
        assert isinstance(wrapped, SampleCache)

    def test_private_methods_not_wrapped(self):
        """Methods starting with _ are NOT intercepted."""
        cache = SampleCache()
        wrapped = wrap(cache)
        # _internal should still work, unmodified
        assert wrapped._internal() == "internal"

    def test_repr(self):
        """repr shows the wrapper info."""
        cache = SampleCache()
        wrapped = wrap(cache, name="redis")
        r = repr(wrapped)
        assert "rastir.wrap(redis)" in r

    def test_str_delegates(self):
        """str() delegates to the original object."""
        cache = SampleCache()
        wrapped = wrap(cache)
        assert str(wrapped) == str(cache)


class TestWrapSpans:
    def test_span_emitted_on_method_call(self):
        """Each method call emits a span via enqueue_span."""
        cache = SampleCache()
        wrapped = wrap(cache, name="mycache")

        with patch("rastir.wrapper.enqueue_span") as mock_enqueue:
            wrapped.set("k", "v")
            assert mock_enqueue.call_count == 1
            span = mock_enqueue.call_args[0][0]
            assert span.name == "mycache.set"
            assert span.span_type == SpanType.INFRA
            assert span.attributes["wrap.method"] == "set"
            assert span.attributes["wrap.args_count"] == 2

    def test_span_kwargs_recorded(self):
        """kwargs keys are recorded as span attributes."""
        cache = SampleCache()
        wrapped = wrap(cache, name="c")

        with patch("rastir.wrapper.enqueue_span") as mock_enqueue:
            wrapped.get(key="mykey")
            span = mock_enqueue.call_args[0][0]
            assert span.attributes["wrap.kwargs_keys"] == ["key"]

    def test_default_span_type_infra(self):
        """Default span type is INFRA."""
        cache = SampleCache()
        wrapped = wrap(cache)

        with patch("rastir.wrapper.enqueue_span") as mock_enqueue:
            wrapped.get("k")
            span = mock_enqueue.call_args[0][0]
            assert span.span_type == SpanType.INFRA

    def test_custom_span_type(self):
        """Custom span_type is applied."""
        cache = SampleCache()
        wrapped = wrap(cache, span_type="tool")

        with patch("rastir.wrapper.enqueue_span") as mock_enqueue:
            wrapped.get("k")
            span = mock_enqueue.call_args[0][0]
            assert span.span_type == SpanType.TOOL

    def test_default_name_is_class_name(self):
        """Default name prefix is the class name."""
        cache = SampleCache()
        wrapped = wrap(cache)

        with patch("rastir.wrapper.enqueue_span") as mock_enqueue:
            wrapped.get("k")
            span = mock_enqueue.call_args[0][0]
            assert span.name == "SampleCache.get"


class TestWrapAsync:
    def test_async_method_wrapped(self):
        """Async methods are wrapped and emit spans."""
        service = AsyncService()
        wrapped = wrap(service, name="svc")

        async def _run():
            with patch("rastir.wrapper.enqueue_span") as mock_enqueue:
                result = await wrapped.fetch("http://example.com")
                assert result == "response from http://example.com"
                assert mock_enqueue.call_count == 1
                span = mock_enqueue.call_args[0][0]
                assert span.name == "svc.fetch"

        asyncio.get_event_loop().run_until_complete(_run())

    def test_mixed_sync_async(self):
        """Object with both sync and async methods works correctly."""
        service = AsyncService()
        wrapped = wrap(service, name="svc")

        with patch("rastir.wrapper.enqueue_span") as mock_enqueue:
            result = wrapped.sync_method()
            assert result == "sync"
            assert mock_enqueue.call_count == 1


class TestWrapErrors:
    def test_error_recorded_in_span(self):
        """Errors are recorded in the span and re-raised."""
        service = ErrorService()
        wrapped = wrap(service, name="err")

        with patch("rastir.wrapper.enqueue_span") as mock_enqueue:
            with pytest.raises(RuntimeError, match="Something went wrong"):
                wrapped.fail()
            span = mock_enqueue.call_args[0][0]
            assert span.status.value == "ERROR"

    def test_async_error_recorded(self):
        """Async errors are recorded and re-raised."""
        service = ErrorService()
        wrapped = wrap(service, name="err")

        async def _run():
            with patch("rastir.wrapper.enqueue_span") as mock_enqueue:
                with pytest.raises(ValueError, match="Async error"):
                    await wrapped.async_fail()
                span = mock_enqueue.call_args[0][0]
                assert span.status.value == "ERROR"

        asyncio.get_event_loop().run_until_complete(_run())


class TestWrapFiltering:
    def test_include_filter(self):
        """Only methods in include list are wrapped."""
        cache = SampleCache()
        wrapped = wrap(cache, name="c", include=["get"])

        with patch("rastir.wrapper.enqueue_span") as mock_enqueue:
            wrapped.get("k")
            assert mock_enqueue.call_count == 1

            # set should NOT be wrapped
            wrapped.set("k", "v")
            assert mock_enqueue.call_count == 1  # still 1

    def test_exclude_filter(self):
        """Methods in exclude list are NOT wrapped."""
        cache = SampleCache()
        wrapped = wrap(cache, name="c", exclude=["delete"])

        with patch("rastir.wrapper.enqueue_span") as mock_enqueue:
            wrapped.get("k")
            assert mock_enqueue.call_count == 1
            wrapped.delete("k")
            assert mock_enqueue.call_count == 1  # still 1


class TestWrapDoubleWrap:
    def test_double_wrap_prevention(self):
        """Wrapping an already-wrapped object returns it as-is."""
        cache = SampleCache()
        wrapped1 = wrap(cache, name="c")
        wrapped2 = wrap(wrapped1, name="c2")
        assert wrapped2 is wrapped1

    def test_wrapped_marker(self):
        """Wrapped objects have _rastir_wrapped=True."""
        cache = SampleCache()
        wrapped = wrap(cache)
        assert wrapped._rastir_wrapped is True


class TestWrapValidation:
    def test_invalid_span_type_raises(self):
        """Invalid span_type raises ValueError."""
        cache = SampleCache()
        with pytest.raises(ValueError, match="Unknown span_type"):
            wrap(cache, span_type="invalid")

    def test_valid_span_types(self):
        """All valid span types are accepted."""
        cache = SampleCache()
        for st in ["infra", "tool", "llm", "trace", "agent", "retrieval"]:
            wrapped = wrap(SampleCache(), span_type=st)
            assert wrapped._rastir_wrapped is True


class TestWrapSetattr:
    def test_setattr_delegates_to_original(self):
        """Setting attributes on wrapped object sets them on the original."""
        cache = SampleCache()
        wrapped = wrap(cache)
        wrapped.custom_attr = "hello"
        assert cache.custom_attr == "hello"

    def test_method_cache(self):
        """Same method wrapper is returned on repeated access."""
        cache = SampleCache()
        wrapped = wrap(cache, name="c")
        getter1 = wrapped.get
        getter2 = wrapped.get
        assert getter1 is getter2
