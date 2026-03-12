---
layout: default
title: CrewAI
parent: Frameworks
nav_order: 2
---

# CrewAI Integration

Rastir provides `@crew_kickoff` — a single decorator that instruments [CrewAI](https://www.crewai.com/) workflows. It **auto-discovers and wraps** every agent's LLM and tools for per-call tracing — tokens, cost, model, provider, input/output — with no code changes inside your agents.

> **Tip:** You can also use `@framework_agent` which auto-detects CrewAI `Crew` objects from function arguments. The dedicated `@crew_kickoff` decorator is still available for explicit control.

---

## Quick Start

```python
from rastir import configure, crew_kickoff
from crewai import Agent, Task, Crew, LLM

configure(service="my-app", push_url="http://localhost:8080")

researcher = Agent(
    role="Researcher",
    goal="Research AI trends",
    llm=LLM(model="openai/gpt-4o-mini"),
    tools=[SearchTool()],
)

writer = Agent(
    role="Writer",
    goal="Write summaries",
    llm=LLM(model="openai/gpt-4o"),
)

crew = Crew(
    agents=[researcher, writer],
    tasks=[
        Task(description="Research AI trends", agent=researcher, expected_output="Report"),
        Task(description="Summarize findings", agent=writer, expected_output="Summary"),
    ],
)

@crew_kickoff(agent_name="research_crew")
def run(crew):
    return crew.kickoff()

result = run(crew)
```

This produces:

```
research_crew (AGENT)
├── crewai.Researcher.llm.call (LLM)  — model, provider, tokens, cost, input
├── crewai.Researcher.tool.search (TOOL) — tool.input, tool.output
├── crewai.Researcher.llm.call (LLM)  — subsequent calls
├── crewai.Writer.llm.call (LLM)
└── crewai.Writer.llm.call (LLM)      — output on final response
```

---

## Why a Dedicated Decorator?

CrewAI controls the agent loop internally — your code calls `crew.kickoff()` and CrewAI manages all LLM calls, tool invocations, and task delegation inside. `@crew_kickoff` wraps each agent's LLM and tools before `kickoff()` runs, and restores originals after.

---

## API Reference

### `crew_kickoff()`

```python
from rastir import crew_kickoff

@crew_kickoff
def run(crew): ...

@crew_kickoff(agent_name="my_crew")
def run(crew): ...
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `agent_name` | `str` | Function name | Name for the outer agent span |

**MCP tools:** CrewAI handles MCP natively via `mcps=[]` on agents — no Rastir parameter needed.

**Supports:**
- Bare usage (`@crew_kickoff`) and parameterized (`@crew_kickoff(...)`)
- Sync and async functions
- `Crew` passed as positional or keyword argument

---

## What Gets Wrapped

### LLMs

Each agent's `llm` attribute is wrapped with a transparent proxy (`include=["call"]`):

| Attribute | Value |
|-----------|-------|
| Span name | `crewai.<role>.llm.call` (e.g., `crewai.Researcher.llm.call`) |
| Span type | `LLM` |
| Methods wrapped | `call()` only — avoids noise from Pydantic internals |

**LLM span attributes captured:**

| Attribute | Source | Example |
|-----------|--------|---------|
| `model` | LLM object's `model_name` / `model` attribute | `gpt-4o-mini` |
| `provider` | Auto-detected from LLM module path | `openai` |
| `tokens_input` | Per-call token count | `235` |
| `tokens_output` | Per-call token count | `70` |
| `cost_usd` | Calculated from tokens × pricing registry rates | `0.000077` |
| `input` | Prompt messages passed to `.call()` | System + user messages |
| `output` | Final text response | `"The answer is..."` |
| `agent` | Inherited from `@crew_kickoff` agent span | `research_crew` |

### Tools

Each agent's tools have their `.run()` method **patched in-place** via `tool.__dict__["run"]`:

| Attribute | Value |
|-----------|-------|
| Span name | `crewai.<role>.tool.<tool_name>` (e.g., `crewai.Researcher.tool.search`) |
| Span type | `TOOL` |
| Methods wrapped | `run()` only |

**Tool span attributes captured:**

| Attribute | Source | Example |
|-----------|--------|---------|
| `tool.input` | Keyword arguments passed by CrewAI to `.run(**kwargs)` | `{'city': 'Tokyo'}` |
| `tool.output` | Return value from the tool function | `"15°C, rainy"` |
| `agent` | Inherited from `@crew_kickoff` agent span | `research_crew` |

### Skip Already-Wrapped Objects

- LLMs with `_rastir_wrapped = True` are not re-wrapped
- Tools with `_rastir_tool_patched = True` are not re-patched

---

## MCP Tool Tracing

### Propagating Trace Context to MCP Servers

When tools inside your crew call remote MCP servers, you can propagate the trace context so server-side spans appear as children of the client tool span. Use `traceparent_headers()` in your tool's HTTP calls:

```python
from crewai.tools import tool as crewai_tool
from rastir.remote import traceparent_headers
import httpx

MCP_URL = "http://localhost:8080/mcp"

@crewai_tool
def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    headers = {"Accept": "application/json", **traceparent_headers()}
    with httpx.Client(timeout=10) as c:
        r = c.post(MCP_URL, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "tools/call",
            "params": {"name": "get_weather", "arguments": {"city": city}},
        }, headers=headers)
        data = r.json()
        return data["result"]["content"][0]["text"]
