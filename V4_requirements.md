# Rastir --- V4 Requirements (Evaluation + Redaction Architecture)

## 1. Objective

V4 introduces asynchronous evaluation at @llm points and
server-side trace redaction while maintaining:

-   Non-blocking ingestion
-   Horizontal scalability
-   Clean separation of concerns
-   Prometheus + Tempo + Grafana compatibility

Redaction is a standalone server-side telemetry sanitation stage,
completely independent of evaluation logic.

------------------------------------------------------------------------

# 2. High-Level Pipeline

Server-side processing order:

1.  Span ingestion
2.  Metric derivation (ALWAYS)
3.  Sampling decision
4.  Trace Redaction (if enabled, only for sampled spans)
5.  Store / Export span
6.  Evaluation enqueue (if enabled + sampled)
7.  Evaluation worker execution
8.  Emit evaluation span

Redaction NEVER runs inside the evaluation worker.

------------------------------------------------------------------------

# 3. Trace Redaction Requirements

## 3.1 Scope

Redaction applies ONLY to:

-   prompt_text
-   completion_text

Redaction does NOT apply to:

-   metric labels
-   model/provider metadata
-   token counts
-   trace identifiers
-   span IDs

## 3.2 Execution Order

Redaction must occur:

-   After sampling
-   Before span storage
-   Before OTEL export
-   Before evaluation enqueue

If span is not sampled → no redaction is performed.

## 3.3 Redactor Interface

    class Redactor(Protocol):
        def redact(self, text: str, context: RedactionContext) -> str

Redaction must be:

-   Synchronous
-   CPU-bound
-   Deterministic
-   Side-effect free

## 3.4 RedactionContext

    @dataclass
    class RedactionContext:
        service: str
        env: str
        model: str | None
        provider: str | None

## 3.5 Default Implementation

Ship RegexRedactor with:

-   Email masking
-   Phone masking
-   SSN masking
-   Credit card masking
-   Custom regex support

## 3.6 Payload Guard

Redactor must enforce:

-   max_text_length

If exceeded:

-   Truncate text
-   Append "[TRUNCATED]"

## 3.7 Failure Handling

If redaction fails:

-   Increment rastir_redaction_failures_total
-   Log error
-   Drop span (safe default)

Raw text must never pass silently after redaction failure.

------------------------------------------------------------------------

# 4. Client-Side Requirements

## 4.1 @llm Decorator Extension

The decorator must support:

    @llm(evaluate=True)

Optional parameters:

-   evaluation_types: list[str]
-   evaluation_sample_rate (float, 0.0--1.0)
-   evaluation_timeout_ms (int)

Decorator behavior:

-   Must NOT run evaluation locally.
-   Must embed evaluation configuration into span attributes.
-   Must attach trace_id and span_id normally.
-   Must not increase main call latency.

------------------------------------------------------------------------

# 5. Span Schema Extension

For LLM spans with evaluation enabled, include attributes:

-   evaluation_enabled: bool
-   evaluation_types: list[str]
-   evaluation_sample_rate: float
-   evaluation_timeout_ms: int (optional)
-   prompt_text: str (optional, for evaluation)
-   completion_text: str (optional, for evaluation)

These attributes are used by the server to schedule evaluation.
prompt_text and completion_text are captured only when evaluate=True.

------------------------------------------------------------------------

# 6. Server-Side Evaluation Pipeline

## 6.1 Ingestion Phase

When ingesting span (per pipeline order in §2):

1.  Derive metrics (ALWAYS, regardless of sampling)
2.  Apply sampling decision
3.  If sampled: run redaction on prompt_text/completion_text
4.  If sampled: store/export span
5.  If evaluation_enabled + evaluation sampled: enqueue evaluation task

Evaluation enqueue must be O(1).
Evaluation tasks receive already-redacted text.

------------------------------------------------------------------------

## 6.2 Evaluation Task Schema

Evaluation task must contain:

-   trace_id
-   parent_span_id
-   service
-   env
-   model
-   provider
-   agent
-   prompt_text (already redacted if redaction enabled)
-   completion_text (already redacted if redaction enabled)
-   evaluation_types
-   timeout_ms

Evaluation tasks consume already-sanitized span data.

------------------------------------------------------------------------

## 6.3 Evaluation Queue

Requirements:

-   Separate bounded queue
-   Configurable max size
-   Drop policy when full (drop_new or drop_oldest)
-   Dedicated metrics for: rastir_evaluation_queue_size
    rastir_evaluation_dropped_total

