---
layout: default
title: Configuration
nav_order: 6
---

# Configuration Reference

Rastir has two configuration surfaces: the **client library** (used in your application) and the **collector server**.

---

## Client Configuration

### configure()

```python
from rastir import configure

configure(
    service="my-app",
    env="production",
    version="1.0.0",
    push_url="http://localhost:8080",
    api_key="secret",
    batch_size=100,
    flush_interval=5,
    timeout=5,
    max_retries=3,
)
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `service` | `str` | `"unknown"` | Service name (global label) |
| `env` | `str` | `"development"` | Environment name (global label) |
| `version` | `str` | `None` | Application version |
| `push_url` | `str` | `None` | Collector server URL. If unset, spans are queued locally |
| `api_key` | `str` | `None` | API key for the collector (sent as `X-API-Key` header) |
| `batch_size` | `int` | `100` | Spans per batch in the background exporter |
| `flush_interval` | `int` | `5` | Seconds between background flushes |
| `timeout` | `int` | `5` | HTTP request timeout in seconds |
| `max_retries` | `int` | `3` | Retries on transient failures (5xx, 429, connection errors) |
| `retry_backoff` | `float` | `0.5` | Initial backoff in seconds (doubles each retry) |
| `shutdown_timeout` | `float` | `5.0` | Max seconds to wait for exporter thread on shutdown |

### Environment Variables

All settings can be set via environment variables with the `RASTIR_` prefix:

| Variable | Maps to |
|----------|---------|
| `RASTIR_SERVICE` | `service` |
| `RASTIR_ENV` | `env` |
| `RASTIR_VERSION` | `version` |
| `RASTIR_PUSH_URL` | `push_url` |
| `RASTIR_API_KEY` | `api_key` |
| `RASTIR_BATCH_SIZE` | `batch_size` |
| `RASTIR_FLUSH_INTERVAL` | `flush_interval` |
| `RASTIR_TIMEOUT` | `timeout` |
| `RASTIR_MAX_RETRIES` | `max_retries` |

**Precedence:** `configure()` arguments > environment variables > defaults.

{: .warning }
> `configure()` can only be called **once** per process. After initialization, configuration is frozen and immutable. Calling it again raises `RuntimeError: rastir.configure() has already been called`. This is by design — call it at application startup before any decorated functions run.

---

## Server Configuration

The server loads configuration from three sources (in order of precedence):

1. **Environment variables** (`RASTIR_SERVER_*`)
2. **YAML config file** (path via `RASTIR_SERVER_CONFIG` env var)
3. **Defaults**

### YAML Config File

```yaml
# rastir-server.yml

server:
  host: 0.0.0.0
  port: 8080

limits:
  max_traces: 10000
  max_queue_size: 50000
  max_span_attributes: 100
  max_label_value_length: 128
  cardinality_model: 50
  cardinality_provider: 10
  cardinality_tool_name: 200
  cardinality_agent: 200
  cardinality_error_type: 50

histograms:
  duration_buckets: [0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0]
  tokens_buckets: [10, 50, 100, 250, 500, 1000, 2000, 4000, 8000, 16000, 32000]

trace_store:
  enabled: true
  max_spans_per_trace: 500
  ttl_seconds: 0            # 0 = no expiration

exporter:
  otlp_endpoint: null       # Set to enable OTLP forwarding
  batch_size: 200
  flush_interval: 5

multi_tenant:
  enabled: false
  header_name: X-Tenant-ID

sampling:
  enabled: false
  rate: 1.0
  always_retain_errors: true
  latency_threshold_ms: 0

backpressure:
  soft_limit_pct: 80.0
  hard_limit_pct: 95.0
  mode: reject              # "reject" or "drop_oldest"

rate_limit:
  enabled: false
  per_ip_rpm: 600
  per_service_rpm: 3000

exemplars:
  enabled: false

shutdown:
  grace_period_seconds: 30
  drain_queue: true

logging:
  structured: false
  level: INFO
```

### Environment Variables

All server settings can be overridden via `RASTIR_SERVER_*` environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVER_HOST` | `0.0.0.0` | Bind address |
| `RASTIR_SERVER_PORT` | `8080` | Bind port |
| `RASTIR_SERVER_CONFIG` | — | Path to YAML config file |
| `RASTIR_SERVER_MAX_TRACES` | `10000` | Max traces in store |
| `RASTIR_SERVER_MAX_QUEUE_SIZE` | `50000` | Max ingestion queue size |
| `RASTIR_SERVER_TRACE_STORE_ENABLED` | `true` | Enable/disable trace store |
| `RASTIR_SERVER_TRACE_STORE_MAX_SPANS_PER_TRACE` | `500` | Max spans per trace |
| `RASTIR_SERVER_TRACE_STORE_TTL_SECONDS` | `0` | Trace TTL (0=disabled) |
| `RASTIR_SERVER_OTLP_ENDPOINT` | — | OTLP export endpoint |
| `RASTIR_SERVER_OTLP_BATCH_SIZE` | `200` | OTLP batch size |
| `RASTIR_SERVER_OTLP_FLUSH_INTERVAL` | `5` | OTLP flush interval (seconds) |
| `RASTIR_SERVER_MULTI_TENANT_ENABLED` | `false` | Enable multi-tenant mode |
| `RASTIR_SERVER_TENANT_HEADER` | `X-Tenant-ID` | Tenant header name |
| `RASTIR_SERVER_SAMPLING_ENABLED` | `false` | Enable sampling |
| `RASTIR_SERVER_SAMPLING_RATE` | `1.0` | Sampling rate (0.0–1.0) |
| `RASTIR_SERVER_BACKPRESSURE_SOFT_LIMIT_PCT` | `80.0` | Queue soft limit % |
| `RASTIR_SERVER_BACKPRESSURE_HARD_LIMIT_PCT` | `95.0` | Queue hard limit % |
| `RASTIR_SERVER_BACKPRESSURE_MODE` | `reject` | Backpressure mode |
| `RASTIR_SERVER_RATE_LIMIT_ENABLED` | `false` | Enable rate limiting |
| `RASTIR_SERVER_RATE_LIMIT_PER_IP_RPM` | `600` | Per-IP requests/minute |
| `RASTIR_SERVER_RATE_LIMIT_PER_SERVICE_RPM` | `3000` | Per-service requests/minute |
| `RASTIR_SERVER_EXEMPLARS_ENABLED` | `false` | Enable Prometheus exemplars |
| `RASTIR_SERVER_SHUTDOWN_GRACE_PERIOD_SECONDS` | `30` | Shutdown grace period |
| `RASTIR_SERVER_SHUTDOWN_DRAIN_QUEUE` | `true` | Drain queue on shutdown |
| `RASTIR_SERVER_LOGGING_STRUCTURED` | `false` | JSON structured logs |
| `RASTIR_SERVER_LOGGING_LEVEL` | `INFO` | Log level |

### Startup Validation

The server validates configuration at startup and refuses to start if:
- Histogram bucket count exceeds 20
- Histogram buckets contain non-positive or unsorted values
- Queue size exceeds 1,000,000 or is non-positive
- Max traces exceeds 500,000 or is non-positive
- Label value length exceeds 1,024 or is non-positive
- Cardinality caps are non-positive
- Sampling rate is outside 0.0–1.0
- Backpressure soft_limit >= hard_limit
- Rate limit RPM values are non-positive
- Max spans per trace is non-positive
- TTL seconds is negative
- Shutdown grace period is negative
- Logging level is not a valid Python log level
