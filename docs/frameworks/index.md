---
layout: default
title: Frameworks
nav_order: 5
has_children: true
permalink: /frameworks/
---

# Framework Integrations

Rastir provides dedicated decorators for three major AI agent frameworks. Each decorator auto-discovers and wraps the framework's internal components — LLMs, tools, and nodes — for per-call observability.

| | LangGraph | CrewAI | LlamaIndex |
|---|---|---|---|
| **Decorator** | `@langgraph_agent` | `@crew_kickoff` | `@llamaindex_agent` |
| **Agent span** | Automatic | Automatic | Automatic |
| **LLM tracing** | Auto-discovered | Auto-discovered | Manual `wrap(llm)` |
| **Tool tracing** | Auto-discovered | Auto-discovered | Manual `wrap(tool)` |
| **Node tracing** | Automatic (all nodes) | N/A | N/A |
| **MCP tools** | Pass as normal tools | `wrap(session)` + `mcp=` | `wrap()` on McpToolSpec tools |
| **Lines of user code** | 1 decorator | 1 decorator (+`wrap` for MCP) | 1 decorator + `wrap()` calls |

All three decorators:
- Support both sync and async functions
- Create an outer `AGENT` span around the entire execution
- Restore original objects after execution for safe reuse
- Record errors in the span and re-raise exceptions unchanged
