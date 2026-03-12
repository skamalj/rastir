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

Strands agents that use MCP tools work with Rastir's generic MCP client discovery. If an MCP client object is passed as a function argument or captured in a closure, Rastir can discover it and inject the `traceparent` header.

{: .note }
> Strands uses its own MCP integration (`strands.tools.mcp`). For automatic trace propagation, pass the MCP client as a function argument to the decorated function.

---

## What Gets Captured

| Data | Source |
|------|--------|
| Model name | From `model.config["model_id"]`, `model.model_name`, or model string |
| Provider | Auto-detected from model class module (e.g. `strands.models.bedrock` → `bedrock`) |
| Input/output tokens | Accumulated from stream chunks |
| Tool name | From tool object name or function name |
| Duration | Wall-clock timing per span |
| Streaming TTFT | Time-To-First-Token on streaming LLM calls |
| Errors | Exception capture with normalised error type |

---

## Coding Patterns

### Pattern 1: Basic agent (most common)

```python
from strands import Agent
from strands.models.bedrock import BedrockModel

model = BedrockModel(model_id="us.anthropic.claude-sonnet-4-20250514")
agent = Agent(model=model, tools=[search_tool])

@strands_agent(agent_name="research")
def run(agent, prompt):
    return agent(prompt)

result = run(agent, "Research quantum computing")
```

### Pattern 2: Bare decorator (name defaults to function name)

```python
@strands_agent
def research_pipeline(agent, prompt):
    return agent(prompt)

# Agent span name will be "research_pipeline"
```

### Pattern 3: With OpenAI model

```python
from strands.models.openai import OpenAIModel

model = OpenAIModel(model="gpt-4o")
agent = Agent(model=model, tools=[calculator])

@strands_agent(agent_name="calc_agent")
def run(agent, prompt):
    return agent(prompt)
```

### Pattern 4: Async invocation

```python
@strands_agent(agent_name="async_agent")
async def run(agent, prompt):
    return await agent.invoke_async(prompt)
```

### Pattern 5: Multiple agents

```python
@strands_agent(agent_name="researcher")
def research(agent, prompt):
    return agent(prompt)

@strands_agent(agent_name="writer")
def write(agent, prompt):
    return agent(prompt)

research_result = research(research_agent, "Find trends")
final = write(writer_agent, f"Summarize: {research_result}")
```

### Pattern 6: Cost tracking with pricing registry

```python
from rastir import configure
from rastir.config import get_pricing_registry

configure(service="my-app", push_url="...", enable_cost_calculation=True)

pr = get_pricing_registry()
pr.register("bedrock", "us.anthropic.claude-sonnet-4-20250514", input_price=3.0, output_price=15.0)

@strands_agent(agent_name="research")
def run(agent, prompt):
    return agent(prompt)
# Each LLM span will now include cost_usd
```

---

## Span Hierarchy

```
@strands_agent agent span
│
├── LLM  us.anthropic.claude-sonnet-4-20250514
│   → model, provider, tokens_in, tokens_out, latency, ttft
│
├── TOOL search_tool
│   → tool_name, latency
│
├── LLM  us.anthropic.claude-sonnet-4-20250514
│   → follow-up call with tool results
│
└── (more iterations as the agent loops)
```

All child spans inherit the `agent` label from the outer span, so Prometheus metrics are grouped by agent.

---

## Supported Model Providers

Strands supports multiple model backends. Rastir auto-detects the provider from the model class module:

| Model class | Provider label |
|-------------|---------------|
| `BedrockModel` | `bedrock` |
| `OpenAIModel` | `openai` |
| `AnthropicModel` | `anthropic` |
| `GeminiModel` | `gemini` |
| `MistralModel` | `mistral` |
| `OllamaModel` | `ollama` |
| `SageMakerModel` | `bedrock` |
| `LiteLLMModel` | `litellm` |

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
