---
layout: default
title: Architecture — Responsibility Boundaries
nav_order: 13
---

# Architecture — Responsibility Boundaries

This document defines exactly **which layer is responsible for what** across
Rastir's three tiers: **Adapter**, **Client**, and **Server**. Every adapter
implementation must conform to these boundaries.

---

## 1. Adapters (per-provider / per-framework)

Adapters are stateless extraction modules. They inspect request arguments
and response objects to produce structured metadata. They must **never**
create spans, mutate global state, or interact with the server.

### 1.1 Adapter Kinds

| Kind | Priority Range | Purpose |
|---|---|---|
| `framework` | 200–300 | Unwrap framework wrappers (LangChain, LangGraph, LlamaIndex, CrewAI) and detect embedded model objects |
| `provider` | 100–199 | Extract model, provider, tokens, finish_reason from native SDK responses (OpenAI, Anthropic, Bedrock, etc.) |
| `fallback` | 0 | Catch-all for unrecognised responses |

### 1.2 Adapter Capabilities

Every adapter must declare its capabilities via flags on the class:

| Flag | Meaning |
|---|---|
| `supports_tokens` | Adapter extracts `tokens_input` / `tokens_output` from responses |
| `supports_streaming` | Adapter handles streaming chunks via `extract_stream_delta()` |
| `supports_request_metadata` | Adapter inspects request args to extract model/provider/config pre-invocation |
| `supports_guardrail_metadata` | Adapter extracts guardrail IDs, actions, and violation categories |

### 1.3 Adapter Interface — Required Methods

| Method | Phase | Returns | Responsibility |
|---|---|---|---|
| `can_handle(result)` | Response | `bool` | Detect if this adapter owns the response object (use class name + module, never import the SDK) |
| `transform(result)` | Response | `AdapterResult` | Extract `model`, `provider`, `tokens_input`, `tokens_output`, `finish_reason`, `extra_attributes` |
| `can_handle_request(args, kwargs)` | Request | `bool` | Detect if request args contain objects this adapter understands |
| `extract_request_metadata(args, kwargs)` | Request | `RequestMetadata` | Extract `model`, `provider`, and provider-specific config (e.g. guardrail_id) from request args |
| `can_handle_stream(chunk)` | Streaming | `bool` | Detect if a streaming chunk belongs to this provider |
| `extract_stream_delta(chunk)` | Streaming | `TokenDelta` | Extract incremental token counts and model/provider from a chunk |

### 1.4 What Adapters Extract

| Data Point | Request Phase | Response Phase | Streaming Phase |
|---|---|---|---|
| `model` | From model objects in args (e.g. `ChatOpenAI.model_name`) | From response object (e.g. `response.model`) | From first chunk |
| `provider` | From module path of model objects | From response object module | From first chunk |
| `tokens_input` | — | From usage/meta dict | Accumulated from deltas |
| `tokens_output` | — | From usage/meta dict | Accumulated from deltas |
| `finish_reason` | — | From response choices/stop_reason | — |
| `guardrail_id` | From request kwargs (Bedrock) | — | — |
| `guardrail_version` | From request kwargs (Bedrock) | — | — |
| `guardrail_action` | — | From response trace (Bedrock) | — |
| `guardrail_category` | — | From response trace (Bedrock) | — |

### 1.5 What Adapters Must NOT Do

- Create or manage spans / trace context
- Import provider SDKs at module level (use class-name sniffing)
- Write metrics or counters
- Interact with the server or transport
- Hold mutable state across calls

---

## 2. Client (decorators, spans, transport)

The client is the user-facing instrumentation layer. It manages span
lifecycle, context propagation, and batch transport.

### 2.1 Decorators

| Decorator | Span Type | Responsibilities |
|---|---|---|
| `@llm` | `llm` | Create span; call `resolve_request()` pre-invocation; call `resolve()` or stream-accumulate post-invocation; apply `model=`/`provider=` overrides; set `agent` from context |
| `@agent` | `agent` | Create span; set agent name in context for child spans to inherit |
| `@trace` | `system` | Create span; generic function tracing with no AI-specific logic |
| `@metric` | `metric` | Create span for metric emission only (calls, duration, failures) |
| `@retrieval` | `retrieval` | Create span; call retrieval adapter for metadata |

### 2.2 Span Lifecycle (Client Owns)

