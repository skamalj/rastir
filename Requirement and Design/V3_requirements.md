# Rastir --- V3 Requirements (Refined v3)

## 1. Objective

V3 expands Rastir into ecosystem maturity and guardrail-aware
observability while preserving stateless server design and
backward-compatible metrics.

V3 introduces:

-   Expanded provider & framework adapters (15 total)
-   Generic infrastructure object wrapper (`rastir.wrap()`)
-   Bedrock guardrail observability (request + response level)
-   Structured guardrail metrics with model-level visibility
-   **Two-phase enrichment architecture** — metadata captured at
    request phase (pre-invocation) and refined at response phase
-   **Generic model kwarg scanner** — fallback extraction of model
    from common parameter names across SDKs

No enforcement logic is included in V3.

------------------------------------------------------------------------

# 2. Ecosystem Expansion

## 2.1 Provider Adapters

Supported providers (8 adapters):

| Adapter            | Priority | Streaming | Tokens | Request Metadata | Guardrails |
|--------------------|----------|-----------|--------|------------------|------------|
| AzureOpenAI        | 155      | Yes       | Yes    | No               | No         |
| Groq               | 152      | Yes       | Yes    | No               | No         |
| OpenAI             | 150      | Yes       | Yes    | No               | No         |
| Anthropic          | 150      | Yes       | Yes    | No               | No         |
| Gemini             | 150      | Yes       | Yes    | No               | No         |
| Cohere             | 150      | Yes       | Yes    | No               | No         |
| Mistral            | 150      | Yes       | Yes    | No               | No         |
| Bedrock (Converse) | 140      | Yes       | Yes    | Yes              | Yes        |

All provider adapters must:

-   Normalize model identifier
-   Extract input/output token usage
-   Extract latency
-   Support streaming
-   Extract provider-native metadata (if available)

------------------------------------------------------------------------

## 2.2 Framework Adapters

Supported frameworks (4 adapters):

| Adapter   | Priority | Kind      |
|-----------|----------|-----------|
| LangGraph | 260      | framework |
| LangChain | 250      | framework |
| CrewAI    | 245      | framework |
| LlamaIndex| 240      | framework |

Framework integration must:

-   Wrap execution entrypoints only (invoke/run/stream)
-   Avoid patching internal classes
-   Preserve span hierarchy

## 2.3 Utility Adapters

| Adapter   | Priority | Kind      |
|-----------|----------|-----------|
| Retrieval | 50       | provider  |
| Tool      | 10       | provider  |
| Fallback  | 0        | fallback  |

**Total: 15 registered adapters**, sorted by descending priority at
resolution time.

------------------------------------------------------------------------

# 3. Generic Object Wrapper (Infrastructure Layer)

## 3.1 Objective

Provide universal instrumentation for arbitrary Python objects:

    wrapped = rastir.wrap(obj, span_type="infra")

## 3.2 Requirements

Wrapper must:

-   Intercept public callable methods only
-   Support sync and async methods
-   Preserve return values and exceptions
-   Avoid wrapping private/dunder methods
-   Prevent double wrapping
-   Preserve isinstance compatibility
-   Maintain minimal overhead

## 3.3 Validated Use Cases

-   LangGraph MemorySaver
-   LangGraph DynamoDB checkpointer
-   Vector DB clients
-   Redis / cache clients
-   Custom tool classes

------------------------------------------------------------------------

# 4. Two-Phase Enrichment Architecture

## 4.1 Overview

Metadata is captured in two phases:

1.  **Request phase** (pre-invocation) — extract from function
    args/kwargs before the call executes.
2.  **Response phase** (post-invocation) — extract from the API
    response/result.

This ensures metadata (model, provider, guardrails) survives even
when the API call fails, since request-phase attributes are set on
the span before function execution.

## 4.2 Request-Phase Extraction

The `@llm` decorator calls `resolve_request(args, kwargs)` before
function execution.  Resolution follows adapter priority order:

1.  Each adapter with `supports_request_metadata=True` is checked
    via `can_handle_request(args, kwargs)`.
2.  First matching adapter's `extract_request_metadata()` is called.
3.  If no adapter matches, the **generic kwarg scanner** runs as
    fallback.

### 4.2.1 Bedrock Request Metadata

The Bedrock adapter matches on `modelId`, `guardrailIdentifier`, or
`guardrailConfig` in kwargs.  It extracts:

