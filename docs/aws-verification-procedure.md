# AWS ECS Dashboard Verification Procedure

Repeatable procedure to deploy Rastir on AWS ECS Fargate and verify all
Grafana dashboard panels have data.

## Prerequisites

- AWS credentials configured (`ap-south-1`)
- `conda activate llmobserve`
- API keys set: `GOOGLE_API_KEY` (or `GEMINI_API_KEY`), AWS Bedrock access
- Terraform installed

## Part One: Deploy & Basic Verification

### 1. Deploy Infrastructure

```bash
# Network
cd deploy/terraform/aws
terraform apply -target=module.network -auto-approve

# ECS services (rastir-server, prometheus, grafana, otel-collector)
terraform apply -auto-approve
```

### 2. Set Up Port Forwards

```bash
# In separate terminals / background
aws ecs execute-command --cluster rastir \
  --task <TASK_ID> --container rastir-server \
  --interactive --command "/bin/sh -c 'sleep 3600'" &
# Use SSM port forwarding or socat for ports:
#   8080 → rastir-server
#   3000 → grafana
#   9090 → prometheus
```

Alternatively, use the deploy script's port-forward commands.

### 3. Verify Infrastructure

```bash
# Recording rules loaded
curl -s http://localhost:9090/api/v1/rules | python3 -c "
import json,sys; d=json.load(sys.stdin)
groups=d['data']['groups']
rules=sum(len(g['rules']) for g in groups)
print(f'{len(groups)} rule groups, {rules} rules')
"

# Dashboards auto-imported
curl -s -u admin:admin http://localhost:3000/api/search | python3 -c "
import json,sys; print(f\"{len(json.load(sys.stdin))} dashboards\")
"

# Rastir metrics flowing
curl -s 'http://localhost:9090/api/v1/label/__name__/values' | python3 -c "
import json,sys; names=json.load(sys.stdin)['data']
rastir=[n for n in names if n.startswith('rastir_')]
print(f'{len(rastir)} rastir metrics')
"
```

### 4. Run Basic E2E Test

```bash
conda run -n llmobserve PYTHONPATH=src python tests/e2e/test_langgraph_e2e.py
```

### 5. Run Dashboard Verification (Baseline)

```bash
conda run -n llmobserve python scripts/verify_dashboards.py
```

Expected: ~62/74 PASS (84%). SRE Budgets should be 20/20.

---

## Part Two: Fill TTFT + rate() Gaps

### 6. Run Part Two Volume Tests

```bash
conda run -n llmobserve PYTHONPATH=src python tests/e2e/run_part2_verification.py
```

This runs:
- **Strands streaming TTFT** (5 prompts × 2 rounds) → `rastir_ttft_seconds_bucket`
- **LangGraph success requests** (5 prompts × 2 rounds) → rate() data
- **Strands + LangGraph basic e2e** → additional spans
- **Error generation** (5 scenarios) → error rate panels
- 20s pause between rounds for Prometheus scrape

### 7. Wait for Prometheus Scrape

```bash
sleep 30
```

### 8. Re-verify Dashboards

```bash
conda run -n llmobserve python scripts/verify_dashboards.py
```

TTFT panels (3) and rate() panels should now pass.

### Accepted No-Data Panels

These sparse error counters will show "no data" unless specific failure
conditions occur. They are safe to leave:
- `rastir_evaluation_dropped_total`
- `rastir_ingestion_rejections_total`
- `rastir_export_failures_total`
- `rastir_redaction_failures_total`

---

## Teardown

```bash
cd deploy/terraform/aws
terraform destroy -auto-approve               # ECS services
terraform destroy -target=module.network -auto-approve  # VPC
```

## Scripts Reference

| Script | Purpose |
|--------|---------|
| `scripts/verify_dashboards.py` | Query every Grafana panel via Prometheus |
| `tests/e2e/test_strands_streaming_ttft.py` | Strands streaming for TTFT data |
| `tests/e2e/run_part2_verification.py` | Orchestrate all Part Two tests |
| `tests/e2e/run_success_requests.py` | LangGraph volume (5 requests) |
| `tests/e2e/generate_errors.py` | Error spans across frameworks |
| `tests/e2e/test_strands_e2e.py` | Basic Strands e2e |
| `tests/e2e/test_langgraph_e2e.py` | Basic LangGraph e2e |
