# LLM & Agent Observability System --- Requirements

## 1. Objective

Design and implement a Python-based observability library and server for
LLM and agent systems that:

-   Provides structured tracing and metrics
-   Supports LLM, tool, retrieval, and agent spans
-   Exports Prometheus metrics
-   Exports OpenTelemetry traces
-   Requires minimal user annotations
-   Avoids patching third-party libraries
-   Uses adapter-based schema normalization

------------------------------------------------------------------------

## 2. Design Principles

1.  Minimal cognitive load for users
2.  Explicit semantic decorators (`@trace`, `@agent`, `@llm`, `@tool`,
    `@retrieval`, `@metric`)
3.  No monkey-patching of third-party libraries
4.  Adapter-based metadata extraction
5.  Deterministic inference (no heuristic guessing)
6.  Low-cardinality Prometheus labels
7.  Non-blocking asynchronous exporters
8.  Graceful degradation if metadata unavailable

------------------------------------------------------------------------

## 3. Decorator Requirements

### 3.1 @trace

Purpose: - Create root span - Maintain parent-child span hierarchy -
Export OTEL traces - Optionally emit duration metric

Default Behavior: - Create span with name inferred from function name -
Record execution duration - Record success/failure - Inject global
labels into metrics

**Independence from @metric:** `@trace` and `@metric` are fully
independent. `@trace` manages spans and may optionally emit its own
duration metric into the trace. `@metric` emits generic function-level
Prometheus metrics. There is no internal delegation between them. Both
can be stacked on the same function if both span creation and generic
metrics are desired.

------------------------------------------------------------------------

### 3.2 @agent

Purpose: - Mark a function as an agent entry point - Create an
agent-typed span - Provide explicit agent identity for child spans

Parameters: - `agent_name` (optional, defaults to function name)

Default Behavior: - Create a span with `span_type=agent` - Set
`agent_name` in the span context so that child `@llm`, `@tool`, and
`@retrieval` spans can inherit the `agent` label for their Prometheus
metrics - Record execution duration and success/failure

**Agent label rule:** The `agent` label is injected into child LLM/tool
metrics **only** when the parent span is explicitly marked as an agent
via `@agent`. If `@llm` or `@tool` runs under a plain `@trace`, no
`agent` label is injected.

------------------------------------------------------------------------

### 3.3 @metric

Purpose: - Emit generic function metrics

Default Emitted Metrics: - `<function>_calls_total` -
`<function>_duration_seconds` - `<function>_failures_total`

Global Labels Injected: - service - env - version

No AI-specific logic inside @metric.

------------------------------------------------------------------------

### 3.4 @llm

Purpose: - Create semantic LLM span - Extract model, provider, tokens,
cost - Emit LLM-specific Prometheus metrics

Default Emitted Metrics: - `llm_calls_total` - `llm_latency_seconds` -
`llm_tokens_input_total` - `llm_tokens_output_total` - `llm_cost_total`
(if available)

LLM Metrics Labels: - service - env - version - agent (from parent
`@agent` span only; absent if parent is plain `@trace`) - model -
provider

Token extraction handled via adapters.

**Streaming auto-detection:** `@llm` auto-detects when the decorated
function returns a generator or async-generator and switches to
streaming accumulation mode automatically. A manual `streaming=True`
override flag is available but not required.

------------------------------------------------------------------------

### 3.5 @tool

Purpose: - Create tool execution span - Emit tool metrics

Metrics: - `tool_calls_total` - `tool_latency_seconds` -
`tool_failures_total`

Labels: - service - env - tool_name - agent (from parent `@agent` span
only; absent otherwise)

------------------------------------------------------------------------

### 3.6 @retrieval

Purpose: - Observe retrieval/vector operations

Metrics: - `retriever_calls_total` - `retriever_latency_seconds` -
`retrieved_documents_count`

**Document count extraction:** The library attempts to extract document
count via adapter logic (e.g., `len(result)` or `result.documents`). If
the count is not determinable from the return value, the
`retrieved_documents_count` metric is omitted for that call.
Optionally, the user may supply a custom extractor function:
`@retrieval(doc_count_extractor=lambda r: len(r.hits))`.

