# Rastir

<p align="center">
  <img src="https://raw.githubusercontent.com/skamalj/rastir/main/rastir_small.png" alt="Rastir" width="200">
</p>

<p align="center">
  <strong>LLM & Agent Observability for Python</strong><br>
  One decorator per framework. Full visibility. No monkey-patching.
</p>

<p align="center">
  <a href="https://pypi.org/project/rastir/"><img alt="PyPI" src="https://img.shields.io/pypi/v/rastir"></a>
  <a href="https://pypi.org/project/rastir/"><img alt="Python" src="https://img.shields.io/pypi/pyversions/rastir"></a>
  <a href="https://skamalj.github.io/rastir/"><img alt="Docs" src="https://img.shields.io/badge/docs-GitHub%20Pages-blue"></a>
  <a href="https://github.com/skamalj/rastir/blob/main/LICENSE"><img alt="License" src="https://img.shields.io/github/license/skamalj/rastir"></a>
  <a href="https://github.com/skamalj/rastir"><img alt="GitHub" src="https://img.shields.io/github/stars/skamalj?style=social"></a>
</p>

---

## What is Rastir?

Rastir gives you **production observability for LLM agents** — token usage, latency percentiles, cost tracking, tool call rates, error categories — as Prometheus metrics with Grafana dashboards.

Add **one decorator** to your LangGraph, CrewAI, LlamaIndex, ADK, or Strands workflow. Rastir auto-discovers LLMs, tools, and graph nodes inside the framework and wraps them for per-call tracing. No code rewrites. No vendor lock-in.

```python
from rastir import configure, framework_agent

configure(service="my-app", push_url="http://localhost:8080")

@framework_agent
def run(graph_or_agent, prompt):
    return graph_or_agent.invoke(prompt)  # Works with any supported framework
```

That's it. `@framework_agent` auto-detects the framework from function arguments and instruments everything inside. Every LLM call, tool invocation, and node execution is now traced with metrics flowing to Prometheus.

You can also use framework-specific decorators for explicit control: `@langgraph_agent`, `@crew_kickoff`, `@llamaindex_agent`, `@adk_agent`, `@strands_agent`.

---

## Key Features

| Feature | Description |
|---------|-------------|
| **One decorator per framework** | `@framework_agent` (auto-detect), `@langgraph_agent`, `@crew_kickoff`, `@llamaindex_agent`, `@adk_agent`, `@strands_agent` |
| **8 provider adapters** | OpenAI, Azure OpenAI, Anthropic, Bedrock, Gemini, Cohere, Mistral, Groq — auto-detected from client module paths |
| **Two-phase enrichment** | Model/provider metadata captured from function args *before* the call, refined from response *after*. Survives API failures |
| **MCP distributed tracing** | `wrap(session)` propagates trace context across MCP tool boundaries — same `trace_id` links client and server |
| **Cost observability** | Per-model USD cost tracking with `PricingRegistry`, pricing profiles, cost histograms |
| **Streaming TTFT** | Time-To-First-Token measurement on streaming LLM calls |
| **Guardrail tracking** | Automatic AWS Bedrock guardrail violation metrics |
| **Error normalisation** | Exceptions mapped to 6 fixed categories: timeout, rate_limit, validation_error, provider_error, internal_error, unknown |
| **Self-hosted collector** | FastAPI server you own. Prometheus `/metrics`, in-memory trace store, OTLP export to Tempo/Jaeger |
| **SRE budgets & burn rates** | Error and cost budget tracking via Prometheus recording rules — SLO status, burn rates, days-to-exhaustion, all config-driven |
| **7 Grafana dashboards** | LLM Performance, Agent-Tool, Cost-TTFT, Evaluation, Guardrail, SRE Budgets, System Health |
| **Generic `wrap()`** | Instrument any object — Redis, databases, MCP sessions — without decorator access |

---

## Framework Support at a Glance

All five frameworks work with `@framework_agent` (auto-detects the framework) or the dedicated decorator:

| | LangGraph | CrewAI | LlamaIndex | ADK | Strands |
|---|---|---|---|---|---|
| **Decorator** | `@langgraph_agent` | `@crew_kickoff` | `@llamaindex_agent` | `@adk_agent` | `@strands_agent` |
| **Agent span** | Automatic | Automatic | Automatic | Automatic | Automatic |
| **LLM tracing** | Auto-discovered | Auto-discovered | Auto-discovered | Auto-discovered | Auto-discovered |
| **Tool tracing** | Auto-discovered | Auto-discovered | Auto-discovered | Auto-discovered | Auto-discovered |
| **Node tracing** | Automatic (all nodes) | N/A | N/A | N/A | N/A |
| **MCP tools** | Pass as normal tools | Native via `mcps=[]` | MCP tools auto-wrapped | Auto-discovered | Auto-discovered |
| **User code** | 1 decorator | 1 decorator | 1 decorator | 1 decorator | 1 decorator |

