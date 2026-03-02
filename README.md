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
- **Two-phase enrichment** — model/provider metadata is captured from function *arguments* before the call and refined from the *response* after. If the API call fails, metadata still survives.
- **Self-hosted collector** — a lightweight FastAPI server you own. Prometheus metrics out of the box, OTLP export to Tempo/Jaeger if you want it.
- **Zero external infrastructure** — no database, no Redis, no Kafka. The collector is stateless and runs in a single container.

```
Your Python App                          Rastir Collector
┌──────────────────────────────┐         ┌──────────────────────────────┐
│  @agent                      │  HTTP   │  FastAPI ingestion            │
│    @llm (OpenAI)             │ ──────▸ │  ├─ Prometheus /metrics       │
│    @tool (search)            │  spans  │  ├─ Trace store /v1/traces    │
│    @retrieval (RAG)          │         │  ├─ Sampling & backpressure   │
│                              │         │  └─ OTLP → Tempo/Jaeger      │
│  Two-phase enrichment:       │         │                                │
│    request args → response   │         │  Defence-in-depth:             │
│                              │         │    cardinality guards          │
│  wrap(obj, name="cache")     │         │    error normalisation         │
└──────────────────────────────┘         │    bounded enum validation     │
        decorators + wrap()              └──────────────────────────────┘
```

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

15 adapters are priority-ordered and composable: LangGraph → LangChain → OpenAI resolution happens automatically.

**Request-phase enrichment:** For provider adapters, model/provider metadata is extracted from function kwargs (e.g., `model="gpt-4o"`) *before* the API call. If the call fails, the span still contains the model and provider.

## MCP Distributed Tracing

Rastir supports distributed tracing across MCP (Model Context Protocol) tool boundaries. Trace context flows automatically from client to server via tool arguments — no `_meta`, no HTTP headers.

**Server side** — the MCP server must call `configure()` independently to push its server-side spans to the collector:

```python
# ── MCP Server (separate process) ─────────────────
from rastir import configure, mcp_endpoint

configure(service="tool-server", push_url="http://localhost:8080")

@mcp.tool()
@mcp_endpoint
async def search(query: str) -> str:
    return db.search(query)       # server span created with remote="false"
```

**Client side** — wrap the MCP session with `wrap_mcp()`:

```python
# ── Client (your agent process) ───────────────────
from rastir import configure, agent_span, wrap_mcp

configure(service="my-agent", push_url="http://localhost:8080")

@agent_span(agent_name="my_agent")
async def run():
    async with streamable_http_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            session = wrap_mcp(session)              # one line — trace propagation enabled
            tools = await session.list_tools()       # pass to any framework
            result = await session.call_tool("search", {"query": "hello"})
            # client span created with remote="true", trace context injected
```

> **Both processes must call `configure(push_url=...)`** — the client pushes client spans, the server pushes server spans. Both arrive at the same collector and are linked by `trace_id`.

Trace topology:
```
Agent Span
└── Tool Client Span  (remote="true",  model/provider inherited)
      └── Tool Server Span (remote="false", same trace_id)
```