| Responsibility | Method / Location |
|---|---|
| Create span (trace_id, span_id, parent_span_id) | `start_span()` |
| Set span_type | Decorator determines type |
| Set `service`, `env`, `version` | `rastir.configure()` |
| Set `agent` label on child spans | `@agent` pushes to context; `@llm` reads it |
| Compute `duration_ms` | `span.finish()` |
| Set `status` (OK / ERROR) | `span.finish(SpanStatus.OK / ERROR)` |
| Record exception details | `span.record_error(exc)` — stores class, message, traceback |
| Capture prompt/response text | `_extract_request_metadata()` for prompt if applicable |
| Apply `model`/`provider` override | `@llm(model=..., provider=...)` sets before adapter runs |
| Call adapter request phase | `resolve_request(args, bound_kw)` pre-invocation |
| Call adapter response phase | `resolve(result)` post-invocation |
| Accumulate stream chunks | `_accumulate_stream_chunk()` iterates and delegates to adapters |
| Enqueue span for export | `enqueue_span(span)` after `end_span()` |
| Batch HTTP transport | `BatchTransport` posts to `push_url` / `/v1/spans` |

### 2.3 What Client Must NOT Do

- Parse provider-specific response objects (that's the adapter's job)
- Compute metrics or counters
- Apply redaction
- Make decisions about sampling

---

## 3. Server (processing, metrics, export)

The server is the central processing pipeline that receives spans from
clients and derives all observability outputs.

### 3.1 Ingestion Pipeline

| Step | Responsibility |
|---|---|
| Receive spans | FastAPI `/v1/spans` endpoint |
| Queue management | Bounded async queue with backpressure and drop-oldest eviction |
| Sampling | Probabilistic per-trace sampling (retain or drop for storage; metrics always recorded) |
| Redaction | Regex-based PII masking on prompt/response attributes |
| Metrics derivation | Update Prometheus counters/histograms/gauges |
| OTLP forwarding | Convert spans to OTLP protobuf and export to Tempo |
| Evaluation | Async eval queue with registered evaluators (toxicity, etc.) |
| Trace store | In-memory TTL-based span storage for query API |

### 3.2 Metrics Labels (Server Derives)

The server reads raw span attributes and derives these Prometheus labels:

| Label | Source | Applied To |
|---|---|---|
| `service` | Span dict `service` field | All metrics |
| `env` | Span dict `env` field | All metrics |
| `span_type` | Normalised from raw type → canonical set | `spans_ingested`, `duration`, `errors` |
| `status` | Span `status` field (OK/ERROR) | `spans_ingested` |
| `model` | Span attribute `model` (from adapter) | `llm_calls`, `tokens_*`, `duration`, `errors`, `guardrail_*`, `evaluation_*` |
| `provider` | Span attribute `provider` (from adapter) | Same as model |
| `agent` | Span attribute `agent` (from client context) | `llm_calls`, `tokens_*`, `tool_calls`, `guardrail_*` |
| `tool_name` | Span attribute `tool_name` (from client) | `tool_calls` |
| `error_type` | Normalised from exception class → category | `errors` |
| `guardrail_id` | Span attribute (from Bedrock adapter) | `guardrail_requests`, `guardrail_violations` |
| `guardrail_version` | Span attribute (from Bedrock adapter) | `guardrail_requests` |
| `guardrail_action` | Span attribute (from Bedrock adapter) | `guardrail_violations` |
| `guardrail_category` | Span attribute, bounded enum (from Bedrock adapter) | `guardrail_violations` |
| `evaluation_type` | Span attribute (from eval worker) | `evaluation_*` |

### 3.3 Server-Side Guards

| Guard | Purpose |
|---|---|
| Cardinality caps | Per-dimension limits (model=50, provider=10, tool_name=200, agent=200, error_type=50, guardrail_id=100). Overflow values replaced with `__cardinality_overflow__`. |
| Label value length | Truncate labels to `max_label_value_length` (default 128 chars) |
| Span type normalisation | Map unknown types to `system` |
| Error type normalisation | Map raw exception classes to fixed categories: `timeout`, `rate_limit`, `validation_error`, `provider_error`, `internal_error`, `unknown` |
| Guardrail enum validation | Server-side bounded enum for `guardrail_category` and `guardrail_action` |

### 3.4 What Server Must NOT Do

- Import or understand provider SDK types
- Create spans or manage trace context
- Know about decorator logic or function signatures

---

## 4. Data Flow Summary

