---
layout: default
title: Metrics Reference
nav_order: 7
---

# Metrics Reference

Complete reference of all Prometheus metrics exposed by the Rastir collector server on the `/metrics` endpoint.

All metrics are derived **server-side** from ingested span data. The client library does not expose a metrics endpoint.

---

## Span Counters

Core counters tracking span ingestion and call volumes.

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `rastir_spans_ingested_total` | Counter | service, env, span_type, status | Total spans ingested |
| `rastir_llm_calls_total` | Counter | service, env, model, provider, agent | LLM invocations |
| `rastir_tokens_input_total` | Counter | service, env, model, provider, agent | Input (prompt) tokens |
| `rastir_tokens_output_total` | Counter | service, env, model, provider, agent | Output (completion) tokens |
| `rastir_tool_calls_total` | Counter | service, env, tool_name, agent, model, provider | Tool invocations |
| `rastir_retrieval_calls_total` | Counter | service, env, agent | Retrieval operations |
| `rastir_errors_total` | Counter | service, env, span_type, error_type | Error spans by normalised category |

---

## Histograms

Histograms track the **distribution** of values, enabling percentile calculations via PromQL's `histogram_quantile()`.

| Metric | Type | Labels | Default Buckets | Unit |
|--------|------|--------|-----------------|------|
| `rastir_duration_seconds` | Histogram | service, env, span_type | 0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0 | seconds |
| `rastir_tokens_per_call` | Histogram | service, env, model, provider | 10, 50, 100, 250, 500, 1000, 2000, 4000, 8000, 16000, 32000 | tokens |
| `rastir_cost_per_call_usd` | Histogram | service, env, model | 0.0001, 0.0005, 0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100 | USD |
| `rastir_ttft_seconds` | Histogram | service, env, model, provider | 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10 | seconds |