-   **model / provider** from `modelId` (e.g.,
    `"anthropic.claude-3-haiku-v1:0"` → model=`"claude-3-haiku-v1:0"`,
    provider=`"anthropic"`)
-   **guardrail.id**, **guardrail.version**, **guardrail.enabled**
    from guardrail configuration

### 4.2.2 Generic Kwarg Scanner

When no adapter's `can_handle_request()` matches, a fallback scanner
checks kwargs for common model parameter names:

    _COMMON_MODEL_KWARGS = ("model", "model_id", "modelId", "model_name")

The first string-valued match is used as `model`.  This covers
arbitrary user functions that pass model names as keyword arguments.

## 4.3 Response-Phase Override Logic

After function execution, the `@llm` decorator calls `resolve(result)`
and applies this merge strategy:

-   **Concrete response value wins**: if the response returns a model
    or provider that is not `"unknown"`, it overwrites the
    request-phase value.
-   **Request-phase value preserved**: if the response returns
    `"unknown"` or `None`, the request-phase value remains on the
    span.
-   **Fallback**: if neither phase sets a value, `"unknown"` is used.

This handles the **Bedrock Converse limitation**: the Converse API
does not return `modelId` in its response body or headers, so the
response adapter always returns `model="unknown"`, `provider="bedrock"`.
With two-phase enrichment, the model value (e.g.,
`"claude-3-haiku-20240307-v1:0"`) is preserved from the request phase
since the response `"unknown"` does not overwrite.  The provider
attribute resolves to `"bedrock"` (concrete response value wins),
which correctly indicates the service endpoint.

## 4.4 Error Resilience

If a decorated function raises an exception:

-   Request-phase attributes (model, provider, guardrails) are already
    set on the span and persist.
-   Response-phase extraction is skipped.
-   The span is marked with `status="ERROR"` and captures the
    exception info.

------------------------------------------------------------------------

# 5. Bedrock Guardrail Observability (Metadata-Only)

V3 introduces structured observability for Bedrock guardrails.

This includes BOTH:

1.  Guardrail configured (request-level)
2.  Guardrail intervened (response-level)

No enforcement, blocking, or policy engine is included.

------------------------------------------------------------------------

## 5.1 Request-Level Guardrail Detection

When request kwargs contain guardrail configuration
(`guardrailIdentifier`, `guardrailVersion`, or `guardrailConfig`):

Span annotations:

-   `guardrail.id`
-   `guardrail.version`
-   `guardrail.enabled = true`

Metric emitted:

    rastir_guardrail_requests_total

Labels:

-   service
-   env
-   provider
-   guardrail_id
-   guardrail_version

This measures guardrail adoption.

------------------------------------------------------------------------

## 5.2 Response-Level Guardrail Intervention Detection

When response metadata indicates guardrail action (e.g., action !=
NONE):

Adapter extracts structured metadata only (no text inspection):

-   guardrail_action (e.g., GUARDRAIL_INTERVENED)
-   guardrail_category (from structured assessments)
-   guardrail_reason (optional, not used as metric label)

Span annotations:

-   `guardrail.triggered = true`
-   `guardrail.action`
-   `guardrail.category` (bounded)

Metric emitted:

    rastir_guardrail_violations_total

Labels:

-   service
-   env
-   provider
-   model
-   guardrail_id (if available)
-   guardrail_action
-   guardrail_category (bounded enum)

Metric increments only when intervention occurs.

Model label is REQUIRED on violations metric to enable per-model
guardrail analysis.

------------------------------------------------------------------------

## 5.3 Guardrail Cardinality Controls

To prevent explosion:

-   guardrail_id capped (e.g., 100 distinct values)
-   guardrail_category must be bounded enum
-   model label subject to global model cardinality cap
-   No free-text labels allowed
-   actionReason never used as metric label

Overflow must map to:

    __cardinality_overflow__

------------------------------------------------------------------------

## 5.4 Span Status Rules

Guardrail intervention does NOT mark span as ERROR.

Span status remains OK unless provider returns actual invocation error.

------------------------------------------------------------------------

# 6. Streaming Guardrail Handling

For streaming APIs:

-   Guardrail configuration must be detected at request time
-   Guardrail intervention must be extracted from final structured
    metadata
-   Metrics emitted after stream completion
-   Partial metadata accumulation supported

------------------------------------------------------------------------

