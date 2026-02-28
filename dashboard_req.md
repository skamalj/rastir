# Rastir --- Grafana Dashboard Requirements (V4)

This document defines the mandatory Grafana dashboards and alerting
requirements for Rastir V4.

Dashboards must work with Prometheus as the metrics backend and Tempo
for trace correlation.

  -----------------------------
  1\. SYSTEM HEALTH DASHBOARD
  -----------------------------

Purpose: Monitor Rastir server stability and resource pressure.

Panels:

Ingestion: - rate(rastir_spans_ingested_total\[5m\]) -
rastir_queue_size - rastir_queue_utilization_percent -
rastir_ingestion_rate

Memory: - rastir_memory_bytes - rastir_trace_store_size -
rastir_active_traces

Backpressure: - rate(rastir_ingestion_rejections_total\[5m\]) -
rate(rastir_spans_dropped_by_backpressure_total\[5m\]) -
rate(rastir_backpressure_warnings_total\[5m\])

Export Health: - rate(rastir_export_failures_total\[5m\])

  -------------------------------
  2\. LLM PERFORMANCE DASHBOARD
  -------------------------------

Purpose: Track latency, cost, and reliability per model.

Panels:

LLM Throughput: - rate(rastir_llm_calls_total\[5m\]) by (model,
provider)

Latency (p50 / p95 / p99): - histogram_quantile(0.50,
sum(rate(rastir_duration_seconds_bucket{span_type="llm"}\[5m\])) by (le,
model)) - histogram_quantile(0.95, ...) - histogram_quantile(0.99, ...)

Token Usage: - rate(rastir_tokens_input_total\[5m\]) by (model) -
rate(rastir_tokens_output_total\[5m\]) by (model)

Tokens Per Call Distribution: - histogram_quantile(0.95,
rastir_tokens_per_call_bucket)

Error Rate: - rate(rastir_errors_total{span_type="llm"}\[5m\]) by
(error_type)

  ----------------------------
  3\. AGENT & TOOL DASHBOARD
  ----------------------------

Purpose: Monitor agent orchestration and tool usage.

Panels:

Agent Activity: - rate(rastir_llm_calls_total\[5m\]) by (agent) -
rate(rastir_tool_calls_total\[5m\]) by (agent)

Tool Usage: - rate(rastir_tool_calls_total\[5m\]) by (tool_name)

Retrieval Activity: - rate(rastir_retrieval_calls_total\[5m\]) by
(agent)

Agent Error Rate: - rate(rastir_errors_total\[5m\]) by (span_type)

  --------------------------
  4\. EVALUATION DASHBOARD
  --------------------------

Purpose: Track evaluation throughput, latency, and quality scores.

Panels:

Evaluation Throughput: - rate(rastir_evaluation_runs_total\[5m\]) by
(evaluation_type)

Evaluation Failures: - rate(rastir_evaluation_failures_total\[5m\])

Evaluation Latency: - histogram_quantile(0.50,
rastir_evaluation_latency_seconds_bucket) - histogram_quantile(0.95,
rastir_evaluation_latency_seconds_bucket)

Evaluation Score: - avg(rastir_evaluation_score) by (evaluation_type,
model)

Evaluation Queue Health: - rastir_evaluation_queue_size -
rate(rastir_evaluation_dropped_total\[5m\])

  ----------------------------------------------------
  5\. GUARDRAIL DASHBOARD (OPTIONAL BUT RECOMMENDED)
  ----------------------------------------------------

Purpose: Monitor guardrail interventions and violations.

Panels:

Guardrail Usage: - rate(rastir_guardrail_requests_total\[5m\]) by
(provider, guardrail_id)

Guardrail Violations: - rate(rastir_guardrail_violations_total\[5m\]) by
(guardrail_category)

Violation Rate by Model: - rate(rastir_guardrail_violations_total\[5m\])
by (model)

  ----------------------------------
  6\. REQUIRED DASHBOARD VARIABLES
  ----------------------------------

Each dashboard must support filter variables:

-   \$service
-   \$env
-   \$model
-   \$provider
-   \$agent

Dashboards must be multi-tenant ready via label filtering.

  ---------------------------
  7\. ALERTING REQUIREMENTS
  ---------------------------

The following alert rules must be defined in Prometheus:

-   Queue utilization \> 80%
-   Evaluation queue drops \> 0 sustained over 5m
-   OTEL export failures sustained \> threshold
-   LLM error rate spike
-   Evaluation failure spike

Alerts must integrate with standard alertmanager workflows.

  -----------------------------------
  8\. TRACE CORRELATION REQUIREMENT
  -----------------------------------

Where exemplars are enabled:

-   Duration and evaluation metrics must expose trace_id exemplars
-   Grafana panels must support click-through to Tempo traces

  --------------------
  9\. NON-GOALS (V4)
  --------------------

-   No custom Grafana plugins
-   No UI customization layer
-   No embedded dashboard server inside Rastir

Dashboards are delivered as Grafana JSON templates.

End of Dashboard Requirements.
