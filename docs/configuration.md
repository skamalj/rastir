---
layout: default
title: Configuration
nav_order: 11
---

# Configuration Reference

Rastir has two configuration surfaces: the **client library** (used in your application) and the **collector server**. This page covers all configuration options, environment variables, and YAML config.

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
| `enable_cost_calculation` | `bool` | `False` | Enable client-side cost calculation on `@llm` spans |
| `pricing_profile` | `str` | `"default"` | Label identifying the pricing configuration used |
| `pricing_source` | `str` | `None` | Path to pricing JSON file |
| `max_cost_per_call_alert` | `float` | `None` | Per-call cost threshold for warning logs |
| `enable_ttft` | `bool` | `True` | Enable Time-To-First-Token measurement on streaming spans |

### Client Environment Variables

All client settings can be set via environment variables with the `RASTIR_` prefix.

**Precedence:** `configure()` arguments > environment variables > defaults.

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVICE` | `"unknown"` | Logical service name attached to all spans and metrics |
| `RASTIR_ENV` | `"development"` | Deployment environment (e.g. `production`, `staging`) |
| `RASTIR_VERSION` | — | Application version string |
| `RASTIR_PUSH_URL` | — | Collector server URL (e.g. `http://localhost:8080`). Push disabled if unset |
| `RASTIR_API_KEY` | — | Authentication key sent as `X-API-Key` header to the collector |
| `RASTIR_BATCH_SIZE` | `100` | Max spans per push batch |
| `RASTIR_FLUSH_INTERVAL` | `5` | Seconds between background batch flushes |
| `RASTIR_TIMEOUT` | `5` | HTTP request timeout in seconds |
| `RASTIR_MAX_RETRIES` | `3` | Max retry attempts on transient failures (5xx, 429, connection errors) |
| `RASTIR_RETRY_BACKOFF` | `0.5` | Initial backoff in seconds (doubles each retry) |
| `RASTIR_SHUTDOWN_TIMEOUT` | `5.0` | Max seconds to wait for exporter thread on process shutdown |
| `RASTIR_EVALUATION_ENABLED` | `false` | Enable evaluation metadata capture on `@llm` spans |
| `RASTIR_CAPTURE_PROMPT` | `true` | Capture `prompt_text` attribute in LLM spans |
| `RASTIR_CAPTURE_COMPLETION` | `true` | Capture `completion_text` attribute in LLM spans |
| `RASTIR_ENABLE_COST_CALCULATION` | `false` | Enable client-side cost calculation on `@llm` spans |
| `RASTIR_PRICING_PROFILE` | `"default"` | Label identifying the pricing configuration used |
| `RASTIR_PRICING_SOURCE` | — | Path to pricing JSON file |
| `RASTIR_PRICING_DATA` | — | Inline pricing JSON string (alternative to file) |
| `RASTIR_MAX_COST_PER_CALL_ALERT` | — | Per-call cost threshold in USD for warning logs |
| `RASTIR_ENABLE_TTFT` | `true` | Enable Time-To-First-Token measurement on streaming spans |

### Example

```bash
export RASTIR_SERVICE=my-app
export RASTIR_ENV=production
export RASTIR_PUSH_URL=http://collector:8080/v1/telemetry
export RASTIR_API_KEY=secret-key
export RASTIR_BATCH_SIZE=200
export RASTIR_EVALUATION_ENABLED=true
export RASTIR_CAPTURE_PROMPT=false    # disable prompt capture in production
export RASTIR_ENABLE_COST_CALCULATION=true
export RASTIR_PRICING_PROFILE=production_2025_q1
export RASTIR_PRICING_SOURCE=/etc/rastir/pricing.json
```

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

redaction:
  enabled: false
  max_text_length: 50000
  drop_on_failure: true

evaluation:
  enabled: false
  queue_size: 10000
  drop_policy: drop_new
  worker_concurrency: 4
  default_sample_rate: 1.0
  default_timeout_ms: 30000
  max_evaluation_types: 20
  judge_model: gpt-4o-mini
  judge_provider: openai