Full MCP documentation → [MCP Distributed Tracing](https://skamalj.github.io/rastir/mcp-tracing)

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

# Error tracking with normalised categories
rastir_errors_total{span_type="llm",error_type="rate_limit"} 7
rastir_errors_total{span_type="llm",error_type="timeout"} 3
```

### Cost Observability & TTFT (V6)

Enable client-side cost calculation and streaming TTFT measurement:

```python
from rastir import configure, PricingRegistry
from rastir.config import get_pricing_registry

configure(
    service="my-app",
    push_url="http://localhost:8080/v1/telemetry",
    enable_cost_calculation=True,
    pricing_profile="production_2025_q1",
    enable_ttft=True,
)

# Register model pricing (USD per 1M tokens)
registry = get_pricing_registry()
registry.register("openai", "gpt-4o", input_price=2.50, output_price=10.00)
registry.register("anthropic", "claude-sonnet-4-20250514", input_price=3.00, output_price=15.00)
```

Or load pricing from a JSON file:
```python
configure(
    enable_cost_calculation=True,
    pricing_source="/path/to/pricing.json",
)
```

```json
{
  "openai": {
    "gpt-4o": {"input_price": 2.50, "output_price": 10.00},
    "gpt-4o-mini": {"input_price": 0.15, "output_price": 0.60}
  }
}
```

**What you get:**
```
# Cost tracking
rastir_cost_total{model="gpt-4o",provider="openai",pricing_profile="production_2025_q1"} 1.25
rastir_cost_per_call_usd_bucket{model="gpt-4o",le="0.01"} 45

# Streaming TTFT
rastir_ttft_seconds_bucket{model="gpt-4o",provider="openai",le="0.5"} 38

# Pricing gaps
rastir_pricing_missing_total{model="custom-model",provider="openai"} 3
```

## Two-Phase Enrichment

Rastir captures metadata in two phases to ensure observability even when API calls fail:

```
Phase 1 (request): Scan function kwargs for model/provider
  └─ e.g., model="gpt-4o" extracted before the call

Phase 2 (response): Adapter pipeline extracts from return value
  └─ Concrete response values override request-phase guesses
  └─ If call raises, request-phase metadata survives
```

Example — failed API call still produces useful metrics:

```python
@llm
def ask_model(query: str):
    return openai.chat.completions.create(
        model="gpt-4o",          # ← captured in Phase 1
        messages=[...],
    )
    # If this raises RateLimitError, the span still records:
    #   model="gpt-4o", provider="openai", status="ERROR"
    #   error_type="rate_limit"
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

## Works with CrewAI

```python
from crewai import Agent, Task, Crew, LLM

crewai_llm = LLM(model="gemini/gemini-2.5-flash", api_key="...")
researcher = Agent(role="Researcher", goal="Research topics", llm=crewai_llm, tools=[...])
task = Task(description="Research AI trends", expected_output="Summary", agent=researcher)
crew = Crew(agents=[researcher], tasks=[task])

@agent(agent_name="crewai_agent")
def run():
    @llm(model="gemini-2.5-flash", provider="gemini")
    def invoke():
        return crew.kickoff()
        # Rastir detects CrewOutput → extracts crewai_task_count,
        # crewai_total_tokens, crewai_successful_requests, tokens_input/output
    return invoke()
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

Guardrail labels are **cardinality-guarded** on both client and server side:
- `guardrail_category` is validated against a bounded enum (CONTENT_POLICY, TOPIC_POLICY, etc.)
- `guardrail_action` is validated against a bounded enum (GUARDRAIL_INTERVENED, NONE)
- Unknown values are replaced with `__cardinality_overflow__`

## Error Normalisation

Raw exception types are normalised into six fixed categories to prevent label explosion:

| Category | Example exceptions |
|----------|--------------------|
| `timeout` | `TimeoutError`, `httpx.ReadTimeout`, `openai.APITimeoutError` |
| `rate_limit` | `RateLimitError`, `openai.RateLimitError`, `anthropic.RateLimitError` |
| `validation_error` | `ValueError`, `TypeError`, `pydantic.ValidationError` |
| `provider_error` | `openai.APIError`, `anthropic.APIStatusError`, `botocore.ClientError` |
| `internal_error` | `RuntimeError`, `Exception` |
| `unknown` | Anything else |

## Key Metrics at a Glance

| Metric | Type | What it tracks |
|--------|------|----------------|
| `rastir_llm_calls_total` | Counter | LLM invocations by model, provider, agent |
| `rastir_tokens_input_total` | Counter | Input token consumption |
| `rastir_tokens_output_total` | Counter | Output token consumption |
| `rastir_duration_seconds` | Histogram | Latency with P50/P95/P99 + exemplars |
| `rastir_tokens_per_call` | Histogram | Token distribution per LLM call |
| `rastir_tool_calls_total` | Counter | Tool invocations by name, agent, model, provider |
| `rastir_retrieval_calls_total` | Counter | Retrieval operations by agent |
| `rastir_errors_total` | Counter | Failures by span type and normalised error type |
| `rastir_guardrail_requests_total` | Counter | LLM calls with guardrail config |
| `rastir_guardrail_violations_total` | Counter | Guardrail interventions by action/category |
| `rastir_cost_total` | Counter | Accumulated USD cost by model/provider/pricing_profile |
| `rastir_cost_per_call_usd` | Histogram | Cost distribution per LLM call |
| `rastir_pricing_missing_total` | Counter | LLM calls where pricing entry was not found |
| `rastir_ttft_seconds` | Histogram | Time-To-First-Token for streaming LLM calls |
| `rastir_spans_sampled_total` | Counter | Spans retained after sampling |
| `rastir_spans_dropped_by_sampling_total` | Counter | Spans dropped by sampling |
| `rastir_backpressure_warnings_total` | Counter | Queue soft-limit warnings |
| `rastir_ingestion_rate` | Gauge | Spans per second throughput |
| `rastir_queue_utilization_percent` | Gauge | Collector backpressure indicator |

Full metrics reference → [Server Documentation](https://skamalj.github.io/rastir/server)

## Server Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/telemetry` | Ingest span batches |
| GET | `/metrics` | Prometheus exposition |
| GET | `/v1/traces` | Query trace store |
| GET | `/v1/traces/{trace_id}` | Get spans for a specific trace |
| GET | `/health` | Liveness probe |
| GET | `/ready` | Readiness probe (queue pressure) |

## Server Features

- **Sampling** — probabilistic + error-always-retain + latency threshold (metrics always recorded regardless)
- **Backpressure** — soft/hard queue limits with reject or drop-oldest mode
- **Rate limiting** — per-IP and per-service RPM limits
- **Multi-tenant** — inject tenant label from HTTP header
- **Exemplars** — trace_id linked to histogram observations for Grafana → Jaeger drill-down
- **OTLP export** — forward spans to Tempo, Jaeger, or any OTLP backend
- **Cardinality guards** — per-dimension caps (model: 50, provider: 10, tool: 200, agent: 200, etc.)
- **Graceful shutdown** — drains queue and flushes exporter before exit

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
├── decorators.py        # All decorator implementations + two-phase enrichment
├── remote.py            # MCP distributed tracing: wrap_mcp, mcp_endpoint
│                        #   argument-based trace propagation via session proxy
├── wrapper.py           # rastir.wrap() generic object wrapper
├── spans.py             # SpanRecord data model
├── queue.py             # Bounded in-memory span queue
├── transport.py         # TelemetryClient + BackgroundExporter
├── adapters/            # 15 adapters: OpenAI, Azure, Anthropic, Bedrock, Gemini,
│                        #   Cohere, Mistral, Groq, LangChain, LangGraph, LlamaIndex, CrewAI
│   └── registry.py      # Adapter resolution pipeline + request-phase scanning
└── server/              # FastAPI collector
    ├── app.py           # Server factory, routes, lifespan
    ├── config.py        # Server configuration (YAML + env vars)
    ├── metrics.py       # MetricsRegistry — Prometheus counters/histograms/gauges
    ├── ingestion.py     # IngestionWorker — queue → record_span() → store/export
    └── trace_store.py   # In-memory trace store with LRU eviction
```

## Development

```bash
pip install -e ".[all]"           # editable install with all extras
pytest                            # 232+ unit/mock tests, 36+ integration tests
ruff check src/ tests/            # linting
```

## Grafana Dashboards

Rastir ships six pre-built Grafana dashboards in `grafana/dashboards/`:

| Dashboard | Description |
|-----------|-------------|
| **LLM Performance** | Token usage, latency percentiles, throughput by model, error tracking |
| **Agent & Tool** | Agent execution patterns, tool calls with model/provider context |
| **Evaluation** | Eval runs/success/failures, scores by type and model, queue health |
| **Guardrail** | Guardrail violations by category and model, request volumes |
| **System Health** | Ingestion rate, queue pressure, memory, backpressure, OTLP export health |
| **Cost & TTFT** | Cost per model/agent, burn rate, P95 cost, TTFT percentiles, pricing gaps |

All dashboards include template variables for filtering by service, environment, model, provider, and agent. Import via Grafana UI or API.

Full dashboard documentation → [Dashboards](https://skamalj.github.io/rastir/dashboards)

## Documentation

Full documentation at **[skamalj.github.io/rastir](https://skamalj.github.io/rastir/)**:

- [Getting Started](https://skamalj.github.io/rastir/getting-started) — Installation, quick start, nested spans
- [Decorators](https://skamalj.github.io/rastir/decorators) — `@trace`, `@agent`, `@llm`, `@tool`, `@retrieval`, `@metric`
- [MCP Distributed Tracing](https://skamalj.github.io/rastir/mcp-tracing) — `wrap_mcp()`, `@mcp_endpoint`
- [Adapters](https://skamalj.github.io/rastir/adapters) — 15 adapters with two-phase enrichment
- [Server](https://skamalj.github.io/rastir/server) — Collector, metrics, histograms, exemplars, OTLP, sampling
- [Configuration](https://skamalj.github.io/rastir/configuration) — Client & server config reference
- [Dashboards](https://skamalj.github.io/rastir/dashboards) — Six pre-built Grafana dashboards
- [Environment Variables](https://skamalj.github.io/rastir/environment-variables) — Complete env var reference
- [Contributing Adapters](https://skamalj.github.io/rastir/contributing-adapters) — Write your own adapter

## License

MIT — see [LICENSE](LICENSE) for details.