# 7. Adapter Capability Registry

Each adapter declares capability flags:

```python
supports_tokens: bool = False
supports_streaming: bool = False
supports_request_metadata: bool = False
supports_guardrail_metadata: bool = False
```

Decorator must not assume capabilities not declared.

**Implementation note:** The flag name is `supports_request_metadata`
(not `supports_request_guardrail_detection`).  This reflects the
expanded scope — request-phase extraction now covers model/provider
in addition to guardrail configuration.

------------------------------------------------------------------------

# 8. Integration Test Coverage

V3 includes 34 live integration tests across 7 test classes:

### 8.1 OpenAI Tests (`TestOpenAI`)
-   Chat completion (non-streaming)
-   Streaming chat completion
-   resolve() result validation

### 8.2 Anthropic Tests (`TestAnthropic`)
-   Messages API (non-streaming)
-   Streaming messages
-   resolve() result validation

### 8.3 Bedrock Tests (`TestBedrock`)
-   Converse API (non-streaming)
-   Streaming Converse
-   System prompt token counting
-   Guardrail configuration detection (request-level)
-   @llm decorator wrapping (response-phase)
-   **Two-phase enrichment** — modelId as kwarg to decorated function

### 8.4 LangChain Tests (`TestLangChain`)
-   ChatOpenAI invoke
-   ChatAnthropic invoke
-   ChatBedrock invoke
-   Streaming output

### 8.5 LangGraph Tests (`TestLangGraph`)
-   Simple graph execution
-   Graph with wrapped MemorySaver
-   Multi-turn with checkpointer

### 8.6 LlamaIndex Tests (`TestLlamaIndex`)
-   OpenAI LLM complete
-   Anthropic LLM complete
-   Bedrock LLM complete

### 8.7 Cross-Cutting Tests (`TestCrossCutting`)
-   `wrap()` on arbitrary objects
-   Nested decorators (@agent → @llm)
-   @llm decorator with each provider

Tests validate:

-   Correct span annotations (model, provider, tokens)
-   Correct metric emission (guardrail metrics)
-   Model label presence on violations metric
-   No cardinality explosion
-   Proper separation of request-level vs response-level metrics
-   Two-phase enrichment preserves request-phase metadata

------------------------------------------------------------------------

# 9. Architecture Decisions

## 9.1 Adapter Resolution Pipeline

Resolution uses a 3-phase pipeline:

1.  **Sorting**: adapters sorted by descending priority (once, cached)
2.  **Matching**: `can_handle()` predicate evaluated per adapter
3.  **Extraction**: first matching adapter's `resolve()` normalizes
    the result

Higher-priority adapters shadow lower ones.  Framework adapters
(priority 240-260) are evaluated before provider adapters
(priority 140-155) to catch framework-wrapped responses.

## 9.2 Bedrock Converse API Limitation

The AWS Bedrock Converse API does NOT return `modelId` in the
response body or response headers.  This means:

-   `resolve(response_dict)` alone → `model="unknown"`, `provider="bedrock"`
-   Two-phase enrichment via `@llm` decorator → model/provider captured
    from request kwargs, survives the unknown response

This is a known AWS API design characteristic. The two-phase
architecture was designed specifically to handle this pattern.

## 9.3 Span Type Semantics

Six decorator types map to span types:

| Decorator   | Span Type  |
|-------------|------------|
| `@trace`    | trace      |
| `@agent`    | agent      |
| `@llm`      | llm        |
| `@tool`     | tool       |
| `@retrieval`| retrieval  |
| `@metric`   | metric     |

Only `@llm` runs adapter resolution.  All decorators support sync
and async functions, and capture duration, status, and exceptions.

------------------------------------------------------------------------

# 10. Non-Goals (V3)

V3 must NOT:

-   Implement guardrail enforcement
-   Modify LLM outputs
-   Introduce policy engine
-   Introduce persistent storage
-   Add internal dashboard UI

------------------------------------------------------------------------

# 11. Strategic Positioning

V3 establishes Rastir as:

-   Framework-aware (LangChain, LangGraph, LlamaIndex, CrewAI)
-   Infrastructure-extensible (`wrap()`)
-   Guardrail-aware (request + response level)
-   Model-level guardrail analytics capable
-   **Error-resilient** (two-phase enrichment)
-   Enterprise-observable
-   Still lightweight and stateless

End of V3 Requirements (Refined v3).