shutdown:
  grace_period_seconds: 30
  drain_queue: true

logging:
  structured: false
  level: INFO
```

### Server Environment Variables

All server settings can be overridden via `RASTIR_SERVER_*` environment variables.

**Precedence:** Environment variables > YAML config file > defaults.

#### Core

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVER_CONFIG` | — | Path to YAML config file |
| `RASTIR_SERVER_HOST` | `0.0.0.0` | Server bind address |
| `RASTIR_SERVER_PORT` | `8080` | Server bind port |

#### Resource Limits

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVER_LIMITS_MAX_TRACES` | `10000` | Max traces retained in store |
| `RASTIR_SERVER_LIMITS_MAX_QUEUE_SIZE` | `50000` | Max ingestion queue size |
| `RASTIR_SERVER_LIMITS_MAX_SPAN_ATTRIBUTES` | `100` | Max attributes per span |
| `RASTIR_SERVER_LIMITS_MAX_LABEL_VALUE_LENGTH` | `128` | Max Prometheus label value length |
| `RASTIR_SERVER_LIMITS_CARDINALITY_MODEL` | `50` | Cardinality cap for `model` label |
| `RASTIR_SERVER_LIMITS_CARDINALITY_PROVIDER` | `10` | Cardinality cap for `provider` label |
| `RASTIR_SERVER_LIMITS_CARDINALITY_TOOL_NAME` | `200` | Cardinality cap for `tool_name` label |
| `RASTIR_SERVER_LIMITS_CARDINALITY_AGENT` | `200` | Cardinality cap for `agent` label |
| `RASTIR_SERVER_LIMITS_CARDINALITY_ERROR_TYPE` | `50` | Cardinality cap for `error_type` label |

#### Histogram Buckets

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVER_HISTOGRAMS_DURATION_BUCKETS` | `0.01,0.05,...,60.0` | Comma-separated duration bucket boundaries (seconds) |
| `RASTIR_SERVER_HISTOGRAMS_TOKENS_BUCKETS` | `10,50,...,32000` | Comma-separated token count bucket boundaries |

#### Trace Store

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVER_TRACE_STORE_ENABLED` | `true` | Enable in-memory trace store |
| `RASTIR_SERVER_TRACE_STORE_MAX_SPANS_PER_TRACE` | `500` | Max spans retained per trace |
| `RASTIR_SERVER_TRACE_STORE_TTL_SECONDS` | `0` | Trace TTL in seconds (`0` = no expiration) |

#### OTLP Export

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVER_EXPORTER_OTLP_ENDPOINT` | — | OTLP export endpoint (e.g. `http://tempo:4317`). Disabled if unset |
| `RASTIR_SERVER_EXPORTER_BATCH_SIZE` | `200` | Spans per OTLP export batch |
| `RASTIR_SERVER_EXPORTER_FLUSH_INTERVAL` | `5` | Seconds between OTLP export flushes |

#### Multi-Tenant

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVER_MULTI_TENANT_ENABLED` | `false` | Enable multi-tenant mode |
| `RASTIR_SERVER_MULTI_TENANT_HEADER_NAME` | `X-Tenant-ID` | HTTP header for tenant identification |

#### Sampling

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVER_SAMPLING_ENABLED` | `false` | Enable head-based trace sampling |
| `RASTIR_SERVER_SAMPLING_RATE` | `1.0` | Sampling rate (`0.0`–`1.0`). Metrics always recorded regardless of sampling |
| `RASTIR_SERVER_SAMPLING_ALWAYS_RETAIN_ERRORS` | `true` | Always retain error spans regardless of sampling rate |
| `RASTIR_SERVER_SAMPLING_LATENCY_THRESHOLD_MS` | `0` | Always retain spans above this latency in ms (`0` = disabled) |

