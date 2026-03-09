---
layout: default
title: Strands
parent: Frameworks
nav_order: 5
---

# Strands Integration

Rastir provides `@strands_agent` — a single decorator that instruments [AWS Strands Agents](https://strandsagents.com/) workflows. It **auto-discovers and wraps** Strands `Agent` objects, intercepting the model's `stream` method and each tool's `stream` method to create LLM and tool spans automatically — tokens, cost, model, provider, latency — with no code changes inside your agents.

> **Tip:** You can also use `@framework_agent` which auto-detects Strands Agent objects from function arguments. The dedicated `@strands_agent` decorator is still available for explicit control.

---

## Quick Start

```python
from rastir import configure, strands_agent
from strands import Agent
from strands.models.bedrock import BedrockModel

configure(service="my-app", push_url="http://localhost:8080")

model = BedrockModel(model_id="us.anthropic.claude-sonnet-4-20250514")
agent = Agent(model=model, tools=[search_tool, calc_tool])

@strands_agent(agent_name="research_agent")
def run(agent, prompt):
    return agent(prompt)

result = run(agent, "Research quantum computing trends")
```

This produces:

```
AGENT  research_agent              ← @strands_agent
├── LLM  us.anthropic.claude-sonnet-4-20250514  ← auto-discovered
├── TOOL search_tool                ← auto-discovered
├── LLM  us.anthropic.claude-sonnet-4-20250514  ← follow-up call
└── TOOL calc_tool                  ← auto-discovered
```

---

## How It Works

1. **Detection** — the decorator scans function arguments and closures for Strands `Agent` objects using class-name/module inspection (no Strands imports at module scope).

2. **Model wrapping** — the agent's `model.stream` method is temporarily monkey-patched. Each invocation creates an `LLM` span with model name, provider, token counts, and latency.

3. **Tool wrapping** — each tool's `stream` method (or the tool itself) is wrapped to create a `TOOL` span with the tool name and duration.

4. **Restore** — after the decorated function returns, original methods are restored for safe reuse.

---

## Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `agent_name` | `str` | Function name | Agent identity label for metrics |

Supports bare `@strands_agent` or `@strands_agent(agent_name="...")`, sync and async.

---

## MCP Tool Support

Strands agents that use MCP tools are automatically detected. Rastir discovers MCP client objects on the agent and injects `traceparent` headers for distributed tracing across MCP boundaries.

---

## What Gets Captured

| Data | Source |
|------|--------|
| Model name | From `model.model_id`, `model.model_name`, or model string |
| Provider | Inferred from model class module (e.g. `BedrockModel` → `bedrock`) |
| Input/output tokens | Accumulated from stream chunks |
| Tool name | From tool object name or function name |
| Duration | Wall-clock timing per span |
| Errors | Exception capture with normalised error type |

---

## Sync and Async

Strands agents support both sync (`agent(prompt)`) and async (`agent.invoke_async(prompt)`) invocation. The decorator handles both:

```python
# Sync
@strands_agent(agent_name="sync_agent")
def run_sync(agent, prompt):
    return agent(prompt)

# Async
@strands_agent(agent_name="async_agent")
async def run_async(agent, prompt):
    return await agent.invoke_async(prompt)
```
