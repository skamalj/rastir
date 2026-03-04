---
layout: default
title: Dashboards
nav_order: 9
---

# Grafana Dashboards

Rastir ships seven pre-built Grafana dashboards that provide full observability across your LLM application stack. All dashboards are JSON files ready to import into Grafana.

Dashboard JSON files are located in `grafana/dashboards/` in the repository.

---

## Importing Dashboards

### Manual Import

1. Open Grafana → **Dashboards** → **Import**
2. Upload the JSON file or paste its contents
3. Select your Prometheus data source
4. Click **Import**

### API Import

```bash
# Prepare the payload (strip the "id" field for import)
python3 -c "
import json
d = json.load(open('grafana/dashboards/llm-performance.json'))
d.pop('id', None)
payload = {'dashboard': d, 'overwrite': True}
json.dump(payload, open('/tmp/import.json', 'w'))
"

# Import via Grafana API
curl -u 'admin:admin' \
  -H 'Content-Type: application/json' \
  -X POST http://localhost:3000/api/dashboards/db \
  -d @/tmp/import.json
```

Repeat for each dashboard file.

---

## Dashboard Overview

| Dashboard | File | UID | Focus |
|-----------|------|-----|-------|
| LLM Performance | `llm-performance.json` | `rastir-llm-performance` | Token usage, latency, throughput, errors |
| Agent & Tool | `agent-tool.json` | `rastir-agent-tool` | Agent execution, tool calls, retrieval ops |
| Evaluation | `evaluation.json` | `rastir-evaluation` | Eval runs, scores, latency, queue health |
| Guardrail | `guardrail.json` | `rastir-guardrail` | Guardrail requests, violations by category/model |
| System Health | `system-health.json` | `rastir-system-health` | Ingestion rate, queue, memory, backpressure |
| Cost & TTFT | `cost-ttft.json` | `rastir-cost-ttft` | Cost per model/agent, burn rate, TTFT P95 |
| SRE Budgets | `sre-budgets.json` | `rastir-sre-budgets` | Error & cost budgets, burn rates, SLA status, service performance |

All dashboards share common template variables for filtering:

| Variable | Source Metric | Available On |
|----------|--------------|--------------|
| `service` | `rastir_spans_ingested_total` | All dashboards |
| `env` | `rastir_spans_ingested_total` | All dashboards |
| `model` | `rastir_llm_calls_total` | All except System Health |
| `provider` | `rastir_llm_calls_total` | All except System Health |
| `agent` | `rastir_llm_calls_total` | All except System Health |

---

## LLM Performance Dashboard

**File:** `grafana/dashboards/llm-performance.json`

Monitors LLM call throughput, token consumption, latency distribution, and error rates.

### Panels

| Panel | Type | Description |
|-------|------|-------------|
| **Total LLM Calls** | Stat (KPI) | Total LLM invocations in the selected time range |
| **Total Errors** | Stat (KPI) | Total LLM errors (red) |
| **Throughput by Model** | Pie chart | LLM call distribution across models |
| **Throughput by Model (Table)** | Table | Tabular breakdown with sum footer |
| **Latency P50 / P95 / P99** | Time series | Duration percentiles via `histogram_quantile()` |
| **Total Input Tokens** | Stat (KPI) | Cumulative input token count |
| **Total Output Tokens** | Stat (KPI) | Cumulative output token count |
| **Input Tokens by Model** | Time series | Cumulative input token counters per model |
| **Output Tokens by Model** | Time series | Cumulative output token counters per model |
| **Tokens per Call P50 / P95 / P99** | Time series | Token distribution percentiles |
| **Error Totals by Type** | Bar chart | Errors categorised by normalised error type |
| **Error Totals (Table)** | Table | Error breakdown with sum footer |

### Key Queries

```promql
# Total LLM calls (KPI stat)
sum(increase(rastir_llm_calls_total{service=~"$service", env=~"$env",
  model=~"$model", provider=~"$provider", agent=~"$agent"}[$__range]))

# P95 latency
histogram_quantile(0.95,
  sum by (le) (rate(rastir_duration_seconds_bucket{
    service=~"$service", env=~"$env", span_type="llm"}[5m])))

# Cumulative input tokens by model
rastir_tokens_input_total{service=~"$service", env=~"$env",
  model=~"$model", provider=~"$provider", agent=~"$agent"}
```

---

## Agent & Tool Dashboard

**File:** `grafana/dashboards/agent-tool.json`

Tracks agent execution patterns, tool invocations, and retrieval operations with model/provider context.

### Panels

