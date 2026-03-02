---
layout: default
title: CrewAI Integration
nav_order: 6
---

# CrewAI Integration

Rastir provides the `@crew_kickoff` decorator for instrumenting [CrewAI](https://www.crewai.com/) workflows. A single annotation gives you **per-LLM-call and per-tool visibility** inside every agent — with automatic wrapping, MCP tool injection, and safe Crew reuse.

---

## Why a dedicated decorator?

CrewAI controls the agent loop internally — your code calls `crew.kickoff()` and CrewAI manages all LLM calls, tool invocations, and task delegation inside. Standard Rastir decorators like `@llm` and `@tool` can't be applied to CrewAI's internal code.

`@crew_kickoff` solves this by:
1. Scanning function arguments for `Crew` objects
2. Wrapping each agent's **LLM** — every `llm.call()` produces a span with model, tokens, latency
3. Wrapping each agent's **tools** — every `tool.run()` produces a tool span
4. Optionally injecting **MCP tools** (converted to CrewAI `BaseTool` subclasses)
5. Creating an **`@agent` span** around the entire execution
6. **Restoring originals** after execution so the Crew can be reused

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

This produces the following span tree:

```
research_crew (agent, AGENT span)
├── crewai.Researcher.llm (llm span) — model, tokens, latency
│   └── crewai.Researcher.llm (llm span) — subsequent calls
├── SearchTool (tool span) — per-invocation
├── crewai.Writer.llm (llm span)
│   └── ...
```

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

Each agent's LLM is wrapped with `wrap(llm, include=["call"])`:
- Only the `call()` method is intercepted — avoids noise from Pydantic internals
- Span name: `crewai.<role>.llm` (e.g., `crewai.Researcher.llm`)
- Span type: `llm`
- Attributes: model, provider, tokens, latency (extracted by the standard adapter pipeline)

### Tools

Each agent's existing tools are wrapped with `wrap(tool, include=["run"])`:
- Only the `run()` method is intercepted
- Span name: the tool's `name` attribute
- Span type: `tool`

### Skipping already-wrapped objects

If an LLM or tool already has `_rastir_wrapped = True` (e.g., from a prior `wrap()` call), `@crew_kickoff` does not re-wrap it.

---

## MCP Tool Injection

The `mcp=` parameter converts MCP tools into CrewAI `BaseTool` subclasses and injects them into agents — no manual conversion needed.

### Single session — all agents

```python
from rastir import crew_kickoff, wrap_mcp

session = wrap_mcp(mcp_session)

@crew_kickoff(agent_name="my_crew", mcp=session)
def run(crew):
    return crew.kickoff()
    # All agents receive MCP tools from this session
```

### List of sessions — all agents

```python
@crew_kickoff(agent_name="my_crew", mcp=[session1, session2])
def run(crew):
    return crew.kickoff()
    # All agents receive tools from both sessions
```

### Dict — per-agent mapping

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

### How MCP tools are converted

Each MCP tool is converted to a `BaseTool` subclass dynamically:
1. Input schema → Pydantic model (via `create_model()`)
2. Required/optional fields preserved from JSON Schema
3. `_run()` calls `session.call_tool(name, args)` — which, if the session is `wrap_mcp`'d, automatically injects trace context
4. Async `call_tool()` is run synchronously (CrewAI runs sync internally)

### MCP tool caching

Tool lists are fetched once per session and cached — if multiple agents share the same session, `list_tools()` is called only once.

---

## Restore After Execution

After `crew.kickoff()` completes (success or error), `@crew_kickoff` restores the original LLMs and tools on every agent. This means:

- The `Crew` object can be reused across multiple calls
- No accumulated wrapping layers from repeated calls
- Originals are restored even if an exception is raised

---

## Error Handling

If the wrapped function raises an exception:
- The agent span records the error (type + message) as a span event
- Span status is set to `ERROR`
- The exception is re-raised unchanged
- Original LLMs and tools are still restored

---

## Async Support

`@crew_kickoff` works with async functions:

```python
@crew_kickoff(agent_name="async_crew")
async def run(crew):
    return await crew.kickoff_async()
```

The decorator automatically detects `async def` and uses the async code path.

---

## Span Hierarchy

The decorator creates this observability structure:

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

All child spans inherit the `agent` label from the `@crew_kickoff` span, so Prometheus metrics are grouped by crew/agent.

---

## Prometheus Metrics

The wrapped LLM and tool calls produce standard Rastir metrics:

| Metric | Source |
|--------|--------|
| `rastir_llm_calls_total{model, provider, agent}` | Wrapped LLM `call()` |
| `rastir_tokens_input_total{model, provider, agent}` | Token extraction from LLM response |
| `rastir_tokens_output_total{model, provider, agent}` | Token extraction from LLM response |
| `rastir_duration_seconds{span_type="llm"}` | LLM call latency |
| `rastir_tool_calls_total{tool_name, agent}` | Wrapped tool `run()` |
| `rastir_duration_seconds{span_type="tool"}` | Tool invocation latency |
| `rastir_duration_seconds{span_type="agent"}` | Entire crew kickoff latency |

---

## Comparison: Before and After

### Before (`@agent` + `@llm` — manual approach)

```python
@agent(agent_name="crewai_agent")
def run():
    @llm(model="gemini-2.5-flash", provider="gemini")
    def invoke():
        return crew.kickoff()
    return invoke()
```

- Only one LLM span for the entire `kickoff()` — no per-call visibility
- Must manually specify model/provider
- No tool-level metrics inside CrewAI

### After (`@crew_kickoff`)

```python
@crew_kickoff(agent_name="research_crew")
def run(crew):
    return crew.kickoff()
```

- Per-LLM-call spans with automatic model/token detection
- Per-tool invocation spans
- Optional MCP tool injection
- Automatic restore for safe Crew reuse
