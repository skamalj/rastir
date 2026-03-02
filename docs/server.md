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
| `rastir_tool_calls_total` | Counter | service, env, tool_name, agent, model, provider |
| `rastir_retrieval_calls_total` | Counter | service, env, agent |
| `rastir_errors_total` | Counter | service, env, span_type, error_type |
| `rastir_duration_seconds` | Histogram | service, env, span_type |
| `rastir_tokens_per_call` | Histogram | service, env, model, provider |

### Cost & TTFT Metrics

| Metric | Type | Labels |
|--------|------|--------|
| `rastir_cost_total` | Counter | service, env, model, provider, agent, pricing_profile |
| `rastir_cost_per_call_usd` | Histogram | service, env, model |
| `rastir_pricing_missing_total` | Counter | service, env, model, provider |
| `rastir_ttft_seconds` | Histogram | service, env, model, provider |

Cost metrics are only recorded when the client sends `cost_usd` as a span attribute (requires `enable_cost_calculation=True` on the client). TTFT metrics are only recorded for streaming LLM spans that include `ttft_ms`.

The `pricing_profile` label on `rastir_cost_total` is **cardinality-guarded** with a cap of 20 distinct values. The `rastir_cost_per_call_usd` histogram intentionally excludes `pricing_profile` to prevent cardinality explosion.

**Default buckets:**

| Histogram | Buckets |
|-----------|---------|
| `rastir_cost_per_call_usd` | 0.0001, 0.0005, 0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100 |
| `rastir_ttft_seconds` | 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10 |

### Guardrail Metrics

| Metric | Type | Labels |
|--------|------|--------|
| `rastir_guardrail_requests_total` | Counter | service, env, provider, model, agent, guardrail_id, guardrail_version |
| `rastir_guardrail_violations_total` | Counter | service, env, provider, model, agent, guardrail_id, guardrail_action, guardrail_category |

Guardrail labels are **cardinality-guarded** with bounded enum validation:
- `guardrail_category` must be one of: `CONTENT_POLICY`, `SENSITIVE_INFORMATION_POLICY`, `WORD_POLICY`, `TOPIC_POLICY`, `CONTEXTUAL_GROUNDING_POLICY`, `DENIED_TOPIC`
- `guardrail_action` must be one of: `GUARDRAIL_INTERVENED`, `NONE`
- `guardrail_id` is subject to the standard cardinality cap (default: 100)
- Unknown values are replaced with `__cardinality_overflow__`

This defence-in-depth validation runs on **both** the client adapter and the server, preventing label explosion from malformed or injected span data.

### Error Type Normalisation

The `rastir_errors_total` counter uses normalised error categories instead of raw exception class names. This prevents unbounded label cardinality from arbitrary exception types.

| Normalised category | Matched exception patterns |
|---------------------|---------------------------|
| `timeout` | `TimeoutError`, `asyncio.TimeoutError`, `httpx.TimeoutException`, `httpx.ReadTimeout`, `httpx.ConnectTimeout`, `openai.APITimeoutError` |
| `rate_limit` | `RateLimitError`, `openai.RateLimitError`, `anthropic.RateLimitError` |
| `validation_error` | `ValueError`, `TypeError`, `ValidationError`, `pydantic.ValidationError` |
| `provider_error` | `openai.APIError`, `openai.APIConnectionError`, `anthropic.APIError`, `botocore.exceptions.ClientError` |
| `internal_error` | `RuntimeError`, `Exception` |
| `unknown` | Any unrecognised exception type |

Normalisation uses exact match first, then substring heuristics (e.g., any exception with "timeout" in the name maps to `timeout`).

### Histogram Buckets

Histograms track the **distribution** of values, not just averages. Rastir ships two histograms with LLM-optimised default buckets:

| Histogram | Default Buckets | Unit |
|-----------|----------------|------|
| `rastir_duration_seconds` | 0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0 | seconds |
| `rastir_tokens_per_call` | 10, 50, 100, 250, 500, 1000, 2000, 4000, 8000, 16000, 32000 | tokens |

