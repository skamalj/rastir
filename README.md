# Rastir

<p align="center">
  <img src="https://raw.githubusercontent.com/skamalj/rastir/main/rastir_small.png" alt="Rastir" width="200">
</p>

<p align="center">
  <strong>LLM & Agent Observability for Python</strong><br>
  Structured tracing and Prometheus metrics via decorators — no monkey-patching, no vendor lock-in.
</p>

<p align="center">
  <a href="https://pypi.org/project/rastir/"><img alt="PyPI" src="https://img.shields.io/pypi/v/rastir"></a>
  <a href="https://pypi.org/project/rastir/"><img alt="Python" src="https://img.shields.io/pypi/pyversions/rastir"></a>
  <a href="https://skamalj.github.io/rastir/"><img alt="Docs" src="https://img.shields.io/badge/docs-GitHub%20Pages-blue"></a>
  <a href="https://github.com/skamalj/rastir/blob/main/LICENSE"><img alt="License" src="https://img.shields.io/github/license/skamalj/rastir"></a>
  <a href="https://github.com/skamalj/rastir"><img alt="GitHub" src="https://img.shields.io/github/stars/skamalj/rastir?style=social"></a>
</p>

---

## Why Rastir?

Most LLM observability tools require SDK wrappers, monkey-patching, or vendor-specific clients. **Rastir takes a different approach:**

- **Decorators, not wrappers** — add `@llm`, `@agent`, `@tool` to your existing functions. No code rewrites.
- **Adapters, not monkey-patches** — Rastir inspects return values to extract model, tokens, and provider metadata. Works with any SDK version.
- **Self-hosted collector** — a lightweight FastAPI server you own. Prometheus metrics out of the box, OTLP export to Tempo/Jaeger if you want it.
- **Zero external infrastructure** — no database, no Redis, no Kafka. The collector is stateless and runs in a single container.

```
Your Python App                          Rastir Collector
┌──────────────────────┐     HTTP POST    ┌──────────────────────────┐
│  @agent              │ ───────────────▸ │  FastAPI ingestion       │
│    @llm (OpenAI)     │   span batches   │  ├─ Prometheus /metrics  │
│    @tool (search)    │                  │  ├─ Trace store /traces  │
│    @retrieval (RAG)  │                  │  └─ OTLP → Tempo/Jaeger  │
└──────────────────────┘                  └──────────────────────────┘
        decorators                              collector server
```

## Supported Providers

| Provider | Auto-detection | Tokens | Model | Streaming |
|----------|:-:|:-:|:-:|:-:|
| **OpenAI** | ✅ | ✅ | ✅ | ✅ |
| **Azure OpenAI** | ✅ | ✅ | ✅ | ✅ |
| **Anthropic** | ✅ | ✅ | ✅ | ✅ |
| **AWS Bedrock** | ✅ | ✅ | ✅ | ✅ |
| **Google Gemini** | ✅ | ✅ | ✅ | ✅ |
| **Cohere** | ✅ | ✅ | ✅ | — |
| **Mistral** | ✅ | ✅ | ✅ | ✅ |
| **Groq** | ✅ | ✅ | ✅ | ✅ |
| **LangChain** | ✅ | ✅ | ✅ | ✅ |
| **LangGraph** | ✅ | ✅ | ✅ | ✅ |
| **LlamaIndex** | ✅ | ✅ | ✅ | ✅ |
| **CrewAI** | ✅ | ✅ | — | — |

Adapters are priority-ordered and composable: LangGraph → LangChain → OpenAI resolution happens automatically.

## Installation

```bash
pip install rastir              # Client library (decorators + HTTP push)
pip install rastir[server]      # + Collector server (FastAPI, Prometheus, OTLP)
pip install rastir[all]         # Everything including dev tools
```

## Quick Start

### 1. Instrument your code (3 lines to add)

