# Rastir --- V3 Requirements (Refined v2)

## 1. Objective

V3 expands Rastir into ecosystem maturity and guardrail-aware
observability while preserving stateless server design and
backward-compatible metrics.

V3 introduces:

-   Expanded provider & framework adapters
-   Generic infrastructure object wrapper
-   Bedrock guardrail observability (request + response level)
-   Structured guardrail metrics with model-level visibility

No enforcement logic is included in V3.

------------------------------------------------------------------------

# 2. Ecosystem Expansion

## 2.1 Provider Adapters (Extended)

Add support for:

-   Azure OpenAI
-   Google Gemini
-   Cohere
-   Mistral (API)
-   Groq (optional)

All provider adapters must:

-   Normalize model identifier
-   Extract input/output token usage
-   Extract latency
-   Support streaming
-   Extract provider-native metadata (if available)

------------------------------------------------------------------------

## 2.2 Framework Adapters (Extended)

Add support for:

-   LlamaIndex
-   CrewAI

Framework integration must:

-   Wrap execution entrypoints only (invoke/run/stream)
-   Avoid patching internal classes
-   Preserve span hierarchy

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

# 4. Bedrock Guardrail Observability (Metadata-Only)

V3 introduces structured observability for Bedrock guardrails.

This includes BOTH:

1.  Guardrail configured (request-level)
2.  Guardrail intervened (response-level)

No enforcement, blocking, or policy engine is included.

------------------------------------------------------------------------

## 4.1 Request-Level Guardrail Detection

When request contains guardrail configuration (e.g.,
guardrailIdentifier, guardrailVersion):

Decorator must:

-   Annotate span with: guardrail.id guardrail.version guardrail.enabled
    = true

Emit metric:

    rastir_guardrail_requests_total

Labels:

-   service
-   env
-   provider
-   guardrail_id
-   guardrail_version

This measures guardrail adoption.

------------------------------------------------------------------------

## 4.2 Response-Level Guardrail Intervention Detection

When response metadata indicates guardrail action (e.g., action !=
NONE):

Adapter must extract structured metadata only (no text inspection):

-   guardrail_action (e.g., GUARDRAIL_INTERVENED)
-   guardrail_category (from structured assessments)
-   guardrail_reason (optional, not used as metric label)

Decorator must:

-   Annotate span with: guardrail.triggered = true guardrail.action
    guardrail.category (bounded)

Emit metric:

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

## 4.3 Guardrail Cardinality Controls

To prevent explosion:

-   guardrail_id capped (e.g., 100 distinct values)
-   guardrail_category must be bounded enum
-   model label subject to global model cardinality cap
-   No free-text labels allowed
-   actionReason never used as metric label

Overflow must map to:

    __cardinality_overflow__

------------------------------------------------------------------------

## 4.4 Span Status Rules

Guardrail intervention does NOT mark span as ERROR.

Span status remains OK unless provider returns actual invocation error.

------------------------------------------------------------------------

# 5. Streaming Guardrail Handling

For streaming APIs:

-   Guardrail configuration must be detected at request time
-   Guardrail intervention must be extracted from final structured
    metadata
-   Metrics emitted after stream completion
-   Partial metadata accumulation supported

------------------------------------------------------------------------

# 6. Adapter Capability Registry

Each adapter must declare capability flags:

-   supports_tokens
-   supports_streaming
-   supports_guardrail_metadata
-   supports_request_guardrail_detection

Decorator must not assume capabilities not declared.

------------------------------------------------------------------------

# 7. Integration Test Requirements

V3 must include integration tests for:

-   LangGraph + MemorySaver (wrapped)
-   LangGraph + DynamoDB checkpointer (wrapped)
-   Bedrock LLM with guardrails configured but not triggered
-   Bedrock LLM with guardrails triggered
-   Streaming Bedrock guardrail response

Tests must validate:

-   Correct span annotations
-   Correct metric emission
-   Model label presence on violations metric
-   No cardinality explosion
-   Proper separation of request-level vs response-level metrics

------------------------------------------------------------------------

# 8. Non-Goals (V3)

V3 must NOT:

-   Implement guardrail enforcement
-   Modify LLM outputs
-   Introduce policy engine
-   Introduce persistent storage
-   Add internal dashboard UI

------------------------------------------------------------------------

# 9. Strategic Positioning

V3 establishes Rastir as:

-   Framework-aware
-   Infrastructure-extensible
-   Guardrail-aware (request + response level)
-   Model-level guardrail analytics capable
-   Enterprise-observable
-   Still lightweight and stateless

End of V3 Requirements (Refined v2).
