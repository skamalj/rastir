---
layout: default
title: LangGraph
parent: Frameworks
nav_order: 1
---

# LangGraph Integration

Rastir provides `@langgraph_agent` — a single decorator that instruments LangGraph compiled-graph execution. It **auto-discovers** LLMs, tools, and nodes inside the graph — no manual wrapping needed.

> **Tip:** You can also use `@framework_agent` which auto-detects LangGraph graphs from function arguments. The dedicated `@langgraph_agent` decorator is still available for explicit control.

---

## Quick Start

```python
from rastir import configure, langgraph_agent
from langgraph.prebuilt import create_react_agent
from langchain_openai import ChatOpenAI

configure(service="my-app", push_url="http://localhost:8080")

model = ChatOpenAI(model="gpt-4o")
tools = [search_tool, calculator_tool]

@langgraph_agent(agent_name="react_agent")
def run(query):
    graph = create_react_agent(model, tools)
    return graph.invoke({"messages": [("user", query)]})

result = run("What is 2+2?")
```

This produces the following span tree:

```
react_agent (AGENT)
  ├── node:agent (TRACE)
  │   └── langgraph.llm.gpt-4o.invoke (LLM)
  ├── node:tools (TRACE)
  │   └── langgraph.tool.calculator.invoke (TOOL)
  └── node:agent (TRACE)
      └── langgraph.llm.gpt-4o.invoke (LLM)
```

---

## API Reference

### `langgraph_agent()`

```python
from rastir import langgraph_agent

@langgraph_agent
def run(graph): ...

@langgraph_agent(agent_name="my_agent")
def run(graph): ...
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `agent_name` | `str` | Function name | Name for the outer agent span |

**Supports:**
- Bare usage (`@langgraph_agent`) and parameterized (`@langgraph_agent(...)`)
- Sync and async functions
- `CompiledGraph` passed as positional or keyword argument

---

## What Gets Auto-Discovered

When the decorated function is called, `@langgraph_agent` scans all arguments for LangGraph `CompiledGraph` objects. For each graph found, it automatically discovers and wraps:

### 1. Graph Nodes → TRACE spans

Every node in the graph (except `__start__`) gets a `TRACE` span named `node:<name>`, giving you execution-level visibility into the graph flow.

| Attribute | Value |
|-----------|-------|
| Span name | `node:<node_name>` |
| Span type | `TRACE` |

### 2. Chat Models → LLM spans

All `BaseChatModel` instances used inside the graph are discovered and wrapped automatically.

| Attribute | Value |
|-----------|-------|
| Span name | `langgraph.llm.<model_name>` |
| Span type | `LLM` |
| Methods wrapped | `invoke`, `ainvoke`, `stream`, `astream`, `generate`, `agenerate`, `batch`, `abatch` |

### 3. Tools → TOOL spans

All tools inside the graph's `ToolNode` are discovered and wrapped.

| Attribute | Value |
|-----------|-------|
| Span name | `langgraph.tool.<tool_name>` |
| Span type | `TOOL` |
| Methods wrapped | `invoke`, `ainvoke`, `_run`, `_arun`, `run`, `arun` |

### 4. MCP Clients → Trace Propagation

The decorator auto-discovers MCP client objects (e.g. `MultiServerMCPClient`) from three locations:

1. **Function arguments** — positional and keyword args
2. **Function closures** — variables captured in the enclosing scope
3. **Function globals** — module-level variables referenced in the function body

For each discovered MCP client, the `traceparent` header is automatically injected into all connections before execution. This enables **distributed tracing** across MCP tool boundaries — the same `trace_id` links the LangGraph agent span to the MCP server spans.

| Client type | Detection |
|-------------|----------|
| `MultiServerMCPClient` | Sets `traceparent` on each connection's headers dict |

No manual `wrap(session)` call is needed when using `@langgraph_agent`.

---

## Coding Patterns

### Pattern 1: `create_react_agent` (recommended)

The simplest approach. LangGraph creates the graph; Rastir auto-discovers everything inside.

```python
from langgraph.prebuilt import create_react_agent
from langchain_openai import ChatOpenAI

model = ChatOpenAI(model="gpt-4o")
tools = [my_search_tool, my_calculator]

@langgraph_agent(agent_name="react")
def run(query):
    graph = create_react_agent(model, tools)
    return graph.invoke({"messages": [("user", query)]})
```

### Pattern 2: Graph passed as argument

```python
graph = create_react_agent(model, tools)

@langgraph_agent(agent_name="react")
def run(graph, query):
    return graph.invoke({"messages": [("user", query)]})

