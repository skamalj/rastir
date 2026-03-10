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
| `evaluation_enabled` | `bool` | `False` | Enable evaluation metadata capture on `@llm` spans |
| `evaluation_types` | `list[str]` | `None` | Evaluation types to request (e.g. `["relevance", "faithfulness"]`) |
| `capture_prompt` | `bool` | `True` | Capture `prompt_text` attribute in LLM spans |
| `capture_completion` | `bool` | `True` | Capture `completion_text` attribute in LLM spans |
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
| `RASTIR_EVALUATION_TYPES` | — | Comma-separated evaluation types (e.g. `relevance,faithfulness`) |

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
  rate: 1.0                    # 0.0–1.0 probabilistic sampling rate

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

sre:
  enabled: true
  default_slo_error_rate: 0.01    # 1% error budget
  default_cost_budget_usd: 25.0   # $25/period
  agents:
    my_agent:
      slo_error_rate: 0.02        # 2% for this agent
    critical_agent:
      slo_error_rate: 0.005       # stricter 0.5%
      cost_budget_usd: 50.0       # per-agent override
```

### Server Environment Variables

All server settings can be overridden via `RASTIR_SERVER_*` environment variables.

**Precedence:** Environment variables > YAML config file > defaults.

#### Core

Network binding for the FastAPI collector server. The server exposes `/v1/telemetry` (span ingestion), `/metrics` (Prometheus scrape), `/v1/traces` (trace query), `/health`, and `/ready`.

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVER_CONFIG` | — | Path to YAML config file. All settings below can also be set in this file. Env vars always take precedence over YAML values |
| `RASTIR_SERVER_HOST` | `0.0.0.0` | Server bind address. Use `0.0.0.0` in containers, `127.0.0.1` for local-only access |
| `RASTIR_SERVER_PORT` | `8080` | Server bind port. Prometheus scrapes this port at `/metrics`, and clients push spans to `/v1/telemetry` on this port |

#### Resource Limits

Guardrails that prevent the server from consuming unbounded memory or creating Prometheus cardinality explosions. Cardinality caps limit how many distinct values a Prometheus label can have — once the cap is reached, new values are replaced with `__other__`. This protects Prometheus from high-cardinality time series that degrade query performance.

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVER_LIMITS_MAX_TRACES` | `10000` | Maximum number of traces held in the in-memory trace store. When exceeded, oldest traces are evicted (FIFO). Only relevant when `TRACE_STORE_ENABLED=true` |
| `RASTIR_SERVER_LIMITS_MAX_QUEUE_SIZE` | `50000` | Maximum number of spans that can be buffered in the ingestion queue between HTTP receipt and processing. Controls memory usage — see [Backpressure](#backpressure) for what happens when the queue fills |
| `RASTIR_SERVER_LIMITS_MAX_SPAN_ATTRIBUTES` | `100` | Maximum number of key-value attributes retained per span. Excess attributes are silently dropped |
| `RASTIR_SERVER_LIMITS_MAX_LABEL_VALUE_LENGTH` | `128` | Maximum character length for any Prometheus label value. Longer values are truncated. Prevents excessively long model names or agent names from inflating metric storage |
| `RASTIR_SERVER_LIMITS_CARDINALITY_MODEL` | `50` | Max distinct `model` label values (e.g. `gpt-4o`, `claude-3`). Increase if you use many fine-tuned model variants |
| `RASTIR_SERVER_LIMITS_CARDINALITY_PROVIDER` | `10` | Max distinct `provider` label values (e.g. `openai`, `anthropic`). 10 covers all built-in adapters |
| `RASTIR_SERVER_LIMITS_CARDINALITY_TOOL_NAME` | `200` | Max distinct `tool_name` label values. Increase if your agents use many dynamically-named tools |
| `RASTIR_SERVER_LIMITS_CARDINALITY_AGENT` | `200` | Max distinct `agent` label values. Increase if you run hundreds of uniquely-named agents |
| `RASTIR_SERVER_LIMITS_CARDINALITY_ERROR_TYPE` | `50` | Max distinct `error_type` label values. Rastir normalises errors to 6 categories, so 50 is generous |

#### Histogram Buckets

Customise the Prometheus histogram bucket boundaries for latency and token count distributions. The default buckets work well for most LLM workloads. Only change these if your latency or token distributions are unusual (e.g. very long batch jobs, or very small embeddings-only calls).

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVER_HISTOGRAMS_DURATION_BUCKETS` | `0.01,0.05,0.1,0.25,0.5,1.0,2.0,5.0,10.0,30.0,60.0` | Comma-separated duration bucket boundaries in seconds. Used by `rastir_duration_seconds` histogram. The Grafana dashboards compute p50/p95/p99 latency from these buckets |
| `RASTIR_SERVER_HISTOGRAMS_TOKENS_BUCKETS` | `10,50,100,250,500,1000,2000,4000,8000,16000,32000` | Comma-separated token count bucket boundaries. Used by `rastir_tokens_input` and `rastir_tokens_output` histograms |

