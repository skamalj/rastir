# Rastir

<p align="center">
  <img src="https://raw.githubusercontent.com/skamalj/rastir/main/rastir_small.png" alt="Rastir" width="200">
</p>

<p align="center">
  <strong>LLM & Agent Observability for Python</strong><br>
  One decorator per framework. Full visibility. No monkey-patching.
</p>

<p align="center">
  <a href="https://pypi.org/project/rastir/"><img alt="PyPI" src="https://img.shields.io/pypi/v/rastir"></a>
  <a href="https://pypi.org/project/rastir/"><img alt="Python" src="https://img.shields.io/pypi/pyversions/rastir"></a>
  <a href="https://skamalj.github.io/rastir/"><img alt="Docs" src="https://img.shields.io/badge/docs-GitHub%20Pages-blue"></a>
  <a href="https://github.com/skamalj/rastir/blob/main/LICENSE"><img alt="License" src="https://img.shields.io/github/license/skamalj/rastir"></a>
  <a href="https://github.com/skamalj/rastir"><img alt="GitHub" src="https://img.shields.io/github/stars/skamalj?style=social"></a>
</p>

---

## What is Rastir?

Rastir gives you **production observability for LLM agents** — token usage, latency percentiles, cost tracking, tool call rates, error categories — as Prometheus metrics with Grafana dashboards.

Add **one decorator** to your LangGraph, CrewAI, or LlamaIndex workflow. Rastir auto-discovers LLMs, tools, and graph nodes inside the framework and wraps them for per-call tracing. No code rewrites. No vendor lock-in.

```python
from rastir import configure, langgraph_agent

configure(service="my-app", push_url="http://localhost:8080")

@langgraph_agent
def run(query):
    graph = create_react_agent(model, tools)
    return graph.invoke({"messages": [("user", query)]})
```

That's it. Every LLM call, tool invocation, and node execution inside the graph is now traced with metrics flowing to Prometheus.

---

## Key Features

| Feature | Description |
|---------|-------------|
| **One decorator per framework** | `@langgraph_agent`, `@crew_kickoff`, `@llamaindex_agent` — auto-discovers and wraps everything inside |
| **15 provider adapters** | OpenAI, Azure, Anthropic, Bedrock, Gemini, Cohere, Mistral, Groq, LangChain, LangGraph, LlamaIndex, CrewAI — auto-detected |
| **Two-phase enrichment** | Model/provider metadata captured from function args *before* the call, refined from response *after*. Survives API failures |
| **MCP distributed tracing** | `wrap(session)` propagates trace context across MCP tool boundaries — same `trace_id` links client and server |
| **Cost observability** | Per-model USD cost tracking with `PricingRegistry`, pricing profiles, cost histograms |
| **Streaming TTFT** | Time-To-First-Token measurement on streaming LLM calls |
| **Guardrail tracking** | Automatic AWS Bedrock guardrail violation metrics |
| **Error normalisation** | Exceptions mapped to 6 fixed categories: timeout, rate_limit, validation_error, provider_error, internal_error, unknown |
| **Self-hosted collector** | FastAPI server you own. Prometheus `/metrics`, in-memory trace store, OTLP export to Tempo/Jaeger |
| **SRE budgets & burn rates** | Error and cost budget tracking via Prometheus recording rules — SLO status, burn rates, days-to-exhaustion, all config-driven |
| **7 Grafana dashboards** | LLM Performance, Agent-Tool, Cost-TTFT, Evaluation, Guardrail, SRE Budgets, System Health |
| **Generic `wrap()`** | Instrument any object — Redis, databases, MCP sessions — without decorator access |

---

## Framework Support at a Glance

| | LangGraph | CrewAI | LlamaIndex |
|---|---|---|---|
| **Decorator** | `@langgraph_agent` | `@crew_kickoff` | `@llamaindex_agent` |
| **Agent span** | Automatic | Automatic | Automatic |
| **LLM tracing** | Auto-discovered | Auto-discovered | `wrap(llm)` |
| **Tool tracing** | Auto-discovered | Auto-discovered | `wrap(tool)` |
| **Node tracing** | Automatic (all nodes) | N/A | N/A |
| **MCP tools** | Pass as normal tools | Native via `mcps=[]` on agents | `wrap()` on McpToolSpec tools |
| **User code** | 1 decorator | 1 decorator | 1 decorator + `wrap()` calls |

### LangGraph

```python
@langgraph_agent(agent_name="react_agent")
def run(graph, query):
    return graph.invoke({"messages": [("user", query)]})
```

```
react_agent (AGENT)
  ├── node:agent (TRACE)       ← every graph node traced
  │   └── langgraph.llm.gpt-4o.invoke (LLM)
  ├── node:tools (TRACE)
  │   └── langgraph.tool.search.invoke (TOOL)
  └── node:agent (TRACE)
      └── langgraph.llm.gpt-4o.invoke (LLM)
```