```
User Code
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│  CLIENT (decorators)                                    │
│                                                         │
│  @agent / @llm / @trace / @retrieval                    │
│    │                                                    │
│    ├─ start_span()           → create SpanRecord        │
│    ├─ resolve_request()      → ADAPTER request phase    │
│    ├─ fn(*args, **kwargs)    → execute user function    │
│    ├─ resolve() / stream()   → ADAPTER response phase   │
│    ├─ span.finish()          → set status, duration     │
│    ├─ record_error()         → capture exception        │
│    └─ enqueue_span()         → batch transport          │
│                                                         │
│  BatchTransport ──HTTP POST──▶ push_url/v1/spans        │
└─────────────────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────┐
│  SERVER                                                 │
│                                                         │
│  /v1/spans → ingestion queue                            │
│    │                                                    │
│    ├─ sampling (probabilistic per-trace)                │
│    ├─ redaction (PII masking)                           │
│    ├─ metrics derivation (Prometheus)                   │
│    ├─ OTLP forwarding (Tempo)                           │
│    ├─ evaluation (async queue → eval workers)           │
│    └─ trace store (in-memory TTL)                       │
└─────────────────────────────────────────────────────────┘
```

---

## 5. Current Adapter Compliance Matrix

| Adapter | Kind | Priority | Request Metadata | Response Metadata | Streaming | Guardrails | Compliance Notes |
|---|---|---|---|---|---|---|---|
| **OpenAI** | provider | 150 | ✅ model, provider | ✅ model, tokens, finish_reason | ✅ | — | Fully compliant |
| **Anthropic** | provider | 150 | ✅ model, provider | ✅ model, tokens, finish_reason | ✅ | — | Fully compliant |
| **Bedrock** | provider | 140 | ✅ model, provider, guardrail config | ✅ model, tokens, guardrails | ✅ | ✅ | Fully compliant |
| **Azure OpenAI** | provider | 155 | ✅ model, provider | ✅ model, tokens, finish_reason | ✅ | — | Fully compliant |
| **Gemini** | provider | 150 | ✅ model, provider | ✅ model, tokens, finish_reason | ✅ (cumulative) | — | Fully compliant; streaming uses `usage_mode="cumulative"` |
| **Groq** | provider | 152 | ✅ model, provider | ✅ model, tokens, finish_reason, queue_time | ✅ | — | Fully compliant; extracts Groq-specific timing attributes |
| **Mistral** | provider | 150 | ✅ model, provider | ✅ model, tokens, finish_reason | ✅ | — | Fully compliant |
| **Cohere** | provider | 150 | ✅ model, provider | ✅ model, tokens (v1+v2), finish_reason | ✅ | — | Fully compliant; supports both Cohere v1 and v2 SDKs |
| **LangChain** | framework | 250 | ✅ model, provider from model objects | ✅ unwrap raw + LC metadata | ❌ | — | No stream methods; unwraps `RunnableBinding.bound` |
| **LangGraph** | framework | 260 | ✅ model, provider via node/closure walk | ✅ unwrap AIMessage + state | ✅ | — | Fully compliant; deep graph traversal for model discovery |
| **LlamaIndex** | framework | 240 | ❌ | ✅ unwrap raw + source_nodes | ✅ | — | Needs `can_handle_request` for LlamaIndex model objects |
| **CrewAI** | framework | 245 | ❌ | ✅ token_usage, task metadata | ❌ | — | Needs request metadata; streaming overrides are no-ops |
| **Retrieval** | provider | 50 | ❌ | ✅ retrieval doc count | ❌ | — | N/A — no LLM, no tokens |
| **Tool** | provider | 10 | ❌ | ❌ (no-op) | ❌ | — | N/A — `can_handle()` returns False; metadata set by `@tool` decorator |
| **Fallback** | fallback | 0 | ❌ | ✅ basic (unknown/unknown) | ❌ | — | Catch-all, minimal by design |

### Framework Support Modules

ADK and Strands use standalone support modules (`adk_support.py`, `strands_support.py`) instead of the adapter pipeline. They intercept framework events directly rather than going through the `resolve()` / `transform()` adapter chain.

