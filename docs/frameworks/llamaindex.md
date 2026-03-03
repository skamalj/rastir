---
layout: default
title: LlamaIndex
parent: Frameworks
nav_order: 3
---

# LlamaIndex Integration

Rastir provides `@llamaindex_agent` — a decorator that instruments LlamaIndex agent workflows. Unlike LangGraph and CrewAI which auto-discover LLMs, LlamaIndex requires you to explicitly `wrap()` LLMs and tools before passing them to the agent.

---

## Quick Start

```python
from rastir import configure, llamaindex_agent, wrap
from llama_index.llms.openai import OpenAI
from llama_index.core.agent import ReActAgent

configure(service="my-app", push_url="http://localhost:8080")

llm = wrap(OpenAI(model="gpt-4o"), span_type="llm")
tools = [wrap(t, span_type="tool") for t in my_tools]

agent = ReActAgent.from_tools(tools, llm=llm)

@llamaindex_agent(agent_name="qa_agent")
def run(agent, query):
    return agent.chat(query)

result = run(agent, "What is 2+2?")
```

This produces:

```
qa_agent (AGENT)
├── OpenAI.chat (LLM) — model, tokens, latency
├── search.call (TOOL) — per-invocation
├── OpenAI.chat (LLM)
└── ...
```

---

## Why `wrap()` Is Required

You wrap LLMs and tools explicitly with `wrap()` before passing them to the agent. This works with every LlamaIndex LLM provider and tool type — including MCP tools from `McpToolSpec`.

The `@llamaindex_agent` decorator then:
1. Creates an `AGENT` span around the entire execution
2. The pre-wrapped LLMs and tools emit their own child spans during execution

---

## API Reference

### `llamaindex_agent()`

```python
from rastir import llamaindex_agent

@llamaindex_agent
def run(agent): ...

@llamaindex_agent(agent_name="my_agent")
def run(agent): ...
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `agent_name` | `str` | Function name | Name for the outer agent span |

**Supports:**
- Bare usage (`@llamaindex_agent`) and parameterized (`@llamaindex_agent(...)`)
- Sync and async functions
- LlamaIndex agent passed as positional or keyword argument

### Recognised Agent Types

The decorator recognises these LlamaIndex agent classes (and their subclasses):

- `ReActAgent`
- `OpenAIAgent`
- `FunctionAgent`
- `FunctionCallingAgent`
- `StructuredPlannerAgent`
- `AgentRunner`
- `BaseAgent`

Detection uses class name + module (`llama_index` in module path), including MRO walking for subclasses.

---

## What Gets Wrapped

### LLMs

When the agent is passed to the decorated function, `@llamaindex_agent` wraps the agent's LLM (found via `._llm` or `.llm`):

| Attribute | Value |
|-----------|-------|
| Span name | `llamaindex.<AgentClass>.llm` |
| Span type | `LLM` |
| Methods wrapped | `chat`, `complete`, `achat`, `acomplete`, `stream_chat`, `stream_complete`, `astream_chat`, `astream_complete` |

### Tools

Existing tools (found via `._tools` or `.tools`) are wrapped:

| Attribute | Value |
|-----------|-------|
| Span name | Tool's `metadata.name` or `name` attribute |
| Span type | `TOOL` |
| Methods wrapped | `call`, `__call__` |

### Skip Already-Wrapped Objects

If an LLM or tool already has `_rastir_wrapped = True`, the decorator does not re-wrap it.

---

## Coding Patterns

### Pattern 1: ReActAgent with local tools

```python
from llama_index.core.agent import ReActAgent
from llama_index.core.tools import FunctionTool

llm = wrap(OpenAI(model="gpt-4o"), span_type="llm")

def add(a: int, b: int) -> int:
    return a + b

tools = [wrap(FunctionTool.from_defaults(fn=add), span_type="tool")]
agent = ReActAgent.from_tools(tools, llm=llm)

@llamaindex_agent(agent_name="calc_agent")
def run(agent, query):
    return agent.chat(query)
