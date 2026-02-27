"""Context management for Rastir span propagation.

Uses Python contextvars to maintain the active span in the current
execution context. This enables automatic parent-child linking:
when a decorated function calls another decorated function, the inner
span automatically becomes a child of the outer span.

Key concepts:
- _current_span: ContextVar holding the active SpanRecord (or None)
- start_span(): creates a new span, links it to the current parent, sets it as active
- end_span(): restores the parent span as active
- get_current_span(): returns the currently active span (for attribute injection)
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Optional

from rastir.spans import SpanRecord, SpanType

# The active span in the current execution context.
# Each asyncio task / thread gets its own copy automatically via contextvars.
_current_span: ContextVar[Optional[SpanRecord]] = ContextVar("_current_span", default=None)

# Track the agent name from the nearest @agent ancestor span.
# This allows @llm / @tool / @retrieval to inject the agent label
# only when running under an explicit @agent decorator.
_current_agent: ContextVar[Optional[str]] = ContextVar("_current_agent", default=None)


def get_current_span() -> Optional[SpanRecord]:
    """Return the currently active span, or None if outside a traced context."""
    return _current_span.get()


def get_current_agent() -> Optional[str]:
    """Return the agent name from the nearest @agent ancestor, or None."""
    return _current_agent.get()


def start_span(name: str, span_type: SpanType) -> tuple[SpanRecord, Token]:
    """Create a new span and set it as the active span.

    If there is already an active span, the new span becomes its child
    (inheriting trace_id and setting parent_id).

    Args:
        name: Human-readable span name (usually the function name).
        span_type: Semantic type (trace, agent, llm, tool, retrieval, metric).

    Returns:
        A tuple of (the new SpanRecord, a contextvars Token for restoring
        the previous span when this one ends).
    """
    parent = _current_span.get()

    if parent is not None:
        # Child span — inherit trace_id, link to parent
        span = SpanRecord(
            name=name,
            span_type=span_type,
            trace_id=parent.trace_id,
            parent_id=parent.span_id,
        )
    else:
        # Root span — new trace
        span = SpanRecord(
            name=name,
            span_type=span_type,
        )

    token = _current_span.set(span)
    return span, token


def end_span(token: Token) -> None:
    """Restore the previous span as the active span.

    Args:
        token: The Token returned by start_span(), used to reset the ContextVar.
    """
    _current_span.reset(token)


def set_current_agent(agent_name: str) -> Token:
    """Set the current agent name in context.

    Called by the @agent decorator to make the agent identity available
    to child @llm / @tool / @retrieval spans.

    Args:
        agent_name: The name of the agent.

    Returns:
        A Token for restoring the previous agent name.
    """
    return _current_agent.set(agent_name)


def reset_current_agent(token: Token) -> None:
    """Restore the previous agent name in context."""
    _current_agent.reset(token)
