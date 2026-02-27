---
layout: default
title: Getting Started
nav_order: 2
---

# Getting Started

## Installation

### Client Library Only

```bash
pip install rastir
```

### With OpenTelemetry Support

```bash
pip install rastir[otel]
```

### With Collector Server

```bash
pip install rastir[server]
```

### Everything (Development)

```bash
pip install rastir[all]
```

---

## Quick Start

### 1. Configure Rastir

```python
from rastir import configure

configure(
    service="my-llm-app",
    env="production",
    push_url="http://localhost:8080",  # Collector server URL
)
```

Configuration can also be set via environment variables:

```bash
export RASTIR_SERVICE=my-llm-app
export RASTIR_ENV=production
export RASTIR_PUSH_URL=http://localhost:8080
```

### 2. Decorate Your Functions

```python
from rastir import trace, agent, llm, tool, retrieval

@trace
def handle_request(query: str) -> str:
    """Root entry point — creates a trace span."""
    return run_agent(query)

@agent(agent_name="research_agent")
def run_agent(query: str) -> str:
    """Agent span — sets agent identity for child spans."""
    docs = search(query)
    return summarize(query, docs)

@retrieval
def search(query: str) -> list[str]:
    """Retrieval span — tracks vector/document lookups."""
    return vector_store.similarity_search(query)

@llm
def summarize(query: str, docs: list[str]) -> str:
    """LLM span — auto-extracts model, tokens, provider."""
    return openai.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "user", "content": f"{query}\n\nContext: {docs}"}],
    )
```

### 3. Start the Collector Server

```bash
# Start the server
rastir-server

# Or with Python
python -m rastir.server
```

### 4. View Metrics

Open `http://localhost:8080/metrics` in your browser or configure Prometheus to scrape it:

```yaml
# prometheus.yml
scrape_configs:
  - job_name: rastir
    static_configs:
      - targets: ['localhost:8080']
```

---

## What Gets Captured

### Metrics (Prometheus)

| Metric | Type | Description |
|--------|------|-------------|
| `rastir_spans_ingested_total` | Counter | Total spans by service, env, type, status |
| `rastir_llm_calls_total` | Counter | LLM calls by model, provider, agent |
| `rastir_tokens_input_total` | Counter | Input tokens by model, provider, agent |
| `rastir_tokens_output_total` | Counter | Output tokens by model, provider, agent |
| `rastir_tool_calls_total` | Counter | Tool invocations by tool_name, agent |
| `rastir_retrieval_calls_total` | Counter | Retrieval operations by agent |
| `rastir_errors_total` | Counter | Error spans by type and error category |
| `rastir_duration_seconds` | Histogram | Span duration by service, env, type |
| `rastir_tokens_per_call` | Histogram | Tokens per LLM call by model, provider |

### Traces

Each decorated function creates a span with:
- Trace ID, span ID, parent span ID
- Start time, duration
- Status (OK/ERROR)
- Span type (trace, agent, llm, tool, retrieval)
- Attributes (model, provider, tokens, agent name, etc.)
- Error events with exception details

---

## Docker Deployment

```bash
# Build
docker build -t rastir-server .

# Run
docker run -p 8080:8080 rastir-server
```

With custom config:

```bash
docker run -p 8080:8080 \
  -e RASTIR_SERVER_HOST=0.0.0.0 \
  -e RASTIR_SERVER_PORT=8080 \
  -e RASTIR_SERVER_SAMPLING_ENABLED=true \
  -e RASTIR_SERVER_SAMPLING_RATE=0.1 \
  rastir-server
```

---

## Async Support

All decorators work with both sync and async functions:

```python
@llm
async def ask_model(query: str) -> str:
    return await openai_async_client.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "user", "content": query}],
    )
```

## Streaming Support

`@llm` auto-detects generators and async generators:

```python
@llm
def stream_response(query: str):
    return openai.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "user", "content": query}],
        stream=True,
    )

# Usage — consume the stream normally
for chunk in stream_response("Hello"):
    print(chunk)
# Metrics and spans are recorded after the stream completes
```

---

## Graceful Shutdown

Stop the background exporter cleanly:

```python
from rastir import stop_exporter

# At application shutdown
stop_exporter(timeout=5.0)
```