result = run(graph, "Hello")
```

### Pattern 3: Manual `StateGraph` with model

```python
from langgraph.graph import StateGraph

model = ChatOpenAI(model="gpt-4o")

def agent_node(state):
    return {"messages": [model.invoke(state["messages"])]}

def tool_node(state):
    # ... tool execution logic
    pass

graph = StateGraph(State)
graph.add_node("agent", agent_node)
graph.add_node("tools", tool_node)
graph.add_edge("agent", "tools")
compiled = graph.compile()

@langgraph_agent(agent_name="custom_agent")
def run(query):
    return compiled.invoke({"messages": [("user", query)]})
```

### Pattern 4: Manual `StateGraph` with closure model

```python
def create_graph(model):
    def agent_node(state):
        return {"messages": [model.invoke(state["messages"])]}

    graph = StateGraph(State)
    graph.add_node("agent", agent_node)
    return graph.compile()

compiled = create_graph(ChatOpenAI(model="gpt-4o"))

@langgraph_agent
def run(query):
    return compiled.invoke({"messages": [("user", query)]})
```

### Pattern 5: Async graph

```python
@langgraph_agent(agent_name="async_react")
async def run(query):
    graph = create_react_agent(model, tools)
    return await graph.ainvoke({"messages": [("user", query)]})
```

The decorator auto-detects `async def` and uses the async code path.

### Pattern 6: Graph created outside the function

```python
graph = create_react_agent(model, tools)

@langgraph_agent(agent_name="react")
def run(query):
    return graph.invoke({"messages": [("user", query)]})
```

**Tip:** For most reliable discovery, pass the graph as an argument:

```python
@langgraph_agent(agent_name="react")
def run(graph, query):
    return graph.invoke({"messages": [("user", query)]})

run(graph, "Hello")
```

### Pattern 7: MCP tools with distributed tracing

LangGraph handles MCP tools natively — they're converted to LangChain tools by the framework. Rastir wraps them like any other tool **and** auto-discovers MCP client objects to inject trace context.

```python
from langchain_mcp_adapters.client import MultiServerMCPClient

mcp_client = MultiServerMCPClient({
    "math": {"url": "http://localhost:19879/sse", "transport": "sse"}
})

async with mcp_client:
    tools = mcp_client.get_tools()
    graph = create_react_agent(model, tools)

    @langgraph_agent(agent_name="mcp_agent")
    async def run(mcp_client, query):
        return await graph.ainvoke({"messages": [("user", query)]})

    result = await run(mcp_client, "What is 2+2?")
```

The decorator discovers `mcp_client` from the function arguments (it also checks closures and globals) and automatically sets the `traceparent` header on all MCP connections. The MCP server receives the trace context, enabling end-to-end distributed tracing with a single `trace_id`.

{: .note }
> The MCP client can be passed as an argument, captured in a closure, or referenced as a module global — the decorator will find it in all three cases.

---

## Restore After Execution

After the decorated function completes (success or error), all original objects are restored:
- Chat models, tools, and node functions are put back to their originals
- The graph can be reused across multiple calls
- Originals are restored even if an exception is raised

---

## Error Handling

If the decorated function raises an exception:
- The agent span records the error (type + message)
- Span status is set to `ERROR`
- The exception is re-raised unchanged

---

## Span Hierarchy

```
@langgraph_agent agent span
│
├── node:agent (TRACE)
│   └── langgraph.llm.gpt-4o.invoke (LLM)
│       → model, tokens_in, tokens_out, latency
│
├── node:tools (TRACE)
│   └── langgraph.tool.search.invoke (TOOL)
│       → tool_name, latency
│
├── node:agent (TRACE)
│   └── langgraph.llm.gpt-4o.invoke (LLM)
│
└── (more iterations if the agent loops)
```

All child spans inherit the `agent` label from the outer span, so Prometheus metrics are grouped by agent.

---

## Prometheus Metrics Produced

| Metric | Source |
|--------|--------|
| `rastir_llm_calls_total{model, provider, agent}` | Wrapped chat model calls |
| `rastir_tokens_input_total{model, provider, agent}` | Token extraction from LLM response |
| `rastir_tokens_output_total{model, provider, agent}` | Token extraction from LLM response |
| `rastir_duration_seconds{span_type="llm"}` | LLM call latency |
| `rastir_tool_calls_total{tool_name, agent}` | Wrapped tool invocations |
| `rastir_duration_seconds{span_type="tool"}` | Tool invocation latency |
| `rastir_duration_seconds{span_type="agent"}` | Entire graph execution latency |

**Recommendation:** Always pass the compiled graph as an argument to the decorated function for most reliable results.
