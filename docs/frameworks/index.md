---
layout: default
title: Frameworks
nav_order: 5
has_children: true
permalink: /frameworks/
---

# Framework Integrations

Rastir provides dedicated decorators for five major AI agent frameworks. Each decorator auto-discovers and wraps the framework's internal components — LLMs, tools, and nodes — for per-call observability.

| | LangGraph | CrewAI | LlamaIndex | ADK | Strands |
|---|---|---|---|---|---|
| **Decorator** | `@langgraph_agent` | `@crew_kickoff` | `@llamaindex_agent` | `@adk_agent` | `@strands_agent` |
| **Agent span** | Automatic | Automatic | Automatic | Automatic | Automatic |
| **LLM tracing** | Auto-discovered | Auto-discovered | Manual `wrap(llm)` | Auto-discovered | Auto-discovered |
| **Tool tracing** | Auto-discovered | Auto-discovered | Manual `wrap(tool)` | Auto-discovered | Auto-discovered |
| **Node tracing** | Automatic (all nodes) | N/A | N/A | N/A | N/A |
| **MCP tools** | Pass as normal tools | Native via `mcps=[]` on agents | `wrap()` on McpToolSpec tools | Auto-discovered with traceparent injection | Auto-discovered with traceparent injection |
| **Lines of user code** | 1 decorator | 1 decorator | 1 decorator + `wrap()` calls | 1 decorator | 1 decorator |

All five decorators:
- Support both sync and async functions
- Create an outer `AGENT` span around the entire execution
- Restore original objects after execution for safe reuse
- Record errors in the span and re-raise exceptions unchanged