Evaluation queue must not share ingestion queue.

------------------------------------------------------------------------

## 6.4 Evaluation Workers

-   ThreadPoolExecutor-based worker pool
-   Configurable concurrency
-   Timeout enforcement per evaluation type
-   Failure isolation (evaluation error must not affect ingestion)

Workers must emit:

-   evaluation spans (via internal enqueue into ingestion pipeline)
-   evaluation metrics

------------------------------------------------------------------------

# 7. Evaluator Interface

## 7.1 Protocol

    class Evaluator(Protocol):
        name: str
        def evaluate(self, task: EvaluationTask) -> EvaluationResult

Evaluation must be:

-   Pluggable (registry pattern)
-   Backend-agnostic
-   Synchronous in V4 (runs in ThreadPoolExecutor)

## 7.2 Evaluation Types

-   Extensible registry with cardinality cap (max 20 types)
-   Built-in defaults: toxicity, hallucination
-   Faithfulness (RAG-dependent) deferred to V5

------------------------------------------------------------------------

# 8. Trace Correlation Requirements

Evaluation spans must:

-   Use SAME trace_id as original LLM span
-   Use parent_span_id of original LLM span
-   Generate new span_id
-   span_type = "evaluation"

Result:

Trace graph:

    LLM Span (S1)
        └── Evaluation Span (S2)

This must work even if evaluation completes seconds later.

Tempo must reconstruct full trace correctly.

------------------------------------------------------------------------

# 9. Evaluation Metrics

Introduce:

    rastir_evaluation_runs_total
    rastir_evaluation_failures_total
    rastir_evaluation_latency_seconds
    rastir_evaluation_score (gauge)

Labels:

-   service
-   env
-   model
-   provider
-   evaluation_type

High-cardinality labels (trace_id, input text, output text) are strictly
forbidden.

------------------------------------------------------------------------

# 10. Sampling Strategy

Sampling order:

1.  Ingestion sampling (existing)
2.  If sampled → redaction
3.  If evaluation enabled → evaluation sampling
4.  If evaluation sampled → enqueue task

Support:

-   Global evaluation_sample_rate
-   Per-@llm override

Evaluation sampling must not affect trace sampling.
Sampling must occur before enqueue.

------------------------------------------------------------------------

# 11. Backpressure Safety

If evaluation queue is full:

-   Do NOT block ingestion
-   Increment rastir_evaluation_dropped_total
-   Log warning
-   Continue normal processing

Evaluation must never affect main ingestion latency.

------------------------------------------------------------------------

# 12. Redaction Metrics

Introduce:

    rastir_redaction_failures_total
    rastir_redaction_applied_total

Labels:

-   service
-   env

------------------------------------------------------------------------

# 13. Security & Data Handling

Evaluation payload must support:

-   Redaction configuration (server-side, see §3)
-   Disable input/output forwarding
-   Size limits on payload (max_text_length guard, see §3.6)
-   PII-safe mode

Evaluation must be optional per deployment.

------------------------------------------------------------------------

# 14. Scaling Model

Evaluation workers must scale independently of ingestion:

Deployment options:

-   Same process worker pool
-   Sidecar evaluation worker
-   Separate evaluation service (future)

V4 scope: same-process worker pool only.
Sidecar and distributed modes deferred to V5.

Evaluation must not increase ingestion memory footprint excessively.

------------------------------------------------------------------------

# 15. Operational Observability

Add dashboard panels for:

-   Evaluation rate
-   Evaluation latency
-   Evaluation error rate
-   Evaluation queue utilization
-   Evaluation drop rate
-   Redaction applied rate
-   Redaction failure rate

------------------------------------------------------------------------

# 16. Deployment Scope (V4)

-   Same-process evaluation worker only
-   In-memory queue only
-   No Redis dependency
-   No external evaluation service
-   No distributed evaluation workers

Sidecar and distributed modes deferred to V5.

------------------------------------------------------------------------

# 17. Non-Goals (V4)

-   No policy enforcement engine
-   No dataset storage
-   No UI for evaluation feedback
-   No distributed trace store
-   No RAG faithfulness evaluation
-   No annotation system

------------------------------------------------------------------------

# 18. Strategic Positioning

V4 positions Rastir as:

-   AI observability layer
-   Guardrail-aware analytics engine
-   Async evaluation-capable
-   PII-safe via centralized trace redaction
-   Production-ready within Prom + Tempo + Grafana stack

End of V4 Requirements.
