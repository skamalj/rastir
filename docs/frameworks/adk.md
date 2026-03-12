---
layout: default
title: ADK (Google)
parent: Frameworks
nav_order: 4
---

# Google ADK Integration

Rastir provides `@adk_agent` — a single decorator that instruments [Google ADK](https://google.github.io/adk-docs/) (Agent Development Kit) workflows. It **auto-discovers and wraps** ADK `Runner` or `BaseAgent` objects, intercepting events from `run_async` to create LLM and tool spans automatically — tokens, cost, model, provider, latency — with no code changes inside your agents.

> **Tip:** You can also use `@framework_agent` which auto-detects ADK objects from function arguments. The dedicated `@adk_agent` decorator is still available for explicit control.

---

## Quick Start

```python
from rastir import configure, adk_agent
from google.adk.runners import Runner
from google.adk.agents import LlmAgent
from google.genai import types

configure(service="my-app", push_url="http://localhost:8080")

agent = LlmAgent(
    name="weather_agent",
    model="gemini-2.0-flash",
    tools=[get_weather],
)

runner = Runner(agent=agent, app_name="weather-app", session_service=session_service)

@adk_agent(agent_name="weather_agent")
async def run(runner, prompt):
    events = []
    async for event in runner.run_async(
        user_id="user1", session_id="session1",
        new_message=types.Content(role="user", parts=[types.Part(text=prompt)])
    ):
        events.append(event)
    return events

result = await run(runner, "What's the weather in London?")
```

This produces:

```
AGENT  weather_agent         ← @adk_agent
├── LLM  gemini-2.0-flash    ← auto-discovered from events
└── TOOL get_weather          ← auto-discovered from events
└── LLM  gemini-2.0-flash    ← follow-up LLM call
```

---

## How It Works

1. **Detection** — the decorator scans function arguments and closures for ADK `Runner` or `BaseAgent` objects using class-name/module inspection (no ADK imports at module scope).

2. **Wrapping** — `Runner.run_async` is temporarily monkey-patched. The wrapper iterates over the async event stream and creates spans for LLM and tool events.

3. **Span creation** — LLM events produce `LLM` spans with model, tokens, and latency. Tool events produce `TOOL` spans with tool name and duration.

4. **Restore** — after the decorated function returns, original methods are restored for safe reuse.

---

## Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `agent_name` | `str` | Function name | Agent identity label for metrics |

Supports bare `@adk_agent` or `@adk_agent(agent_name="...")`.

---

## MCP Tool Support

ADK agents that use MCP tools work with Rastir's generic MCP client discovery. If an MCP client is passed as a function argument or captured in a closure, Rastir injects the `traceparent` header for distributed tracing.

```python
@adk_agent(agent_name="mcp_agent")
async def run(runner, mcp_client, prompt):
    # mcp_client discovered from args → traceparent injected
    events = []
    async for event in runner.run_async(...):
        events.append(event)
    return events
```

---

## What Gets Captured

| Data | Source |
|------|--------|
| Model name | Agent's `model` attribute |
| Provider | Inferred from model string or client module |
| Input/output tokens | From LLM response events |
| Tool name | From tool call events |
| Duration | Wall-clock timing per span |
| Errors | Exception capture with normalised error type |

---

## Coding Patterns

### Pattern 1: Runner with `run_async` (most common)

```python
from google.adk.runners import Runner
from google.adk.agents import LlmAgent
from google.genai import types

agent = LlmAgent(name="assistant", model="gemini-2.0-flash", tools=[my_tool])
runner = Runner(agent=agent, app_name="my-app", session_service=session_service)

@adk_agent(agent_name="assistant")
async def run(runner, prompt):
    events = []
    async for event in runner.run_async(
        user_id="u1", session_id="s1",
        new_message=types.Content(role="user", parts=[types.Part(text=prompt)])
    ):
        events.append(event)
    return events
```

### Pattern 2: Bare decorator (name defaults to function name)

```python
@adk_agent
async def assistant(runner, prompt):
    async for event in runner.run_async(...):
        pass

# Agent span name will be "assistant"
```

### Pattern 3: Agent with sub-agents

```python
sub = LlmAgent(name="researcher", model="gemini-2.0-flash", tools=[search])
main = LlmAgent(name="coordinator", model="gemini-2.0-flash", sub_agents=[sub])
runner = Runner(agent=main, app_name="my-app", session_service=session_service)

@adk_agent(agent_name="coordinator")
async def run(runner, prompt):
    events = []
    async for event in runner.run_async(...):
        events.append(event)
    return events
```

Rastir recurses into `sub_agents` and installs callbacks on each.

### Pattern 4: Cost tracking with pricing registry

```python
from rastir import configure
from rastir.config import get_pricing_registry

configure(service="my-app", push_url="...", enable_cost_calculation=True)

pr = get_pricing_registry()
pr.register("gemini", "gemini-2.0-flash", input_price=0.075, output_price=0.30)

@adk_agent(agent_name="assistant")
async def run(runner, prompt):
    ...
# Each LLM span will now include cost_usd
```

---

## Span Hierarchy

```
@adk_agent agent span
│
├── LLM  gemini-2.0-flash
│   → model, tokens_in, tokens_out, latency
│
├── TOOL get_weather
│   → tool_name, latency
│
├── LLM  gemini-2.0-flash
│   → follow-up call with tool results
│
└── (more iterations as the agent loops)
```

All child spans inherit the `agent` label from the outer span, so Prometheus metrics are grouped by agent.

---

## Limitations

- ADK is **async-first** — the decorator works with `async def` functions.
- Only `Runner.run_async` event streams are intercepted. Direct agent calls bypass the wrapper.
- Streaming TTFT is not supported — ADK uses a callback model, not streaming generators.
