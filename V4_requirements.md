# Rastir --- V4 Requirements (Extended with Async Evaluation Design)

## 1. Objective

V4 focuses on operational maturity and production readiness.

Additionally, V4 introduces **optional asynchronous evaluation triggered
at @llm points**, without increasing main request latency.

Rastir must remain:

-   Non-blocking
-   Horizontally scalable
-   Compatible with Prometheus + Tempo + Grafana

------------------------------------------------------------------------

# 2. Async Evaluation at @llm Points

## 2.1 Design Goals

-   Evaluation must NOT block or delay the original LLM call.
-   Evaluation must run asynchronously via server-side workers.
-   Evaluation must preserve full trace linkage.
-   Evaluation must emit metrics and spans.
-   Evaluation must be optional and configurable.
-   Evaluation must be sampling-aware.
-   Evaluation must be bounded and backpressure-safe.

------------------------------------------------------------------------

# 3. Client-Side Requirements

## 3.1 @llm Decorator Extension

The decorator must support:

    @llm(evaluate=True)

Or:

    @llm(evaluate={
        "toxicity": True,
        "hallucination": True,
        "faithfulness": True
    })

Optional parameters:

-   evaluation_sample_rate (float, 0.0--1.0)
-   evaluation_timeout_ms (int)

Decorator behavior:

-   Must NOT run evaluation locally.
-   Must embed evaluation configuration into span attributes.
-   Must attach trace_id and span_id normally.
-   Must not increase main call latency.

------------------------------------------------------------------------

# 4. Span Schema Extension

For LLM spans with evaluation enabled, include attributes:

-   evaluation_enabled: bool
-   evaluation_types: list\[str\]
-   evaluation_sample_rate: float
-   evaluation_timeout_ms: int (optional)

These attributes are used by the server to schedule evaluation.

------------------------------------------------------------------------

# 5. Server-Side Evaluation Pipeline

## 5.1 Ingestion Phase

When ingesting span:

IF: - span_type == "llm" - evaluation_enabled == True

THEN: - Apply sampling decision - Enqueue evaluation task - Continue
normal metric derivation

Evaluation enqueue must be O(1).

------------------------------------------------------------------------

## 5.2 Evaluation Task Schema

Evaluation task must contain:

-   trace_id
-   parent_span_id
-   service
-   env
-   model
-   provider
-   agent
-   input (optional, configurable)
-   output (optional, configurable)
-   evaluation_types

Sensitive data handling must be configurable (allow redaction).

------------------------------------------------------------------------

## 5.3 Evaluation Queue

Requirements:

-   Separate bounded queue
-   Configurable max size
-   Drop policy when full (drop_new or drop_oldest)
-   Dedicated metrics for: rastir_evaluation_queue_size
    rastir_evaluation_dropped_total

Evaluation queue must not share ingestion queue.

------------------------------------------------------------------------

## 5.4 Evaluation Workers

-   Separate worker thread/process pool
-   Configurable concurrency
-   Timeout enforcement per task
-   Failure isolation (evaluation error must not affect ingestion)

Workers must emit:

-   evaluation spans
-   evaluation metrics

------------------------------------------------------------------------

# 6. Trace Correlation Requirements

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

# 7. Evaluation Metrics

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

# 8. Sampling Strategy

Support:

-   Global evaluation_sample_rate
-   Per-@llm override
-   Deterministic sampling based on trace_id hash (optional future)

Sampling must occur before enqueue.

------------------------------------------------------------------------

# 9. Backpressure Safety

If evaluation queue is full:

-   Do NOT block ingestion
-   Increment rastir_evaluation_dropped_total
-   Log warning
-   Continue normal processing

Evaluation must never affect main ingestion latency.

------------------------------------------------------------------------

# 10. Security & Data Handling

Evaluation payload must support:

-   Redaction configuration
-   Disable input/output forwarding
-   Size limits on payload
-   PII-safe mode

Evaluation must be optional per deployment.

------------------------------------------------------------------------

# 11. Scaling Model

Evaluation workers must scale independently of ingestion:

Deployment options:

-   Same process worker pool
-   Sidecar evaluation worker
-   Separate evaluation service (future)

Evaluation must not increase ingestion memory footprint excessively.

------------------------------------------------------------------------

# 12. Operational Observability

Add dashboard panels for:

-   Evaluation rate
-   Evaluation latency
-   Evaluation error rate
-   Evaluation queue utilization
-   Evaluation drop rate

------------------------------------------------------------------------

# 13. Non-Goals

Evaluation system must NOT:

-   Replace offline dataset evaluation tools
-   Store historical prompt-response datasets
-   Implement annotation UI
-   Perform policy enforcement (guardrails remain separate)
-   Block user response

------------------------------------------------------------------------

# 14. Strategic Positioning

Async evaluation positions Rastir as:

-   AI observability + AI quality monitoring layer
-   Fully async
-   Trace-correlated
-   Production-safe

End of Extended V4 Requirements.
