"""Rastir — LLM & Agent Observability Library.

Provides decorator-based instrumentation for LLM applications and AI agents.
Captures metrics and traces, pushes to a collector server that exposes
Prometheus metrics and OTLP traces.

Usage:
    from rastir import configure, trace, agent, llm, tool, retrieval, metric

    configure(service="my-app", env="production")

    @trace
    def handle_request(query: str) -> str:
        return run_agent(query)

    @agent(agent_name="qa_agent")
    def run_agent(query: str) -> str:
        ...
"""

from rastir.config import configure
from rastir.context import get_current_span
from rastir.decorators import agent, llm, metric, retrieval, tool, trace
from rastir.transport import get_export_stats, stop_exporter

__all__ = [
    "configure",
    "get_current_span",
    "trace",
    "agent",
    "metric",
    "llm",
    "tool",
    "retrieval",
    "stop_exporter",
    "get_export_stats",
]

__version__ = "0.1.0"