### LangGraph

```python
@langgraph_agent(agent_name="react_agent")
def run(graph, query):
    return graph.invoke({"messages": [("user", query)]})
```

```
react_agent (AGENT)
  ├── node:agent (TRACE)       ← every graph node traced
  │   └── langgraph.llm.gpt-4o.invoke (LLM)
  ├── node:tools (TRACE)
  │   └── langgraph.tool.search.invoke (TOOL)
  └── node:agent (TRACE)
      └── langgraph.llm.gpt-4o.invoke (LLM)
```

### CrewAI

```python
@crew_kickoff(agent_name="research_crew")
def run(crew):
    return crew.kickoff()
```

```
research_crew (AGENT)
  ├── crewai.Researcher.llm.call (LLM) — model, provider, tokens, cost
  ├── crewai.Researcher.tool.search (TOOL) — tool.input, tool.output
  │   └── mcpserver:search (TOOL)       ← server span via traceparent
  ├── crewai.Researcher.llm.call (LLM)
  └── crewai.Writer.llm.call (LLM)
```

### LlamaIndex

```python
from rastir import llamaindex_agent
from llama_index.core.agent import ReActAgent

agent = ReActAgent(llm=llm, tools=tools, streaming=False)

@llamaindex_agent(agent_name="qa_agent")
async def run(agent, query):
    return await agent.run(query)
```

```
qa_agent (AGENT)
├── llamaindex.ReActAgent.llm.achat (LLM) — model, provider, tokens, cost
├── search.acall (TOOL)                   — tool.input, tool.output
│   └── mcpserver:search (TOOL)           ← server span via traceparent
├── llamaindex.ReActAgent.llm.achat (LLM)
└── llamaindex.ReActAgent.llm.achat (LLM)
```

### ADK

```python
@adk_agent(agent_name="weather_agent")
async def run(runner, prompt):
    events = []
    async for event in runner.run_async(user_id="u1", session_id="s1",
        new_message=types.Content(role="user", parts=[types.Part(text=prompt)])):
        events.append(event)
    return events
```

```
weather_agent (AGENT)
├── LLM  gemini-2.0-flash
├── TOOL get_weather
└── LLM  gemini-2.0-flash
```

### Strands

```python
@strands_agent(agent_name="research_agent")
def run(agent, prompt):
    return agent(prompt)
```

```
research_agent (AGENT)
├── LLM  us.anthropic.claude-sonnet-4-20250514
├── TOOL search_tool
└── LLM  us.anthropic.claude-sonnet-4-20250514
```

