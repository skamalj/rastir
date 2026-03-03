"""Rastir — LLM & Agent Observability Library.

Provides decorator-based instrumentation for LLM applications and AI agents.
Captures metrics and traces, pushes to a collector server that exposes
Prometheus metrics and OTLP traces.

Usage:
    from rastir import configure, trace, agent, llm, retrieval, metric

    # When using alongside LangChain/LangGraph, use _span aliases
    # to avoid name collisions with langchain_core names:
    from rastir import configure, trace_span, agent_span, llm_span

    configure(service="my-app", env="production")

    @trace_span
    def handle_request(query: str) -> str:
        return run_agent(query)

    @agent_span(agent_name="qa_agent")
    def run_agent(query: str) -> str:
        ...
"""

from rastir.config import configure
from rastir.context import get_current_span
from rastir.decorators import agent, llm, metric, retrieval, trace
from rastir.pricing import PricingRegistry
from rastir.remote import mcp_endpoint, wrap_mcp
from rastir.transport import get_export_stats, stop_exporter
from rastir.wrapper import wrap
from rastir.crewai_support import crew_kickoff
from rastir.langgraph_support import langgraph_agent
from rastir.llamaindex_support import llamaindex_agent

# _span aliases — preferred when using alongside LangChain/LangGraph
# to avoid name collisions (e.g. langchain_core names)
trace_span = trace
agent_span = agent
llm_span = llm
retrieval_span = retrieval
metric_span = metric

__all__ = [
    "configure",
    "get_current_span",
    "trace",
    "agent",
    "metric",
    "llm",
    "retrieval",
    "wrap",
    "PricingRegistry",
    # _span aliases
    "trace_span",
    "agent_span",
    "llm_span",
    "retrieval_span",
    "metric_span",
    # remote tracing
    "wrap_mcp",
    "mcp_endpoint",
    # CrewAI
    "crew_kickoff",
    # LangGraph
    "langgraph_agent",
    # LlamaIndex
    "llamaindex_agent",
    "stop_exporter",
    "get_export_stats",
]

__version__ = "0.1.1"
