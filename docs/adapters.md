---
layout: default
title: Adapters
nav_order: 4
---

# Adapter System

Rastir uses an **adapter pipeline** to extract metadata from LLM responses without monkey-patching provider libraries. When `@llm` decorates a function, metadata extraction happens in two phases: first from the function's *arguments* (request phase), then from the *return value* (response phase).

---

## Two-Phase Enrichment

```
Phase 1 — Request (before the call):
  Scan function kwargs for model/provider hints
  e.g., model="gpt-4o" in kwargs → pre-populate span metadata
  Bedrock: parse modelId → model + provider

Phase 2 — Response (after the call):
  Adapter pipeline extracts from return value
  Concrete response values override request-phase guesses
  If call raises an exception, request-phase metadata survives
```

This ensures that even when an API call fails (rate limit, timeout, network error), the span still records:
- Which **model** was being called
- Which **provider** was being used
- The **error type** (normalised to a fixed category)

### Generic Kwarg Scanner

For any provider, the registry scans common keyword argument patterns:

| Kwarg pattern | Extracts |
|---------------|----------|
| `model` | Model name |
| `model_id`, `modelId` | Model name |
| `model_name` | Model name |

The Bedrock adapter additionally parses compound `modelId` values like `anthropic.claude-3-sonnet` into `model="claude-3-sonnet"` and `provider="anthropic"`.

---

## How It Works

```
LLM function return value
         │
         ▼
┌─────────────────────────────────┐
│ Phase 1: Framework Unwrap       │  (priority 200–300)
│ e.g., LangChain → OpenAI obj   │
└─────────────┬───────────────────┘
              │ unwrapped result
              ▼
┌─────────────────────────────────┐
│ Phase 2: Provider Extraction    │  (priority 100–199)
│ e.g., OpenAI → model, tokens   │
└─────────────┬───────────────────┘
              │ AdapterResult
              ▼
┌─────────────────────────────────┐
│ Phase 3: Fallback               │  (priority 0)
│ Returns "unknown" if no match   │
└─────────────────────────────────┘
```

### Phase 1: Framework Unwrap

Framework adapters (like LangChain) unwrap high-level response objects to expose the underlying provider response. This phase runs repeatedly until no further unwrapping is possible.

### Phase 2: Provider Extraction

Provider adapters extract semantic metadata — model name, token counts, provider identifier, finish reason — from the raw provider response object.

### Phase 3: Fallback

If no provider adapter matches, the fallback adapter returns an `AdapterResult` with `provider="unknown"`.

---

## Built-in Adapters

| Adapter | Kind | Priority | Handles |
|---------|------|----------|---------|
| **LangGraph** | framework | 260 | State dicts from `graph.invoke()`, `StateSnapshot` → unwraps last `AIMessage` |
| **LangChain** | framework | 250 | `AIMessage`, `LLMResult` → unwraps to provider response |
| **CrewAI** | framework | 245 | `CrewOutput`, `TaskOutput` → unwraps token usage and task metadata |
| **LlamaIndex** | framework | 240 | `Response`, `ChatResponse` → unwraps to provider response |
| **Azure OpenAI** | provider | 155 | Azure-hosted `ChatCompletion` (detects `azure` in module/base_url) |
| **Groq** | provider | 152 | Groq-hosted `ChatCompletion` (detects `groq` in module) |
| **OpenAI** | provider | 150 | `ChatCompletion`, `Completion`, `ChatCompletionChunk` |
| **Anthropic** | provider | 150 | `Message`, `ContentBlockDelta` |
| **Gemini** | provider | 150 | `GenerateContentResponse` from Google Generative AI |
| **Cohere** | provider | 150 | `ChatResponse`, `Generation` from Cohere API |
| **Mistral** | provider | 150 | `ChatCompletionResponse` from Mistral API |
| **Bedrock** | provider | 140 | Bedrock `converse()` response dicts + `modelId` parsing |
| **Retrieval** | provider | 50 | Retrieval-specific response objects |
| **Tool** | provider | 10 | Tool execution results |
| **Fallback** | fallback | 0 | Anything — returns `provider="unknown"` |

---

## LangGraph Adapter

The LangGraph adapter handles responses from compiled LangGraph state graphs. LangGraph is the most popular agent framework built on LangChain, and its `graph.invoke()` returns a state dict containing LangChain message objects.

### What It Detects

