---
layout: default
title: Server
nav_order: 5
---

# Collector Server

The Rastir collector server is a FastAPI application that receives span batches from client libraries, derives Prometheus metrics, stores traces in memory, and optionally forwards data via OTLP.

---

## Running the Server

```bash
# Console script (installed with pip install rastir[server])
rastir-server

# Python module
python -m rastir.server

# Docker
docker run -p 8080:8080 rastir-server
```

---

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v1/telemetry` | Ingest span batches |
| `GET` | `/metrics` | Prometheus metrics exposition |
| `GET` | `/v1/traces` | Query recent traces |
| `GET` | `/v1/traces/{trace_id}` | Get spans for a specific trace |
| `GET` | `/health` | Liveness probe |
| `GET` | `/ready` | Readiness probe |

### POST /v1/telemetry

Accepts JSON payloads with span batches:

```json
{
  "service": "my-app",
  "env": "production",
  "version": "1.0.0",
  "spans": [
    {
      "name": "ask_llm",
      "span_type": "llm",
      "trace_id": "abc-123",
      "status": "OK",
      "duration_ms": 1234.5,
      "attributes": {
        "model": "gpt-4",
        "provider": "openai",
        "tokens_input": 150,
        "tokens_output": 300
      }
    }
  ]
}
```

**Response:** `202 Accepted` with `{"status": "accepted", "spans_received": N}`

**Error responses:**
- `400` — Invalid JSON or missing `spans`
- `429` — Rate limited or queue full

### GET /v1/traces

Query parameters:
- `trace_id` — Look up a specific trace
- `service` — Filter traces by service name
- `limit` — Max results (default: 20)

### GET /v1/traces/{trace_id}

Returns all spans for a specific trace by path parameter.

### GET /ready

Returns `200` when healthy, `503` when degraded:

```json
{
  "status": "ready",
  "queue_pct": 12.5
}
```

If unhealthy:

```json
{
  "status": "not_ready",
  "queue_pct": 96.2,
  "reasons": ["queue_pct=96.2% >= hard_limit=95.0%"]
}
```

---

## Prometheus Metrics

### Span Metrics

| Metric | Type | Labels |
|--------|------|--------|
| `rastir_spans_ingested_total` | Counter | service, env, span_type, status |
| `rastir_llm_calls_total` | Counter | service, env, model, provider, agent |
| `rastir_tokens_input_total` | Counter | service, env, model, provider, agent |
| `rastir_tokens_output_total` | Counter | service, env, model, provider, agent |
| `rastir_tool_calls_total` | Counter | service, env, tool_name, agent |
| `rastir_retrieval_calls_total` | Counter | service, env, agent |
| `rastir_errors_total` | Counter | service, env, span_type, error_type |
| `rastir_duration_seconds` | Histogram | service, env, span_type |
| `rastir_tokens_per_call` | Histogram | service, env, model, provider |

### Operational Metrics

| Metric | Type | Description |
|--------|------|-------------|
| `rastir_queue_size` | Gauge | Current queue depth |
| `rastir_queue_utilization_percent` | Gauge | Queue fill percentage |
| `rastir_memory_bytes` | Gauge | Process RSS memory |
| `rastir_trace_store_size` | Gauge | Total spans in trace store |
| `rastir_active_traces` | Gauge | Distinct trace count |
| `rastir_ingestion_rate` | Gauge | Spans per second |
| `rastir_ingestion_rejections_total` | Counter | Rejected spans |
| `rastir_export_failures_total` | Counter | OTLP export failures |

### Sampling & Backpressure Metrics

| Metric | Type | Description |
|--------|------|-------------|
| `rastir_spans_sampled_total` | Counter | Spans retained after sampling |
| `rastir_spans_dropped_by_sampling_total` | Counter | Spans dropped by sampling |
| `rastir_backpressure_warnings_total` | Counter | Soft limit warnings |
| `rastir_spans_dropped_by_backpressure_total` | Counter | Spans dropped by backpressure |
| `rastir_rate_limited_total` | Counter | Rate-limited requests (by dimension) |

---

## Exemplar Support

When enabled, `trace_id` exemplars are attached to `rastir_duration_seconds` and `rastir_llm_calls_total`. The `/metrics` endpoint switches to OpenMetrics format automatically.

```yaml
# server-config.yml
exemplars:
  enabled: true
```

Enable in Grafana by configuring exemplar support on your Prometheus data source.

---

## Sampling

Control which spans are stored/exported (metrics are always recorded):

```yaml
sampling:
  enabled: true
  rate: 0.1              # Keep 10% of spans
  always_retain_errors: true
  latency_threshold_ms: 5000  # Always keep spans > 5s
```

---

## Backpressure

Configure queue-based flow control:

```yaml
backpressure:
  soft_limit_pct: 80   # Warning threshold
  hard_limit_pct: 95   # Rejection/drop threshold
  mode: reject          # "reject" or "drop_oldest"
```

---

## Rate Limiting

Optional per-IP and per-service rate limits:

```yaml
rate_limit:
  enabled: true
  per_ip_rpm: 600       # Requests per minute per IP
  per_service_rpm: 3000 # Requests per minute per service
```

---

## Multi-Tenant Mode

Inject a tenant label from HTTP headers:

```yaml
multi_tenant:
  enabled: true
  header_name: X-Tenant-ID
```

---

## OTLP Export

Forward spans to Jaeger, Tempo, or any OTLP-compatible backend:

```yaml
exporter:
  otlp_endpoint: http://jaeger:4317
  batch_size: 200
  flush_interval: 5
```

---

## Graceful Shutdown

```yaml
shutdown:
  grace_period_seconds: 30
  drain_queue: true
```

The server drains the ingestion queue and flushes exporter buffers before exiting.

---

## Structured Logging

Enable JSON-structured logs for production:

```yaml
logging:
  structured: true
  level: INFO
```

Output:

```json
{"timestamp": "2026-02-27 10:30:00", "level": "INFO", "logger": "rastir.server", "message": "Span batch ingested", "service": "my-app"}
```