| Panel | Type | Description |
|-------|------|-------------|
| **Agent Calls by Name** | Pie chart | Agent invocation distribution |
| **Agent Calls (Table)** | Table | Agent call counts with sum footer |
| **Tool Calls by Name** | Pie chart | Tool invocation distribution |
| **Tool Calls (Table)** | Table | Tool call counts with sum footer |
| **Tool Calls by Model** | Bar chart | Tool usage broken down by LLM model |
| **Tool Calls by Provider** | Bar chart | Tool usage broken down by provider |
| **Retrieval Calls** | Stat | Total retrieval operation count |
| **Agent Duration P50 / P95 / P99** | Time series | Agent latency percentiles |
| **Tool Duration P50 / P95 / P99** | Time series | Tool latency percentiles |

### Key Queries

```promql
# Tool calls with model/provider context
sum by (tool_name) (increase(rastir_tool_calls_total{
  service=~"$service", env=~"$env", model=~"$model",
  provider=~"$provider", agent=~"$agent"}[$__range]))

# Agent duration P95
histogram_quantile(0.95,
  sum by (le) (rate(rastir_duration_seconds_bucket{
    service=~"$service", env=~"$env", span_type="agent"}[5m])))
```

{: .note }
> The `rastir_tool_calls_total` metric carries `model` and `provider` labels, propagated from the parent `@llm` decorator context. This enables tool-level analysis filtered by which LLM model triggered the tool call.

---

## Evaluation Dashboard

**File:** `grafana/dashboards/evaluation.json`

Monitors async server-side evaluations — run counts, success/failure rates, scores, and queue health.

### Panels

| Panel | Type | Description |
|-------|------|-------------|
| **Total Eval Runs** | Stat (KPI) | Total evaluation runs (blue) |
| **Total Success** | Stat (KPI) | Successful evaluations (green, computed as runs − failures) |
| **Total Failures** | Stat (KPI) | Failed evaluations (red) |
| **Evaluations by Type** | Pie chart | Distribution across evaluation types, switchable by status |
| **Evaluations by Model** | Pie chart | Distribution across judge models, switchable by status |
| **Eval Latency P50 / P95 / P99** | Time series | Evaluation execution time percentiles |
| **Eval Score by Type** | Time series | Average evaluation scores per type |
| **Eval Score by Model** | Time series | Average evaluation scores per judge model |
| **Queue Health** | Time series | Evaluation queue depth over time |

### Template Variables

In addition to the standard filters, this dashboard includes:

| Variable | Options | Description |
|----------|---------|-------------|
| `eval_status` | Total, Success, Failed | Dynamically switches pie chart data between total runs, successes, and failures using PromQL coefficient math |

### Key Queries

```promql
# Total eval runs
sum(increase(rastir_eval_runs_total{service=~"$service", env=~"$env"}[$__range]))

# Dynamic pie chart with status filter (coefficient math)
# When eval_status=0 (Total): shows runs
# When eval_status=1 (Success): shows runs - failures
# When eval_status=2 (Failed): shows failures
sum by (eval_type) (
  increase(rastir_eval_runs_total{...}[$__range])
    * (1 - floor($eval_status / 2))
  + increase(rastir_eval_failures_total{...}[$__range])
    * (sgn($eval_status) * (2 * $eval_status - 3))
)
```

---

## Guardrail Dashboard

**File:** `grafana/dashboards/guardrail.json`

Tracks AWS Bedrock guardrail activity — request volumes, violation counts, and breakdowns by category and model.

### Panels

| Panel | Type | Description |
|-------|------|-------------|
| **Total Violations** | Stat (KPI) | Total guardrail interventions (red) |
| **Total Guardrail Requests** | Stat (KPI) | Total LLM calls with guardrail config (blue) |
| **Violations by Category** | Pie chart | Violation distribution across guardrail categories |
| **Violations by Category (Table)** | Table | Category breakdown with sum footer |
| **Violations by Model** | Bar chart (horizontal) | Violations grouped by LLM model |
| **Violations by Model (Table)** | Table | Model breakdown with sum footer |

### Guardrail Categories

The `guardrail_category` label is validated against a bounded enum:

| Category | Description |
|----------|-------------|
| `CONTENT_POLICY` | Content filtering violations |
| `SENSITIVE_INFORMATION_POLICY` | PII/sensitive data detection |
| `WORD_POLICY` | Blocked word/phrase detection |
| `TOPIC_POLICY` | Off-topic content detection |
| `CONTEXTUAL_GROUNDING_POLICY` | Grounding/hallucination detection |
| `DENIED_TOPIC` | Explicitly denied topic areas |

### Key Queries

```promql
# Total violations
sum(increase(rastir_guardrail_violations_total{service=~"$service",
  env=~"$env", model=~"$model", provider=~"$provider",
  agent=~"$agent"}[$__range]))

# Violations by category (pie chart)
sum by (guardrail_category) (increase(
  rastir_guardrail_violations_total{...}[$__range]))

# Violations by model (bar chart)
sum by (model) (increase(
  rastir_guardrail_violations_total{...}[$__range]))
```

