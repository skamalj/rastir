---
layout: default
title: CrewAI
parent: Frameworks
nav_order: 2
---

# CrewAI Integration

Rastir provides `@crew_kickoff` — a single decorator that instruments [CrewAI](https://www.crewai.com/) workflows. It **auto-discovers and wraps** every agent's LLM and tools, with optional MCP tool injection.

---

## Quick Start

```python
from rastir import configure, crew_kickoff
from crewai import Agent, Task, Crew, LLM

configure(service="my-app", push_url="http://localhost:8080")

researcher = Agent(
    role="Researcher",
    goal="Research AI trends",
    llm=LLM(model="gemini/gemini-2.5-flash"),
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
├── crewai.Researcher.llm (LLM) — model, tokens, latency
│   └── crewai.Researcher.llm (LLM) — subsequent calls
├── SearchTool (TOOL) — per-invocation
├── crewai.Writer.llm (LLM)
│   └── ...
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

@crew_kickoff(agent_name="my_crew", mcp=session)
def run(crew): ...
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `agent_name` | `str` | Function name | Name for the outer agent span |
| `mcp` | session / list / dict | `None` | MCP session(s) to inject as CrewAI tools |

**Supports:**
- Bare usage (`@crew_kickoff`) and parameterized (`@crew_kickoff(...)`)
- Sync and async functions
- `Crew` passed as positional or keyword argument

---

## What Gets Wrapped

### LLMs

Each agent's `llm` attribute is wrapped with `wrap(llm, include=["call"])`:

| Attribute | Value |
|-----------|-------|
| Span name | `crewai.<role>.llm` (e.g., `crewai.Researcher.llm`) |
| Span type | `LLM` |
| Methods wrapped | `call()` only — avoids noise from Pydantic internals |
| Metadata | Model, provider, tokens, latency extracted by the adapter pipeline |

### Tools

Each agent's existing tools are wrapped with `wrap(tool, include=["run"])`:

| Attribute | Value |
|-----------|-------|
| Span name | The tool's `name` attribute |
| Span type | `TOOL` |
| Methods wrapped | `run()` only |

### Skip Already-Wrapped Objects

If an LLM or tool already has `_rastir_wrapped = True`, `@crew_kickoff` does not re-wrap it.

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

### Pattern 5: MCP tools — single session (all agents)

```python
from rastir import crew_kickoff, wrap

session = wrap(mcp_session)  # auto-detects MCP session

@crew_kickoff(agent_name="my_crew", mcp=session)
def run(crew):
    return crew.kickoff()
# All agents receive MCP tools from this session
```

### Pattern 6: MCP tools — list of sessions (all agents)

```python
@crew_kickoff(agent_name="my_crew", mcp=[research_session, data_session])
def run(crew):
    return crew.kickoff()
# All agents receive tools from both sessions
```

### Pattern 7: MCP tools — per-agent dict mapping

```python
@crew_kickoff(
    agent_name="my_crew",
    mcp={"Researcher": research_session, "Writer": writer_session},
)
def run(crew):
    return crew.kickoff()
# Only "Researcher" gets tools from research_session
# Only "Writer" gets tools from writer_session
```

The dict keys match on the agent's `role` attribute.

### Pattern 8: Multiple Crews

```python
@crew_kickoff(agent_name="crew_a")
def run_a(crew):
    return crew.kickoff()

@crew_kickoff(agent_name="crew_b")
def run_b(crew):
    return crew.kickoff()

# Each decorated function wraps/restores independently
result_a = run_a(crew_a)
result_b = run_b(crew_b)
```

### Pattern 9: Reusing the same Crew

```python
@crew_kickoff(agent_name="my_crew")
def run(crew):
    return crew.kickoff()

# Safe to call multiple times — originals restored after each call
result1 = run(crew)
result2 = run(crew)
result3 = run(crew)
```

---

## Restore After Execution

After `crew.kickoff()` completes (success or error), `@crew_kickoff` restores:
- Original LLMs on every agent
- Original tools list on every agent (MCP-injected tools are removed)

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

```
@crew_kickoff agent span
│
├── Agent "Researcher"
│   ├── crewai.Researcher.llm (LLM call 1)
│   ├── crewai.Researcher.llm (LLM call 2)
│   ├── SearchTool (tool invocation)
│   └── MCP_web_search (injected MCP tool)
│
├── Agent "Writer"
│   ├── crewai.Writer.llm (LLM call 1)
│   └── crewai.Writer.llm (LLM call 2)
```

All child spans inherit the `agent` label, so Prometheus metrics are grouped by crew.

---

## Prometheus Metrics Produced

| Metric | Source |
|--------|--------|
| `rastir_llm_calls_total{model, provider, agent}` | Wrapped LLM `call()` |
| `rastir_tokens_input_total{model, provider, agent}` | Token extraction from LLM response |
| `rastir_tokens_output_total{model, provider, agent}` | Token extraction from LLM response |
| `rastir_duration_seconds{span_type="llm"}` | LLM call latency |
| `rastir_tool_calls_total{tool_name, agent}` | Wrapped tool `run()` |
| `rastir_duration_seconds{span_type="tool"}` | Tool invocation latency |
| `rastir_duration_seconds{span_type="agent"}` | Entire crew kickoff latency |

**Recommendation:** Always pass the `Crew` object as an argument to the decorated function. Define all agents and their LLMs/tools before the decorated function call.
