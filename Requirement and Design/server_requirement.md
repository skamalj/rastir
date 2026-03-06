# Server Requirements --- LLM & Agent Observability Platform (Revised)

## 1. Objective

Define a **simple, easy-to-deploy observability server** with:

-   No external database dependencies
-   Fully in-memory storage
-   Prometheus metric derivation
-   Optional OTLP forwarding
-   Bounded memory usage
-   Horizontal scalability

The server must remain stateless and production-safe.

------------------------------------------------------------------------

# 2. Core Architecture Principles

1.  No external databases (Postgres, Redis, ClickHouse, etc.)
2.  No durable storage required
3.  Fully in-memory metric + trace handling
4.  Bounded memory with hard caps
5.  Stateless and horizontally scalable
6.  Prometheus + OTLP backends handle durability

------------------------------------------------------------------------

# 3. Ingestion API

## Endpoint

POST /v1/telemetry

Accepts structured span payloads.

Server responsibilities:

-   Validate JSON schema
-   Enforce size limits
-   Apply tenant identification
-   Push spans into ingestion queue

------------------------------------------------------------------------

# 4. Metrics Storage (In-Memory)

## 4.1 Prometheus Registry

-   Use in-process Prometheus client registry
-   Counters and Histograms stored in memory
-   Exposed via GET /metrics
-   Reset on server restart (standard Prom behavior)

No custom aggregation database required.

## 4.2 Label Injection

Metrics must inject:

-   service
-   env
-   version
-   tenant (if enabled)
-   semantic labels (model, provider, agent, tool)

Cardinality guardrails must be enforced.

------------------------------------------------------------------------

# 5. Trace Storage (In-Memory Ring Buffer)

## 5.1 Storage Model

-   Use bounded in-memory ring buffer
-   Default: max_traces = 10,000
-   Spans grouped by trace_id
-   FIFO eviction (oldest traces removed first)

## 5.2 Data Structure

trace_store: trace_id → list\[span\]

Eviction must not block ingestion.

## 5.3 Optional Trace Querying (Debug Mode)

GET /v1/traces?trace_id=...

Trace storage may be disabled by default for minimal mode.

------------------------------------------------------------------------

# 6. OTLP Export (Stateless Forwarding)

## 6.1 Strategy

Server forwards spans to OTLP backend (Jaeger/Tempo/etc.).

No local persistence required.

## 6.2 Implementation

-   Use official opentelemetry-sdk
-   BatchSpanProcessor
-   Configurable batch size
-   Configurable flush interval

## 6.3 Failure Handling

-   Retry with exponential backoff
-   Drop spans after retry budget exhausted
-   Increment export_failures_total metric

------------------------------------------------------------------------

# 7. Queues & Backpressure

## 7.1 Ingestion Queue

-   asyncio.Queue(maxsize=N)
-   Default maxsize = 50,000 spans

## 7.2 Export Queue

-   Bounded queue
-   Separate from ingestion queue

## 7.3 Rejection Policy

If ingestion queue full: - Return HTTP 429 - Increment
ingestion_rejections_total

Optional configuration: - Drop oldest OR reject new (default: reject
new)

------------------------------------------------------------------------

# 8. Multi-Tenant Isolation (No DB)

## 8.1 Tenant Identification

Tenant determined via: - JSON payload field OR - HTTP header
(configurable)

## 8.2 Isolation Strategy

-   Metrics separated by tenant label
-   Trace store partitioned by tenant namespace
-   Per-tenant memory caps (optional)

No physical database separation required.

------------------------------------------------------------------------

# 9. Server Configuration (YAML)

Example:

server: host: 0.0.0.0 port: 8080

limits: max_traces: 10000 max_queue_size: 50000 max_span_attributes: 100
max_label_value_length: 128

trace_store: enabled: true

exporter: otlp_endpoint: http://tempo:4318 batch_size: 200
flush_interval: 5

multi_tenant: enabled: true header_name: X-Tenant-ID

Environment variables override YAML.

------------------------------------------------------------------------

# 10. Health & Readiness

GET /health - Returns 200 if process running

GET /ready - Returns 200 if: - Ingestion queue below threshold -
Exporter healthy

------------------------------------------------------------------------

# 11. Startup & Shutdown Lifecycle

Startup: - Load config - Initialize metric registry - Initialize trace
ring buffer - Initialize queues - Initialize OTLP exporter - Start
worker loops

Shutdown: - Stop accepting new requests - Drain ingestion queue - Flush
OTLP exporter - Exit cleanly

------------------------------------------------------------------------

# 12. Performance Targets

-   Sustain \>10k spans/sec
-   O(1) metric updates
-   O(1) trace insertion
-   Memory bounded by configuration
-   No blocking network calls on ingestion path

------------------------------------------------------------------------

# 13. Tradeoffs (Explicitly Accepted)

Because no database is used:

-   Metrics reset on restart
-   Trace history lost on restart
-   No historical querying beyond memory window
-   No replay capability

Durability delegated to: - Prometheus - OTLP backend (Tempo/Jaeger/etc.)

------------------------------------------------------------------------

# 14. Summary

The server is:

-   Stateless
-   Database-free
-   Fully in-memory
-   Horizontally scalable
-   Easy to deploy
-   Production-safe with bounded memory

External systems handle long-term storage.

End of Revised Server Requirements.
