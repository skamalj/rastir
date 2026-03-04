---
layout: default
title: LlamaIndex
parent: Frameworks
nav_order: 3
---

# LlamaIndex Integration

Rastir provides `@llamaindex_agent` — a single decorator that instruments [LlamaIndex](https://www.llamaindex.ai/) agent workflows. It **auto-discovers and wraps** the agent's LLM and tools for per-call tracing — tokens, cost, model, provider, input/output — with no code changes inside your agents.

---

## Quick Start

```python
from rastir import configure, llamaindex_agent
from llama_index.llms.openai import OpenAI
from llama_index.core.agent import ReActAgent
from llama_index.core.tools import FunctionTool

configure(service="my-app", push_url="http://localhost:8080")

def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b

llm = OpenAI(model="gpt-4o-mini")
tools = [FunctionTool.from_defaults(fn=add)]
agent = ReActAgent(llm=llm, tools=tools, streaming=False)

@llamaindex_agent(agent_name="calc_agent")
async def run(agent, query):
    return await agent.run(query)

result = asyncio.run(run(agent, "What is 3 + 5?"))
```

This produces:

```
calc_agent (AGENT)
├── llamaindex.ReActAgent.llm.achat (LLM) — model, provider, tokens, cost, input
├── add.acall (TOOL) — tool.input, tool.output
├── llamaindex.ReActAgent.llm.achat (LLM) — subsequent calls
└── llamaindex.ReActAgent.llm.achat (LLM) — output on final response
```

---

## Why a Dedicated Decorator?

LlamaIndex controls the agent loop internally — your code calls `agent.run()` or `agent.chat()` and LlamaIndex manages all LLM calls, tool invocations, and reasoning steps inside. `@llamaindex_agent` wraps the agent's LLM and tools before execution begins, and restores originals after.

---

## API Reference

### `llamaindex_agent()`

```python
from rastir import llamaindex_agent

@llamaindex_agent
def run(agent): ...

@llamaindex_agent(agent_name="my_agent")
async def run(agent): ...
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `agent_name` | `str` | Function name | Name for the outer agent span |

**MCP tools:** LlamaIndex handles MCP via `llama-index-tools-mcp` — MCP tools become regular `FunctionTool` objects, auto-discovered and wrapped like any other tool.

**Supports:**
- Bare usage (`@llamaindex_agent`) and parameterized (`@llamaindex_agent(...)`)
- Sync and async functions
- Agent passed as positional or keyword argument

### Recognised Agent Types

The decorator auto-discovers these LlamaIndex agent classes (and subclasses via MRO):

- `ReActAgent`
- `FunctionAgent`
- `OpenAIAgent`
- `FunctionCallingAgent`
- `StructuredPlannerAgent`
- `AgentRunner`
- `BaseAgent`

Detection uses class name + module path (`llama_index` in module), including MRO walking for subclasses.

---

## What Gets Wrapped

### LLMs

Each agent's `llm` attribute (via `._llm` or `.llm`) is wrapped with a transparent proxy:

| Attribute | Value |
|-----------|-------|
| Span name | `llamaindex.<AgentClass>.llm.<method>` (e.g., `llamaindex.ReActAgent.llm.achat`) |
| Span type | `LLM` |
| Methods wrapped | `chat`, `achat`, `complete`, `acomplete`, `stream_chat`, `astream_chat`, `stream_complete`, `astream_complete`, `chat_with_tools`, `achat_with_tools`, `stream_chat_with_tools`, `astream_chat_with_tools` |

**LLM span attributes captured:**

| Attribute | Source | Example |
|-----------|--------|---------|
| `model` | LlamaIndex adapter unwraps `ChatResponse.message.raw` to access the underlying provider response | `gpt-4o-mini-2024-07-18` |
| `provider` | Module path detection — `llama_index.llms.openai` → `openai` | `openai` |
| `tokens_input` | Extracted from the raw provider response's usage object | `634` |
| `tokens_output` | Extracted from the raw provider response's usage object | `45` |
| `cost_usd` | Calculated from tokens × pricing registry rates | `0.000122` |
| `input` | Messages passed to the LLM — `messages`, `user_msg`, or `chat_history` kwargs | `system: You are designed to help...` |
| `output` | Response text, or `tool_call: func(args)` for tool-calling responses | `tool_call: add({"a": 3, "b": 5})` |
| `agent` | Inherited from `@llamaindex_agent` span | `calc_agent` |

**Why `chat_with_tools`?** `FunctionAgent` uses `llm.achat_with_tools()` instead of `llm.achat()`. Without wrapping these methods, FunctionAgent LLM calls would be invisible.

**Provider detection:** LlamaIndex LLM classes live at `llama_index.llms.<provider>`. Rastir maps these module paths: `openai`, `anthropic`, `gemini`, `azure_openai`, `bedrock`, `mistral`, `groq`, `cohere`.

**Token extraction:** LlamaIndex wraps provider responses in `ChatResponse`. The LlamaIndex adapter unwraps `ChatResponse.message.raw` to access the original provider response (e.g., OpenAI `ChatCompletion`), which the provider adapter then extracts tokens from.

### Tools

Each agent's tools (via `._tools` or `.tools`) are wrapped with a transparent proxy:

| Attribute | Value |
|-----------|-------|
| Span name | `<tool_name>.acall` (e.g., `add.acall`, `get_weather.acall`) |
| Span type | `TOOL` |
| Methods wrapped | `call`, `__call__`, `acall` |

**Tool span attributes captured:**

| Attribute | Source | Example |
|-----------|--------|---------|
| `tool.input` | Keyword arguments passed to `.acall(**kwargs)` | `{'a': 3, 'b': 5}` |
| `tool.output` | Return value from the tool function | `8` |
| `agent` | Inherited from `@llamaindex_agent` span | `calc_agent` |

**Why `acall`?** LlamaIndex invokes tools via `tool.acall(**tool_input)` (async), not `tool.call()`. Without wrapping `acall`, tool spans would not appear.

### Skip Already-Wrapped Objects

- LLMs with `_rastir_wrapped = True` are not re-wrapped
- Tools with `_rastir_wrapped = True` are not re-wrapped

---

## MCP Tool Tracing

### How MCP Tools Work in LlamaIndex

LlamaIndex uses `llama-index-tools-mcp` to connect to MCP servers. `McpToolSpec.to_tool_list_async()` converts MCP tools into regular `FunctionTool` objects — the decorator wraps them like any other tool.

```python
from llama_index.tools.mcp import BasicMCPClient, McpToolSpec

mcp_client = BasicMCPClient("http://localhost:8080/mcp")
mcp_tool_spec = McpToolSpec(client=mcp_client)
mcp_tools = await mcp_tool_spec.to_tool_list_async()

agent = ReActAgent(llm=llm, tools=mcp_tools, streaming=False)

@llamaindex_agent(agent_name="mcp_agent")
async def run(agent, query):
    return await agent.run(query)
```

### Trace Propagation to MCP Servers

`@llamaindex_agent` auto-discovers `BasicMCPClient` instances in agent tools and injects the `traceparent` header. This also updates the underlying `httpx.AsyncClient` headers so the trace context reaches the MCP server's HTTP transport.

This produces a fully linked trace:

```
mcp_agent (AGENT)
├── llamaindex.ReActAgent.llm.achat (LLM)
├── mcpserver:get_weather (TOOL)           ← server span (same trace)
│   └── get_weather.acall (TOOL)           ← client tool span
├── llamaindex.ReActAgent.llm.achat (LLM)  — final answer
```

**How it works:** The decorator discovers `BasicMCPClient` objects, sets `traceparent` on both `client.headers` and `client.http_client.headers`. When the MCP tool calls the server, the `traceparent` header links server-side spans back to the client trace.

---

## Agent Types

### ReActAgent

Uses a Thought → Action → Observation loop. Calls `llm.achat()` for each reasoning step.

```python
agent = ReActAgent(llm=llm, tools=tools, streaming=False)
```

**Note:** Set `streaming=False` — streaming uses `astream_chat` which returns an async generator, and token extraction from streams requires additional handling.

### FunctionAgent

Uses the LLM's native function/tool calling capability. Calls `llm.achat_with_tools()`.

```python
from llama_index.core.agent import FunctionAgent

agent = FunctionAgent(llm=llm, tools=tools)
```

Both agent types are fully supported — the decorator wraps all relevant LLM methods.

---

## Coding Patterns

### Pattern 1: ReActAgent with local tools (most common)

```python
agent = ReActAgent(llm=llm, tools=tools, streaming=False)

@llamaindex_agent(agent_name="calc_agent")
async def run(agent, query):
    return await agent.run(query)
```

### Pattern 2: FunctionAgent with MCP tools

```python
mcp_tools = await mcp_tool_spec.to_tool_list_async()
agent = FunctionAgent(llm=llm, tools=mcp_tools)

@llamaindex_agent(agent_name="mcp_agent")
async def run(agent, query):
    return await agent.run(query)
```

### Pattern 3: Bare decorator (name defaults to function name)

```python
@llamaindex_agent
async def research_pipeline(agent, query):
    return await agent.run(query)

# Agent span name will be "research_pipeline"
```

### Pattern 4: Cost tracking with pricing registry

```python
from rastir import configure
from rastir.config import get_pricing_registry

configure(service="my-app", push_url="...", enable_cost_calculation=True)

pr = get_pricing_registry()
pr.register("openai", "gpt-4o-mini", input_price=0.15, output_price=0.60)
# Also register the dated variant that OpenAI returns
pr.register("openai", "gpt-4o-mini-2024-07-18", input_price=0.15, output_price=0.60)

@llamaindex_agent(agent_name="my_agent")
async def run(agent, query):
    return await agent.run(query)
# Each LLM span will now include cost_usd
```

### Pattern 5: Agent reuse across calls

```python
@llamaindex_agent(agent_name="reusable")
async def run(agent, query):
    return await agent.run(query)

# Safe to call multiple times — originals restored after each call
result1 = await run(agent, "Hello")
result2 = await run(agent, "World")
```

---

## Restore After Execution

After the agent run completes (success or error), `@llamaindex_agent` restores:
- Original LLM on the agent (`._llm` or `.llm`)
- Original tools list on the agent (`._tools` or `.tools`)

This means the agent can be safely reused across multiple calls with no accumulated wrapping.

---

## Error Handling

If the decorated function raises an exception:
- The agent span records the error (type + message)
- Span status is set to `ERROR`
- The exception is re-raised unchanged
- Originals are still restored (via `finally` block)

---

## Span Hierarchy

A typical LlamaIndex trace looks like this:

```
@llamaindex_agent agent span
│
├── llamaindex.ReActAgent.llm.achat (LLM)     — model=gpt-4o-mini, tokens, cost
│                                                input=messages, output=tool_call
├── add.acall (TOOL)                           — input={'a': 3, 'b': 5}, output=8
├── llamaindex.ReActAgent.llm.achat (LLM)      — tool result fed back to LLM
│                                                output=tool_call: multiply(...)
├── multiply.acall (TOOL)                      — input={'a': 8, 'b': 2}, output=16
├── llamaindex.ReActAgent.llm.achat (LLM)      — final answer, has text output
```

With MCP tools:

```
@llamaindex_agent agent span
│
├── llamaindex.FunctionAgent.llm.achat_with_tools (LLM)
├── mcpserver:get_population (TOOL)            — server span (via traceparent)
│   └── get_population.acall (TOOL)            — client tool execution
├── llamaindex.FunctionAgent.llm.achat_with_tools (LLM) — final answer
```

All child spans inherit the `agent` label, so Prometheus metrics are grouped by agent.

---

## Span Attributes in Tempo

Here's what you'll see in Tempo/Grafana for each span type:

### Agent span

| Attribute | Example |
|-----------|---------|
| `rastir.span_type` | `agent` |
| `rastir.agent_name` | `calc_agent` |

### LLM span

| Attribute | Example |
|-----------|---------|
| `rastir.span_type` | `llm` |
| `rastir.model` | `gpt-4o-mini-2024-07-18` |
| `rastir.provider` | `openai` |
| `rastir.tokens_input` | `634` |
| `rastir.tokens_output` | `45` |
| `rastir.cost_usd` | `0.000122` |
| `rastir.input` | `system: You are designed to help...` |
| `rastir.output` | `tool_call: add({"a": 3, "b": 5})` or `The answer is 8` |
| `rastir.agent` | `calc_agent` |

### Tool span

| Attribute | Example |
|-----------|---------|
| `rastir.span_type` | `tool` |
| `rastir.tool.input` | `{'a': 3, 'b': 5}` |
| `rastir.tool.output` | `8` |
| `rastir.agent` | `calc_agent` |

---

## Prometheus Metrics Produced

| Metric | Source |
|--------|--------|
| `rastir_llm_calls_total{model, provider, agent}` | Wrapped LLM method calls |
| `rastir_tokens_input_total{model, provider, agent}` | Token extraction from provider response |
| `rastir_tokens_output_total{model, provider, agent}` | Token extraction from provider response |
| `rastir_cost_total{model, provider, agent}` | Cost calculation from pricing registry |
| `rastir_duration_seconds{span_type="llm"}` | LLM call latency |
| `rastir_tool_calls_total{tool_name, agent}` | Tool `.acall()` invocation |
| `rastir_duration_seconds{span_type="tool"}` | Tool invocation latency |
| `rastir_duration_seconds{span_type="agent"}` | Entire agent execution latency |

---

## Technical Notes

### LlamaIndex Token Extraction

LlamaIndex wraps provider responses in its own `ChatResponse` schema. The `ChatResponse.message.raw` field holds the original provider response (e.g., OpenAI `ChatCompletion`). The LlamaIndex adapter unwraps this and delegates to the provider-specific adapter (OpenAI, Anthropic, etc.) for token extraction.

### LlamaIndex Output Extraction

- **Text responses**: Extracted from `ChatResponse.message.content`
- **Tool-calling responses**: When the LLM returns tool calls with no text content, Rastir extracts tool call blocks from `message.blocks` (for `ToolCallBlock`) or `message.additional_kwargs["tool_calls"]`, formatting them as `tool_call: func_name(args)`

### LlamaIndex Input Extraction

- **ReActAgent**: Passes `messages=` kwarg to `llm.achat(messages=[...])`
- **FunctionAgent**: Passes `chat_history=` kwarg to `llm.achat_with_tools(chat_history=[...])`
- Rastir captures both patterns via `_capture_llm_input()`

### Pydantic Compatibility

LlamaIndex agents are Pydantic `BaseModel` subclasses (v2). Unlike CrewAI, LlamaIndex's Pydantic models accept proxy wrappers for `llm` and `tools` fields — no in-place patching workaround is needed. The decorator sets wrapped objects directly via `setattr()`.

### Streaming Limitation

When `streaming=True` (the default for `ReActAgent`), LlamaIndex uses `astream_chat` which returns an async generator. Token extraction from async generators is a known limitation. Set `streaming=False` on agents to ensure full token capture.