```

This produces a fully linked trace:

```
research_crew (AGENT)
├── crewai.Researcher.llm.call (LLM)
├── crewai.Researcher.tool.get_weather (TOOL)     ← client span
│   └── mcpserver:get_weather (TOOL)               ← server span (same trace)
├── crewai.Researcher.llm.call (LLM)
```

### Native MCP via `mcps=[]`

CrewAI 1.9+ supports MCP natively via the `mcps=[]` field on agents:

```python
from crewai.mcp import MCPServerHTTP

agent = Agent(
    role="Researcher",
    llm=llm,
    mcps=[MCPServerHTTP(url="http://localhost:8080/mcp")],
)
```

`@crew_kickoff` auto-discovers `MCPServerHTTP` / `MCPServerSSE` configs and injects the `traceparent` header on them. The tools CrewAI discovers from the MCP server are wrapped like any other tool.

---

## Coding Patterns

### Pattern 1: Basic Crew (most common)

```python
crew = Crew(agents=[researcher, writer], tasks=[...])

@crew_kickoff(agent_name="my_crew")
def run(crew):
    return crew.kickoff()

result = run(crew)
```

### Pattern 2: Bare decorator (name defaults to function name)

```python
@crew_kickoff
def research_pipeline(crew):
    return crew.kickoff()

research_pipeline(crew)
# Agent span name will be "research_pipeline"
```

### Pattern 3: Crew as keyword argument

```python
@crew_kickoff(agent_name="my_crew")
def run(topic, crew=None):
    return crew.kickoff(inputs={"topic": topic})

run("AI trends", crew=my_crew)
```

### Pattern 4: Async with `kickoff_async()`

```python
@crew_kickoff(agent_name="async_crew")
async def run(crew):
    return await crew.kickoff_async()
```

The decorator auto-detects `async def` and uses the async code path.

### Pattern 5: Multiple Crews

```python
@crew_kickoff(agent_name="crew_a")
def run_a(crew):
    return crew.kickoff()

@crew_kickoff(agent_name="crew_b")
def run_b(crew):
    return crew.kickoff()

result_a = run_a(crew_a)
result_b = run_b(crew_b)
```

### Pattern 6: Tools with MCP trace propagation

```python
from rastir.remote import traceparent_headers

@crewai_tool
def remote_search(query: str) -> str:
    """Search via remote MCP server."""
    headers = {"Accept": "application/json", **traceparent_headers()}
    with httpx.Client() as c:
        r = c.post(MCP_URL, json={...}, headers=headers)
        return r.json()["result"]["content"][0]["text"]

agent = Agent(role="Searcher", llm=llm, tools=[remote_search])
```

### Pattern 7: Cost tracking with pricing registry

```python
from rastir import configure
from rastir.config import get_pricing_registry

configure(service="my-app", push_url="...", enable_cost_calculation=True)

pr = get_pricing_registry()
pr.register("openai", "gpt-4o-mini", input_price=0.15, output_price=0.60)

@crew_kickoff(agent_name="my_crew")
def run(crew):
    return crew.kickoff()
# Each LLM span will now include cost_usd
```

### Pattern 8: Reusing the same Crew

```python
@crew_kickoff(agent_name="my_crew")
def run(crew):
    return crew.kickoff()