1. **State dicts** — `dict` with a `messages` key containing LangChain message objects (`AIMessage`, `HumanMessage`, `ToolMessage`)
2. **StateSnapshot** — `langgraph.types.StateSnapshot` from `graph.get_state()`
3. **Streaming tuples** — `(AIMessageChunk, metadata)` from `graph.stream(stream_mode="messages")`

### Resolution Chain

```
graph.invoke() returns {"messages": [HumanMessage(...), AIMessage(...)]}
         │
         ▼
┌─────────────────────────────┐
│ LangGraph (priority 260)    │  Detects state dict with messages
│ Extracts last AIMessage     │  Adds graph metadata (message counts)
└─────────────┬───────────────┘
              │ unwrapped AIMessage
              ▼
┌─────────────────────────────┐
│ LangChain (priority 250)    │  Detects AIMessage
│ Extracts response_metadata  │  Unwraps native provider response
└─────────────┬───────────────┘
              │ unwrapped ChatCompletion
              ▼
┌─────────────────────────────┐
│ OpenAI (priority 150)       │  Extracts model, tokens, provider
└─────────────────────────────┘
```

### Graph Metadata Extracted

| Attribute | Source | Description |
|-----------|--------|-------------|
| `langgraph_message_count` | State dict | Total messages in the conversation |
| `langgraph_ai_message_count` | State dict | Number of AI responses |
| `langgraph_tool_message_count` | State dict | Number of tool call results |
| `langgraph_next_nodes` | StateSnapshot | Pending node names |
| `langgraph_task_count` | StateSnapshot | Number of tasks in the snapshot |
| `langgraph_task_names` | StateSnapshot | Names of executed graph nodes |
| `langgraph_step` | StateSnapshot metadata | Superstep number in the graph loop |
| `langgraph_source` | StateSnapshot metadata | Checkpoint source (`"input"`, `"loop"`, `"fork"`) |

### Example Usage

```python
from rastir import configure, agent, llm, trace
from langgraph.graph import StateGraph, MessagesState, START, END

configure(service="my-langgraph-app", push_url="http://localhost:8080")

# Define graph nodes
@llm
def chatbot(state: MessagesState):
    return {"messages": [model.invoke(state["messages"])]}

@trace
def search(state: MessagesState):
    query = state["messages"][-1].tool_calls[0]["args"]["query"]
    return {"messages": [ToolMessage(content=results, tool_call_id=...)]}

# Build the graph
graph = StateGraph(MessagesState)
graph.add_node("chatbot", chatbot)
graph.add_node("search", search)
graph.add_edge(START, "chatbot")
graph.add_conditional_edges("chatbot", should_search, {"search": "search", END: END})
graph.add_edge("search", "chatbot")
app = graph.compile()

# Invoke — Rastir traces the full agent loop
@agent(agent_name="research_agent")
def run_agent(query: str):
    return app.invoke({"messages": [HumanMessage(query)]})
```

Rastir captures:
- **Agent span** for `run_agent` with full loop duration
- **LLM spans** for each `chatbot` node invocation (model, tokens, provider)
- **Tool spans** for each `search` node invocation
- **Graph metadata** — message count, AI/tool message counts from the state dict

### Streaming with LangGraph

```python
@agent(agent_name="streaming_agent")
def stream_agent(query: str):
    for chunk in app.stream(
        {"messages": [HumanMessage(query)]},
        stream_mode="messages",
    ):
        yield chunk  # (AIMessageChunk, metadata) tuples
```

The adapter's `extract_stream_delta()` extracts model name, provider, and token usage from each streaming tuple.

---

## CrewAI Adapter

The CrewAI adapter handles response objects from CrewAI multi-agent workflows. CrewAI's `crew.kickoff()` returns a `CrewOutput` object containing aggregated token usage and task metadata across all agents and tasks in the crew.

### What It Detects

1. **`CrewOutput`** — returned by `crew.kickoff()`, contains aggregated results from all tasks, token usage, and task outputs
2. **`TaskOutput`** — individual task result with per-task description, agent, and token usage

### Resolution Chain

```
crew.kickoff() returns CrewOutput
         │
         ▼
┌─────────────────────────────┐
│ CrewAI (priority 245)       │  Detects CrewOutput
│ Extracts token_usage,       │  task_count, task metadata
│ raw output length           │
└─────────────────────────────┘
```

### CrewAI Metadata Extracted

