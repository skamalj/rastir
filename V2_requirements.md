# Rastir --- V2 Requirements

## 1. Objective

Define enhancements for Rastir V2 focused on:

-   Metric stability and query flexibility
-   Histogram bucket governance
-   Error normalization
-   Span type standardization
-   Observability maturity (SLO readiness)
-   Optional advanced features (sampling, exemplars)

V2 must remain backward compatible with V1 metric names and labels.

------------------------------------------------------------------------

# 2. Histogram Bucket Governance

## 2.1 Default Buckets

Rastir must ship with sensible default histogram buckets optimized for
LLM workloads.

Example (LLM latency):

\[0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60\]

Buckets must: - Provide finer resolution at low latency - Provide
coarser resolution at high latency - Cover realistic AI workload
durations

------------------------------------------------------------------------

## 2.2 User Configuration

Users must be able to override histogram buckets via:

-   YAML configuration
-   Environment variables

Constraints:

-   Buckets configurable only at server startup
-   Buckets applied globally (not per metric/service)
-   Maximum bucket count capped (recommended: ≤ 20)
-   Buckets immutable at runtime

------------------------------------------------------------------------

## 2.3 Query-Time Aggregation Clarification

Prometheus histograms allow users to:

-   Aggregate to larger logical buckets at query time
-   Compute p95/p99 using histogram_quantile()
-   Define SLA thresholds using existing buckets

Users cannot create finer granularity than configured buckets.

------------------------------------------------------------------------

# 3. Span Type Standardization

`span_type` must be a strict enum:

-   agent
-   llm
-   tool
-   retrieval
-   system

No free-form span types allowed.

------------------------------------------------------------------------

# 4. Error Type Normalization

`error_type` must be standardized and bounded.

Allowed values:

-   timeout
-   rate_limit
-   validation_error
-   provider_error
-   internal_error
-   unknown

Raw exception class names must not be used directly as labels.

------------------------------------------------------------------------

# 5. Metric Stability Guarantees

-   Metric names must remain stable across minor versions
-   Label schema must not change after V2 release
-   Bucket changes require major version bump
-   Backward compatibility with V1 dashboards required

------------------------------------------------------------------------

# 6. Sampling Strategy (Optional Feature)

V2 may introduce configurable sampling:

-   Head-based sampling
-   Error spans always retained
-   High-volume success spans sampled

Sampling must not affect error visibility.

------------------------------------------------------------------------

# 7. Exemplars (Optional Enhancement)

Support attaching `trace_id` as Prometheus exemplars to:

-   rastir_duration_seconds
-   rastir_llm_calls_total

This enables metric → trace linking in Grafana.

Exemplar support must be optional and guarded by configuration.

------------------------------------------------------------------------

# 8. SLO Readiness

V2 must support PromQL-based SLIs such as:

-   p95 LLM latency
-   Error rate percentage
-   Queue saturation percentage
-   Export failure rate

No server-side percentile computation required --- rely on Prometheus.

------------------------------------------------------------------------

# 9. Cardinality Governance Refinement

Refine cardinality caps:

-   model ≤ 50
-   provider ≤ 10
-   tool_name ≤ 200
-   agent ≤ 200
-   error_type ≤ 50

Overflow replacement value:

**cardinality_overflow**

All label values truncated to max_label_value_length (default: 128
chars).

------------------------------------------------------------------------

# 10. Non-Goals for V2

-   No internal dashboard UI
-   No database introduction
-   No runtime bucket mutation
-   No dynamic label creation
-   No schema-breaking metric renames

------------------------------------------------------------------------

# 11. Summary

Rastir V2 strengthens:

-   Metric schema discipline
-   Histogram governance
-   Error normalization
-   Query-time flexibility
-   SLO compatibility
-   Production-grade stability

While preserving the simplicity and stateless design of V1.

End of V2 Requirements.
