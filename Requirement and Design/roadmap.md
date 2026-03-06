# Rastir Roadmap

## Near-term (high impact, low effort)

### 1. Streaming token aggregation
Currently a known limitation. Accumulate tokens from async generators so `streaming=True` works fully with ReActAgent and other streaming workflows.

### 2. GitHub Actions CI
Automated `pytest` + `twine upload` on tag push. No CI exists yet.

### 3. Unit test coverage
E2E tests exist but unit test coverage is thin. Add unit tests for `wrapper.py`, `types.py`, adapters, and framework-specific support modules.

### 4. Pre-built alerting rules
Prometheus alerting rules alongside existing recording rules — error rate spikes, token budget exhaustion, latency P99 breaches.

---

## Medium-term (differentiation)

### 5. Prompt/template versioning
Track which prompt template + version produced each span, enabling A/B analysis in Grafana.

### 6. Retry & fallback tracking
Detect and annotate retries (same prompt, different model) and model fallbacks with dedicated span attributes.

### 7. Evaluation system extensions
**Already implemented:** Full evaluation pipeline exists — `@llm(evaluate=True, evaluation_types=[...])` stamps spans → server ingestion enqueues → `EvaluationWorkerPool` runs evaluators in thread pool → child evaluation spans emitted → Prometheus metrics recorded → Grafana dashboard. Two built-in LLM-as-judge evaluators: `ToxicityEvaluator` and `HallucinationEvaluator`.

**Remaining work:**
- Add more built-in evaluators (relevance, faithfulness, groundedness)
- Expose `EvaluatorRegistry` for user-defined custom evaluators (currently server-internal)
- External queue backend (Redis) for the `EvaluationQueue` protocol (only in-memory today)
- Export evaluation types (`EvaluationResult`, `Evaluator`) in public `__init__.py`

### 8. Sampling strategies
Head-based and tail-based sampling for high-volume production deployments where tracing every call is too expensive.

---

## Longer-term (ecosystem)

### 9. More frameworks
AutoGen, Semantic Kernel, Haystack, DSPy.

### 10. Log correlation
Inject `trace_id` into structured logging (structlog/loguru) so logs and traces link together.

### 11. User feedback correlation
API to attach thumbs-up/down or scores to a `trace_id` after the fact, visible in Grafana.

### 12. Grafana dashboard marketplace
Publish dashboards to grafana.com for one-click import.