Maximum of **20 buckets** per histogram. Buckets are configurable via YAML or environment variables.

#### Custom Bucket Configuration

```yaml
# server-config.yml
histograms:
  duration_buckets: [0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0]
  tokens_buckets: [50, 100, 500, 1000, 5000, 10000]
```

Or via environment variables (comma-separated):

```bash
export RASTIR_SERVER_HISTOGRAMS_DURATION_BUCKETS=0.1,0.5,1.0,2.0,5.0,10.0,30.0
export RASTIR_SERVER_HISTOGRAMS_TOKENS_BUCKETS=50,100,500,1000,5000,10000
```

#### What Prometheus Exposes

For each histogram, Prometheus creates:

```
# rastir_duration_seconds_bucket{..., le="0.25"} → count of spans ≤ 0.25s
# rastir_duration_seconds_bucket{..., le="1.0"}  → count of spans ≤ 1.0s
# rastir_duration_seconds_bucket{..., le="+Inf"} → total count
# rastir_duration_seconds_sum{...}               → sum of all values
# rastir_duration_seconds_count{...}             → same as +Inf bucket
```

### Percentiles — P50, P95, P99 with PromQL

Histograms enable **percentile calculations** via PromQL's `histogram_quantile()` function. These give you tail-latency and token-usage insights.

#### LLM Latency Percentiles

```promql
# P50 (median) LLM call duration
histogram_quantile(0.50,
  rate(rastir_duration_seconds_bucket{span_type="llm"}[5m])
)

# P95 LLM call duration — 95% of calls complete within this time
histogram_quantile(0.95,
  rate(rastir_duration_seconds_bucket{span_type="llm"}[5m])
)

# P99 LLM call duration — tail latency
histogram_quantile(0.99,
  rate(rastir_duration_seconds_bucket{span_type="llm"}[5m])
)
```

#### Per-Model Latency Percentiles

Use `sum by` to break down by model:

```promql
# P95 duration per model
histogram_quantile(0.95,
  sum by (model, le) (
    rate(rastir_duration_seconds_bucket{span_type="llm"}[5m])
  )
)
```

#### Token Usage Percentiles

```promql
# P50 tokens per LLM call
histogram_quantile(0.50,
  rate(rastir_tokens_per_call_bucket[5m])
)

# P95 tokens per call — catches outlier prompts
histogram_quantile(0.95,
  rate(rastir_tokens_per_call_bucket[5m])
)

# P99 tokens per call by model
histogram_quantile(0.99,
  sum by (model, le) (
    rate(rastir_tokens_per_call_bucket[5m])
  )
)
```

#### Agent & Tool Latency

```promql
# P95 tool execution time — detect slow tools
histogram_quantile(0.95,
  rate(rastir_duration_seconds_bucket{span_type="tool"}[5m])
)

# P95 agent end-to-end time — full loop duration
histogram_quantile(0.95,
  rate(rastir_duration_seconds_bucket{span_type="agent"}[5m])
)

# P95 retrieval latency — vector DB performance
histogram_quantile(0.95,
  rate(rastir_duration_seconds_bucket{span_type="retrieval"}[5m])
)
```

#### Average & Throughput Queries

```promql
# Average LLM call duration
rate(rastir_duration_seconds_sum{span_type="llm"}[5m])
  /
rate(rastir_duration_seconds_count{span_type="llm"}[5m])

# LLM calls per second
rate(rastir_llm_calls_total[5m])

# Error rate as a percentage
rate(rastir_errors_total[5m])
  /
rate(rastir_spans_ingested_total[5m]) * 100

# Average tokens per call
rate(rastir_tokens_per_call_sum[5m])
  /
rate(rastir_tokens_per_call_count[5m])
```

#### Grafana Dashboard Panel Examples

Create a Grafana dashboard with these panels:

