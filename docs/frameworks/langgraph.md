---
layout: default
title: LangGraph
parent: Frameworks
nav_order: 1
---

# LangGraph Integration

Rastir provides `@langgraph_agent` — a single decorator that instruments LangGraph compiled-graph execution. It **auto-discovers** LLMs, tools, and nodes inside the graph — no manual wrapping needed.

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

When the decorated function is called, `@langgraph_agent` scans all arguments for LangGraph `CompiledGraph` objects. For each graph found, it walks the internal structure to discover and wrap:

### 1. Graph Nodes → TRACE spans

Every node in `graph.nodes` (except `__start__`) gets its function wrapped with a `TRACE` span named `node:<name>`. This gives you execution-level visibility into the graph flow.

| Attribute | Value |
|-----------|-------|
| Span name | `node:<node_name>` |
| Span type | `TRACE` |
| `langgraph.node` | The node name |

Both `func` (sync) and `afunc` (async) are wrapped if present.

### 2. Chat Models → LLM spans

The decorator walks each node's Runnable chain to find `BaseChatModel` instances:

| Location found | How discovered |
|---|---|
| `RunnableBinding.bound` | Direct attribute check |
| `RunnableSequence.first / .last / .middle` | Recursive walk |
| `RunnableCallable.func` closure | `__closure__` cell inspection |
| `RunnableCallable.func` globals | `__globals__` by `co_names` check |

Each chat model is wrapped with:

| Attribute | Value |
|-----------|-------|
| Span name | `langgraph.llm.<model_name>` |
| Span type | `LLM` |
| Methods wrapped | `invoke`, `ainvoke`, `stream`, `astream`, `generate`, `agenerate`, `batch`, `abatch` |

### 3. Tools → TOOL spans

`ToolNode` instances are detected by class name. Each tool in `toolnode._tools_by_name` is wrapped:

| Attribute | Value |
|-----------|-------|
| Span name | `langgraph.tool.<tool_name>` |
| Span type | `TOOL` |
| Methods wrapped | `invoke`, `ainvoke`, `_run`, `_arun`, `run`, `arun` |

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

**How discovery works:** The model is captured in a closure inside the agent node's function. Rastir inspects `func.__closure__` cells to find the `RunnableBinding(bound=ChatOpenAI)` and wraps its `.bound`.

### Pattern 2: Graph passed as argument

```python
graph = create_react_agent(model, tools)

@langgraph_agent(agent_name="react")
def run(graph, query):
    return graph.invoke({"messages": [("user", query)]})

result = run(graph, "Hello")
```

**How discovery works:** The decorator scans all function arguments for `CompiledGraph` instances.

### Pattern 3: Manual `StateGraph` with global model

```python
from langgraph.graph import StateGraph

model = ChatOpenAI(model="gpt-4o")  # module-level variable

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

**How discovery works:** The model is a module-level variable. Rastir inspects `agent_node.__globals__` by looking at `agent_node.__code__.co_names` to find only the global names the function actually references. It finds `model` → detects it as a `BaseChatModel` subclass → wraps it.

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

**How discovery works:** The model is captured in `agent_node.__closure__`. Rastir walks the closure cells, finds the `ChatOpenAI` instance, and wraps it.

### Pattern 5: Model inside RunnableSequence

```python
from langchain_core.runnables import RunnableSequence

prompt = ChatPromptTemplate.from_messages([...])
model = ChatOpenAI(model="gpt-4o")
chain = prompt | model  # creates RunnableSequence

def agent_node(state):
    return {"messages": [chain.invoke(state["messages"])]}

graph = StateGraph(State)
graph.add_node("agent", agent_node)
compiled = graph.compile()

@langgraph_agent
def run(query):
    return compiled.invoke({"messages": [("user", query)]})
```

**How discovery works:** Rastir walks the closure cells, finds the `RunnableSequence`, then recursively walks `.first` and `.last` to find the model inside a `RunnableBinding.bound`.

### Pattern 6: Async graph

```python
@langgraph_agent(agent_name="async_react")
async def run(query):
    graph = create_react_agent(model, tools)
    return await graph.ainvoke({"messages": [("user", query)]})
```

The decorator auto-detects `async def` and uses the async code path. All wrapping and discovery is identical — `afunc` is also traced in addition to `func`.

### Pattern 7: Graph created outside the function

```python
# Graph compiled once at module startup
graph = create_react_agent(model, tools)

@langgraph_agent(agent_name="react")
def run(query):
    return graph.invoke({"messages": [("user", query)]})