#### Trace Store

An in-memory ring buffer that holds recent traces, queryable via `GET /v1/traces`. This is a **lightweight debug tool** — useful for `curl http://localhost:8080/v1/traces` to inspect recent spans without needing a full trace backend. It is **not Tempo/OTLP-compatible** and cannot be used as a Grafana datasource. For production trace visualization, use the OTLP exporter to forward traces to Tempo, Jaeger, X-Ray, etc.

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVER_TRACE_STORE_ENABLED` | `true` | Enable the in-memory trace store and `/v1/traces` query endpoint. Disable in production to save memory if you are forwarding traces via OTLP |
| `RASTIR_SERVER_TRACE_STORE_MAX_SPANS_PER_TRACE` | `500` | Maximum spans retained per trace. Traces with more spans (e.g. large agent graphs) have their oldest spans dropped |
| `RASTIR_SERVER_TRACE_STORE_TTL_SECONDS` | `0` | Time-to-live for traces in seconds. `0` = no expiration (traces are only evicted when `max_traces` is exceeded). Set to e.g. `3600` to auto-expire traces after 1 hour |

#### OTLP Export

Forwards processed spans from the Rastir server to an external trace backend via the OpenTelemetry Protocol (OTLP). This is the **production trace pipeline** — use it to send traces to Tempo, Jaeger, or any OTLP-compatible receiver. In cloud deployments, this typically points to a local OTel Collector sidecar (e.g. ADOT on AWS) which then forwards to the cloud trace service (X-Ray, Cloud Trace, etc.).

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVER_EXPORTER_OTLP_ENDPOINT` | — | OTLP HTTP endpoint URL (e.g. `http://tempo:4318`, `http://localhost:4318` for a sidecar). Export is **disabled** when unset. The server posts to `{endpoint}/v1/traces` |
| `RASTIR_SERVER_EXPORTER_BATCH_SIZE` | `200` | Number of spans accumulated before sending an OTLP export batch. Larger values reduce HTTP overhead but increase latency to the trace backend |
| `RASTIR_SERVER_EXPORTER_FLUSH_INTERVAL` | `5` | Maximum seconds to wait before flushing a partial batch. Ensures spans reach the trace backend even under low throughput |

#### Multi-Tenant

