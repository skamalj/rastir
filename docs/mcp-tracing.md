---
layout: default
title: MCP Distributed Tracing
nav_order: 4
---

# MCP Distributed Tracing

Rastir supports distributed tracing across [MCP](https://modelcontextprotocol.io/) (Model Context Protocol) tool boundaries. Trace context flows automatically from agent to tool server, giving you end-to-end visibility across process and network boundaries.

---

## How It Works

Rastir uses **argument-based trace propagation** — the simplest possible approach. When your agent calls a remote MCP tool, Rastir injects two hidden fields (`rastir_trace_id`, `rastir_span_id`) into the tool arguments dict. On the server side, `@mcp_endpoint` pops these fields and creates a child span linked to the client.

```
┌─────────────────────────────┐           ┌──────────────────────────────┐
│  Client (your agent)        │           │  MCP Tool Server              │
│                             │           │                               │
│  @agent                     │           │  @mcp.tool()                  │
│  └── call_tool("search",   │  HTTP     │  @mcp_endpoint                │
│        {"query": "hello",  │ ────────▸ │  async def search(query):     │
│         "rastir_trace_id":  │  args     │    ...  # server span created │
│              "abc...",      │           │                               │
│         "rastir_span_id":   │           │  rastir_trace_id / _span_id   │
│              "def..."})     │           │  popped before your function  │
│                             │           │  sees them                    │
│  client span: remote=true   │           │  server span: remote=false    │
└─────────────────────────────┘           └──────────────────────────────┘
```

If the server does **not** use `@mcp_endpoint`, the extra fields are silently dropped by FastMCP's Pydantic validation — no errors, it just works without server-side spans.

---

## API Reference

### `@trace_remote_tools`

Wraps an MCP session-returning function so every `call_tool()` invocation creates a client span and injects trace context.

```python
from rastir import trace_remote_tools

@trace_remote_tools
async def get_session():
    async with streamable_http_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return session
```

Or wrap inline after session is created:

```python
@trace_remote_tools
def wrap():
    return session   # already initialised ClientSession

wrapped = wrap()
result = await wrapped.call_tool("search", {"query": "hello"})
```

**What it does to each `call_tool()`:**

1. Creates a client-side tool span (`span_type="tool"`, `remote="true"`)
2. Sets `tool_name`, `agent`, `model`, `provider` attributes from context
3. Injects `rastir_trace_id` and `rastir_span_id` into tool arguments
4. Forwards to the original `session.call_tool()`
5. Records errors and finishes the span

**Client span attributes:**

| Attribute  | Type   | Source    | Description                                |
|------------|--------|-----------|--------------------------------------------|
| `tool_name`| string | call arg  | MCP tool name passed to `call_tool()`      |
| `remote`   | string | auto      | Always `"true"` for client spans           |
| `agent`    | string | context   | Parent `@agent` name (if present)          |
| `model`    | string | context   | From `@llm` or `set_current_model()`       |
| `provider` | string | context   | From `@llm` or `set_current_provider()`    |

---

### `@mcp_endpoint`

Server-side decorator placed **under** `@mcp.tool()`. Creates a child span linked to the client's trace context.

```python
from mcp.server.fastmcp import FastMCP
from rastir import configure, mcp_endpoint

# The MCP server must call configure() to export its spans
configure(service="tool-server", push_url="http://localhost:8080")

mcp = FastMCP("my-server")

@mcp.tool()
@mcp_endpoint
async def search(query: str) -> str:
    """Search the database.

    Args:
        query: The search query.
    """
    return db.search(query)
```

> **Important:** The MCP server process must call `configure(push_url=...)`
> independently. Without this, `@mcp_endpoint` spans are created but never
> exported — they are silently dropped because no exporter is configured.

**What it does:**

1. Pops `rastir_trace_id` and `rastir_span_id` from kwargs (before your function sees them)
2. Creates a server-side tool span (`span_type="tool"`, `remote="false"`)
3. Links the span to the client via `trace_id` and `parent_id`
4. Your original function runs unchanged — it never sees the trace fields
5. The wrapper extends the function's `__signature__` so FastMCP's Pydantic validation passes the trace fields through

**Server span attributes:**

| Attribute  | Type   | Source    | Description                                |
|------------|--------|-----------|--------------------------------------------|
| `tool_name`| string | auto      | Server function name                       |
| `remote`   | string | auto      | Always `"false"` for server spans          |
| `agent`    | string | context   | Server-side agent context (if set)         |

**Important:** The decorator order matters — `@mcp_endpoint` must be placed *after* (below) `@mcp.tool()`:

```python
@mcp.tool()        # ← FastMCP registration (outermost)
@mcp_endpoint      # ← Rastir tracing (wraps the function)
async def my_tool(arg: str) -> str:
    ...
```

---

### `mcp_to_langchain_tools()`

One-line bridge that converts MCP tools to LangChain `StructuredTool` instances with automatic trace injection. Ready for use with `create_react_agent` or any LangChain agent.

```python
from rastir import mcp_to_langchain_tools

async with ClientSession(read, write) as session:
    await session.initialize()
    tools = await mcp_to_langchain_tools(session)
    agent = create_react_agent(llm, tools)
```

**Parameters:**

| Parameter | Type            | Default | Description                                    |
|-----------|-----------------|---------|------------------------------------------------|
| `session` | `ClientSession` | —       | An initialised MCP client session              |
| `trace`   | `bool`          | `True`  | Wrap session with `@trace_remote_tools`        |

**What it handles:**

1. Fetches tool list via `session.list_tools()`
2. Wraps the session with `@trace_remote_tools` (unless `trace=False`)
3. Builds Pydantic `args_schema` from each tool's JSON `inputSchema`
4. Filters out `rastir_*` internal fields from the schema
5. Returns `StructuredTool` objects that call `session.call_tool()` under the hood

**Requires:** `pip install langchain-core`

---

## Complete Examples

### Example 1: Direct MCP tool call

The MCP server and client are typically separate processes. **Both must call
`configure(push_url=...)`** to push their spans to the same collector.

```python
# ── server.py  (MCP server process) ─────────────────
from rastir import configure, mcp_endpoint
from mcp.server.fastmcp import FastMCP

# Server must configure() independently to export @mcp_endpoint spans
configure(service="tool-server", push_url="http://localhost:8080")

mcp = FastMCP("weather-server", host="0.0.0.0", port=9000, stateless_http=True)

@mcp.tool()
@mcp_endpoint
async def get_weather(city: str) -> str:
    """Get weather for a city.

    Args:
        city: City name.
    """
    return f"22°C, sunny in {city}"

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
```

```python
# ── client.py  (agent process) ──────────────────────
from rastir import configure, agent_span, trace_remote_tools
from mcp.client.streamable_http import streamable_http_client
from mcp.client.session import ClientSession

configure(service="my-agent", push_url="http://localhost:8080")

@agent_span(agent_name="weather_agent")
async def run():
    async with streamable_http_client("http://localhost:9000/mcp") as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()

            @trace_remote_tools
            def wrap():
                return session

            wrapped = wrap()
            result = await wrapped.call_tool("get_weather", {"city": "Tokyo"})
            return result
```

> **Why both need `configure()`:** The client process creates client spans
> (`remote="true"`) and the server process creates server spans
> (`remote="false"`). Each process pushes its spans independently to the
> collector. They are linked automatically by `trace_id`.

The trace in Tempo will show:

```
weather_agent (agent)
└── get_weather (tool, remote=true, agent=weather_agent)
      └── get_weather (tool, remote=false)   ← same trace_id
```

### Example 2: LangGraph agent with MCP tools

```python
from rastir import configure, agent_span, mcp_to_langchain_tools
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.prebuilt import create_react_agent
from langchain_core.messages import HumanMessage
from mcp.client.streamable_http import streamable_http_client
from mcp.client.session import ClientSession

configure(service="my-agent", push_url="http://localhost:8080")

async def run():
    async with streamable_http_client("http://localhost:9000/mcp") as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()

            # One line: MCP tools → LangChain tools with trace injection
            tools = await mcp_to_langchain_tools(session)

            llm = ChatGoogleGenerativeAI(model="gemini-2.5-flash", temperature=0)
            agent = create_react_agent(llm, tools)

            @agent_span(agent_name="gemini_agent")
            async def invoke():
                return await agent.ainvoke(
                    {"messages": [HumanMessage(content="What is the weather in Tokyo?")]}
                )

            response = await invoke()
            print(response["messages"][-1].content)
```

### Example 3: Model and provider context propagation

When a tool call happens inside an `@llm`-decorated function, the model and provider are automatically propagated to tool spans:

```python
from rastir import agent_span, llm_span, trace_remote_tools

@agent_span(agent_name="research_agent")
async def run():
    return await call_llm("What is the weather?")

@llm_span(model="gpt-4o", provider="openai")
async def call_llm(prompt: str):
    # Any tool calls made here will inherit model="gpt-4o", provider="openai"
    async with streamable_http_client(url) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()

            @trace_remote_tools
            def wrap():
                return session

            wrapped = wrap()
            # This client span will have: model="gpt-4o", provider="openai", agent="research_agent"
            result = await wrapped.call_tool("search", {"query": prompt})
            return result
```

You can also set model/provider manually:

```python
from rastir.context import set_current_model, set_current_provider

set_current_model("gemini-2.5-flash")
set_current_provider("google")
# Subsequent tool calls will inherit these values
```

---

## Span Attributes in Tempo

All Rastir attributes are prefixed with `rastir.` in OTLP/Tempo. The full attribute set for MCP tool spans:

### Client span (created by `@trace_remote_tools`)

| Tempo attribute        | Type   | Value                  |
|------------------------|--------|------------------------|
| `rastir.span_type`     | string | `"tool"`               |
| `rastir.tool_name`     | string | MCP tool name          |
| `rastir.remote`        | string | `"true"`               |
| `rastir.agent`         | string | Parent agent name      |
| `rastir.model`         | string | Model from context     |
| `rastir.provider`      | string | Provider from context  |

### Server span (created by `@mcp_endpoint`)

| Tempo attribute        | Type   | Value                  |
|------------------------|--------|------------------------|
| `rastir.span_type`     | string | `"tool"`               |
| `rastir.tool_name`     | string | Server function name   |
| `rastir.remote`        | string | `"false"`              |

Both spans share the same `traceId`. The server span's `parentSpanId` points to the client span.

---

## With and Without `@mcp_endpoint`

| Scenario | Client span | Server span | Trace propagation |
|----------|:-----------:|:-----------:|:-----------------:|
| Server uses `@mcp_endpoint` | ✅ | ✅ | Full — same trace_id |
| Server does NOT use `@mcp_endpoint` | ✅ | ❌ | Client-side only |

When the server doesn't use `@mcp_endpoint`, the `rastir_trace_id` and `rastir_span_id` fields are silently dropped by FastMCP's Pydantic validation. The client span is still created, your tool still works — you just don't get server-side visibility.

---

## Architecture Note

Rastir uses argument-based injection rather than HTTP headers or `_meta` because:

1. **MCP transport-agnostic** — works over stdio, SSE, and Streamable HTTP
2. **No monkey-patching** — no need to intercept transport layer
3. **Schema-safe** — FastMCP's Pydantic validation drops unknown fields automatically, so servers without `@mcp_endpoint` silently ignore trace fields
4. **Simple** — the approach adds two string fields to the arguments dict; no protocol extensions needed