```

**Important:** This works because the graph is passed implicitly via the function's closure. The decorator scans all arguments first — if the graph is found there, it's used. Otherwise, the compiled graph referenced inside the function body still gets invoked with the pre-discovered wrapped models.

**Note:** If the graph is not passed as an argument AND is only referenced inside the function body (not as a closure or global that Rastir scans), the LLMs/tools won't be auto-discovered. To be safe, pass the graph as an argument:

```python
@langgraph_agent(agent_name="react")
def run(graph, query):
    return graph.invoke({"messages": [("user", query)]})

run(graph, "Hello")
```

### Pattern 8: MCP tools

LangGraph handles MCP tools natively — they're converted to LangChain tools by the framework. Rastir wraps them like any other tool inside `ToolNode`.

```python
from langchain_mcp_adapters.tools import load_mcp_tools

tools = await load_mcp_tools(session)
graph = create_react_agent(model, tools)

@langgraph_agent(agent_name="mcp_agent")
def run(query):
    return graph.invoke({"messages": [("user", query)]})
```

No special MCP handling is needed — `@langgraph_agent` wraps MCP-sourced tools the same way it wraps any tool in the `ToolNode._tools_by_name` dict.

---

## How Discovery Works Under the Hood

The decorator walks the compiled graph's internal structure:

```
graph.nodes = {
    "__start__": PregelNode(bound=RunnableCallable)    ← SKIPPED
    "agent":     PregelNode(bound=RunnableCallable)    ← WALKED
    "tools":     PregelNode(bound=ToolNode)            ← WALKED
}
```

For each node's `.bound`:

1. **`_wrap_runnable(bound)`** — pattern-matches on `type(obj).__name__`:
   - `ToolNode` → wrap each tool in `_tools_by_name`
   - `RunnableBinding` → check `.bound` — if `BaseChatModel`, wrap it; else recurse
   - `RunnableSequence` → recurse into `.first`, `.last`, `.middle`
   - `RunnableCallable` → inspect `.func`'s closures and globals

2. **`_wrap_node_func(bound, name)`** — replace `bound.func` with a traced wrapper that emits a `TRACE` span. Also wraps `bound.afunc` if present.

**Order matters:** LLM/tool discovery runs *before* node func wrapping, because the traced wrapper would replace the original function and its closure/globals wouldn't be traversable.

---

## Restore After Execution

After the decorated function completes (success or error), all original objects are restored:
- Chat models put back on `RunnableBinding.bound` or function globals
- Tools put back in `ToolNode._tools_by_name`
- Node functions put back on `bound.func` / `bound.afunc`

This means:
- The graph can be reused across multiple calls
- No accumulated wrapping layers from repeated calls
- Originals are restored even if an exception is raised

---

## Double-Wrap Prevention

All wrapping operations check for marker attributes before wrapping:
- Chat models: `_rastir_wrapped`
- Tools: `_rastir_wrapped`
- Node functions: `_rastir_node_traced`

Calling `@langgraph_agent` on a function that uses an already-wrapped graph is safe — no double wrapping occurs.

---

## Error Handling

If the decorated function raises an exception:
- The agent span records the error (type + message)
- Span status is set to `ERROR`
- The exception is re-raised unchanged
- All originals are still restored (via `finally` block)

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

---

## Limitations and Edge Cases

### Covered Patterns

All common LangGraph patterns are supported:

| Pattern | Status |
|---------|--------|
| `create_react_agent` | ✅ Fully auto-discovered |
| Manual `StateGraph` with global model | ✅ Discovered via `__globals__` |
| Manual `StateGraph` with closure model | ✅ Discovered via `__closure__` |
| Model inside `RunnableSequence` | ✅ Recursive walk through `.first`/`.last`/`.middle` |
| Model inside `RunnableBinding` | ✅ Direct `.bound` check |
| `ToolNode` with multiple tools | ✅ All tools in `_tools_by_name` wrapped |
| MCP tools via `langchain_mcp_adapters` | ✅ Treated as regular tools |
| Async `ainvoke` / `astream` | ✅ Both sync and async paths |
| Graph reuse across calls | ✅ Originals restored after each call |
| Nested graphs | ✅ If the inner graph is an argument or in a closure |

### Known Constraints

| Scenario | Behaviour |
|----------|-----------|
| Graph not passed as argument and not in closure/globals | LLMs/tools won't be discovered — pass the graph as an argument |
| Custom Runnable subclasses (not `RunnableBinding`/`RunnableSequence`/`RunnableCallable`) | May not be walked — add a fallback `.bound`/`.first` attribute if needed |
| Models constructed dynamically inside a node function body (not captured in closure or globals) | Not discoverable — move to closure or module scope |
| `__start__` node | Intentionally skipped |

**Recommendation:** Always pass the compiled graph as an argument to the decorated function. This guarantees discovery works regardless of how the graph was constructed.