# Safe to call multiple times — originals restored after each call
result1 = run(crew)
result2 = run(crew)
```

---

## Restore After Execution

After `crew.kickoff()` completes (success or error), `@crew_kickoff` restores:
- Original LLM proxy on every agent
- Tool `.run()` methods unpatched (instance `__dict__` entries removed)

This means the `Crew` object can be safely reused across multiple calls with no accumulated wrapping.

---

## Error Handling

If the decorated function raises an exception:
- The agent span records the error (type + message)
- Span status is set to `ERROR`
- The exception is re-raised unchanged
- Originals are still restored (via `finally` block)

---

## Span Hierarchy

A typical CrewAI trace looks like this:

```
@crew_kickoff agent span
│
├── crewai.Researcher.llm.call (LLM)    — model=gpt-4o-mini, tokens, cost
├── crewai.Researcher.tool.search (TOOL) — input={query}, output=results
│   └── mcpserver:search (TOOL)          — server span (if using traceparent)
├── crewai.Researcher.llm.call (LLM)    — tool result fed back to LLM
├── crewai.Researcher.llm.call (LLM)    — final answer, has output
│
├── crewai.Writer.llm.call (LLM)
└── crewai.Writer.llm.call (LLM)
```

All child spans inherit the `agent` label from `@crew_kickoff`, so Prometheus metrics are grouped by crew.

---

## Span Attributes in Tempo

Here's what you'll see in Tempo/Grafana for each span type:

### Agent span

| Attribute | Example |
|-----------|---------|
| `rastir.span_type` | `agent` |
| `rastir.agent_name` | `research_crew` |

### LLM span

| Attribute | Example |
|-----------|---------|
| `rastir.span_type` | `llm` |
| `rastir.model` | `gpt-4o-mini` |
| `rastir.provider` | `openai` |
| `rastir.tokens_input` | `235` |
| `rastir.tokens_output` | `70` |
| `rastir.cost_usd` | `0.000077` |
| `rastir.input` | `system: You are Researcher...` |
| `rastir.output` | `The answer is...` (final call only) |
| `rastir.agent` | `research_crew` |

### Tool span

| Attribute | Example |
|-----------|---------|
| `rastir.span_type` | `tool` |
| `rastir.tool.input` | `{'city': 'Tokyo'}` |
| `rastir.tool.output` | `15°C, rainy, humidity 80%` |
| `rastir.agent` | `research_crew` |

---

## Prometheus Metrics Produced

| Metric | Source |
|--------|--------|
| `rastir_llm_calls_total{model, provider, agent}` | Wrapped LLM `call()` |
| `rastir_tokens_input_total{model, provider, agent}` | Per-call token delta |
| `rastir_tokens_output_total{model, provider, agent}` | Per-call token delta |
| `rastir_cost_total{model, provider, agent}` | Cost calculation from pricing registry |
| `rastir_duration_seconds{span_type="llm"}` | LLM call latency |
| `rastir_tool_calls_total{tool_name, agent}` | Tool `.run()` invocation |
| `rastir_duration_seconds{span_type="tool"}` | Tool invocation latency |
| `rastir_duration_seconds{span_type="agent"}` | Entire crew kickoff latency |

---

## Technical Notes

### CrewAI Token Extraction

CrewAI's LLM `.call()` method calls `self.client.chat.completions.create()` internally, extracts `usage` into a cumulative `_token_usage` dict on the LLM instance, and returns only the response text (a plain string). Since the adapter pipeline receives a string rather than a `ChatCompletion` object, Rastir snapshots `_token_usage` before each call and computes a per-call delta. This gives accurate per-call prompt and completion token counts.

### CrewAI Tool Wrapping

CrewAI's `Agent` is a Pydantic `BaseModel` with a `tools` field typed as `list[BaseTool]`. A Pydantic `@field_validator` checks each tool via `isinstance(tool, BaseTool)` — proxy wrapper objects fail this validation and are silently replaced. Rastir works around this by patching the tool's `.run()` method directly in the instance's `__dict__`, which:

1. Takes precedence over the class method in Python's attribute lookup
2. Is invisible to Pydantic's model validation
3. Is cleanly reversible by removing the `__dict__` entry after execution
