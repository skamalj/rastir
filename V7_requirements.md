
# Rastir V7 — SRE Layer Requirements
## (SLO / SLA / Error Budget / Cost Budget / Burn Rate)

---

# 1. Scope

V7 introduces a server-side SRE Engine responsible for:

- SLO tracking (weekly + monthly calendar windows)
- SLA evaluation
- Error budget computation
- Cost budget computation
- Burn rate monitoring
- Budget exhaustion projection
- Exposure of derived Prometheus metrics for Grafana + Alerting

This layer MUST run inside the Rastir server and expose additional Prometheus metrics.
No SRE logic is allowed in the client.

---

# 2. Period Model

Two SLA periods must be supported:

- period="week"  → Calendar week (EOW reset)
- period="month" → Calendar month (EOM reset)

Expected volume MUST be computed using rolling history:

- Weekly → rolling last 7 days
- Monthly → rolling last 30 days

SLA measurement window = calendar-based.
Expected volume estimation window = rolling-based.

These MUST remain independent.

---

# 3. Agent as Deployment Unit

All SRE metrics MUST include:

- service
- env
- agent
- period (where applicable)

Agent is considered the unit of deployment and SLO boundary.

Cardinality guard MUST apply to agent label.

---

# 4. Expected Volume Metrics

## 4.1 rastir_expected_volume
Type: Gauge

Definition:
Rolling request volume used to estimate allowed budget.

Weekly:
Sum of requests over last 7 days.

Monthly:
Sum of requests over last 30 days.

Labels:
service, env, agent, period

Dependency:
- Requires base request counter metric (e.g., rastir_spans_ingested_total or dedicated request counter).
- System must ensure request counter exists per agent.

---

# 5. Error Budget Metrics

## 5.1 rastir_error_budget_total
Type: Gauge

Definition:
Total allowed errors for current period.

Logic:
rastir_expected_volume × configured_slo_error_rate

Labels:
service, env, agent, period

Dependencies:
- rastir_expected_volume
- Configured SLO error rate (per agent)


## 5.2 rastir_error_budget_remaining
Type: Gauge

Definition:
Remaining allowed errors for the current SLA period.

Logic:
rastir_error_budget_total − observed_period_errors

Labels:
service, env, agent, period

Dependencies:
- rastir_error_budget_total
- rastir_errors_total (period-scoped)


## 5.3 rastir_error_budget_consumed_percent
Type: Gauge

Definition:
Percentage of error budget consumed.

Logic:
(observed_period_errors / rastir_error_budget_total) × 100

Labels:
service, env, agent, period

Dependencies:
- rastir_error_budget_total
- rastir_errors_total

---

# 6. Error Burn Rate Metrics

## 6.1 rastir_error_burn_rate_short
Type: Gauge

Definition:
Short-window burn rate.

Logic:
(error_rate_last_1h / slo_error_rate)

Labels:
service, env, agent

Dependencies:
- rastir_errors_total (1h window)
- request counter (1h window)
- Configured SLO error rate


## 6.2 rastir_error_burn_rate_long
Type: Gauge

Definition:
Long-window burn rate.

Logic:
(error_rate_last_6h / slo_error_rate)

Labels:
service, env, agent

Dependencies:
- rastir_errors_total (6h window)
- request counter (6h window)
- Configured SLO error rate

Burn windows MUST be fixed at:
- Short = 1 hour
- Long = 6 hours

---

# 7. Cost Budget Metrics

## 7.1 rastir_cost_budget_total
Type: Gauge

Definition:
Configured cost budget for the current period.

Labels:
service, env, agent, period

Dependency:
- Configured cost budget per agent


## 7.2 rastir_cost_budget_remaining
Type: Gauge

Definition:
Remaining cost budget.

Logic:
cost_budget_total − period_cost

Labels:
service, env, agent, period

Dependency:
- rastir_cost_total


## 7.3 rastir_cost_budget_consumed_percent
Type: Gauge

Definition:
Percent of cost budget consumed.

Logic:
(period_cost / cost_budget_total) × 100

Labels:
service, env, agent, period

Dependency:
- rastir_cost_total


## 7.4 rastir_cost_burn_rate_daily
Type: Gauge

Definition:
Average cost burn per elapsed day in current period.

Logic:
period_cost / elapsed_days

Labels:
service, env, agent, period

Dependency:
- rastir_cost_total
- Period start timestamp


## 7.5 rastir_cost_days_to_exhaustion
Type: Gauge

Definition:
Estimated days until cost budget exhaustion.

Logic:
cost_budget_remaining / daily_burn_rate

Labels:
service, env, agent, period

Dependencies:
- rastir_cost_budget_remaining
- rastir_cost_burn_rate_daily

---

# 8. Exhaustion Projection Metrics

## 8.1 rastir_error_days_to_exhaustion
Type: Gauge

Definition:
Estimated days until error budget exhaustion.

Logic:
error_budget_remaining / daily_error_rate

Labels:
service, env, agent, period

Dependencies:
- rastir_error_budget_remaining
- period error rate
- elapsed_days

---

# 9. SLA Status Metric

## 9.1 rastir_sla_status
Type: Gauge (0 or 1)

Definition:
Current SLA health indicator.

Logic:
0 if rastir_error_budget_remaining ≤ 0
1 otherwise

Labels:
service, env, agent, period

Dependency:
- rastir_error_budget_remaining

---

# 10. Hard Constraints

1. All SRE metrics MUST be Gauges.
2. No histograms allowed in SRE layer.
3. period label must be enum: week, month.
4. agent label MUST be deployment-level only (not user/session).
5. Cardinality caps MUST apply to agent label.
6. All calculations MUST occur in server SREEngine.
7. Metrics update interval MUST be fixed (e.g., 60 seconds).
8. Rolling windows for estimation:
   - 7d for weekly
   - 30d for monthly
9. Burn rate windows fixed:
   - 1h short
   - 6h long
10. No additional labels allowed unless explicitly added to this document.

---

# 11. Required Existing Metrics

The following base metrics MUST already exist in the system:

- rastir_spans_ingested_total OR dedicated request counter
- rastir_errors_total
- rastir_cost_total
- Period start tracking capability

If any of these are missing, they MUST be implemented before enabling V7.

---

# 12. Architectural Location

Implementation MUST reside in:

rastir/server/sre_engine.py

SREEngine must:
- Read existing counters
- Maintain rolling calculations
- Update derived gauges
- Expose via /metrics endpoint

Client layer MUST remain unaware of SRE logic.

---

End of V7 SRE Requirements.