------------------------------------------------------------------------

## 4. Adapter Architecture

> **Detailed specification:** See
> [adapters_requirements.md](adapters_requirements.md) for the full
> adapter subsystem design including the base interface, `AdapterResult`
> contract, 3-phase resolution (Framework → Provider → Fallback),
> priority rules, streaming support, error handling, and testing
> requirements. This section provides a summary.

### 4.1 Adapter Categories

1.  Provider Adapters
    -   OpenAI
    -   Anthropic
    -   Bedrock
    -   Others
2.  Framework Adapters
    -   LangChain
    -   LangGraph
    -   CrewAI
3.  Utility Adapters (implemented as `kind="provider"` with lower
    priorities; see §4.1.1)
    -   Retrieval Adapter
    -   Tool Adapter
    -   Generic/Fallback Adapter (`kind="fallback"`)

#### 4.1.1 Utility Adapter Priority Range

**Implementation note:** The `BaseAdapter` interface defines three
kinds: `"framework"`, `"provider"`, and `"fallback"`. Utility
adapters (Retrieval, Tool) are implemented as `kind="provider"` but
use a dedicated priority range **10–99** (below the standard provider
range of 100–199, above the fallback at 0). This ensures they only
match after all real provider adapters have been tried.

Provider adapters extract: - model - provider - token usage - finish
reason

Framework adapters: - Unwrap framework-specific response objects -
Normalize metadata - Delegate to provider adapter for final extraction

Utility adapters (implemented as `kind="provider"`, priority 10–99): -
Retrieval: extract document count - Tool: no-op (duration and failure
handled by decorator, not adapter) - Generic/Fallback
(`kind="fallback"`, priority 0): fallback for unknown response types

------------------------------------------------------------------------

### 4.2 Adapter Interface

Each adapter must implement (see
[adapters_requirements.md §4–5](adapters_requirements.md) for full
contract):

-   name: str
-   kind: `"framework"` | `"provider"` | `"fallback"`
-   priority: int
-   can_handle(result) → bool
-   transform(result) → AdapterResult

`AdapterResult` contains: `unwrapped_result`, `model`, `provider`,
`tokens_input`, `tokens_output`, `finish_reason`, `extra_attributes`
(all optional).

Resolution follows three phases: Framework unwrap → Provider extraction
→ Fallback. See [adapters_requirements.md §6](adapters_requirements.md)
for details.

### 4.3 Adapter Fallback (No Match)

If no adapter's `can_handle(result)` returns `True`:

-   Emit the span with `model="unknown"` and `provider="unknown"`
-   Emit `llm_calls_total` and `llm_latency_seconds` metrics
-   **Skip** `llm_tokens_input_total`, `llm_tokens_output_total`, and
    `llm_cost_total` — token/cost metrics are only emitted when usage
    data is successfully extracted
-   Log a debug-level warning identifying the unrecognized result type

### 4.4 V1 Adapter Plan

The following adapters are included in the initial build, prioritized
to validate all observability semantics end-to-end:

#### 4.4.1 OpenAI Adapter (Mandatory)

-   **Extracts:** model, provider (`"openai"`), prompt/completion
    tokens, finish_reason
-   **Handles:** normal `ChatCompletion` responses + streaming
    chunk responses
-   **Purpose:** Most common provider; validates the full LLM metric
    pipeline including streaming accumulation

#### 4.4.2 Anthropic Adapter (Recommended)

-   **Extracts:** model, provider (`"anthropic"`), input/output tokens
-   **Handles:** Anthropic streaming chunks format (different shape
    from OpenAI)
-   **Purpose:** Tests schema variability across providers; confirms
    the adapter abstraction generalizes

#### 4.4.3 Bedrock Adapter (Recommended if AWS target)

-   **Extracts:** nested usage fields from Bedrock response envelope
-   **Normalizes:** provider/model naming (e.g.,
    `anthropic.claude-3-sonnet` → model=`claude-3-sonnet`,
    provider=`anthropic`)
-   **Purpose:** Validates handling of wrapped/nested JSON schemas