When enabled, the server extracts a tenant identifier from an HTTP header on each request and adds it as a Prometheus label. This allows a single Rastir instance to serve multiple teams or applications with per-tenant metric isolation.

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVER_MULTI_TENANT_ENABLED` | `false` | Enable multi-tenant label extraction. When enabled, every Prometheus metric gets an additional `tenant` label |
| `RASTIR_SERVER_MULTI_TENANT_HEADER_NAME` | `X-Tenant-ID` | HTTP header name to read the tenant identifier from. Clients must include this header on every `/v1/telemetry` request |

#### Sampling

Controls probabilistic trace sampling on the server. Sampling affects **trace storage, OTLP export, exemplars, and evaluation enqueue**. It does **not** affect Prometheus metrics — all spans always contribute to counters, histograms, and gauges regardless of sampling. This means you can sample down to reduce storage/export costs while keeping 100% accurate metrics.

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVER_SAMPLING_RATE` | `1.0` | Probability that a trace is stored/exported (`0.0`–`1.0`). `1.0` = keep all traces, `0.1` = keep 10%. Set lower in high-throughput production to control Tempo/Jaeger storage costs while retaining full metric accuracy |

#### Backpressure

Safety valve for the ingestion queue. The server has a bounded queue (`LIMITS_MAX_QUEUE_SIZE`) between HTTP span receipt and the worker that processes spans (metrics, store, OTLP export). When clients send spans faster than the server can process them, the queue grows. Backpressure defines what happens:

- **Soft limit** — queue reaches this % → server logs a warning and exposes a metric. No spans are dropped yet. Use this as an early alert.
- **Hard limit** — queue reaches this % → server takes action based on the mode setting to prevent out-of-memory.

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVER_BACKPRESSURE_SOFT_LIMIT_PCT` | `80.0` | Queue usage percentage that triggers warning logs and a `rastir_backpressure_soft_limit_reached` metric. Set up a Grafana alert on this |
| `RASTIR_SERVER_BACKPRESSURE_HARD_LIMIT_PCT` | `95.0` | Queue usage percentage that activates the backpressure mode. Must be greater than `soft_limit_pct` |
| `RASTIR_SERVER_BACKPRESSURE_MODE` | `reject` | What to do when the hard limit is hit: **`reject`** — drop new incoming spans and return HTTP 429/503 to the client (protects server memory); **`drop_oldest`** — evict oldest spans from the head of the queue to make room (prioritises recency over completeness) |

#### Rate Limiting

Optional request-level rate limiting to protect the server from misbehaving clients. This is separate from backpressure (which operates on queue depth). Rate limiting operates at the HTTP layer before spans enter the queue.

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVER_RATE_LIMIT_ENABLED` | `false` | Enable rate limiting. When disabled, no rate checks are performed |
| `RASTIR_SERVER_RATE_LIMIT_PER_IP_RPM` | `600` | Maximum requests per minute from a single client IP address. Protects against a single runaway client flooding the server |
| `RASTIR_SERVER_RATE_LIMIT_PER_SERVICE_RPM` | `3000` | Maximum requests per minute from a single service (identified by the `service` field in the telemetry payload). Prevents one noisy service from starving others |

#### Exemplars

Prometheus exemplars attach a `trace_id` to individual histogram observations, allowing you to jump from a Grafana metric panel directly to the specific trace that caused a latency spike or error. Requires Prometheus ≥ 2.39 with `--enable-feature=exemplar-storage` and Grafana's Tempo datasource configured.

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVER_EXEMPLARS_ENABLED` | `false` | Attach `trace_id` exemplars to duration and token histograms. Enable this when you have a Tempo/Jaeger backend configured, so Grafana can link metrics → traces |

#### Redaction

Server-side PII/sensitive data redaction for `prompt_text` and `completion_text` span attributes. Redaction runs **after** sampling but **before** trace storage, OTLP export, and evaluation enqueue — ensuring sensitive data never leaves the server. Built-in patterns detect common PII (SSNs, credit cards, emails, etc.). You can add custom patterns via JSON.

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVER_REDACTION_ENABLED` | `false` | Enable server-side redaction. When disabled, `prompt_text` and `completion_text` are stored/exported as-is |
| `RASTIR_SERVER_REDACTION_MAX_TEXT_LENGTH` | `50000` | Maximum character length for prompt/completion text. Text exceeding this is truncated before redaction to bound CPU cost |
| `RASTIR_SERVER_REDACTION_DROP_ON_FAILURE` | `true` | If redaction processing fails (e.g. regex timeout), drop the entire span rather than risk leaking unredacted data. Security-first default — set `false` only if availability matters more than data privacy |
| `RASTIR_SERVER_REDACTION_CUSTOM_PATTERNS_JSON` | — | JSON array of custom regex patterns. Format: `[{"pattern": "\\b\\d{3}-\\d{2}-\\d{4}\\b", "replacement": "[SSN]"}]`. Each matched pattern is replaced with its replacement string. Patterns run in order after built-in redaction |

