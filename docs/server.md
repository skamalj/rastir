---
layout: default
title: Server
nav_order: 10
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

The server derives all Prometheus metrics from ingested span data and exposes them on the `/metrics` endpoint. See the [Metrics Reference](metrics) page for the complete list of counters, histograms, gauges, cardinality guards, error normalisation rules, exemplar support, and PromQL examples.

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

## SRE — Prometheus Recording Rules

Rastir's SRE layer uses a **Prometheus-native** approach: the server exposes static config gauges (`rastir_slo_error_rate`, `rastir_cost_budget_usd`) and Prometheus recording rules compute all derived SRE metrics from raw counters.

### Architecture

```
Rastir Server                    Prometheus
┌──────────────────────┐        ┌──────────────────────────────┐
│ rastir_slo_error_rate │───────▸│ Recording Rules              │
│ rastir_cost_budget_usd│  scrape│ ├─ rastir:errors:week/month  │
│ rastir_llm_calls_total│       │ ├─ rastir:error_budget_*     │
│ rastir_errors_total   │       │ ├─ rastir:cost:week/month    │
│ rastir_cost_total     │       │ ├─ rastir:cost_budget_*      │
└──────────────────────┘        │ ├─ rastir:error_burn_rate:*  │
                                │ └─ rastir:*_days_to_exhaust* │
                                └──────────────┬───────────────┘
                                               │ query
                                               ▼
                                ┌──────────────────────────────┐
                                │ Grafana SRE Dashboard        │
                                └──────────────────────────────┘
```

### Why Recording Rules?

- **No server-side state** — no in-memory rolling accumulators or snapshot files
- **Survives server restarts** — Prometheus retains all history
- **Standard PromQL** — budget calculations are transparent and auditable
- **Alertable** — recording rules can feed Alertmanager rules directly

### Deploying the Rules

The recording rules file is at `grafana/prometheus/rastir-sre-rules.yml`. Mount it into Prometheus:

```yaml
# docker-compose.yml (excerpt)
prometheus:
  volumes:
    - ./prometheus/rastir-sre-rules.yml:/etc/prometheus/rules/rastir-sre-rules.yaml
```

Ensure `prometheus.yml` includes the rules directory:

```yaml
rule_files:
  - /etc/prometheus/rules/*.yaml
```

After deploying, verify rules are loaded: `http://localhost:9090/rules`

### Rule Groups

| Group | Interval | Description |
|-------|----------|-------------|
| `rastir_sre_weekly` | 15s | 7-day rolling error/cost budgets, volume, exhaustion |
| `rastir_sre_monthly` | 15s | 30-day rolling error/cost budgets, volume, exhaustion |
| `rastir_sre_burn_rates` | 15s | 1h and 6h error burn rate windows |

### Month-Boundary Scaling

All period-based rules use `day_of_month()` scaling to produce correct values early in the month. For example, on March 3 with a 7-day window:

```
increase(counter[7d]) × clamp_max(day_of_month / 7, 1)
```

This ensures that at the start of a new month (day 1–2), the `increase(7d)` value (which spans into the previous month) is scaled down proportionally.

### Server Configuration

Enable SRE in `rastir-server-config.yaml`:

```yaml
sre:
  enabled: true
  default_slo_error_rate: 0.01    # 1% error budget
  default_cost_budget_usd: 25.0   # $25/period
  agents:
    my_agent:
      slo_error_rate: 0.02        # agent-specific override
```

See [Configuration — SRE](configuration#sre) for all options.

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

---

## Capacity & Performance

The collector is a single-process async FastAPI application. The push endpoint (`POST /v1/telemetry`) is the hot path — it validates the payload, enqueues the span batch, and returns `202`. All heavy work (metrics derivation, trace storage, OTLP export) happens asynchronously in a background worker.

### Benchmark Results (single uvicorn worker)

Tested with 10 spans per request (typical for one agent invocation):

| Push Rate | p50 Latency | p95 Latency | p99 Latency | Spans/min |
|----------:|------------:|------------:|------------:|----------:|
| 100 req/s | 1.3 ms | 3.4 ms | 6 ms | 60,000 |
| 500 req/s | 0.6 ms | 1.9 ms | 3.9 ms | 300,000 |
| 1,000 req/s | 5.9 ms | 45 ms | 523 ms | 600,000 |
| 2,000 req/s | 191 ms | 344 ms | 19 s | 1,200,000 |

The practical ceiling on a single worker is **~1,000 requests/sec** (600,000 spans/min). Beyond that, latency degrades significantly.

### Sizing Recommendations

| Setup | Comfortable Push Rate | Concurrent Agents (1 call/min) |
|-------|----------------------:|-------------------------------:|
| 1 vCPU / 512 MB | 500 req/s | ~5,000 |
| 1 vCPU / 1 GB | 500 req/s | ~8,000 |
| 2 vCPU / 2 GB | 1,000 req/s (2 workers) | ~20,000 |
| 4 vCPU / 4 GB | 2,000 req/s (4 workers) | ~50,000 |

**Scaling tips:**
- Run multiple uvicorn workers (`--workers N`) to scale linearly with CPU cores
- The ingestion queue (default 50,000) absorbs traffic bursts — tune `max_queue_size` for spike-heavy workloads
- Rate limiting (`per_ip_rpm`, `per_service_rpm`) protects against runaway clients
- Memory is driven by queue depth and trace store retention — each queued span batch is ~1–5 KB
