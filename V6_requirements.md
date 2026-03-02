# Rastir V6 Requirements Specification  
## Cost Observability + Streaming TTFT Support

---

# 1. Overview

V6 introduces financial observability and streaming latency insight into Rastir.

This version adds:

- Cost calculation support (client-side)
- Cost-based Prometheus metrics (server-derived from spans)
- Streaming Time-To-First-Token (TTFT) measurement
- Pricing profile labeling (NOT pricing versioning)
- Budget monitoring hooks (optional)
- Grafana dashboard support for cost & TTFT

V6 does NOT introduce billing logic or pricing governance.

---

# 2. Cost Calculation Architecture

## 2.1 Cost Calculation Location

Cost MUST be calculated on the CLIENT at span finalization time.

Rationale:
- Client has model, provider, and token usage.
- Server remains pricing-agnostic.
- Avoids pricing drift across environments.

Cost MUST NOT be computed on server.

---

## 2.2 Pricing Registry (Client-Side Only)

A `PricingRegistry` MUST exist on the client.

Requirements:

- Lookup by provider + model
- Separate input and output token pricing
- Configurable override (file / env / inline)
- Graceful fallback if pricing missing

If pricing is missing:

- `span.attribute["pricing_missing"] = true`
- `cost_usd = 0`
- Increment metric: `rastir_pricing_missing_total`

The server MUST NOT contain pricing logic.

---

## 2.3 Pricing Profile Label

V6 introduces:

### `pricing_profile` (string)

Purpose:

- Indicates which pricing configuration was used at emission time
- Enables cost-shift analysis when pricing changes
- Keeps pricing governance outside Rastir

Examples:

- "default_2025_q1"
- "enterprise_contract_v2"
- "azure_discounted"
- "internal_estimate"

This value MUST be set via client configuration when cost calculation is enabled.

Rastir does NOT manage pricing lifecycle.

---

## 2.4 Required LLM Span Attributes

When cost calculation is enabled, LLM spans MUST include:

- `cost_usd` (float)
- `pricing_profile` (string)
- `streaming` (bool)
- `ttft_ms` (float, if streaming enabled)

---

# 3. Prometheus Metrics – Cost

## 3.1 Counter

### `rastir_cost_total`

Type: Counter

Labels:

- service
- env
- model
- provider
- agent
- pricing_profile

Purpose:

Tracks total accumulated USD cost.

Cardinality guard MUST apply to `pricing_profile`.

---

## 3.2 Histogram

### `rastir_cost_per_call_usd`

Type: Histogram

Labels:

- service
- env
- model

IMPORTANT:

`pricing_profile` MUST NOT be included in histogram labels  
(to prevent cardinality explosion).

### Default Buckets (logarithmic)

0.0001  
0.0005  
0.001  
0.002  
0.005  
0.01  
0.02  
0.05  
0.1  
0.2  
0.5  
1  
2  
5  
10  
20  
50  
100  

Bucket count MUST NOT exceed 20.

---

# 4. Streaming TTFT Support

## 4.1 Definition

TTFT = first_token_timestamp − request_start_timestamp

Measured in milliseconds.

Only applicable when `streaming=true`.

---

## 4.2 Client Responsibilities

Client MUST:

- Capture request start timestamp
- Detect first streamed chunk
- Compute `ttft_ms`
- Attach value before span finalization

TTFT measurement MUST NOT block streaming delivery.

---

## 4.3 Prometheus Metric

### `rastir_ttft_seconds`

Type: Histogram

Labels:

- service
- env
- model
- provider

### Default Buckets

0.05  
0.1  
0.2  
0.5  
1  
2  
5  
10  

---

# 5. Additional Metrics

### `rastir_pricing_missing_total`

Counter  
Incremented when pricing entry not found.

---

### `rastir_budget_exceeded_total` (Optional – V6.1)

Counter  
Incremented when per-call cost exceeds configured threshold.

---

# 6. Server Responsibilities

Server MUST:

- Accept `cost_usd`
- Accept `pricing_profile`
- Derive cost metrics
- Derive TTFT histogram
- Apply cardinality guards

Server MUST NOT:

- Compute pricing
- Store pricing logic
- Manage pricing lifecycle
- Recalculate cost

---

# 7. Configuration Additions (Client)

New configuration fields:

- `enable_cost_calculation` (bool)
- `pricing_profile` (string, required if cost enabled)
- `pricing_source` (file / env / inline)
- `enable_ttft` (bool)
- `max_cost_per_call_alert` (optional)

If `pricing_profile` not set, default to `"default"`.

---

# 8. Dashboard Requirements

Grafana dashboards MUST include:

- Cost per model
- Cost per agent
- Cost burn rate
- Cost P95 per call
- TTFT P95 per model
- TTFT trend over time
- Cost split by pricing_profile

---

# 9. Backward Compatibility

- If cost disabled → no cost metrics emitted
- If streaming disabled → no TTFT metrics emitted
- Existing spans remain valid
- No breaking changes to V5

---

# 10. Non-Goals

V6 does NOT include:

- Billing system
- Persistent cost storage beyond Prometheus
- Multi-currency support
- Cross-cloud pricing synchronization
- Server-side pricing enforcement

---

End of V6 Specification
