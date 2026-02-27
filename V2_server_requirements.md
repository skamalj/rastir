# Rastir --- Server V2 Requirements

## 1. Objective

Enhance the Rastir server with operational maturity, safety controls,
and advanced observability features while preserving:

-   Stateless architecture
-   No external database dependencies
-   Bounded memory guarantees
-   Horizontal scalability

V2 must remain backward compatible with V1 ingestion schema and metric
names.

------------------------------------------------------------------------

# 2. Exemplar Support

## 2.1 Purpose

Enable metric → trace correlation in Grafana via Prometheus exemplars.

## 2.2 Requirements

-   Attach `trace_id` as exemplar to:
    -   rastir_duration_seconds
    -   rastir_llm_calls_total
-   Controlled via configuration flag (disabled by default)
-   Must not significantly impact performance

------------------------------------------------------------------------

# 3. Span Sampling Controls

## 3.1 Sampling Strategy

Introduce configurable trace sampling:

-   Head-based sampling (percentage)
-   Always retain error spans
-   Always retain spans above latency threshold (configurable)

## 3.2 Metric Integrity

Sampling must:

-   NOT affect metric counters
-   NOT affect histogram aggregation
-   Only affect trace storage/export

------------------------------------------------------------------------

# 4. Advanced Backpressure Controls

## 4.1 Queue Thresholds

-   Soft limit warning threshold
-   Hard limit rejection threshold
-   Optional drop-oldest mode
-   Default mode: reject new spans

## 4.2 Additional Metrics

Expose:

-   rastir_queue_utilization_percent (Gauge)
-   rastir_ingestion_rate (Gauge or Counter-based rate)

------------------------------------------------------------------------

# 5. Trace Retention Policies

## 5.1 Retention Controls

-   Configurable max traces per service
-   Configurable max spans per trace
-   Optional TTL-based expiration

## 5.2 Eviction Policy

-   Deterministic FIFO eviction
-   Eviction must not block ingestion

------------------------------------------------------------------------

# 6. Trace Query API (Optional Debug Mode)

Expose read-only API endpoints:

GET /v1/traces/{trace_id} GET /v1/traces?service=...&limit=...

Constraints:

-   Memory-bounded
-   Disabled by default in production mode

------------------------------------------------------------------------

# 7. Rate Limiting

Optional protection mechanisms:

-   Per-IP rate limiting
-   Per-service rate limiting
-   Configurable thresholds
-   Return HTTP 429 on violation

Must integrate with existing backpressure metrics.

------------------------------------------------------------------------

# 8. Structured Logging

Introduce structured JSON logs for:

-   Span ingestion
-   Rejections
-   Export failures
-   Queue overflow warnings

Log fields must include:

-   service
-   span_type
-   trace_id (if available)
-   error_type (if applicable)

------------------------------------------------------------------------

# 9. High Availability Readiness

## 9.1 Graceful Shutdown

-   Configurable shutdown grace period
-   Drain ingestion queue before exit
-   Flush exporter buffers

## 9.2 Readiness Health

/ready must fail if:

-   Exporter unhealthy
-   Queue utilization above critical threshold

------------------------------------------------------------------------

# 10. Configuration Validation & Safety

Server must validate at startup:

-   Bucket count limits
-   Queue size limits
-   Label length limits
-   Trace store limits

Server must refuse startup if configuration exceeds safe memory
thresholds.

------------------------------------------------------------------------

# 11. Memory Usage Telemetry

Expose new metrics:

-   rastir_memory_bytes
-   rastir_trace_store_size
-   rastir_active_traces

These must reflect real-time in-memory usage.

------------------------------------------------------------------------

# 12. Non-Goals

Server V2 must NOT:

-   Introduce a database
-   Persist traces to disk
-   Add internal dashboard UI
-   Introduce cross-instance coordination
-   Modify V1 metric names

------------------------------------------------------------------------

# 13. Summary

Server V2 enhances:

-   Trace--metric correlation
-   Operational safety
-   Backpressure robustness
-   Sampling controls
-   HA readiness
-   Memory transparency

While maintaining Rastir's stateless and dependency-free architecture.

End of Server V2 Requirements.
