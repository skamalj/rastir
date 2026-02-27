# Rastir

<p align="center">
  <img src="rastir.png" alt="Rastir" width="200">
</p>

LLM & Agent Observability — structured tracing and Prometheus metrics via Python decorators, with a built-in collector server.

## Features

- **Decorator-based instrumentation** — `@trace`, `@agent`, `@llm`, `@tool`, `@retrieval`, `@metric`
- **Adapter-based metadata extraction** — OpenAI, Anthropic, Bedrock, LangChain (no monkey-patching)
- **Built-in collector server** — FastAPI ingestion, Prometheus `/metrics`, in-memory trace store
- **OTLP forwarding** — optional export to Tempo, Jaeger, or any OTLP-compatible backend
- **Multi-tenant support** — tenant isolation via configurable HTTP header
- **Zero external dependencies** — no database, no Redis, no Kafka; fully stateless

## Installation

```bash
pip install rastir            # Client library only (decorators + HTTP push)
pip install rastir[otel]      # + OpenTelemetry SDK & OTLP exporter
pip install rastir[server]    # + Collector server (FastAPI, Prometheus, OTLP)
pip install rastir[all]       # Everything including dev tools
```

## Quick Start

### Client Instrumentation

```python
from rastir import configure, trace, agent, llm, tool, retrieval

configure(
    service="my-app",
    env="production",
    push_url="http://localhost:8080/v1/telemetry",
)

@agent(agent_name="research_agent")
def run_research(query: str) -> str:
    context = fetch_docs(query)
    return ask_llm(query, context)

@retrieval
def fetch_docs(query: str) -> list[str]:
    return vector_db.search(query)

@llm(model="gpt-4o", provider="openai")
def ask_llm(query: str, context: list[str]) -> str:
    return openai.chat(messages=[...])
```

### Collector Server

```bash
# Start with defaults (0.0.0.0:8080)
rastir-server

# Or via module
python -m rastir.server
```

### Docker

```bash
docker build -t rastir-server .
docker run -p 8080:8080 rastir-server
```

## Server Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/telemetry` | Ingest span batches from client libraries |
| GET | `/metrics` | Prometheus exposition endpoint |
| GET | `/v1/traces` | Query in-memory trace store |
| GET | `/health` | Liveness probe |
| GET | `/ready` | Readiness probe (queue pressure) |

## Configuration

### Client (Environment Variables)

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVICE` | `unknown` | Service name |
| `RASTIR_ENV` | `development` | Environment |
| `RASTIR_PUSH_URL` | — | Collector URL |
| `RASTIR_API_KEY` | — | Optional auth header |
| `RASTIR_BATCH_SIZE` | `100` | Spans per batch |
| `RASTIR_FLUSH_INTERVAL` | `5` | Seconds between flushes |
| `RASTIR_MAX_RETRIES` | `3` | Retry count for transient failures |
| `RASTIR_SHUTDOWN_TIMEOUT` | `5.0` | Max seconds for graceful shutdown |

### Server (Environment Variables or YAML)

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVER_HOST` | `0.0.0.0` | Bind address |
| `RASTIR_SERVER_PORT` | `8080` | Bind port |
| `RASTIR_SERVER_MAX_TRACES` | `10000` | Trace store ring buffer size |
| `RASTIR_SERVER_MAX_QUEUE_SIZE` | `50000` | Ingestion queue limit |
| `RASTIR_SERVER_OTLP_ENDPOINT` | — | OTLP backend URL |

## Prometheus Metrics

| Metric | Type | Labels |
|--------|------|--------|
| `rastir_spans_ingested_total` | Counter | service, env, span_type, status |
| `rastir_llm_calls_total` | Counter | service, env, model, provider, agent |
| `rastir_tokens_input_total` | Counter | service, env, model, provider, agent |
| `rastir_tokens_output_total` | Counter | service, env, model, provider, agent |
| `rastir_tool_calls_total` | Counter | service, env, tool_name, agent |
| `rastir_retrieval_calls_total` | Counter | service, env, agent |
| `rastir_errors_total` | Counter | service, env, span_type, error_type |
| `rastir_duration_seconds` | Histogram | service, env, span_type |
| `rastir_tokens_per_call` | Histogram | service, env, model, provider |
| `rastir_ingestion_rejections_total` | Counter | service, env |
| `rastir_export_failures_total` | Counter | service, env |
| `rastir_queue_size` | Gauge | — |

## Development

### Prerequisites

- Python 3.9+
- [Miniconda](https://docs.conda.io/en/latest/miniconda.html) or [Anaconda](https://www.anaconda.com/)

### Setup

```bash
conda create -n llmobserve python=3.12 -y
conda run -n llmobserve pip install -e ".[all]"
```

### Running Tests

```bash
conda run -n llmobserve pytest           # all 201 tests
conda run -n llmobserve pytest tests/test_integration.py -v  # integration only
```

### Code Quality

```bash
conda run -n llmobserve ruff check src/ tests/
conda run -n llmobserve mypy src/
```

## Project Structure

```
src/rastir/
├── __init__.py          # Public API: configure, trace, agent, llm, tool, retrieval
├── config.py            # GlobalConfig, ExporterConfig, configure()
├── context.py           # Span & agent context (ContextVar-based)
├── decorators.py        # @trace, @agent, @llm, @tool, @retrieval, @metric
├── spans.py             # SpanRecord data model
├── queue.py             # Bounded in-memory span queue
├── transport.py         # TelemetryClient + BackgroundExporter
├── adapters/
│   ├── base.py          # BaseAdapter interface
│   ├── registry.py      # Priority-based adapter resolution
│   ├── openai.py        # OpenAI adapter
│   ├── anthropic.py     # Anthropic adapter
│   ├── bedrock.py       # AWS Bedrock adapter
│   ├── langchain.py     # LangChain framework adapter
│   ├── retrieval.py     # Retrieval adapter
│   ├── tool.py          # Tool adapter
│   └── fallback.py      # Fallback (always matches)
└── server/
    ├── __main__.py      # python -m rastir.server support
    ├── app.py           # FastAPI application + routes
    ├── config.py        # Server config (YAML + env vars)
    ├── metrics.py       # MetricsRegistry (Prometheus)
    ├── ingestion.py     # IngestionWorker (async queue consumer)
    ├── trace_store.py   # In-memory ring buffer trace store
    └── otlp_exporter.py # OTLPForwarder (BatchSpanProcessor)
```

## Documentation

- [Requirements](requirements.md)
- [Adapter Requirements](adapters_requirements.md)
- [Configuration Requirements](configuration_requirements.md)
- [Server Requirements](server_requirement.md)
- [Deployment Requirements](deployment_requirements.md)