#### Evaluation

Async server-side LLM-as-a-judge evaluation. When enabled, the server uses a separate LLM (the "judge") to evaluate the quality of LLM responses — checking for hallucination, relevance, toxicity, etc. Evaluation runs **asynchronously** in worker threads after the span is stored/exported, so it does not block ingestion.

**How it works**: The client-side `@llm` decorator captures `prompt_text`, `completion_text`, and `evaluation_types` in the span. The server picks up these spans, applies evaluation sampling, then sends prompt+completion to the judge LLM for each evaluation type. Results are emitted as new evaluation spans with scores.

**Cost note**: Evaluation calls the judge LLM for every sampled span × every evaluation type. With high throughput, this can be expensive. Use `DEFAULT_SAMPLE_RATE` to control cost — e.g. `0.1` evaluates only 10% of eligible spans. This is **independent** of trace sampling (`SAMPLING_RATE`), which controls storage/export. Both rates stack: with `SAMPLING_RATE=0.5` and `DEFAULT_SAMPLE_RATE=0.5`, only ~25% of LLM spans are evaluated.

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVER_EVALUATION_ENABLED` | `false` | Enable async evaluation. Requires a judge LLM to be configured below |
| `RASTIR_SERVER_EVALUATION_QUEUE_SIZE` | `10000` | Bounded queue capacity for spans awaiting evaluation. Sized independently from the ingestion queue |
| `RASTIR_SERVER_EVALUATION_DROP_POLICY` | `drop_new` | What happens when the evaluation queue is full: **`drop_new`** — discard newly arriving spans (safe default); **`drop_oldest`** — evict oldest queued spans |
| `RASTIR_SERVER_EVALUATION_WORKER_CONCURRENCY` | `4` | Number of concurrent worker threads making judge LLM API calls. Higher values = faster evaluation throughput but more API cost |
| `RASTIR_SERVER_EVALUATION_DEFAULT_SAMPLE_RATE` | `1.0` | Probability that a sampled span is also evaluated (`0.0`–`1.0`). Applies after trace sampling. Per-span `evaluation_sample_rate` attribute (set by client decorator) overrides this |
| `RASTIR_SERVER_EVALUATION_DEFAULT_TIMEOUT_MS` | `30000` | Timeout for each judge LLM API call in milliseconds. Timed-out evaluations are recorded as failures |
| `RASTIR_SERVER_EVALUATION_MAX_EVALUATION_TYPES` | `20` | Cardinality cap for `evaluation_type` metric label. Prevents unbounded metric growth from dynamically-named evaluation types |
| `RASTIR_SERVER_EVALUATION_JUDGE_MODEL` | `gpt-4o-mini` | LLM model used as the evaluation judge. Use a fast, cheap model for cost efficiency |
| `RASTIR_SERVER_EVALUATION_JUDGE_PROVIDER` | `openai` | Provider for the judge model (`openai`, `anthropic`, `gemini`, `bedrock`, etc.) |
| `RASTIR_SERVER_EVALUATION_JUDGE_API_KEY` | — | API key for the judge LLM provider. Required unless using IAM-based auth (e.g. Bedrock) |
| `RASTIR_SERVER_EVALUATION_JUDGE_BASE_URL` | — | Custom base URL for the judge LLM API (e.g. Azure OpenAI endpoint, or a local proxy) |

{: .note }
> Evaluation types (e.g. `hallucination`, `relevance`, `toxicity`) are configured **client-side**, not on the server. Set them per-decorator (`@llm(evaluation_types=[...])`) or globally via `configure(evaluation_types=[...])` / `RASTIR_EVALUATION_TYPES=hallucination,relevance`. The server evaluates whatever types each span requests.

#### Shutdown

Graceful shutdown behaviour when the server receives SIGTERM (e.g. during ECS task stop, Kubernetes pod termination, or `docker stop`). The server stops accepting new requests and optionally drains in-flight spans before exiting.

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVER_SHUTDOWN_GRACE_PERIOD_SECONDS` | `30` | Maximum seconds to wait during graceful shutdown. Should be less than the container orchestrator's stop timeout (ECS default: 30s, Kubernetes default: 30s) |
| `RASTIR_SERVER_SHUTDOWN_DRAIN_QUEUE` | `true` | Process remaining spans in the ingestion queue before shutdown. Set `false` for faster shutdowns at the cost of losing in-flight spans |