#### 4.4.4 LangChain Adapter (Framework Unwrapper)

-   **Detects:** `AIMessage`, `AIMessageChunk`, `ChatResult`,
    `LLMResult`, `ChatGeneration`, `Generation` (by class name +
    `langchain` / `langchain_core` module)
-   **Extracts:** usage from `response_metadata` and `usage_metadata`
    (modern LangChain ≥ 0.2 API). Supports both OpenAI-style
    (`prompt_tokens` / `completion_tokens`) and Anthropic-style
    (`input_tokens` / `output_tokens`) keys.
-   **Unwraps:** Looks for a native provider response object in
    `response_metadata["raw"]` or `additional_kwargs["raw_response"]`
    and delegates to the appropriate provider adapter for final
    extraction
-   **Extra attributes:** When no native object is found, extracted
    metadata (`tokens_input`, `tokens_output`, `model`,
    `finish_reason`) is placed in `extra_attributes` so it propagates
    through the fallback path
-   **Purpose:** Validates framework-wrapped responses; confirms the
    framework → provider adapter delegation pattern works

#### 4.4.5 Generic/Fallback Adapter (Mandatory)

-   **Always matches:** acts as the last adapter in the chain
-   **Returns:** `model="unknown"`, `provider="unknown"`
-   **Emits:** latency + call count only (no token/cost metrics)
-   **Purpose:** Ensures graceful degradation when no other adapter
    matches

#### 4.4.6 Retrieval Adapter (Minimal)

-   **Handles:** `list`, objects with `.documents`, or objects with
    `.page_content`
-   **Extracts:** document count only
-   **Purpose:** Validates the retrieval metrics pipeline
    (`retrieved_documents_count`)

#### 4.4.7 Tool Adapter (Minimal)

-   **No schema extraction** — tools have arbitrary return types
-   **Wraps:** duration + success/failure only
-   **Purpose:** Validates tool span creation and tool metrics emission

#### Minimal Viable Adapter Set

For a lean starting point that still covers all test scenarios:

| Adapter | Tests |
|---------|-------|
| OpenAI | Direct LLM calls, streaming, full token extraction |
| LangChain | Framework-wrapped LLM calls, adapter delegation |
| Generic/Fallback | Unknown-provider graceful degradation |
| Retrieval | Retrieval metrics pipeline |

This set validates: direct LLM calls, framework-wrapped calls,
streaming, unknown-provider fallback, and retrieval metrics.

------------------------------------------------------------------------

## 5. Span & Context Management

-   Use contextvars for span propagation
-   Each span stores:
    -   trace_id
    -   span_id
    -   parent_id
    -   span_type
    -   name
    -   start_time
    -   end_time
    -   attributes
    -   status

Parent-child linking must rely on active execution context.

------------------------------------------------------------------------

## 6. Streaming & MCP Requirements

**Auto-detection:** `@llm` inspects the return type of the decorated
function. If it is a `Generator`, `AsyncGenerator`, or iterator, the
decorator automatically enters streaming mode. No user annotation is
required. A manual `@llm(streaming=True)` flag is available as an
override for edge cases.

For streaming LLM calls:

-   Maintain a single long-lived LLM session span
-   Accumulate token deltas
-   Detect MCP tool calls via structured messages
-   Create child tool spans
-   Emit final LLM metrics at stream completion
-   Emit tool metrics at tool span completion

------------------------------------------------------------------------

## 7. Prometheus Metrics Requirements

### 7.1 Global Labels (Always Injected)

-   service
-   env
-   version

Optional: - tenant (bounded set only)

### 7.2 Label Restrictions

Must NOT use: - user_id - trace_id - prompt - request_id - raw text

Prevent high cardinality explosions.

------------------------------------------------------------------------

## 8. Trace Export Requirements

-   Support OTEL-compatible span export
-   Export spans asynchronously
-   Support batching
-   Never block application execution
-   Track exporter failures via internal metrics

------------------------------------------------------------------------

## 9. Failure Handling

-   Span must close even on exception
-   Emit failure metrics
-   Mark span status as ERROR
-   Never raise observability errors into application logic

------------------------------------------------------------------------