| Panel | Type | PromQL |
|-------|------|--------|
| LLM Latency P50/P95/P99 | Time series | `histogram_quantile(0.50\|0.95\|0.99, rate(rastir_duration_seconds_bucket{span_type="llm"}[5m]))` |
| Token Usage Distribution | Heatmap | `rastir_tokens_per_call_bucket` |
| Calls per Second | Time series | `rate(rastir_llm_calls_total[5m])` |
| Error Rate % | Stat | `rate(rastir_errors_total[5m]) / rate(rastir_spans_ingested_total[5m]) * 100` |
| Token Cost by Model | Time series | `rate(rastir_tokens_input_total[5m])` + `rate(rastir_tokens_output_total[5m])` |

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

Exemplars attach a **trace_id** to histogram observations and counter increments, creating a direct link from a Prometheus metric to the distributed trace that produced it. This lets you click from a latency spike in Grafana directly to the trace in Jaeger/Tempo.

### How It Works

When exemplars are enabled:
1. Every `rastir_duration_seconds.observe()` call includes `{trace_id: "abc-123"}` as an exemplar
2. Every `rastir_llm_calls_total.inc()` call includes the same exemplar
3. The `/metrics` endpoint automatically switches to **OpenMetrics** format (required for exemplars)

### Enabling Exemplars

```yaml
# server-config.yml
exemplars:
  enabled: true
```

Or via environment variable:

```bash
export RASTIR_SERVER_EXEMPLARS_ENABLED=true
```

### What the /metrics Output Looks Like

**Without exemplars** (classic Prometheus format):

```
rastir_duration_seconds_bucket{service="my-app",env="prod",span_type="llm",le="1.0"} 42
rastir_duration_seconds_bucket{service="my-app",env="prod",span_type="llm",le="2.0"} 58
```

**With exemplars** (OpenMetrics format):

```
rastir_duration_seconds_bucket{service="my-app",env="prod",span_type="llm",le="1.0"} 42 # {trace_id="a1b2c3d4-e5f6-7890"} 0.847 1709042400.0
rastir_duration_seconds_bucket{service="my-app",env="prod",span_type="llm",le="2.0"} 58 # {trace_id="f9e8d7c6-b5a4-3210"} 1.923 1709042401.0
rastir_llm_calls_total{service="my-app",env="prod",model="gpt-4",provider="openai",agent="qa_bot"} 145 # {trace_id="a1b2c3d4-e5f6-7890"} 1.0 1709042400.0
```

The exemplar fields are: `# {trace_id="..."} <observed_value> <timestamp>`

### Grafana + Exemplars Setup

#### 1. Configure Prometheus Data Source

In Grafana, edit your Prometheus data source:
- **Type:** Prometheus
- Enable **"Exemplars"** toggle
- Set **"Internal link"** → your Jaeger/Tempo data source
- Map label **`trace_id`** → trace ID field

#### 2. Create a Panel with Exemplars

```promql
# P95 LLM latency — exemplars shown as diamonds on the graph
histogram_quantile(0.95,
  rate(rastir_duration_seconds_bucket{span_type="llm"}[5m])
)
```

In the panel options:
- Toggle **"Exemplars"** on in the query editor
- Exemplars appear as **diamond markers** on the time series
- Click a diamond → follows the internal link to the trace in Jaeger/Tempo

#### 3. Typical Workflow

```
Grafana: P95 latency spike at 14:32 →
  Click exemplar diamond →
    Jaeger: trace_id=a1b2c3d4 →
      research_agent (2.3s)
        ├─ plan_step (0.8s)        ✓ normal
        ├─ web_search (1.2s)       ← slow! (timeout?)
        └─ synthesize (0.3s)       ✓ normal
```

This bridges the gap between **metrics** (what's happening at scale) and **traces** (why a specific request was slow).

### Metrics That Carry Exemplars

| Metric | Exemplar Label |
|--------|---------------|
| `rastir_duration_seconds` | `trace_id` |
| `rastir_llm_calls_total` | `trace_id` |

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