---

## System Health Dashboard

**File:** `grafana/dashboards/system-health.json`

Monitors the Rastir collector server's operational health — ingestion throughput, queue pressure, memory usage, and export reliability.

### Panels

| Panel | Type | Description |
|-------|------|-------------|
| **Span Ingestion Rate** | Time series | Spans ingested per second over time |
| **Queue Size** | Stat | Current ingestion queue depth (colour-coded thresholds) |
| **Queue Utilization** | Gauge | Queue fill percentage (green/orange/red zones) |
| **Ingestion Rate** | Stat | Current measured throughput (spans/s) |
| **Memory Usage** | Time series | Server RSS memory consumption over time |
| **Trace Store Size** | Stat | Number of spans stored in the trace store |
| **Active Traces** | Stat | Distinct incomplete traces being assembled |
| **Ingestion Rejections** | Time series | Rejected spans due to backpressure |
| **Spans Dropped by Backpressure** | Time series | Dropped spans rate |
| **Backpressure Warnings** | Time series | Soft-limit warning events |
| **OTLP Export Failures** | Time series | Export failure rate |
| **Redaction Applied** | Time series | Redaction rule application rate |
| **Redaction Failures** | Time series | Redaction processing failure rate |

### Key Queries

```promql
# Span ingestion rate
rate(rastir_spans_ingested_total{service=~"$service", env=~"$env"}[5m])

# Queue utilization
rastir_queue_utilization_percent

# Memory usage
rastir_memory_bytes
```

---

## Prerequisites

- **Grafana 12+** (tested with 12.4.0)
- **Prometheus** data source configured in Grafana with UID `prometheus`
- Rastir collector server running and scraped by Prometheus

### Prometheus Scrape Configuration

Add the Rastir server as a scrape target in your `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: "rastir"
    scrape_interval: 15s
    static_configs:
      - targets: ["localhost:8080"]
```

### Dashboard Data Source

All dashboards reference a Prometheus data source with `uid: "prometheus"`. If your Grafana installation uses a different UID, update the `datasource` fields in the JSON files before import.

---

## Cost & TTFT Dashboard

**File:** `grafana/dashboards/cost-ttft.json`

Provides financial observability and streaming latency insight for LLM calls.

### Template Variables

| Variable | Source | Description |
|----------|--------|-------------|
| `service` | `rastir_cost_total` | Filter by service |
| `env` | `rastir_cost_total` | Filter by environment |
| `model` | `rastir_cost_total` | Filter by model |
| `pricing_profile` | `rastir_cost_total` | Filter by pricing profile label |

### Panels

| Panel | Type | Description |
|-------|------|-------------|
| **Total Cost (USD)** | Stat (KPI) | Accumulated cost in selected time range |
| **Cost per Model** | Pie chart | Cost breakdown by model |
| **Cost per Agent** | Pie chart | Cost breakdown by agent |
| **Cost Burn Rate** | Time series | USD/minute rate, total and per-model |
| **Cost P95 per Call** | Time series | P95 and P50 cost per LLM call by model |
| **Cost by Pricing Profile** | Time series | Cost split by pricing_profile label |
| **Pricing Missing Rate** | Time series | Rate of LLM calls missing pricing data |
| **TTFT P95 per Model** | Time series | P95 and P50 Time-To-First-Token by model |
| **TTFT Trend Over Time** | Time series | P95/P75/P50 TTFT by provider |
| **TTFT Heatmap** | Heatmap | Distribution of TTFT values over time |

### Metrics Used

| Metric | Type | Purpose |
|--------|------|---------|
| `rastir_cost_total` | Counter | Accumulated USD cost by model/provider/agent/pricing_profile |
| `rastir_cost_per_call_usd` | Histogram | Cost distribution per LLM call (no pricing_profile label) |
| `rastir_pricing_missing_total` | Counter | Calls where pricing entry was not found |
| `rastir_ttft_seconds` | Histogram | Time-To-First-Token for streaming LLM calls |

---

## SRE Budgets & Burn Rates Dashboard

**File:** `grafana/dashboards/sre-budgets.json`