## 10. Performance Requirements

-   \< 5% overhead per observed function
-   No global tracing hooks
-   No AST rewriting
-   No sys.settrace usage
-   Streaming must not block iterator yield

------------------------------------------------------------------------

## 11. Server Requirements

> **Detailed specification:** See
> [server_requirement.md](server_requirement.md) for the full server
> design including in-memory metrics/trace storage, OTLP forwarding,
> queue/backpressure handling, multi-tenant isolation, configuration
> schema, health endpoints, startup/shutdown lifecycle, and performance
> targets. This section provides a summary.

The server must:

1.  Accept telemetry events via HTTP API
2.  Validate schema
3.  Normalize span data
4.  Aggregate Prometheus metrics
5.  Export OTEL traces
6.  Expose `/metrics` endpoint
7.  Support multi-tenant isolation
8.  Provide backpressure handling

### 11.1 Ingestion Protocol

> **Client-side configuration:** See
> [configuration_requirements.md](configuration_requirements.md) for the
> full client configuration spec including `configure()` API, env var
> fallback, exporter behavior, batching, retry, and auth.

The client SDK pushes structured telemetry events to a single endpoint:

**Endpoint:** `POST /v1/telemetry`

**Content-Type:** `application/json`

**Payload Schema:**

```json
{
  "service": "my-app",
  "env": "production",
  "version": "1.2.0",
  "spans": [
    {
      "type": "span",
      "trace_id": "abc123...",
      "span_id": "def456...",
      "parent_span_id": null,
      "span_type": "llm",
      "name": "call_openai",
      "start_time": "2026-02-27T10:00:00.000Z",
      "end_time": "2026-02-27T10:00:01.234Z",
      "status": "OK",
      "attributes": {
        "model": "gpt-4",
        "provider": "openai",
        "tokens_input": 150,
        "tokens_output": 80,
        "cost": 0.0069,
        "agent": "research_agent"
      },
      "events": []
    }
  ]
}
```

**Server processing per event:**

-   **Span events (`type: "span"`):** Stored for OTEL trace export.
    The server also **derives** Prometheus metrics from span attributes
    (e.g., a span with `span_type=llm` increments `llm_calls_total`,
    records `llm_latency_seconds`, etc.). This means the client pushes
    spans only — the server is responsible for metric derivation.
-   Batching: clients buffer events and push periodically
    (configurable interval, default 5s, max batch size 100)
-   Auth: optional `X-API-Key` header

------------------------------------------------------------------------

## 12. Extensibility

System must support:

-   Adding new provider adapters
-   Adding new framework adapters
-   Custom metric decorators
-   Pluggable exporters
-   Sampling strategies
-   Future evaluation scoring modules

------------------------------------------------------------------------

## 13. Non-Goals

The system must NOT:

-   Rewrite user code
-   Monkey patch third-party libraries
-   Guess metadata from prompt text
-   Infer tokens via string length
-   Introduce unbounded metric cardinality

------------------------------------------------------------------------

## 15. Technology & Tooling Decisions

The following choices have been finalized for the initial implementation:

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Server HTTP framework | **FastAPI** | Async-native, Pydantic validation, auto-generated OpenAPI docs — ideal for a telemetry ingestion service |
| Client HTTP transport | **httpx** | Native async support, connection pooling, background push without thread hacks; avoids the complexity `requests` introduces for async/streaming flows |
| OTEL trace export (server-side) | **opentelemetry-sdk** (official) | Stable, well-tested, handles batching/retries/exporters; writing a custom OTLP client is unnecessary risk |
| Configuration API | **`configure()` + env vars** | Priority: explicit `configure()` > environment variables > built-in defaults. Flexible without hidden behavior |
| Package build system | **hatchling** | Modern PEP 517/518, clean `pyproject.toml` config, no legacy `setup.py` baggage |

------------------------------------------------------------------------

## 16. Summary

This observability system provides:

-   Structured tracing
-   Deterministic AI metric extraction
-   Adapter-based schema handling
-   Clean decorator-based API
-   Prometheus + OpenTelemetry integration
-   Safe, scalable, production-ready behavior

End of Requirements.