→ **Detailed framework documentation:** [LangGraph](https://skamalj.github.io/rastir/frameworks/langgraph) · [CrewAI](https://skamalj.github.io/rastir/frameworks/crewai) · [LlamaIndex](https://skamalj.github.io/rastir/frameworks/llamaindex) · [ADK](https://skamalj.github.io/rastir/frameworks/adk) · [Strands](https://skamalj.github.io/rastir/frameworks/strands)

---

## Supported Providers

| Provider | Auto-detection | Tokens | Model | Streaming | Request-phase |
|----------|:-:|:-:|:-:|:-:|:-:|
| **OpenAI** | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Azure OpenAI** | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Anthropic** | ✅ | ✅ | ✅ | ✅ | ✅ |
| **AWS Bedrock** | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Google Gemini** | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Cohere** | ✅ | ✅ | ✅ | — | ✅ |
| **Mistral** | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Groq** | ✅ | ✅ | ✅ | ✅ | ✅ |

Providers are auto-detected from LLM client module paths — no configuration needed. Each provider adapter extracts model name, token counts, and cost from the provider's native response format.

---

## Installation

```bash
pip install rastir              # Client library
pip install rastir[server]      # + Collector server
pip install rastir[all]         # Everything
```

## Quick Start

```python
from rastir import configure, agent, llm, trace

configure(service="my-app", push_url="http://localhost:8080")

@agent(agent_name="qa_bot")
def answer(query):
    return ask_llm(search(query))

@trace
def search(query):
    return vector_db.search(query)

@llm
def ask_llm(context):
    return openai.chat.completions.create(model="gpt-4o", messages=[...])
```

Start the collector:

```bash
rastir-server   # Prometheus metrics at :8080/metrics
```

## What You Get in Prometheus

```
rastir_llm_calls_total{model="gpt-4o", provider="openai", agent="qa_bot"} 150
rastir_tokens_input_total{model="gpt-4o"} 25000
rastir_tokens_output_total{model="gpt-4o"} 8500
rastir_duration_seconds_bucket{span_type="llm", le="1.0"} 120
rastir_errors_total{span_type="llm", error_type="rate_limit"} 3
rastir_cost_total{model="gpt-4o", pricing_profile="prod"} 12.50
rastir_ttft_seconds_bucket{model="gpt-4o", le="0.5"} 95
```

---

## Architecture

```
Your Application                             Rastir Collector
┌────────────────────────────────┐           ┌────────────────────────────┐
│  @framework_agent (auto-detect)│   HTTP    │  FastAPI                   │
│  @langgraph_agent / @adk_agent │  ──────▸  │  ├── Prometheus /metrics   │
│  @crew_kickoff / @strands_agent│   spans   │  ├── Trace store /v1/traces│
│  @llamaindex_agent             │           │  ├── Sampling & backpressure│
│  @agent / @llm / wrap(obj)     │           │  └── OTLP → Tempo/Jaeger  │
└────────────────────────────────┘           └────────────────────────────┘
```

---

## Deployment

Rastir ships with ready-to-use deployment for **local dev**, **3 clouds** (AWS, Azure, GCP), and **Kubernetes**:

| Target | Tool | Command |
|--------|------|---------|
| **Local** | Docker Compose | `cd deploy/docker && ./deploy.sh` |
| **AWS** | Terraform (ECS Fargate) | `cd deploy/terraform/aws && ./deploy.sh` |
| **Azure** | Terraform (ACI) | `cd deploy/terraform/azure && ./deploy.sh` |
| **GCP** | Terraform (Cloud Run) | `cd deploy/terraform/gcp && ./deploy.sh` |
| **Kubernetes** | Helm | `cd deploy/k8s && ./deploy.sh` |

Each deployment includes the full stack: **Rastir Server + OTel Collector + Prometheus + Grafana**.
Traces go to Tempo (local/k8s), X-Ray (AWS), Application Insights (Azure), or Cloud Trace (GCP).

See [Deployment Guide](https://skamalj.github.io/rastir/deployment) for details.

---

## Documentation

Full documentation at **[skamalj.github.io/rastir](https://skamalj.github.io/rastir/)**:

| Section | Pages |
|---------|-------|
| **Getting Started** | [Installation & Quick Start](https://skamalj.github.io/rastir/getting-started) |
| **Core** | [Decorators](https://skamalj.github.io/rastir/decorators) · [Adapters](https://skamalj.github.io/rastir/adapters) · [wrap() & MCP](https://skamalj.github.io/rastir/wrap) · [MCP Tracing](https://skamalj.github.io/rastir/mcp-tracing) |
| **Frameworks** | [LangGraph](https://skamalj.github.io/rastir/frameworks/langgraph) · [CrewAI](https://skamalj.github.io/rastir/frameworks/crewai) · [LlamaIndex](https://skamalj.github.io/rastir/frameworks/llamaindex) · [ADK](https://skamalj.github.io/rastir/frameworks/adk) · [Strands](https://skamalj.github.io/rastir/frameworks/strands) |
| **Operations** | [Metrics](https://skamalj.github.io/rastir/metrics) · [Dashboards](https://skamalj.github.io/rastir/dashboards) · [Server](https://skamalj.github.io/rastir/server) · [Configuration](https://skamalj.github.io/rastir/configuration) |
| **Deployment** | [Docker Compose](https://skamalj.github.io/rastir/deployment#docker-compose) · [AWS](https://skamalj.github.io/rastir/deployment#aws) · [Azure](https://skamalj.github.io/rastir/deployment#azure) · [GCP](https://skamalj.github.io/rastir/deployment#gcp) · [Kubernetes](https://skamalj.github.io/rastir/deployment#kubernetes) |
| **Reference** | [Architecture](https://skamalj.github.io/rastir/architecture-responsibilities) · [Environment Variables](https://skamalj.github.io/rastir/environment-variables) · [Contributing Adapters](https://skamalj.github.io/rastir/contributing-adapters) |

---

## License

MIT — see [LICENSE](LICENSE) for details.
