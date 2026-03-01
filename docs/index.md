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

## Key Features

- **Six semantic decorators** — `@trace`, `@agent`, `@llm`, `@tool`, `@retrieval`, `@metric`
- **MCP distributed tracing** — `@trace_remote_tools`, `@mcp_endpoint`, and `mcp_to_langchain_tools()` for end-to-end tracing across MCP tool boundaries
- **15 adapters** — automatic model, token, and provider detection for OpenAI, Azure OpenAI, Anthropic, AWS Bedrock, Google Gemini, Cohere, Mistral, Groq, LangChain, LangGraph, LlamaIndex, and CrewAI
- **Two-phase enrichment** — model/provider extracted from function kwargs *before* the call, refined from the response *after*. Metadata survives even when API calls fail.
- **Generic object wrapper** — `rastir.wrap(obj)` instruments any object (Redis, databases, caches) without decorator access
- **Prometheus metrics** — duration histograms, token counters, error rates with normalised categories, cardinality-guarded labels
- **Guardrail observability** — automatic tracking of AWS Bedrock guardrail requests and violations with bounded enum validation
- **Error normalisation** — raw exceptions mapped to six fixed categories (timeout, rate_limit, validation_error, provider_error, internal_error, unknown)
- **OpenTelemetry traces** — full parent-child span hierarchy with OTLP export and exemplar support
- **Built-in collector server** — FastAPI-based server with in-memory trace store, sampling, backpressure, rate limiting, and exemplar support
- **Zero external dependencies for tracing** — no database, no Redis, no Kafka

---

## Quick Example

```python
from rastir import configure, trace, agent, llm, tool

configure(service="my-app", env="production", push_url="http://localhost:8080")

@agent(agent_name="qa_bot")
def answer_question(query: str) -> str:
    context = search_docs(query)
    return ask_llm(query, context)

@tool
def search_docs(query: str) -> list[str]:
    return vector_db.search(query, top_k=5)

@llm
def ask_llm(query: str, context: list[str]) -> str:
    return openai.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "user", "content": query}],
    )
```

That's it. Rastir automatically:
- Creates parent-child spans (`qa_bot → search_docs → ask_llm`)
- Extracts model name, token counts, and provider from the OpenAI response
- Emits `rastir_llm_calls_total`, `rastir_duration_seconds`, `rastir_tokens_input_total`, etc.
- Pushes span data to the collector server

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│  Your Application                               │
│  ┌──────────────────────────────────────────┐   │
│  │  @trace / @agent / @llm / @tool          │   │
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

- [Getting Started](getting-started.md) — Installation, configuration, first steps
- [Decorators](decorators.md) — Full decorator reference (`@trace`, `@agent`, `@llm`, `@tool`, `@retrieval`, `@metric`)
- [MCP Distributed Tracing](mcp-tracing.md) — `@trace_remote_tools`, `@mcp_endpoint`, `mcp_to_langchain_tools()`
- [Adapters](adapters.md) — 15 adapters with two-phase enrichment
- [Server](server.md) — Collector, metrics, guardrails, error normalisation, sampling
- [Configuration](configuration.md) — Client and server configuration reference
- [Dashboards](dashboards.md) — Five pre-built Grafana dashboards for LLM observability
- [Environment Variables](environment-variables.md) — Complete reference of all environment variables
- [Contributing Adapters](contributing-adapters.md) — How to write and register custom adapters
