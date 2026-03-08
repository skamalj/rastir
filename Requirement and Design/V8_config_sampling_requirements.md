# V8 Requirements — External Configuration & Sampling

## 1. External Configuration via JSON Deploy Config

### Problem
- Server config YAML is baked into the Docker image at build time
- Complex nested config (per-agent SLOs, custom redaction patterns) cannot be overridden via environment variables
- Current `config.env` uses a flat KEY=VALUE format that can't express nested structures
- Changing any config requires rebuilding and redeploying the image

### Requirements

#### 1.1 JSON-based deploy config file
- Replace the flat `config.env` with a structured JSON config file (`config.json`)
- The file must have three top-level sections:
  - `deploy` — infrastructure/CloudFormation parameters (region, VPC, subnets, CPU, memory, image, AMP endpoint, etc.)
  - `server` — rastir server configuration, mirroring the structure of `rastir-server-config.yaml`
  - `secrets` — secret references (Secrets Manager ARNs, SSM Parameter Store ARNs)

#### 1.2 deploy.sh flattening
- `deploy.sh` must read `config.json` and:
  - Extract `deploy` section → CloudFormation stack parameters
  - Flatten `server` section into `RASTIR_SERVER_*` environment variables:
    - Leaf scalar values → `RASTIR_SERVER_{SECTION}_{KEY}=value` (e.g., `RASTIR_SERVER_EVALUATION_ENABLED=true`)
    - Complex values (dicts, lists) → `RASTIR_SERVER_{SECTION}_{KEY}_JSON='<json>'` (e.g., `RASTIR_SERVER_SRE_AGENTS_JSON='{"agent1":{"slo_error_rate":0.02}}'`)
  - Extract `secrets` section → ECS task definition `secrets` entries with `valueFrom`
- All env vars are injected into the ECS container definition via CloudFormation

#### 1.3 Server config loader changes
- `load_config()` in `src/rastir/server/config.py` must support `*_JSON` env vars:
  - For each config field that can be a dict or list, check if a corresponding `RASTIR_SERVER_{SECTION}_{KEY}_JSON` env var exists
  - If it exists, `json.loads()` it and use the result
  - Priority remains: **env var > YAML file > hardcoded default**
- Affected fields:
  - `sre.agents` → `RASTIR_SERVER_SRE_AGENTS_JSON`
  - `redaction.custom_patterns` → `RASTIR_SERVER_REDACTION_CUSTOM_PATTERNS_JSON`
  - Any future complex config fields

#### 1.4 Backward compatibility
- Existing flat `RASTIR_SERVER_*` env vars must continue to work
- If no `config.json` is provided, `deploy.sh` should fall back to `config.env` (if present)
- The baked-in YAML in the Docker image remains the base — env vars override it

#### 1.5 Kubernetes compatibility
- The same `config.json` structure should work for Kubernetes deployments
- A separate script or Helm values template can read `config.json` and produce a ConfigMap + Secret

---

## 2. Probabilistic Sampling

### Problem
- Exporting all traces to the trace backend (X-Ray/Tempo) is expensive at scale
- Users need to control the volume of exported traces
- Evaluation (LLM judge) costs scale with trace volume and must be bounded
- Exemplars must only link to traces that exist in the trace backend

### Requirements

#### 2.1 Sampling configuration
- Single user-configurable knob:
  ```yaml
  sampling:
    rate: 0.10                # 10% of traces exported (0.0 to 1.0)
  ```
- `rate` — probability that any given trace is sampled. Default: `1.0` (export all, sampling disabled)
- Configurable via env var: `RASTIR_SERVER_SAMPLING_RATE`

#### 2.2 Sampling algorithm — simple probabilistic
- For each trace, roll `random() < sampling_rate`:
  - **Yes** → trace is **sampled** (exported, eligible for evaluation and exemplars)
  - **No** → trace is **dropped** (not exported, not evaluated, no exemplar)
- Stateless — no windows, no counters, no buffering
- Statistically converges to the configured rate over time
- Decision is made per trace (by trace_id) — all spans in a sampled trace are exported, all spans in a dropped trace are dropped

#### 2.3 Sampling drives evaluation (non-errors only)
- If evaluation is enabled, only **sampled non-error traces** are sent to the evaluation pipeline (LLM judge)
- Error traces are **never evaluated** — they failed, there is nothing to judge for quality
- This naturally limits evaluation volume to the sampling rate, preventing excessive LLM judge costs

#### 2.4 Sampling drives exemplars
- Exemplars (trace_id links attached to Prometheus metrics) are only generated for **sampled traces**
- This ensures that exemplar trace_ids always point to traces that actually exist in the trace backend
- Without this, exemplars could link to traces that were dropped by sampling — producing dead links in Grafana

#### 2.5 Metrics are never sampled
- Prometheus metrics (counters, histograms, SRE budgets) are computed from **ALL traces**, regardless of sampling
- Sampling only controls what is exported to the trace backend, evaluated, and linked via exemplars
- This guarantees metric accuracy is never compromised by sampling

#### 2.6 Backward compatibility
- Default `rate: 1.0` means all traces are exported — existing behavior preserved
- Sampling is fully opt-in
- If a trace is dropped, **all spans** in that trace are dropped

#### 2.7 Backward compatibility
- Default `rate: 1.0` means all traces are exported (no sampling) — existing behavior preserved
- Sampling is fully opt-in