```python
from rastir import configure, agent, llm, tool, retrieval

configure(
    service="my-app",
    push_url="http://localhost:8080/v1/telemetry",
)

@agent(agent_name="research_agent")
def run_research(query: str) -> str:
    context = fetch_docs(query)
    return ask_llm(query, context)

@retrieval
def fetch_docs(query: str) -> list[str]:
    return vector_db.search(query)           # auto-tracked

@llm(model="gpt-4o", provider="openai")
def ask_llm(query: str, context: list[str]) -> str:
    return openai.chat(messages=[...])        # tokens & model extracted automatically
```

### 2. Start the collector

```bash
rastir-server                              # default: 0.0.0.0:8080
# or
docker run -p 8080:8080 rastir-server
```

### 3. Query metrics

```bash
curl http://localhost:8080/metrics          # Prometheus format
curl http://localhost:8080/v1/traces        # JSON trace store
```

**That's it.** Prometheus scrapes `/metrics`, you build Grafana dashboards, and optionally forward spans to Tempo or Jaeger via OTLP.

### What you get in Prometheus

```
# Token usage by model
rastir_tokens_input_total{model="gpt-4o",provider="openai",agent="research_agent"} 1250
rastir_tokens_output_total{model="gpt-4o",provider="openai",agent="research_agent"} 380

# Latency percentiles
rastir_duration_seconds_bucket{span_type="llm",le="0.5"} 12
rastir_duration_seconds_bucket{span_type="llm",le="1.0"} 45

# Tool & retrieval call rates
rastir_tool_calls_total{tool_name="web_search",agent="research_agent"} 89
rastir_retrieval_calls_total{agent="research_agent"} 156
```

## Nested Spans

Rastir automatically links parent–child relationships for agent call trees:

```python
@agent(agent_name="supervisor")
def supervisor(task):
    plan = planner(task)            # nested agent
    return executor(plan)

@agent(agent_name="planner")
def planner(task):
    return ask_llm(task)            # nested LLM call

@llm(model="gpt-4o")
def ask_llm(prompt):
    return openai.chat(messages=[...])
```

```
supervisor (agent, 3200ms)
├── planner (agent, 1100ms)
│   └── ask_llm (llm, 980ms) → model=gpt-4o, tokens_in=150, tokens_out=85
└── executor (agent, 2000ms)
    ├── web_search (tool, 450ms)
    └── ask_llm (llm, 1200ms) → model=gpt-4o, tokens_in=320, tokens_out=200
```

## Works with LangGraph

```python
from langgraph.prebuilt import create_react_agent

app = create_react_agent(ChatOpenAI(model="gpt-4o-mini"), tools=[search, calc])

@agent(agent_name="react_agent")
def run(query: str):
    return app.invoke({"messages": [HumanMessage(query)]})
    # Rastir auto-detects LangGraph state → LangChain messages → OpenAI response
    # Extracts: model, tokens, tool calls, message counts — zero config
```

## Generic Object Wrapper

Instrument any object without decorator access using `rastir.wrap()`:

```python
import rastir

# Wrap a Redis client, vector store, or any infrastructure component
wrapped_cache = rastir.wrap(redis_client, name="redis")
wrapped_cache.get("key")       # creates INFRA span: "redis.get"
wrapped_cache.set("key", val)  # creates INFRA span: "redis.set"

# Wrap with filtering
wrapped_db = rastir.wrap(db_client, name="postgres",
                         include=["query", "execute"],
                         span_type="tool")
```

- Supports sync + async methods
- Preserves `isinstance()` behaviour
- Prevents double-wrapping
- Configurable `span_type`: infra, tool, llm, trace, agent, retrieval

## Bedrock Guardrail Observability

Rastir automatically detects and tracks AWS Bedrock guardrails:

```python
@llm
def call_bedrock(prompt: str):
    return bedrock.converse(
        modelId="anthropic.claude-3-sonnet",
        messages=[...],
        guardrailIdentifier="my-guardrail",  # auto-detected
        guardrailVersion="1",
    )
```

