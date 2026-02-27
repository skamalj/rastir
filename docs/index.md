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
- **Adapter-based metadata extraction** — automatic model, token, and provider detection for OpenAI, Anthropic, Bedrock, and LangChain
- **Prometheus metrics** — duration histograms, token counters, error rates, cardinality-guarded labels
- **OpenTelemetry traces** — full parent-child span hierarchy with OTLP export
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
- [Adapters](adapters.md) — How adapter-based metadata extraction works
- [Server](server.md) — Collector server configuration, endpoints, metrics
- [Configuration](configuration.md) — Client and server configuration reference
- [Contributing Adapters](contributing-adapters.md) — How to write and register custom adapters