Maximum of **20 buckets** per histogram. Buckets are configurable via YAML or environment variables — see [Configuration](configuration#histogram-buckets).

### What Prometheus Exposes

For each histogram, Prometheus creates:

```
rastir_duration_seconds_bucket{..., le="0.25"} → count of spans ≤ 0.25s
rastir_duration_seconds_bucket{..., le="1.0"}  → count of spans ≤ 1.0s
rastir_duration_seconds_bucket{..., le="+Inf"} → total count
rastir_duration_seconds_sum{...}               → sum of all values
rastir_duration_seconds_count{...}             → same as +Inf bucket
```

### Percentile Queries — P50, P95, P99

```promql
# P50 (median) LLM call duration
histogram_quantile(0.50,
  rate(rastir_duration_seconds_bucket{span_type="llm"}[5m])
)

# P95 LLM call duration
histogram_quantile(0.95,
  rate(rastir_duration_seconds_bucket{span_type="llm"}[5m])
)

# P99 LLM call duration — tail latency
histogram_quantile(0.99,
  rate(rastir_duration_seconds_bucket{span_type="llm"}[5m])
)

# P95 duration per model
histogram_quantile(0.95,
  sum by (model, le) (
    rate(rastir_duration_seconds_bucket{span_type="llm"}[5m])
  )
)

# P50 tokens per LLM call
histogram_quantile(0.50,
  rate(rastir_tokens_per_call_bucket[5m])
)

# P95 tool execution time
histogram_quantile(0.95,
  rate(rastir_duration_seconds_bucket{span_type="tool"}[5m])
)
```

### Average & Throughput Queries

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

---

## Cost & TTFT Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `rastir_cost_total` | Counter | service, env, model, provider, agent, pricing_profile | Accumulated USD cost |
| `rastir_cost_per_call_usd` | Histogram | service, env, model | Cost distribution per LLM call |
| `rastir_pricing_missing_total` | Counter | service, env, model, provider | LLM calls missing pricing data |
| `rastir_ttft_seconds` | Histogram | service, env, model, provider | Time-To-First-Token for streaming calls |

Cost metrics are only recorded when the client sends `cost_usd` as a span attribute (requires `enable_cost_calculation=True`). TTFT metrics are only recorded for streaming LLM spans that include `ttft_ms`.

The `pricing_profile` label on `rastir_cost_total` is **cardinality-guarded** with a cap of 20 distinct values. The `rastir_cost_per_call_usd` histogram intentionally excludes `pricing_profile` to prevent cardinality explosion.

---

## Guardrail Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `rastir_guardrail_requests_total` | Counter | service, env, provider, model, agent, guardrail_id, guardrail_version | Guardrail-enabled LLM calls |
| `rastir_guardrail_violations_total` | Counter | service, env, provider, model, agent, guardrail_id, guardrail_action, guardrail_category | Guardrail interventions |

Guardrail labels are **cardinality-guarded** with bounded enum validation:

| Label | Allowed Values |
|-------|---------------|
| `guardrail_category` | `CONTENT_POLICY`, `SENSITIVE_INFORMATION_POLICY`, `WORD_POLICY`, `TOPIC_POLICY`, `CONTEXTUAL_GROUNDING_POLICY`, `DENIED_TOPIC` |
| `guardrail_action` | `GUARDRAIL_INTERVENED`, `NONE` |
| `guardrail_id` | Subject to cardinality cap (default: 100) |

Unknown values are replaced with `__cardinality_overflow__`. Validation runs on **both** the client adapter and the server.

---

## Evaluation Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `rastir_eval_runs_total` | Counter | service, env, model, provider, evaluation_type, evaluator_model, evaluator_provider | Evaluation runs |
| `rastir_eval_failures_total` | Counter | service, env, model, provider, evaluation_type, evaluator_model, evaluator_provider | Failed evaluations |
| `rastir_eval_latency_seconds` | Histogram | service, env, model, provider, evaluation_type, evaluator_model, evaluator_provider | Evaluation execution time |
| `rastir_eval_score` | Gauge | service, env, model, provider, evaluation_type, evaluator_model, evaluator_provider | Evaluation score |
| `rastir_eval_queue_size` | Gauge | — | Evaluation queue depth |

---

## Operational Metrics

Server health and performance metrics.

| Metric | Type | Description |
|--------|------|-------------|
| `rastir_queue_size` | Gauge | Current ingestion queue depth |
| `rastir_queue_utilization_percent` | Gauge | Queue fill percentage |
| `rastir_memory_bytes` | Gauge | Server process RSS memory |
| `rastir_trace_store_size` | Gauge | Total spans in trace store |
| `rastir_active_traces` | Gauge | Distinct trace count in store |
| `rastir_ingestion_rate` | Gauge | Spans ingested per second |
| `rastir_ingestion_rejections_total` | Counter | Rejected spans (backpressure) |
| `rastir_export_failures_total` | Counter | OTLP export failures |

---

## Sampling & Backpressure Metrics

| Metric | Type | Description |
|--------|------|-------------|
| `rastir_spans_sampled_total` | Counter | Spans retained after sampling |
| `rastir_spans_dropped_by_sampling_total` | Counter | Spans dropped by sampling |
| `rastir_backpressure_warnings_total` | Counter | Soft limit warnings |
| `rastir_spans_dropped_by_backpressure_total` | Counter | Spans dropped by backpressure |
| `rastir_rate_limited_total` | Counter | Rate-limited requests (by dimension) |

---

## Error Type Normalisation

The `rastir_errors_total` counter uses normalised error categories instead of raw exception class names, preventing unbounded label cardinality.

| Normalised category | Matched exception patterns |
|---------------------|---------------------------|
| `timeout` | `TimeoutError`, `asyncio.TimeoutError`, `httpx.TimeoutException`, `httpx.ReadTimeout`, `httpx.ConnectTimeout`, `openai.APITimeoutError` |
| `rate_limit` | `RateLimitError`, `openai.RateLimitError`, `anthropic.RateLimitError` |
| `validation_error` | `ValueError`, `TypeError`, `ValidationError`, `pydantic.ValidationError` |
| `provider_error` | `openai.APIError`, `openai.APIConnectionError`, `anthropic.APIError`, `botocore.exceptions.ClientError` |
| `internal_error` | `RuntimeError`, `Exception` |
| `unknown` | Any unrecognised exception type |

Normalisation uses exact match first, then substring heuristics (e.g., any exception with "timeout" in the name maps to `timeout`).

---

## Cardinality Guards

All high-cardinality labels are subject to per-dimension caps. Values exceeding the cap are replaced with `__cardinality_overflow__`.

| Label | Default Cap | Applies To |
|-------|------------|------------|
| `model` | 50 | `llm_calls`, `tokens_*`, `cost_*`, `guardrail_*`, `eval_*` |
| `provider` | 10 | Same as model |
| `tool_name` | 200 | `tool_calls` |
| `agent` | 200 | `llm_calls`, `tokens_*`, `tool_calls`, `guardrail_*` |
| `error_type` | 50 | `errors` |
| `guardrail_id` | 100 | `guardrail_*` |
| `pricing_profile` | 20 | `cost_total` |

Caps are configurable via server config — see [Configuration](configuration#resource-limits).

---

## Exemplar Support

Exemplars attach a **trace_id** to histogram observations and counter increments, creating a direct link from a Prometheus metric to the distributed trace that produced it.

### Metrics That Carry Exemplars

| Metric | Exemplar Label |
|--------|---------------|
| `rastir_duration_seconds` | `trace_id` |
| `rastir_llm_calls_total` | `trace_id` |

### Enabling Exemplars

```yaml
# server-config.yml
exemplars:
  enabled: true
```

Or: `export RASTIR_SERVER_EXEMPLARS_ENABLED=true`

When enabled, the `/metrics` endpoint automatically switches to **OpenMetrics** format (required for exemplars).

### Output Format

```
# Without exemplars (classic Prometheus)
rastir_duration_seconds_bucket{...,le="1.0"} 42

# With exemplars (OpenMetrics)
rastir_duration_seconds_bucket{...,le="1.0"} 42 # {trace_id="a1b2c3d4"} 0.847 1709042400.0
```

### Grafana Integration

1. Edit your Prometheus data source → enable **Exemplars** toggle
2. Set **Internal link** to your Jaeger/Tempo data source
3. Map label **`trace_id`** to trace ID field
4. In panel queries, toggle **Exemplars** on — they appear as diamond markers
5. Click a diamond to jump directly to the trace

```
Grafana: P95 latency spike at 14:32 →
  Click exemplar diamond →
    Tempo: trace_id=a1b2c3d4 →
      research_agent (2.3s)
        ├─ plan_step (0.8s)        OK
        ├─ web_search (1.2s)       ← slow!
        └─ synthesize (0.3s)       OK
```