| Module | Decorator | LLM Discovery | Tool Discovery | Streaming | MCP Tracing | Notes |
|---|---|---|---|---|---|---|
| **ADK** | `@adk_agent` | ✅ via `run_async` event interception | ✅ via event interception | ✅ (async event stream) | ✅ traceparent injection | Detects `Runner`/`BaseAgent` objects; creates LLM + tool spans from ADK events |
| **Strands** | `@strands_agent` | ✅ via `model.stream` wrapping | ✅ via `tool.stream` wrapping | ✅ (stream wrapping) | ✅ traceparent injection | Detects `Agent` objects; wraps model and tool stream methods |

### Alignment Gaps (TODO)

1. **LlamaIndex needs request metadata:** Should implement `can_handle_request()` / `extract_request_metadata()` to detect LlamaIndex model objects in function arguments.

2. **CrewAI needs request metadata:** Should implement request-phase extraction for CrewAI agent/crew objects. Streaming support is not applicable for CrewAI's batch execution model.

3. **LangChain streaming:** Does not implement `can_handle_stream()` / `extract_stream_delta()`. Stream processing relies on the underlying provider adapter after LangChain unwraps the response.

---

## 6. Label Inheritance — Parent → Child Propagation

### 6.1 Propagation Mechanism

Rastir does **not** copy attributes from parent spans to children. Instead,
it uses dedicated `ContextVar` variables that decorators set and read:

| ContextVar | Set by | Read by | Value |
|---|---|---|---|
| `_current_span` | `start_span()` | `get_current_span()` | Current span (for parent-child linking) |
| `_current_agent` | `@agent` | `@llm`, `@retrieval` | Agent name string |
| `_current_model` | `@llm` (in `_finalize_llm_span`) | — | Model name string |
| `_current_provider` | `@llm` (in `_finalize_llm_span`) | — | Provider name string |

### 6.2 Inheritance Rules (Normative)

Every span type MUST carry these labels when available. The table below
specifies who sets each label and where the value comes from.

| Label | `@agent` | `@llm` | `@retrieval` | `@trace` | `evaluation` (server) |
|---|---|---|---|---|---|
| `agent` | Sets from decorator arg | Reads from `_current_agent` | Reads from `_current_agent` | — | Copies from parent LLM span |
| `model` | — | Adapter extracts (request + response) or decorator override | **Should** read from `_current_model` | — | Copies from parent LLM span |
| `provider` | — | Adapter extracts (request + response) or decorator override | **Should** read from `_current_provider` | — | Copies from parent LLM span |
| `service` | From `configure()` | From `configure()` | From `configure()` | From `configure()` | From parent LLM span |
| `env` | From `configure()` | From `configure()` | From `configure()` | From `configure()` | From parent LLM span |
| `evaluator_model` | — | — | — | — | **NEW:** From `JudgeConfig.model` |
| `evaluator_provider` | — | — | — | — | **NEW:** From `JudgeConfig.provider` |

### 6.3 Typical Call Tree & Label Flow

```
@agent("travel_planner")
  │  sets: agent="travel_planner"
  │  pushes: _current_agent = "travel_planner"
  │
  └─▶ @llm(evaluate=True)
        │  reads: agent from _current_agent → "travel_planner"
        │  sets:  model="gpt-4o-mini" (from adapter request/response phase)
        │         provider="openai"   (from adapter)
        │  pushes: _current_model = "gpt-4o-mini"
        │          _current_provider = "openai"
        │
        ├─▶ @retrieval("search_docs")
        │     reads: agent    from _current_agent    → "travel_planner"
        │            model    from _current_model    → "gpt-4o-mini"   ← GAP: not implemented yet
        │            provider from _current_provider → "openai"        ← GAP: not implemented yet
        │
        └─▶ evaluate:toxicity  (server-side, child of LLM span)
              copies: model="gpt-4o-mini", provider="openai", agent="travel_planner"
              NEW:    evaluator_model="gpt-4o-mini", evaluator_provider="openai"
```

### 6.4 Server-Side Label Usage

The server reads labels from span attributes to derive metrics. These labels
MUST be present on the span for the corresponding metric to be meaningful:

| Metric | Required Labels | Label Source |
|---|---|---|
| `rastir_spans_ingested_total` | service, env, span_type, status | Universal |
| `rastir_llm_calls_total` | service, env, model, provider, agent | Adapter + context |
| `rastir_tokens_input_total` | service, env, model, provider, agent | Adapter |
| `rastir_tokens_output_total` | service, env, model, provider, agent | Adapter |
| `rastir_tool_calls_total` | service, env, tool_name, agent, model, provider | Context inheritance |
| `rastir_retrieval_calls_total` | service, env, agent | Context |
| `rastir_duration_seconds` | service, env, span_type, model, provider | Context inheritance for non-LLM spans |
| `rastir_errors_total` | service, env, span_type, error_type, model, provider | Context inheritance for non-LLM spans |
| `rastir_tokens_per_call` | service, env, model, provider | Adapter |
| `rastir_guardrail_requests_total` | service, env, provider, model, agent, guardrail_id, guardrail_version | Adapter (Bedrock) |
| `rastir_guardrail_violations_total` | service, env, provider, model, agent, guardrail_id, guardrail_action, guardrail_category | Adapter (Bedrock) |
| `rastir_evaluation_runs_total` | service, env, model, provider, evaluation_type, **evaluator_model**, **evaluator_provider** | Parent LLM span + JudgeConfig |
| `rastir_evaluation_failures_total` | service, env, model, provider, evaluation_type, **evaluator_model**, **evaluator_provider** | Parent LLM span + JudgeConfig |
| `rastir_evaluation_latency_seconds` | service, env, model, provider, evaluation_type, **evaluator_model**, **evaluator_provider** | Parent LLM span + JudgeConfig |
| `rastir_evaluation_score` | service, env, model, provider, evaluation_type, **evaluator_model**, **evaluator_provider** | Parent LLM span + JudgeConfig |

---

## 7. Identified Gaps & Alignment Plan

### 7.1 Client-Side Gaps

| # | Gap | Location | Fix |
|---|---|---|---|
| 1 | `@retrieval` does not inherit `model`/`provider` from context | `decorators.py` `@retrieval` wrapper | Add `get_current_model()` / `get_current_provider()` reads |
| 2 | `duration` histogram gets empty `model`/`provider` for non-LLM spans | `metrics.py` `record_span()` model extraction gated on `span_type == "llm"` | Extract model/provider from attrs for ALL span types that carry them (tool, evaluation, retrieval) |
| 3 | `errors` metric gets empty `model`/`provider` for non-LLM spans | Same gating as #2 | Same fix as #2 |

### 7.2 Server-Side Gaps

| # | Gap | Location | Fix |
|---|---|---|---|
| 4 | No `evaluator_model` / `evaluator_provider` labels on evaluation metrics | `evaluation_worker.py`, `metrics.py` | Add `evaluator_model`, `evaluator_provider` to `_eval_labels`; populate from `JudgeConfig` passed through `EvaluationTask` |
| 5 | Evaluation spans re-ingested with empty `model`/`provider` in `duration`/`errors` | `metrics.py` `record_span()` | Fix #2 covers this — extract model/provider from attrs regardless of span_type |
| 6 | `retrieval_calls_total` missing `model`/`provider` labels | `metrics.py` retrieval section | Add `model`, `provider` labels to `rastir_retrieval_calls_total` |

### 7.3 Adapter Gaps (from Section 5)

| # | Gap | Fix |
|---|---|---|
| 7 | LlamaIndex lacks request-phase metadata extraction | Implement `can_handle_request()` / `extract_request_metadata()` |
| 8 | CrewAI lacks request-phase metadata extraction | Implement `can_handle_request()` / `extract_request_metadata()` |
| 9 | LangChain does not implement stream methods (`can_handle_stream()` / `extract_stream_delta()`) | Either implement or document that stream processing defers to provider adapters |

---

## 8. Rules for New Adapters

1. **Never import the provider SDK** at module level. Use `type(obj).__name__` and `type(obj).__module__` for detection.
2. **Always implement both request and response phases.** A well-behaved adapter provides `can_handle_request()` + `extract_request_metadata()` AND `can_handle()` + `transform()`.
3. **Use `BaseAdapter._find_in_args()`** to scan positional and keyword arguments.
4. **Use `BaseAdapter._extract_model_attr()`** to read the first available model attribute from an object.
5. **Use `detect_provider_from_module()`** to map module names to canonical provider strings.
6. **Declare capability flags** accurately — the registry uses them to skip unnecessary calls.
7. **Set priority** in the correct range for your adapter kind.
8. **Register via `__init__.py`** — adapters are auto-registered on import.
9. **Labels must flow downward.** Any label set by a parent decorator (`agent`, `model`, `provider`) must be readable by child spans via `ContextVar`s. If adding a new inheritable label, add a `ContextVar` + getter/setter in `context.py`.