#### Logging

Server log output configuration. In containerised deployments (ECS, Kubernetes), use structured JSON logging so log aggregators (CloudWatch, Loki, etc.) can parse fields automatically.

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVER_LOGGING_STRUCTURED` | `false` | Enable JSON structured logging. Recommended `true` for Docker/ECS/Kubernetes. Plain text is easier to read for local development |
| `RASTIR_SERVER_LOGGING_LEVEL` | `INFO` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. Use `DEBUG` to see per-span processing steps (very verbose) |
| `RASTIR_SERVER_LOGGING_LOG_FILE` | — | Path to an additional log file. Logs always go to stderr; this mirrors them to a file for debugging. Not typically needed in containers where logs go to stdout/stderr |

#### SRE

SRE (Site Reliability Engineering) configuration for error budgets, cost budgets, and burn rate tracking. When enabled, the server exposes **config gauge metrics** that Prometheus recording rules consume to compute derived SRE metrics (budget remaining, burn rate, days-to-exhaustion).

| Variable | Default | Description |
|----------|---------|-------------|
| `RASTIR_SERVER_SRE_ENABLED` | `false` | Enable SRE config gauges. When enabled, `rastir_slo_error_rate` and `rastir_cost_budget_usd` gauges are registered and populated at startup |
| `RASTIR_SERVER_SRE_DEFAULT_SLO_ERROR_RATE` | `0.01` | Default error rate SLO for agents without a per-agent override. `0.01` = 1% error budget — if more than 1% of calls fail, the error budget is consumed |
| `RASTIR_SERVER_SRE_DEFAULT_COST_BUDGET_USD` | `0.0` | Default cost budget in USD per Prometheus evaluation period. `0` = cost budget tracking disabled. Set to e.g. `500.0` to track cost consumption against a $500 budget |
| `RASTIR_SERVER_SRE_AGENTS_JSON` | — | JSON object for per-agent SLO and cost budget overrides. Format: `{"my_agent": {"slo_error_rate": 0.02, "cost_budget_usd": 100.0}}`. Agents not listed here use the defaults above |

Per-agent overrides can also be set in the YAML config file under `sre.agents` (see YAML example above).

When enabled, the server exposes two Prometheus **Gauge** metrics at startup:

| Gauge | Labels | Description |
|-------|--------|-------------|
| `rastir_slo_error_rate` | `agent` | Configured SLO error rate per agent |
| `rastir_cost_budget_usd` | `agent` | Configured cost budget in USD per agent |

These gauges are consumed by **Prometheus recording rules** (see [Server — SRE Recording Rules](server#sre--prometheus-recording-rules)) to derive error budgets, burn rates, cost budgets, and days-to-exhaustion metrics.

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
- SRE default_slo_error_rate is outside (0.0, 1.0]
- SRE default_cost_budget_usd is negative
- Per-agent SLO error rates are outside (0.0, 1.0]
