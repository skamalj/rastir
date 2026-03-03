---
layout: default
title: Decorators
nav_order: 3
---

# Decorator Reference

Rastir provides six **core** decorators for manual instrumentation, plus three **framework** decorators that auto-discover and wrap everything inside a framework's agent loop. All support both sync and async functions.

---

## Which Decorator Should I Use?

| Scenario | Decorator | What it does |
|----------|-----------|-------------|
| Building with **LangGraph** | `@langgraph_agent` | Auto-discovers LLMs, tools, and nodes inside the compiled graph. **No manual wrapping needed.** |
| Building with **CrewAI** | `@crew_kickoff` | Auto-discovers LLMs and tools on every agent in the Crew. Optional MCP injection. |
| Building with **LlamaIndex** | `@llamaindex_agent` | Creates the agent span; you pre-wrap LLMs/tools with `wrap()`. |
| Building your **own agent loop** | `@agent` + `@llm` + `@tool` | Full manual control — you decorate each function yourself. |
| **Simple tracing** (no agent) | `@trace` | General-purpose span for any function. |
| **Standalone metrics** only | `@metric` | Prometheus counters/histograms, no tracing. |

**Rule of thumb:** If you're using LangGraph, CrewAI, or LlamaIndex — use the corresponding framework decorator. It does all the heavy lifting. Use `@agent` / `@llm` / `@tool` only when you're calling LLM APIs directly without a framework.

---

## Framework Decorators

### @langgraph_agent

**Purpose:** Instrument a LangGraph compiled graph. Auto-discovers all chat models, tools, and graph nodes — wraps them for tracing and restores originals after execution.

```python
from rastir import langgraph_agent
from langgraph.prebuilt import create_react_agent

@langgraph_agent(agent_name="react")
def run(query):
    graph = create_react_agent(model, tools)
    return graph.invoke({"messages": [("user", query)]})
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `agent_name` | `str` | Function name | Agent identity label |

**What gets auto-discovered:**
- Graph nodes → `TRACE` spans (`node:<name>`)
- Chat models → `LLM` spans with token/latency metrics
- Tools in `ToolNode` → `TOOL` spans

**Supports:** bare `@langgraph_agent` or `@langgraph_agent(...)`, sync/async, graph as argument or in closure.

→ Full details: [LangGraph framework page](frameworks/langgraph)

---

### @crew_kickoff

**Purpose:** Instrument a CrewAI Crew. Auto-discovers each agent's LLM and tools, wraps them before `kickoff()`, and restores after.

```python
from rastir import crew_kickoff

@crew_kickoff(agent_name="research_crew", mcp=session)
def run(crew):
    return crew.kickoff()
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `agent_name` | `str` | Function name | Agent identity label |
| `mcp` | session / list / dict | `None` | MCP session(s) to inject as CrewAI tools |

**What gets auto-discovered:**
- Each agent's `llm` → `LLM` spans
- Each agent's `tools` → `TOOL` spans
- MCP tools → converted to CrewAI `BaseTool` and injected

**Supports:** bare `@crew_kickoff` or `@crew_kickoff(...)`, sync/async, per-agent MCP mapping via dict.

→ Full details: [CrewAI framework page](frameworks/crewai)

---

### @llamaindex_agent

**Purpose:** Create an agent span around LlamaIndex agent execution. You pre-wrap LLMs and tools with `wrap()` before creating the agent.

```python
from rastir import llamaindex_agent, wrap
from llama_index.core.agent import ReActAgent

llm = wrap(OpenAI(model="gpt-4o"), span_type="llm")
tools = [wrap(t, span_type="tool") for t in my_tools]
agent = ReActAgent.from_tools(tools, llm=llm)

@llamaindex_agent(agent_name="qa_agent")
def run(agent, query):
    return agent.chat(query)
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `agent_name` | `str` | Function name | Agent identity label |

**Note:** Unlike `@langgraph_agent` and `@crew_kickoff`, LlamaIndex requires explicit `wrap()` calls on LLMs and tools. The decorator provides the outer agent span and restore-after-execution.

→ Full details: [LlamaIndex framework page](frameworks/llamaindex)

---

## Core Decorators

These decorators are for **manual instrumentation** — use them when you're calling LLM APIs directly without a framework, or when building a custom agent loop.

---

### @trace

**Purpose:** Create a root or general span. Entry point for request tracing.

```python
from rastir import trace

# Bare usage
@trace
def handle_request(query: str) -> str:
    ...

# With options
@trace(name="custom_span_name", emit_metric=True)
def process(data: dict) -> dict:
    ...
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | Function name | Custom span name |
| `emit_metric` | `bool` | `False` | Record duration as a span attribute |

**Span type:** `trace`

**Behaviour:**
- Creates a span with parent-child hierarchy via context propagation
- Records execution duration and success/failure status
- If `emit_metric=True`, adds `emit_metric` attribute to the span