Produces metrics:
```
rastir_guardrail_requests_total{guardrail_id="my-guardrail",provider="bedrock"} 42
rastir_guardrail_violations_total{guardrail_action="GUARDRAIL_INTERVENED",model="claude-3"} 3
```

## Key Metrics at a Glance

| Metric | Type | What it tracks |
|--------|------|----------------|
| `rastir_llm_calls_total` | Counter | LLM invocations by model, provider, agent |
| `rastir_tokens_input_total` | Counter | Input token consumption |
| `rastir_tokens_output_total` | Counter | Output token consumption |
| `rastir_duration_seconds` | Histogram | Latency with P50/P95/P99 + exemplars |
| `rastir_tool_calls_total` | Counter | Tool invocations by name and agent |
| `rastir_errors_total` | Counter | Failures by span type and error type |
| `rastir_guardrail_requests_total` | Counter | LLM calls with guardrail config |
| `rastir_guardrail_violations_total` | Counter | Guardrail interventions by action/category |
| `rastir_queue_size` | Gauge | Collector backpressure indicator |

Full metrics reference → [Server Documentation](https://skamalj.github.io/rastir/server)

## Server Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/telemetry` | Ingest span batches |
| GET | `/metrics` | Prometheus exposition |
| GET | `/v1/traces` | Query trace store |
| GET | `/health` | Liveness probe |
| GET | `/ready` | Readiness probe (queue pressure) |

## Configuration

Configure via `configure()` call or environment variables:

```python
configure(
    service="my-app",
    env="production",
    push_url="http://collector:8080/v1/telemetry",
    api_key="secret",
    batch_size=100,
    flush_interval=5,
)
```

Or equivalently:
```bash
export RASTIR_SERVICE=my-app
export RASTIR_ENV=production
export RASTIR_PUSH_URL=http://collector:8080/v1/telemetry
```

Full configuration reference → [Configuration Documentation](https://skamalj.github.io/rastir/configuration)

## Project Structure

```
src/rastir/
├── __init__.py          # Public API: configure, trace, agent, llm, tool, retrieval, wrap
├── config.py            # GlobalConfig, configure()
├── context.py           # Span & agent context (ContextVar-based)
├── decorators.py        # All decorator implementations
├── wrapper.py           # rastir.wrap() generic object wrapper
├── spans.py             # SpanRecord data model
├── queue.py             # Bounded in-memory span queue
├── transport.py         # TelemetryClient + BackgroundExporter
├── adapters/            # 15 adapters: OpenAI, Azure, Anthropic, Bedrock, Gemini, Cohere, Mistral, Groq, LangChain, LangGraph, LlamaIndex, CrewAI, ...
└── server/              # FastAPI collector with Prometheus, trace store, OTLP export
```

## Development

```bash
pip install -e ".[all]"           # editable install with all extras
pytest                            # 411 tests (unit + integration)
ruff check src/ tests/            # linting
```

## Documentation

Full documentation at **[skamalj.github.io/rastir](https://skamalj.github.io/rastir/)**:

- [Getting Started](https://skamalj.github.io/rastir/getting-started) — Installation, quick start, nested spans
- [Decorators](https://skamalj.github.io/rastir/decorators) — `@trace`, `@agent`, `@llm`, `@tool`, `@retrieval`, `@metric`
- [Adapters](https://skamalj.github.io/rastir/adapters) — OpenAI, Azure, Anthropic, Bedrock, Gemini, Cohere, Mistral, Groq, LangChain, LangGraph, LlamaIndex, CrewAI
- [Server](https://skamalj.github.io/rastir/server) — Collector, metrics, histograms, exemplars, OTLP
- [Configuration](https://skamalj.github.io/rastir/configuration) — Client & server config reference
- [Contributing Adapters](https://skamalj.github.io/rastir/contributing-adapters) — Write your own adapter

## License

MIT — see [LICENSE](LICENSE) for details.
