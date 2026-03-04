---
layout: default
title: MCP Distributed Tracing
nav_order: 7
---

# MCP Distributed Tracing

Rastir supports distributed tracing across [MCP](https://modelcontextprotocol.io/) (Model Context Protocol) tool boundaries. Trace context flows automatically from agent to tool server using W3C `traceparent` HTTP headers.

---

## How It Works

Rastir uses **W3C `traceparent` HTTP headers** — the industry-standard approach for distributed tracing.

**Client side**: Framework decorators (`@langgraph_agent`, `@crew_kickoff`, `@llamaindex_agent`) auto-discover MCP client objects and set the `traceparent` header before each tool call. No client-side code needed.

**Server side**: `RastirMCPMiddleware` (ASGI middleware) reads `traceparent` from incoming HTTP requests. `@mcp_endpoint` creates server spans linked to the client's trace.

```
┌──────────────────────────────┐           ┌──────────────────────────────┐
│  Client (your agent)         │           │  MCP Tool Server             │
│                              │           │                              │
│  @langgraph_agent            │           │  RastirMCPMiddleware         │
│  └── auto-discovers MCP      │  HTTP     │  reads traceparent header    │
│      client, sets            │ ────────▸ │                              │
│      traceparent header      │  header   │  @mcp.tool()                 │
│                              │           │  @mcp_endpoint               │
│  client span: remote=true    │           │  server span: remote=false   │
└──────────────────────────────┘           └──────────────────────────────┘
```

W3C traceparent format: `00-<32-char-trace-id>-<16-char-span-id>-01`

---

## Framework Integration (Zero Config)

When using framework decorators, MCP trace propagation is **automatic**:

### LangGraph

```python
from rastir import configure, langgraph_agent
from langchain_mcp_adapters.client import MultiServerMCPClient

configure(service="my-agent", push_url="http://localhost:8080")

async def run():
    async with MultiServerMCPClient({
        "weather": {"url": "http://localhost:9000/mcp", "transport": "streamable_http"},
    }) as mcp_client:
        tools = mcp_client.get_tools()
        llm = ChatGoogleGenerativeAI(model="gemini-2.5-flash")
        graph = create_react_agent(llm, tools)

        @langgraph_agent(agent_name="react")
        async def invoke(graph, mcp_client):
            # traceparent is auto-injected into mcp_client.connections headers
            return await graph.ainvoke({"messages": [("user", "What's the weather?")]})

        return await invoke(graph, mcp_client)
```

### CrewAI

```python
from rastir import configure, crew_kickoff
from crewai import Agent, Task, Crew
from crewai.mcp.config import MCPServerHTTP

configure(service="my-agent", push_url="http://localhost:8080")

mcp_server = MCPServerHTTP(url="http://localhost:9000/mcp")

agent = Agent(
    role="researcher",
    llm="gemini/gemini-2.5-flash",
    mcp_servers=[mcp_server],  # Rastir auto-discovers this
)

@crew_kickoff(agent_name="research_crew")
def run(crew):
    return crew.kickoff()
```

### LlamaIndex

```python
from rastir import configure, llamaindex_agent
from llama_index.tools.mcp import BasicMCPClient, McpToolSpec

configure(service="my-agent", push_url="http://localhost:8080")

mcp_client = BasicMCPClient("http://localhost:9000/mcp")

@llamaindex_agent(agent_name="qa")
async def run(agent, mcp_client):
    # traceparent is auto-injected into mcp_client.headers
    return await agent.achat("What's the weather?")
```

---

## Server Side Setup

The MCP server needs two things: `RastirMCPMiddleware` to read headers, and `@mcp_endpoint` to create server spans.

```python
# ── server.py ──
from mcp.server.fastmcp import FastMCP
from rastir import configure, mcp_endpoint
from rastir.remote import RastirMCPMiddleware

# Server must configure() independently
configure(service="tool-server", push_url="http://localhost:8080")

mcp = FastMCP("weather-server", host="0.0.0.0", port=9000, stateless_http=True)

@mcp.tool()
@mcp_endpoint
async def get_weather(city: str) -> str:
    """Get weather for a city."""
    return f"22°C, sunny in {city}"

if __name__ == "__main__":
    # Wrap the ASGI app with middleware
    app = mcp.streamable_http_app()
    app = RastirMCPMiddleware(app)

    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9000)
```

> **Important:** The MCP server process must call `configure(push_url=...)`
> independently. Without this, `@mcp_endpoint` spans are created but never
> exported.

---

## Standalone Usage (wrap_mcp)

For direct MCP SDK usage without a framework decorator:

```python
from rastir import configure, agent_span, wrap_mcp
from mcp.client.streamable_http import streamable_http_client
from mcp.client.session import ClientSession

configure(service="my-agent", push_url="http://localhost:8080")

@agent_span(agent_name="weather_agent")
async def run():
    async with streamable_http_client("http://localhost:9000/mcp") as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            session = wrap_mcp(session)   # creates client spans per tool call
            result = await session.call_tool("get_weather", {"city": "Tokyo"})
            return result
```

**With `http_client`** for header propagation to standalone httpx clients:

```python
import httpx
from rastir import wrap_mcp

http_client = httpx.AsyncClient()
session = wrap_mcp(session, http_client=http_client)
# traceparent header is set on http_client.headers before each call_tool()
```

---

## Manual Header Injection

For custom scenarios, use `traceparent_headers()`:

```python
from rastir import traceparent_headers

headers = traceparent_headers()
# Returns {"traceparent": "00-<trace_id>-<span_id>-01"} or {} if no active span
```

---

## API Reference

### `wrap_mcp(session, *, http_client=None)`

Wraps an MCP `ClientSession` with a transparent proxy. Only `call_tool()` is intercepted.

**Client span attributes:**

| Attribute  | Type   | Source  | Description                            |
|------------|--------|---------|----------------------------------------|
| `tool_name`| string | call    | MCP tool name                          |
| `remote`   | string | auto    | Always `"true"`                        |
| `agent`    | string | context | Parent `@agent` name (if present)      |
| `model`    | string | context | From `@llm` context                    |
| `provider` | string | context | From `@llm` context                    |

### `@mcp_endpoint`

Server-side decorator placed **under** `@mcp.tool()`. Creates a child span linked to the client's trace context (read from `_incoming_trace_context` ContextVar set by `RastirMCPMiddleware`).

**Server span attributes:**

| Attribute  | Type   | Source  | Description                            |
|------------|--------|---------|----------------------------------------|
| `tool_name`| string | auto    | Server function name                   |
| `remote`   | string | auto    | Always `"false"`                       |

### `RastirMCPMiddleware(app)`

ASGI middleware that reads `traceparent` from incoming HTTP requests and stores parsed trace context in a ContextVar for `@mcp_endpoint`.

### `traceparent_headers()`

Returns `{"traceparent": "00-<trace_id>-<span_id>-01"}` from the current active span, or `{}` if no span exists.

---

## Trace Topology

```
Agent Span
└── Tool Client Span  (remote="true")
      └── Tool Server Span (remote="false")  ← same trace_id
```

Both spans share the same `traceId`. The server span's `parentSpanId` points to the client span.

---

## With and Without `@mcp_endpoint`

| Scenario | Client span | Server span | Trace propagation |
|----------|:-----------:|:-----------:|:-----------------:|
| Server uses `RastirMCPMiddleware` + `@mcp_endpoint` | ✅ | ✅ | Full |
| Server does NOT use either | ✅ | ❌ | Client-side only |

When the server doesn't use `@mcp_endpoint`, the `traceparent` header is sent but ignored. The client span is still created — you just don't get server-side visibility.