---

### @agent

**Purpose:** Mark a function as an agent entry point. Use this when you're building your own agent loop (calling LLM APIs directly). If you're using LangGraph, CrewAI, or LlamaIndex, use the corresponding framework decorator instead — it handles everything automatically. Sets agent identity so child `@llm`, `@tool`, and `@retrieval` spans inherit the `agent` label in their Prometheus metrics.

```python
from rastir import agent

# Bare usage — agent_name defaults to function name
@agent
def my_agent(query: str) -> str:
    ...

# With explicit name
@agent(agent_name="research_bot")
def run_research(query: str) -> str:
    ...
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `agent_name` | `str` | Function name | Agent identity label |

**Span type:** `agent`

**Agent label rule:** The `agent` label is injected into child LLM/tool/retrieval metrics **only** when the parent span is explicitly marked via `@agent`. If `@llm` or `@tool` runs under a plain `@trace`, no `agent` label is injected.

---

### @llm

**Purpose:** Create an LLM span. Automatically extracts model, provider, token usage, and finish reason from the return value using the adapter pipeline.

```python
from rastir import llm

@llm
def ask_gpt(query: str) -> str:
    return openai.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "user", "content": query}],
    )

# With explicit metadata (overrides adapter extraction)
@llm(model="gpt-4", provider="openai")
def ask_with_hints(query: str) -> str:
    ...
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | Function name | Custom span name |
| `model` | `str` | Auto-detected | LLM model name |
| `provider` | `str` | Auto-detected | Provider name |

**Span type:** `llm`

**Metrics emitted:**
- `rastir_llm_calls_total{service, env, model, provider, agent}`
- `rastir_tokens_input_total{service, env, model, provider, agent}`
- `rastir_tokens_output_total{service, env, model, provider, agent}`
- `rastir_duration_seconds{service, env, span_type="llm"}`
- `rastir_tokens_per_call{service, env, model, provider}`

**Streaming:** Auto-detects when the function returns a generator or async generator. Token deltas are accumulated as the stream is consumed. Metrics are recorded after the stream completes.

---

### @tool

**Purpose:** Track tool/function call invocations within an agent pipeline.

```python
from rastir import tool

@tool
def search_database(query: str) -> list[dict]:
    ...

@tool(name="web_search")
def google_search(query: str) -> list[str]:
    ...
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | Function name | Tool name (used as `tool_name` label) |

**Span type:** `tool`

**Metrics emitted:**
- `rastir_tool_calls_total{service, env, tool_name, agent}`
- `rastir_duration_seconds{service, env, span_type="tool"}`

---

### @retrieval

**Purpose:** Track retrieval/vector search operations.

```python
from rastir import retrieval

@retrieval
def vector_search(query: str, top_k: int = 5) -> list[str]:
    return chroma_client.query(query, n_results=top_k)
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | Function name | Span name |

**Span type:** `retrieval`

**Metrics emitted:**
- `rastir_retrieval_calls_total{service, env, agent}`
- `rastir_duration_seconds{service, env, span_type="retrieval"}`

---

### @metric

**Purpose:** Emit generic function-level Prometheus metrics. Independent of tracing — does not create spans.

```python
from rastir import metric

@metric
def process_request(data: dict) -> dict:
    ...

@metric(name="custom_op")
def my_function() -> None:
    ...
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | Function name | Metric name prefix |

**Metrics emitted:**
- `<name>_calls_total{service, env}`
- `<name>_duration_seconds{service, env}`
- `<name>_failures_total{service, env}`

---

## Stacking Decorators

Decorators can be stacked for combined behaviour:

```python
@agent(agent_name="qa_bot")
def run_qa(query: str) -> str:
    result = search(query)
    return answer(query, result)

@tool
@retrieval  # Not typical — usually pick one
def search(query: str) -> list[str]:
    ...
```

The most common pattern is:

```
@trace (or @agent)
  └── @llm
  └── @tool
  └── @retrieval
```

---

## Error Handling

All decorators automatically:
- Catch exceptions and set span status to `ERROR`
- Record exception details as span events
- Re-raise the exception (decorators are transparent)
- Increment `rastir_errors_total` counter with normalised error type

```python
@llm
def risky_call(query: str):
    # If this raises, Rastir records the error and re-raises
    return openai.chat.completions.create(...)
```

Error types are normalised into six categories:
- `timeout` — `TimeoutError`, `httpx.TimeoutException`, etc.
- `rate_limit` — `RateLimitError` from any provider
- `validation_error` — `ValueError`, `TypeError`, `ValidationError`
- `provider_error` — API errors from OpenAI, Anthropic, Bedrock
- `internal_error` — `RuntimeError`, generic `Exception`
- `unknown` — anything else