```

### Pattern 2: Bare decorator

```python
@llamaindex_agent
def my_qa_agent(agent, query):
    return agent.chat(query)

# Agent span name defaults to "my_qa_agent"
```

### Pattern 3: Async

```python
@llamaindex_agent(agent_name="async_agent")
async def run(agent, query):
    return await agent.achat(query)
```

### Pattern 4: MCP tools via McpToolSpec

LlamaIndex handles MCP natively via `llama-index-tools-mcp`. MCP tools become regular `FunctionTool` objects — wrap them the same way:

```python
from llama_index.tools.mcp import McpToolSpec

mcp_spec = McpToolSpec(url="http://localhost:3001/sse")
mcp_tools = await mcp_spec.to_tool_list_async()

# Wrap each MCP tool for observability
wrapped_tools = [wrap(t, span_type="tool") for t in mcp_tools]

agent = ReActAgent.from_tools(wrapped_tools, llm=wrapped_llm)

@llamaindex_agent(agent_name="mcp_agent")
def run(agent, query):
    return agent.chat(query)
```

No special MCP handling needed — `wrap()` works on MCP-sourced tools the same as local tools.

### Pattern 5: OpenAIAgent

```python
from llama_index.agent.openai import OpenAIAgent

agent = OpenAIAgent.from_tools(wrapped_tools, llm=wrapped_llm)

@llamaindex_agent(agent_name="openai_agent")
def run(agent, query):
    return agent.chat(query)
```

### Pattern 6: FunctionCallingAgent

```python
from llama_index.core.agent import FunctionCallingAgent

agent = FunctionCallingAgent.from_tools(wrapped_tools, llm=wrapped_llm)

@llamaindex_agent(agent_name="fc_agent")
def run(agent, query):
    return agent.chat(query)
```

### Pattern 7: Pre-wrapped with `wrap()` before agent creation

```python
llm = wrap(OpenAI(model="gpt-4o"), span_type="llm")
tools = [
    wrap(FunctionTool.from_defaults(fn=search), span_type="tool"),
    wrap(FunctionTool.from_defaults(fn=calculate), span_type="tool"),
]

# Agent created with already-wrapped components
agent = ReActAgent.from_tools(tools, llm=llm)

@llamaindex_agent(agent_name="pre_wrapped")
def run(agent, query):
    return agent.chat(query)

# The decorator detects _rastir_wrapped and skips re-wrapping
```

### Pattern 8: Agent reuse across calls

```python
@llamaindex_agent(agent_name="reusable")
def run(agent, query):
    return agent.chat(query)

# Safe to call multiple times — originals restored after each call
result1 = run(agent, "Hello")
result2 = run(agent, "World")
```

---

## Restore After Execution

After the decorated function completes (success or error), `@llamaindex_agent` restores:
- Original LLM on the agent (`._llm` or `.llm`)
- Original tools list on the agent (`._tools` or `.tools`)

This means the agent can be safely reused.

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
@llamaindex_agent agent span
│
├── llamaindex.ReActAgent.llm.chat (LLM call 1)
├── search_tool.call (tool invocation)
├── llamaindex.ReActAgent.llm.chat (LLM call 2)
└── ...
```

All child spans inherit the `agent` label, so Prometheus metrics are grouped by agent.

---

## Prometheus Metrics Produced

| Metric | Source |
|--------|--------|
| `rastir_llm_calls_total{model, provider, agent}` | Wrapped LLM method calls |
| `rastir_tokens_input_total{model, provider, agent}` | Token extraction from LLM response |
| `rastir_tokens_output_total{model, provider, agent}` | Token extraction from LLM response |
| `rastir_duration_seconds{span_type="llm"}` | LLM call latency |
| `rastir_tool_calls_total{tool_name, agent}` | Wrapped tool calls |
| `rastir_duration_seconds{span_type="tool"}` | Tool invocation latency |
| `rastir_duration_seconds{span_type="agent"}` | Entire agent execution latency |

**Tip:** Always pass the agent as an argument to the decorated function and pre-wrap LLMs and tools with `wrap()` for predictable results.
