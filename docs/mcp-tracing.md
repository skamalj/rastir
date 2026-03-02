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

### `wrap_mcp()`

Wraps an MCP `ClientSession` with a transparent proxy. Only `call_tool()` is intercepted — all other methods (`list_tools()`, `initialize()`, etc.) pass through unchanged.

```python
from rastir import wrap_mcp

async with ClientSession(read, write) as session:
    await session.initialize()
    session = wrap_mcp(session)         # one line
    tools = await session.list_tools()  # pass to any framework
```

**What the proxy does to each `call_tool()`:**

1. Creates a client-side tool span (`span_type="tool"`, `remote="true"`)
2. Sets `tool_name`, `agent`, `model`, `provider` attributes from context
3. Injects `rastir_trace_id` and `rastir_span_id` into tool arguments
4. Forwards to the original `session.call_tool()`
5. Records errors and finishes the span

No tool schemas are modified. No framework-specific code.

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
from rastir import configure, agent_span, wrap_mcp
from mcp.client.streamable_http import streamable_http_client
from mcp.client.session import ClientSession

configure(service="my-agent", push_url="http://localhost:8080")

@agent_span(agent_name="weather_agent")
async def run():
    async with streamable_http_client("http://localhost:9000/mcp") as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            session = wrap_mcp(session)
            result = await session.call_tool("get_weather", {"city": "Tokyo"})
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
from rastir import configure, agent_span, wrap_mcp
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.prebuilt import create_react_agent
from langchain_core.messages import HumanMessage
from langchain_core.tools import StructuredTool
from pydantic import create_model
from mcp.client.streamable_http import streamable_http_client
from mcp.client.session import ClientSession

configure(service="my-agent", push_url="http://localhost:8080")

async def run():
    async with streamable_http_client("http://localhost:9000/mcp") as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            session = wrap_mcp(session)

            # Fetch tools and convert to LangChain format (your code)
            tools_resp = await session.list_tools()
            lc_tools = []
            for t in tools_resp.tools:
                # ... build StructuredTool from t.inputSchema ...
                pass

            llm = ChatGoogleGenerativeAI(model="gemini-2.5-flash", temperature=0)
            agent = create_react_agent(llm, lc_tools)

            @agent_span(agent_name="gemini_agent")
            async def invoke():
                return await agent.ainvoke(
                    {"messages": [HumanMessage(content="What is the weather in Tokyo?")]}
                )

            response = await invoke()
            print(response["messages"][-1].content)
```

> **Note:** Converting MCP tools to LangChain `StructuredTool` is your
> framework's responsibility. Rastir only handles trace propagation —
> `wrap_mcp()` ensures that when any framework calls `session.call_tool()`,
> trace IDs are injected automatically.

### Example 3: Model and provider context propagation

When a tool call happens inside an `@llm`-decorated function, the model and provider are automatically propagated to tool spans:

```python
from rastir import agent_span, llm_span, wrap_mcp

@agent_span(agent_name="research_agent")
async def run():
    return await call_llm("What is the weather?")

@llm_span(model="gpt-4o", provider="openai")
async def call_llm(prompt: str):
    # Any tool calls made here will inherit model="gpt-4o", provider="openai"
    async with streamable_http_client(url) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            session = wrap_mcp(session)
            # This client span will have: model="gpt-4o", provider="openai", agent="research_agent"
            result = await session.call_tool("search", {"query": prompt})
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

### Client span (created by `wrap_mcp()`)

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
2. **Framework-agnostic** — `wrap_mcp()` is a simple proxy; no LangChain, CrewAI, or other framework coupling
3. **Schema-safe** — FastMCP's Pydantic validation drops unknown fields automatically, so servers without `@mcp_endpoint` silently ignore trace fields
4. **Simple** — the approach adds two string fields to the arguments dict; no protocol extensions needed
