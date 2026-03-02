---
layout: default
title: Environment Variables
nav_order: 11
---

# Environment Variables Reference

Complete reference of all environment variables used by Rastir. Variables are organised by component: client library, collector server, and testing/scripts.

{: .note }
> For `configure()` parameters and YAML config file options, see the [Configuration](configuration) page. This page consolidates **all** environment variables in one place.

---

## Client Library

These variables configure the Rastir client library used in your application. They are read by `configure()` and can be overridden by passing arguments directly.

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
export RASTIR_FLUSH_INTERVAL=10
export RASTIR_EVALUATION_ENABLED=true
export RASTIR_CAPTURE_PROMPT=false    # disable prompt capture in production
export RASTIR_ENABLE_COST_CALCULATION=true
export RASTIR_PRICING_PROFILE=production_2025_q1
export RASTIR_PRICING_SOURCE=/etc/rastir/pricing.json
export RASTIR_ENABLE_TTFT=true
```

---

## Collector Server

These variables configure the Rastir collector server. They override values from the YAML config file.

**Precedence:** Environment variables > YAML config file > defaults.

### Core

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVER_CONFIG` | — | Path to YAML config file |
| `RASTIR_SERVER_HOST` | `0.0.0.0` | Server bind address |
| `RASTIR_SERVER_PORT` | `8080` | Server bind port |

### Resource Limits

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

### Histogram Buckets

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVER_HISTOGRAMS_DURATION_BUCKETS` | `0.01,0.05,0.1,0.25,0.5,1.0,2.0,5.0,10.0,30.0,60.0` | Comma-separated duration histogram bucket boundaries (seconds) |
| `RASTIR_SERVER_HISTOGRAMS_TOKENS_BUCKETS` | `10,50,100,250,500,1000,2000,4000,8000,16000,32000` | Comma-separated token count histogram bucket boundaries |

### Trace Store

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVER_TRACE_STORE_ENABLED` | `true` | Enable in-memory trace store |
| `RASTIR_SERVER_TRACE_STORE_MAX_SPANS_PER_TRACE` | `500` | Max spans retained per trace |
| `RASTIR_SERVER_TRACE_STORE_TTL_SECONDS` | `0` | Trace TTL in seconds (`0` = no expiration) |

### OTLP Export

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVER_EXPORTER_OTLP_ENDPOINT` | — | OTLP export endpoint (e.g. `http://tempo:4317`). Disabled if unset |
| `RASTIR_SERVER_EXPORTER_BATCH_SIZE` | `200` | Spans per OTLP export batch |
| `RASTIR_SERVER_EXPORTER_FLUSH_INTERVAL` | `5` | Seconds between OTLP export flushes |

### Multi-Tenant

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVER_MULTI_TENANT_ENABLED` | `false` | Enable multi-tenant mode |
| `RASTIR_SERVER_MULTI_TENANT_HEADER_NAME` | `X-Tenant-ID` | HTTP header for tenant identification |

### Sampling

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVER_SAMPLING_ENABLED` | `false` | Enable head-based trace sampling |
| `RASTIR_SERVER_SAMPLING_RATE` | `1.0` | Sampling rate (`0.0`–`1.0`). Metrics are always recorded regardless of sampling |
| `RASTIR_SERVER_SAMPLING_ALWAYS_RETAIN_ERRORS` | `true` | Always retain error spans regardless of sampling rate |
| `RASTIR_SERVER_SAMPLING_LATENCY_THRESHOLD_MS` | `0` | Always retain spans above this latency in ms (`0` = disabled) |

### Backpressure

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVER_BACKPRESSURE_SOFT_LIMIT_PCT` | `80.0` | Queue usage % that triggers warning metrics |
| `RASTIR_SERVER_BACKPRESSURE_HARD_LIMIT_PCT` | `95.0` | Queue usage % that triggers rejection or drop |
| `RASTIR_SERVER_BACKPRESSURE_MODE` | `reject` | Backpressure mode: `reject` (return 429) or `drop_oldest` |

### Rate Limiting

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVER_RATE_LIMIT_ENABLED` | `false` | Enable per-IP and per-service rate limiting |
| `RASTIR_SERVER_RATE_LIMIT_PER_IP_RPM` | `600` | Max requests per minute per client IP |
| `RASTIR_SERVER_RATE_LIMIT_PER_SERVICE_RPM` | `3000` | Max requests per minute per service name |

### Exemplars

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVER_EXEMPLARS_ENABLED` | `false` | Enable Prometheus exemplars (attach `trace_id` to histogram observations) |

### Shutdown

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVER_SHUTDOWN_GRACE_PERIOD_SECONDS` | `30` | Graceful shutdown grace period in seconds |
| `RASTIR_SERVER_SHUTDOWN_DRAIN_QUEUE` | `true` | Drain ingestion queue before shutdown |

### Logging

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVER_LOGGING_STRUCTURED` | `false` | Enable JSON structured logging |
| `RASTIR_SERVER_LOGGING_LEVEL` | `INFO` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |

### Redaction

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVER_REDACTION_ENABLED` | `false` | Enable server-side redaction of prompt/completion text |
| `RASTIR_SERVER_REDACTION_MAX_TEXT_LENGTH` | `50000` | Max text length before truncation (characters) |
| `RASTIR_SERVER_REDACTION_DROP_ON_FAILURE` | `true` | Drop the span if redaction processing fails (security-first default) |

### Evaluation

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

---

## Testing & Scripts

These variables are used by integration tests and load-testing scripts. They are not needed for normal Rastir operation.

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENAI_API_KEY` | — | OpenAI API key for integration tests |
| `API_OPENAI_KEY` | — | Fallback OpenAI API key (checked if `OPENAI_API_KEY` is unset) |
| `ANTHROPIC_API_KEY` | — | Anthropic API key for integration tests |
| `API_ANTHROPIC_KEY` | — | Fallback Anthropic API key (checked if `ANTHROPIC_API_KEY` is unset) |
| `LOAD_ROUNDS` | `12` | Number of rounds for load test scripts |
| `ROUND_PAUSE` | `6` | Pause between load test rounds in seconds |
| `BEDROCK_GUARDRAIL_ID` | `i3rttxfu7kow` | AWS Bedrock guardrail ID for load tests |
