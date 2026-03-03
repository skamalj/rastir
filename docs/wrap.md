---
layout: default
title: wrap() & MCP
nav_order: 6
---

# `wrap()` — Generic Object Wrapper

`wrap()` is Rastir's universal instrumentation function. It wraps any Python object so its public methods emit Rastir spans — useful for infrastructure components (Redis, databases, vector stores) and MCP sessions.

---

## Basic Usage

```python
import rastir

# Wrap a Redis client — all public methods emit INFRA spans
cache = rastir.wrap(redis_client, name="redis")
cache.get("key")       # → span: "redis.get"
cache.set("key", val)  # → span: "redis.set"

# Wrap a database client with filtering
db = rastir.wrap(db_client, name="postgres",
                 include=["query", "execute"],
                 span_type="tool")
```

---

## Smart MCP Detection

`wrap()` auto-detects MCP `ClientSession` objects and delegates to the MCP-specific proxy that intercepts `call_tool()` and injects distributed trace context.

```python
from rastir import wrap

# These are equivalent:
session = wrap(mcp_session)           # auto-detects MCP session
session = wrap_mcp(mcp_session)       # explicit (still works)

# After wrapping, call_tool() injects trace IDs automatically:
await session.call_tool("search", {"query": "hello"})
# → client span with remote="true", trace context injected
```

`wrap_mcp()` remains available as an explicit alias for backward compatibility.

---

## API Reference

```python
wrap(
    obj,
    *,
    name: str = None,        # Span name prefix (default: class name)
    span_type: str = "infra", # infra, tool, llm, trace, agent, retrieval
    include: list[str] = None,# Only wrap these methods
    exclude: list[str] = None,# Skip these methods
)
```

**For MCP sessions:** `name`, `span_type`, `include`, and `exclude` are ignored — the MCP proxy handles its own span creation.

---

## Features

| Feature | Description |
|---------|-------------|
| **Sync + async** | Both sync and async methods are wrapped automatically |
| **`isinstance` preserved** | `isinstance(wrapped, OriginalClass)` returns `True` |
| **Double-wrap prevention** | Objects with `_rastir_wrapped` are returned as-is |
| **Method caching** | Wrapped methods are cached — no overhead on repeated access |
| **Private methods skipped** | Methods starting with `_` are not wrapped |
| **MCP auto-detection** | Detects `ClientSession` from MCP SDK and delegates to MCP proxy |

---

## Span Attributes

Each wrapped method call produces a span with:

| Attribute | Value |
|-----------|-------|
| `wrap.method` | Method name (e.g., `get`, `set`) |
| `wrap.args_count` | Number of positional arguments |
| `wrap.kwargs_keys` | Sorted list of keyword argument names |

---

## Usage in Frameworks

| Framework | Usage |
|-----------|-------|
| **LangGraph** | Not needed — `@langgraph_agent` auto-discovers LLMs/tools |
| **CrewAI** | `wrap(mcp_session)` for MCP tool injection via `mcp=` param |
| **LlamaIndex** | `wrap(llm, span_type="llm")` and `wrap(tool, span_type="tool")` |
| **Infrastructure** | `wrap(redis_client, name="redis")` for any object |

---

## Valid Span Types

| Type | Use for |
|------|---------|
| `infra` (default) | Databases, caches, HTTP clients |
| `tool` | Tool invocations |
| `llm` | LLM calls |
| `trace` | General tracing |
| `agent` | Agent-level operations |
| `retrieval` | RAG retrieval operations |
