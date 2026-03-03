---
layout: default
title: Home
nav_order: 1
permalink: /
---

# Rastir

**LLM & Agent Observability Library for Python**

Rastir provides decorator-based instrumentation for LLM applications and AI agents. It captures structured traces and Prometheus metrics with minimal code changes — no monkey-patching, no framework lock-in.

---

## Why Rastir?

Most LLM observability tools require SDK wrappers, monkey-patching, or vendor-specific clients. Rastir takes a different approach:

- **One decorator per framework** — `@langgraph_agent`, `@crew_kickoff`, `@llamaindex_agent` auto-discover and wrap LLMs, tools, and nodes inside the framework
- **Adapters, not patches** — 15 adapters extract model, tokens, and provider from return values. Works across SDK versions
- **Two-phase enrichment** — metadata captured from function kwargs *before* the call, refined from the response *after*. Survives API failures
- **Self-hosted collector** — a lightweight FastAPI server you own. Prometheus metrics, OTLP export, zero external infrastructure

---

## Quick Example

```python
from rastir import configure, langgraph_agent

configure(service="my-app", push_url="http://localhost:8080")

@langgraph_agent(agent_name="react_agent")
def run(query):
    graph = create_react_agent(model, tools)
    return graph.invoke({"messages": [("user", query)]})
```

Every LLM call, tool invocation, and node execution is now traced:

```
react_agent (AGENT)
  ├── node:agent (TRACE)
  │   └── langgraph.llm.gpt-4o.invoke (LLM)
  ├── node:tools (TRACE)
  │   └── langgraph.tool.search.invoke (TOOL)
  └── node:agent (TRACE)
      └── langgraph.llm.gpt-4o.invoke (LLM)
```

---

## Key Features

- **Framework decorators** — `@langgraph_agent`, `@crew_kickoff`, `@llamaindex_agent` with automatic LLM/tool discovery
- **15 provider adapters** — OpenAI, Azure, Anthropic, Bedrock, Gemini, Cohere, Mistral, Groq, LangChain, LangGraph, LlamaIndex, CrewAI
- **MCP distributed tracing** — `wrap(session)` and `@mcp_endpoint` for end-to-end tracing across MCP tool boundaries
- **Generic `wrap()`** — instrument any object (Redis, databases, MCP sessions) without decorator access
- **Cost observability** — per-model USD cost tracking with `PricingRegistry`, pricing profiles
- **Streaming TTFT** — Time-To-First-Token measurement on streaming LLM calls
- **Guardrail tracking** — automatic AWS Bedrock guardrail violation metrics
- **Error normalisation** — exceptions mapped to 6 fixed categories
- **Prometheus metrics** — duration histograms, token counters, cost metrics, error rates
- **7 Grafana dashboards** — ready-to-import dashboards for LLM, agent, cost, SRE, and system health
- **OTLP export** — forward spans to Tempo, Jaeger, or any OTLP backend
- **Self-hosted collector** — FastAPI server with sampling, backpressure, rate limiting, cardinality guards

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│  Your Application                               │
│  ┌──────────────────────────────────────────┐   │
│  │  @langgraph_agent / @crew_kickoff /      │   │
│  │  @llamaindex_agent                       │   │
│  │  @agent / @llm / wrap()                 │   │
│  │  Decorators → SpanRecord → Queue         │   │
│  └───────────────┬──────────────────────────┘   │
│                  │ HTTP POST /v1/telemetry       │
└──────────────────┼──────────────────────────────┘
                   ▼
┌─────────────────────────────────────────────────┐
│  Rastir Collector Server                        │
│  ┌────────────┐  ┌──────────┐  ┌────────────┐  │
│  │ Ingestion  │→ │ Metrics  │→ │ Prometheus │  │
│  │ Worker     │  │ Registry │  │ /metrics   │  │
│  │            │→ │ Trace    │  │            │  │
│  │            │  │ Store    │  │ /v1/traces │  │
│  │            │→ │ OTLP     │→ │ Jaeger/    │  │
│  │            │  │ Exporter │  │ Tempo      │  │
│  └────────────┘  └──────────┘  └────────────┘  │
└─────────────────────────────────────────────────┘
```

---

## Pages

### Getting Started
- [Installation & Quick Start](getting-started.md)

### Core
- [Decorators](decorators.md) — `@trace`, `@agent`, `@llm`, `@retrieval`, `@metric`
- [Adapters](adapters.md) — 15 adapters with two-phase enrichment
- [wrap() & MCP](wrap.md) — Generic object wrapper and MCP session wrapping
- [MCP Distributed Tracing](mcp-tracing.md) — `wrap(session)`, `@mcp_endpoint`

### Frameworks
- [LangGraph](frameworks/langgraph.md) — `@langgraph_agent` decorator
- [CrewAI](frameworks/crewai.md) — `@crew_kickoff` decorator
- [LlamaIndex](frameworks/llamaindex.md) — `@llamaindex_agent` decorator

### Operations
- [Metrics Reference](metrics.md) — All Prometheus counters, histograms, gauges
- [Dashboards](dashboards.md) — 7 Grafana dashboards
- [Server](server.md) — Collector architecture, endpoints, sampling, OTLP
- [Configuration](configuration.md) — Client & server config, environment variables

### Reference
- [Architecture](architecture-responsibilities.md) — Responsibility boundaries
- [Environment Variables](environment-variables.md) — Complete env var reference
- [Contributing Adapters](contributing-adapters.md) — Write your own adapter