| Attribute | Source | Description |
|-----------|--------|-------------|
| `tokens_input` | `token_usage.prompt_tokens` | Prompt tokens aggregated across all tasks |
| `tokens_output` | `token_usage.completion_tokens` | Completion tokens aggregated across all tasks |
| `crewai_total_tokens` | `token_usage.total_tokens` | Total tokens across the entire crew run |
| `crewai_successful_requests` | `token_usage.successful_requests` | Number of successful LLM API calls |
| `crewai_task_count` | `tasks_output` | Number of tasks executed by the crew |
| `crewai_raw_length` | `raw` | Character length of the raw text output |
| `crewai_has_json_output` | `json_dict` | `true` if the crew produced JSON output |
| `crewai_has_pydantic_output` | `pydantic` | `true` if the crew produced a Pydantic model |

For individual `TaskOutput` objects:

| Attribute | Source | Description |
|-----------|--------|-------------|
| `crewai_task_description` | `description` | Task description text |
| `crewai_agent` | `agent` / `name` | Agent role that executed the task |
| `crewai_raw_length` | `raw` | Character length of the task's raw output |

### Example Usage

```python
from rastir import configure, agent, llm
from crewai import Agent, Task, Crew, LLM

configure(service="my-crewai-app", push_url="http://localhost:8080")

crewai_llm = LLM(model="gemini/gemini-2.5-flash", api_key="...")

researcher = Agent(
    role="Geography Expert",
    goal="Answer geography questions using tools",
    backstory="You are a world geography expert.",
    llm=crewai_llm,
    tools=[...],
)

task = Task(
    description="What is the capital of France?",
    expected_output="The capital city name",
    agent=researcher,
)

crew = Crew(agents=[researcher], tasks=[task])

@agent(agent_name="crewai_agent")
def run_agent():
    @llm(model="gemini-2.5-flash", provider="gemini")
    def invoke_crew():
        return crew.kickoff()  # Returns CrewOutput
    return invoke_crew()
```

Rastir captures:
- **Agent span** for `run_agent` with full crew duration
- **LLM span** for `invoke_crew` with CrewAI adapter metadata:
  - `crewai_task_count` — number of tasks executed
  - `crewai_total_tokens` — aggregated token count
  - `crewai_successful_requests` — number of LLM calls made
  - `tokens_input` / `tokens_output` — prompt and completion token counts

### Multi-Agent Crews

For crews with multiple agents and tasks, the adapter aggregates token usage across all tasks:

```python
geo_agent = Agent(role="Geography Researcher", ...)
demo_agent = Agent(role="Demographics Researcher", ...)

task1 = Task(description="Find capital of Japan", agent=geo_agent)
task2 = Task(description="Find population of Japan", agent=demo_agent)

crew = Crew(agents=[geo_agent, demo_agent], tasks=[task1, task2])

@agent(agent_name="multi_agent_crew")
def run():
    @llm(model="gemini-2.5-flash", provider="gemini")
    def invoke():
        return crew.kickoff()
    return invoke()
```

The resulting LLM span will show `crewai_task_count=2` and aggregated token counts across both tasks.

---

## AdapterResult

Every adapter produces an `AdapterResult`:

```python
@dataclass
class AdapterResult:
    unwrapped_result: Any = None        # For framework adapters
    model: Optional[str] = None         # e.g., "gpt-4"
    provider: Optional[str] = None      # e.g., "openai"
    tokens_input: Optional[int] = None  # Prompt tokens
    tokens_output: Optional[int] = None # Completion tokens
    finish_reason: Optional[str] = None # e.g., "stop"
    extra_attributes: dict = field(default_factory=dict)
```

---

## Streaming Support

Adapters also handle streaming via `TokenDelta`:

```python
@dataclass
class TokenDelta:
    tokens_input: Optional[int] = None
    tokens_output: Optional[int] = None
    model: Optional[str] = None
    provider: Optional[str] = None
```

For streaming LLM calls, the adapter's `extract_stream_delta()` method is called for each chunk. Token counts are accumulated and recorded when the stream finishes.

---

## Detection Without Hard Imports

Adapters detect response types by **class name and module** rather than importing provider libraries:

```python
def can_handle(self, result: Any) -> bool:
    cls_name = type(result).__name__
    module = type(result).__module__ or ""
    return cls_name == "ChatCompletion" and "openai" in module
```

This means Rastir works without installing provider SDKs — adapters gracefully skip uninstalled providers.

---

## Explicit Overrides

You can bypass adapter detection by providing metadata directly:

```python
@llm(model="gpt-4", provider="openai")
def my_llm_call(query: str):
    # Adapter pipeline still runs, but explicit values take priority
    return custom_api_call(query)
```

---

## Custom Adapters

See [Contributing Adapters](contributing-adapters.md) for a complete guide on writing your own adapter.
