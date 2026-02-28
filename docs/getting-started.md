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
| `rastir_errors_total` | Counter | Error spans by type and normalised error category |
| `rastir_guardrail_requests_total` | Counter | Guardrail-enabled LLM calls |
| `rastir_guardrail_violations_total` | Counter | Guardrail interventions |
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

### Two-Phase Enrichment

Rastir captures model/provider metadata in two phases:

1. **Request phase** — before the API call, function kwargs are scanned for `model`, `model_id`, or `modelId` hints
2. **Response phase** — the adapter pipeline extracts metadata from the return value

If the API call raises an exception (rate limit, timeout, etc.), the request-phase metadata survives:

```python
@llm
def risky_call(query: str):
    return openai.chat.completions.create(
        model="gpt-4o",          # ← captured in request phase
        messages=[...],
    )
    # If RateLimitError is raised:
    #   span still has model="gpt-4o", provider="openai"
    #   error_type="rate_limit"
```

### Error Normalisation

Raw exception types are normalised into six categories for consistent metrics: `timeout`, `rate_limit`, `validation_error`, `provider_error`, `internal_error`, `unknown`. See the [Server documentation](server.md#error-type-normalisation) for the full mapping.

---

## Nested Spans — Agent Loop Example

Rastir automatically builds parent-child span trees from your call graph.
Here's a realistic **LangGraph-style agent** that loops over tool calls —
each iteration creates nested spans under the agent.

```
trace("handle_request")
 └─ agent("research_agent")          # agent loop
     ├─ llm("plan")                  # 1st LLM call → decides to use tools
     ├─ tool("web_search")           # tool execution
     ├─ tool("calculator")           # another tool
     ├─ llm("synthesize")            # 2nd LLM call → final answer
     └─ retrieval("fetch_sources")   # optional retrieval step
```

### Full Code

```python
from rastir import configure, trace, agent, llm, tool, retrieval

configure(
    service="research-assistant",
    env="production",
    push_url="http://localhost:8080",
)


# ── Root entry point ──────────────────────────────────
@trace
def handle_request(user_query: str) -> str:
    """Creates the top-level trace span."""
    return research_agent(user_query)


# ── Agent (may loop over LLM + tools) ────────────────
@agent(agent_name="research_agent")
def research_agent(query: str) -> str:
    """
    Agent span that orchestrates planning, tool use, and synthesis.
    All child calls (llm, tool, retrieval) become nested spans.
    """
    # Step 1: Ask the LLM to plan which tools to call
    plan = plan_step(query)

    # Step 2: Execute tools based on the plan
    results = {}
    if "web_search" in plan:
        results["web"] = web_search(plan["web_search"])
    if "calculator" in plan:
        results["calc"] = calculator(plan["calculator"])

    # Step 3: Retrieve supporting documents
    sources = fetch_sources(query)

    # Step 4: Synthesize a final answer
    return synthesize(query, results, sources)


# ── LLM calls ────────────────────────────────────────
@llm
def plan_step(query: str) -> dict:
    """LLM span — decides which tools to invoke."""
    response = openai.chat.completions.create(
        model="gpt-4",
        messages=[
            {"role": "system", "content": "Decide which tools to call."},
            {"role": "user", "content": query},
        ],
    )
    return parse_tool_calls(response)


@llm
def synthesize(query: str, tool_results: dict, sources: list[str]) -> str:
    """LLM span — combines tool output + sources into a final answer."""
    context = f"Tool results: {tool_results}\nSources: {sources}"
    response = openai.chat.completions.create(
        model="gpt-4",
        messages=[
            {"role": "system", "content": "Answer using the provided context."},
            {"role": "user", "content": f"{query}\n\n{context}"},
        ],
    )
    return response.choices[0].message.content


# ── Tools ─────────────────────────────────────────────
@tool
def web_search(search_query: str) -> list[str]:
    """Tool span — tracked as rastir_tool_calls_total{tool_name='web_search'}."""
    return search_api.query(search_query, max_results=5)


@tool
def calculator(expression: str) -> float:
    """Tool span — tracked as rastir_tool_calls_total{tool_name='calculator'}."""
    return eval_expression(expression)


# ── Retrieval ─────────────────────────────────────────
@retrieval
def fetch_sources(query: str) -> list[str]:
    """Retrieval span — tracked as rastir_retrieval_calls_total."""
    return vector_store.similarity_search(query, top_k=3)
```

### What Rastir Captures

Calling `handle_request("What is the mass of Jupiter in kg?")` produces this span tree:

```
TraceID: abc123
│
├─ handle_request            type=trace     status=OK   dur=2.3s
│  └─ research_agent         type=agent     status=OK   dur=2.2s
│     ├─ plan_step           type=llm       status=OK   dur=0.8s
│     │   model=gpt-4  provider=openai  tokens_in=45  tokens_out=32
│     ├─ web_search          type=tool      status=OK   dur=0.5s
│     │   tool_name=web_search  agent=research_agent
│     ├─ calculator          type=tool      status=OK   dur=0.01s
│     │   tool_name=calculator  agent=research_agent
│     ├─ fetch_sources       type=retrieval status=OK   dur=0.3s
│     │   agent=research_agent
│     └─ synthesize          type=llm       status=OK   dur=1.1s
│         model=gpt-4  provider=openai  tokens_in=210  tokens_out=85
```

Prometheus metrics emitted:
- `rastir_llm_calls_total{model="gpt-4", provider="openai", agent="research_agent"}` → 2
- `rastir_tool_calls_total{tool_name="web_search", agent="research_agent"}` → 1
- `rastir_tool_calls_total{tool_name="calculator", agent="research_agent"}` → 1
- `rastir_retrieval_calls_total{agent="research_agent"}` → 1
- `rastir_tokens_input_total{model="gpt-4"}` → 255
- `rastir_tokens_output_total{model="gpt-4"}` → 117
- `rastir_duration_seconds` histogram buckets for each span

### Multi-Agent Nesting

Agents can call other agents — Rastir tracks the full hierarchy:

```python
@agent(agent_name="supervisor")
def supervisor(task: str) -> str:
    """Top-level agent that delegates to sub-agents."""
    plan = plan_task(task)
    research = research_agent(plan["research_query"])
    code = coding_agent(plan["code_task"])
    return combine_results(research, code)

@agent(agent_name="coding_agent")
def coding_agent(task: str) -> str:
    """Sub-agent — its spans nest under the supervisor."""
    code = generate_code(task)
    result = run_code(code)
    return result

@llm
def generate_code(task: str) -> str:
    return anthropic.messages.create(
        model="claude-sonnet-4-20250514",
        messages=[{"role": "user", "content": f"Write code: {task}"}],
    )

@tool
def run_code(code: str) -> str:
    return sandbox.execute(code)
```

Resulting span tree:

```
supervisor (agent)
 ├─ plan_task (llm)
 ├─ research_agent (agent)        ← sub-agent
 │   ├─ plan_step (llm)
 │   ├─ web_search (tool)
 │   └─ synthesize (llm)
 ├─ coding_agent (agent)          ← sub-agent
 │   ├─ generate_code (llm)
 │   └─ run_code (tool)
 └─ combine_results (llm)
```

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