Provides SRE-style error budget and cost budget tracking with burn rates, exhaustion estimates, and service-level performance panels. This dashboard consumes **Prometheus recording rules** — see [Server — SRE Recording Rules](server#sre--prometheus-recording-rules) for setup.

### Prerequisites

1. Enable `sre.enabled: true` in server config (see [Configuration — SRE](configuration#sre))
2. Deploy `grafana/prometheus/rastir-sre-rules.yml` to Prometheus
3. Verify rules are loaded at `http://localhost:9090/rules`

### Template Variables

| Variable | Source | Description |
|----------|--------|-------------|
| `service` | `rastir:volume:month` | Filter by service |
| `env` | `rastir:volume:month` | Filter by environment |
| `agent` | `rastir:volume:month` | Filter by agent (`.+` = all) |
| `model` | `rastir:errors_by_model:month` | Filter by model |
| `provider` | `rastir_llm_calls_total` | Filter by provider |
| `period` | `week` / `month` | Toggle between 7-day and 30-day windows |

### Dashboard Layout

#### Row 1 — Overview

| Panel | Type | Description |
|-------|------|-------------|
| **SLA Status** | Stat | 1 = healthy, 0 = breached (green/red) |
| **Expected Volume** | Stat | Rolling request volume used for budget estimation |
| **Error Budget Total** | Stat | Allowed errors (volume × SLO error rate) |
| **Allocated Cost Budget** | Stat | Configured cost budget for the period |

#### Row 2 — Status

| Panel | Type | Description |
|-------|------|-------------|
| **Total Requests** | Stat | Actual request volume in the period |
| **Total Errors** | Stat | Error count in the period |
| **Error Budget Remaining %** | Stat | Percentage of error budget still available |
| **Cost Incurred** | Stat | Total cost consumed in the period |
| **Cost Budget Remaining $** | Stat | Remaining cost budget (conditional green/amber/red) |

#### Row 3 — Burn & Exhaustion

| Panel | Type | Description |
|-------|------|-------------|
| **Error Budget — Days to Exhaustion** | Stat | Estimated days until error budget is depleted |
| **Cost Budget — Days to Exhaustion** | Stat | Estimated days until cost budget is depleted |
| **Error & Cost Budget Burn Rate** | Time series | Error burn rate (1h/6h) and cost burn rate on one chart |

#### Row 4 — Error Budget Breakdown

| Panel | Type | Description |
|-------|------|-------------|
| **Errors by Agent** | Pie chart | Error distribution across agents |
| **Errors by Model** | Pie chart | Error distribution across models |

#### Row 5 — Cumulative Volume

| Panel | Type | Description |
|-------|------|-------------|
| **Cumulative Volume** | Time series | Request volume over time |

#### Row 6 — Service Performance

| Panel | Type | Description |
|-------|------|-------------|
| **Avg Latency per Service** | Time series | Average span duration per service |
| **RPS per Service** | Time series | Requests per second per service |
| **Token Consumption Rate per Service** | Time series | Input/output token rate per service |
| **Cost Rate per Service** | Time series | Cost consumption rate per service |
| **P95/P99 Latency per Service** | Time series | Tail latency percentiles per service |

### Key Queries

```promql
# SLA status (1=healthy, 0=breached)
rastir:sla_status:$period{service=~"$service",env=~"$env",agent=~"$agent"}

# Error budget remaining %
100 - rastir:error_budget_consumed_pct:$period{...}

# Days to error budget exhaustion
rastir:error_days_to_exhaustion:$period{...}

# Error burn rate (1h window)
rastir:error_burn_rate:1h{...}

# Cost budget remaining
rastir:cost_budget_remaining:$period

# Service-level latency
sum by(service)(rate(rastir_duration_seconds_sum[5m]))
  / sum by(service)(rate(rastir_duration_seconds_count[5m]))
```

### Metrics Used

This dashboard queries **recording rules** (prefixed `rastir:`) rather than raw counters:

| Source | Type | Purpose |
|--------|------|--------|
| `rastir:sla_status:*` | Recording rule | SLA health indicator |
| `rastir:expected_volume:*` | Recording rule | Rolling request volume |
| `rastir:error_budget_total:*` | Recording rule | Allowed error count |
| `rastir:error_budget_remaining:*` | Recording rule | Remaining error count |
| `rastir:error_budget_consumed_pct:*` | Recording rule | Error budget consumed % |
| `rastir:error_days_to_exhaustion:*` | Recording rule | Days until error budget depleted |
| `rastir:error_burn_rate:*` | Recording rule | 1h/6h error burn rate |
| `rastir:cost:*` | Recording rule | Cost consumed in period |
| `rastir:cost_budget_remaining:*` | Recording rule | Remaining cost budget |
| `rastir:cost_days_to_exhaustion:*` | Recording rule | Days until cost budget depleted |
| `rastir_cost_budget_usd` | Config gauge | Allocated cost budget |
| `rastir_duration_seconds` | Histogram | Latency for service performance panels |
| `rastir_tokens_input_total` | Counter | Token consumption rate |
| `rastir_cost_total` | Counter | Cost consumption rate |
