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

ADK agents that use MCP tools are automatically detected. Rastir discovers MCP client objects on the agent and injects `traceparent` headers for distributed tracing across MCP boundaries.

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

## Limitations

- ADK is **async-first** — the decorator works with `async def` functions.
- Only `Runner.run_async` event streams are intercepted. Direct agent calls bypass the wrapper.