### CrewAI

```python
@crew_kickoff(agent_name="research_crew")
def run(crew):
    return crew.kickoff()
```

### LlamaIndex

```python
llm = wrap(OpenAI(model="gpt-4o"))
tools = [wrap(t) for t in my_tools]

@llamaindex_agent(agent_name="qa_agent")
def run(agent):
    return agent.chat("Hello")
```

→ **Detailed framework documentation:** [LangGraph](https://skamalj.github.io/rastir/frameworks/langgraph) · [CrewAI](https://skamalj.github.io/rastir/frameworks/crewai) · [LlamaIndex](https://skamalj.github.io/rastir/frameworks/llamaindex)

---

## Supported Providers

| Provider | Auto-detection | Tokens | Model | Streaming | Request-phase |
|----------|:-:|:-:|:-:|:-:|:-:|
| **OpenAI** | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Azure OpenAI** | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Anthropic** | ✅ | ✅ | ✅ | ✅ | ✅ |
| **AWS Bedrock** | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Google Gemini** | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Cohere** | ✅ | ✅ | ✅ | — | ✅ |
| **Mistral** | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Groq** | ✅ | ✅ | ✅ | ✅ | ✅ |
| **LangChain** | ✅ | ✅ | ✅ | ✅ | — |
| **LangGraph** | ✅ | ✅ | ✅ | ✅ | — |
| **LlamaIndex** | ✅ | ✅ | ✅ | ✅ | — |
| **CrewAI** | ✅ | ✅ | — | — | — |

---

## Installation

```bash
pip install rastir              # Client library
pip install rastir[server]      # + Collector server
pip install rastir[all]         # Everything
```

## Quick Start

```python
from rastir import configure, agent, llm, trace

configure(service="my-app", push_url="http://localhost:8080")

@agent(agent_name="qa_bot")
def answer(query):
    return ask_llm(search(query))

@trace
def search(query):
    return vector_db.search(query)

@llm
def ask_llm(context):
    return openai.chat.completions.create(model="gpt-4o", messages=[...])
```

Start the collector:

```bash
rastir-server   # Prometheus metrics at :8080/metrics
```

## What You Get in Prometheus

```
rastir_llm_calls_total{model="gpt-4o", provider="openai", agent="qa_bot"} 150
rastir_tokens_input_total{model="gpt-4o"} 25000
rastir_tokens_output_total{model="gpt-4o"} 8500
rastir_duration_seconds_bucket{span_type="llm", le="1.0"} 120
rastir_errors_total{span_type="llm", error_type="rate_limit"} 3
rastir_cost_total{model="gpt-4o", pricing_profile="prod"} 12.50
rastir_ttft_seconds_bucket{model="gpt-4o", le="0.5"} 95
```

---

## Architecture

```
Your Application                             Rastir Collector
┌────────────────────────────────┐           ┌────────────────────────────┐
│  @langgraph_agent              │   HTTP    │  FastAPI                   │
│  @crew_kickoff                 │  ──────▸  │  ├── Prometheus /metrics   │
│  @llamaindex_agent             │   spans   │  ├── Trace store /v1/traces│
│  @agent / @llm                 │           │  ├── Sampling & backpressure│
│  wrap(obj)                     │           │  └── OTLP → Tempo/Jaeger  │
└────────────────────────────────┘           └────────────────────────────┘
```

---

## Documentation

Full documentation at **[skamalj.github.io/rastir](https://skamalj.github.io/rastir/)**:

| Section | Pages |
|---------|-------|
| **Getting Started** | [Installation & Quick Start](https://skamalj.github.io/rastir/getting-started) |
| **Core** | [Decorators](https://skamalj.github.io/rastir/decorators) · [Adapters](https://skamalj.github.io/rastir/adapters) · [wrap() & MCP](https://skamalj.github.io/rastir/wrap) · [MCP Tracing](https://skamalj.github.io/rastir/mcp-tracing) |
| **Frameworks** | [LangGraph](https://skamalj.github.io/rastir/frameworks/langgraph) · [CrewAI](https://skamalj.github.io/rastir/frameworks/crewai) · [LlamaIndex](https://skamalj.github.io/rastir/frameworks/llamaindex) |
| **Operations** | [Metrics](https://skamalj.github.io/rastir/metrics) · [Dashboards](https://skamalj.github.io/rastir/dashboards) · [Server](https://skamalj.github.io/rastir/server) · [Configuration](https://skamalj.github.io/rastir/configuration) |
| **Reference** | [Architecture](https://skamalj.github.io/rastir/architecture-responsibilities) · [Environment Variables](https://skamalj.github.io/rastir/environment-variables) · [Contributing Adapters](https://skamalj.github.io/rastir/contributing-adapters) |

---

## License

MIT — see [LICENSE](LICENSE) for details.