#### Backpressure

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVER_BACKPRESSURE_SOFT_LIMIT_PCT` | `80.0` | Queue usage % that triggers warning metrics |
| `RASTIR_SERVER_BACKPRESSURE_HARD_LIMIT_PCT` | `95.0` | Queue usage % that triggers rejection or drop |
| `RASTIR_SERVER_BACKPRESSURE_MODE` | `reject` | Backpressure mode: `reject` (return 429) or `drop_oldest` |

#### Rate Limiting

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVER_RATE_LIMIT_ENABLED` | `false` | Enable per-IP and per-service rate limiting |
| `RASTIR_SERVER_RATE_LIMIT_PER_IP_RPM` | `600` | Max requests per minute per client IP |
| `RASTIR_SERVER_RATE_LIMIT_PER_SERVICE_RPM` | `3000` | Max requests per minute per service name |

#### Exemplars

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVER_EXEMPLARS_ENABLED` | `false` | Enable Prometheus exemplars (attach `trace_id` to histogram observations) |

#### Redaction

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVER_REDACTION_ENABLED` | `false` | Enable server-side redaction of prompt/completion text |
| `RASTIR_SERVER_REDACTION_MAX_TEXT_LENGTH` | `50000` | Max text length before truncation (characters) |
| `RASTIR_SERVER_REDACTION_DROP_ON_FAILURE` | `true` | Drop the span if redaction processing fails (security-first default) |

#### Evaluation

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVER_EVALUATION_ENABLED` | `false` | Enable async server-side evaluation |
| `RASTIR_SERVER_EVALUATION_QUEUE_SIZE` | `10000` | Evaluation queue capacity |
| `RASTIR_SERVER_EVALUATION_DROP_POLICY` | `drop_new` | Queue full policy: `drop_new` or `drop_oldest` |
| `RASTIR_SERVER_EVALUATION_WORKER_CONCURRENCY` | `4` | Number of concurrent evaluation workers |
| `RASTIR_SERVER_EVALUATION_DEFAULT_SAMPLE_RATE` | `1.0` | Default evaluation sampling rate (`0.0`–`1.0`) |
| `RASTIR_SERVER_EVALUATION_DEFAULT_TIMEOUT_MS` | `30000` | Default evaluation timeout in milliseconds |
| `RASTIR_SERVER_EVALUATION_MAX_EVALUATION_TYPES` | `20` | Max registered evaluation types |
| `RASTIR_SERVER_EVALUATION_JUDGE_MODEL` | `gpt-4o-mini` | LLM model used as evaluation judge |
| `RASTIR_SERVER_EVALUATION_JUDGE_PROVIDER` | `openai` | LLM provider for evaluation judge |
| `RASTIR_SERVER_EVALUATION_JUDGE_API_KEY` | — | API key for the judge LLM provider |
| `RASTIR_SERVER_EVALUATION_JUDGE_BASE_URL` | — | Custom base URL for the judge LLM (e.g. Azure endpoint) |

#### Shutdown

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVER_SHUTDOWN_GRACE_PERIOD_SECONDS` | `30` | Graceful shutdown grace period in seconds |
| `RASTIR_SERVER_SHUTDOWN_DRAIN_QUEUE` | `true` | Drain ingestion queue before shutdown |

#### Logging

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVER_LOGGING_STRUCTURED` | `false` | Enable JSON structured logging |
| `RASTIR_SERVER_LOGGING_LEVEL` | `INFO` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |

---

## Testing & Script Variables

These variables are used by integration tests and load-testing scripts. They are not needed for normal Rastir operation.

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENAI_API_KEY` | — | OpenAI API key for integration tests |
| `API_OPENAI_KEY` | — | Fallback OpenAI API key (checked if `OPENAI_API_KEY` is unset) |
| `ANTHROPIC_API_KEY` | — | Anthropic API key for integration tests |
| `API_ANTHROPIC_KEY` | — | Fallback Anthropic API key |
| `LOAD_ROUNDS` | `12` | Number of rounds for load test scripts |
| `ROUND_PAUSE` | `6` | Pause between load test rounds in seconds |
| `BEDROCK_GUARDRAIL_ID` | `i3rttxfu7kow` | AWS Bedrock guardrail ID for load tests |

---

## Startup Validation

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
