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

- **One decorator per framework** — `@framework_agent` auto-detects the framework; or use `@langgraph_agent`, `@crew_kickoff`, `@llamaindex_agent`, `@adk_agent`, `@strands_agent` for explicit control
- **Adapters, not patches** — 15 adapters extract model, tokens, and provider from return values. Works across SDK versions
- **Two-phase enrichment** — metadata captured from function kwargs *before* the call, refined from the response *after*. Survives API failures
- **Self-hosted collector** — a lightweight FastAPI server you own. Prometheus metrics, OTLP export, zero external infrastructure

---

## Quick Example

```python
from rastir import configure, framework_agent

configure(service="my-app", push_url="http://localhost:8080")

@framework_agent(agent_name="react_agent")
def run(graph, query):
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

- **Framework decorators** — `@framework_agent` (auto-detect), plus `@langgraph_agent`, `@crew_kickoff`, `@llamaindex_agent`, `@adk_agent`, `@strands_agent` with automatic LLM/tool discovery
- **15 provider adapters** — OpenAI, Azure, Anthropic, Bedrock, Gemini, Cohere, Mistral, Groq, LangChain, LangGraph, LlamaIndex, CrewAI — auto-detected
- **Two-phase enrichment** — metadata captured from function kwargs *before* the call, refined from response *after*. Survives API failures
- **MCP distributed tracing** — `wrap(session)` propagates trace context across MCP tool boundaries — same `trace_id` links client and server
- **Generic `wrap()`** — instrument any object (Redis, databases, MCP sessions) without decorator access
- **Cost observability** — per-model USD cost tracking with `PricingRegistry`, pricing profiles, cost histograms
- **Streaming TTFT** — Time-To-First-Token measurement on streaming LLM calls
- **Guardrail tracking** — automatic AWS Bedrock guardrail violation metrics
- **Error normalisation** — exceptions mapped to 6 fixed categories: timeout, rate_limit, validation_error, provider_error, internal_error, unknown
- **Self-hosted collector** — FastAPI server you own. Prometheus `/metrics`, in-memory trace store, OTLP export to Tempo/Jaeger
- **SRE budgets & burn rates** — error and cost budget tracking via Prometheus recording rules — SLO status, burn rates, days-to-exhaustion
- **7 Grafana dashboards** — LLM Performance, Agent-Tool, Cost-TTFT, Evaluation, Guardrail, SRE Budgets, System Health

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│  Your Application                               │
│  ┌──────────────────────────────────────────┐   │
│  │  @framework_agent (auto-detect)         │   │
│  │  @langgraph_agent / @crew_kickoff /      │   │
│  │  @llamaindex_agent / @adk_agent /         │   │
│  │  @strands_agent                           │   │
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
- [ADK (Google)](frameworks/adk.md) — `@adk_agent` decorator
- [Strands](frameworks/strands.md) — `@strands_agent` decorator

### Operations
- [Metrics Reference](metrics.md) — All Prometheus counters, histograms, gauges
- [Dashboards](dashboards.md) — 7 Grafana dashboards
- [Server](server.md) — Collector architecture, endpoints, sampling, OTLP
- [Configuration](configuration.md) — Client & server config, environment variables

### Deployment
- [Deployment Guide](deployment.md) — Docker Compose, AWS, Azure, GCP, Kubernetes

### Reference
- [Architecture](architecture-responsibilities.md) — Responsibility boundaries
- [Environment Variables](environment-variables.md) — Complete env var reference
- [Contributing Adapters](contributing-adapters.md) — Write your own adapter
