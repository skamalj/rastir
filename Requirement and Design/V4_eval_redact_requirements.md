
# Rastir — Requirements (V4: Evaluation + Redaction Architecture)

## 1. Objective

V4 introduces asynchronous evaluation at @llm points while maintaining:

- Non-blocking ingestion
- Horizontal scalability
- Clean separation of concerns
- Prometheus + Tempo + Grafana compatibility

Redaction is defined as a server-side telemetry sanitation stage
and is completely independent of evaluation logic.

---

# 2. High-Level Architecture

Pipeline:

1. Span ingestion
2. Sampling decision
3. Trace Redaction (if enabled)
4. Store / Export span
5. Evaluation enqueue (if enabled + sampled)
6. Evaluation worker execution
7. Emit evaluation span

Redaction NEVER runs inside the evaluation worker.

---

# 3. Trace Redaction Requirements

## 3.1 Scope

Redaction applies ONLY to:

- prompt_text
- completion_text

Redaction does NOT apply to:

- metric labels
- model/provider metadata
- token counts
- trace identifiers
- span IDs

## 3.2 Execution Order

Redaction must occur:

- After sampling
- Before span storage
- Before OTEL export
- Before evaluation enqueue

If span is not sampled → no redaction is performed.

## 3.3 Redactor Interface

    class Redactor(Protocol):
        def redact(self, text: str, context: RedactionContext) -> str

Redaction must be:

- Synchronous
- CPU-bound
- Deterministic
- Side-effect free

## 3.4 RedactionContext

    @dataclass
    class RedactionContext:
        service: str
        env: str
        model: str | None
        provider: str | None

## 3.5 Default Implementation

Ship RegexRedactor with:

- Email masking
- Phone masking
- SSN masking
- Credit card masking
- Custom regex support

## 3.6 Payload Guard

Redactor must enforce:

- max_text_length

If exceeded:

- Truncate text
- Append "[TRUNCATED]"

## 3.7 Failure Handling

If redaction fails:

- Increment rastir_redaction_failures_total
- Log error
- Drop span (safe default)

Raw text must never pass silently after redaction failure.

---

# 4. Evaluation Engine Requirements

Evaluation is asynchronous and optional.

Redaction is NOT part of evaluation.

Evaluation consumes already-sanitized span data.

## 4.1 Decorator Extension

    @llm(evaluate=True)

Optional parameters:

- evaluation_types: list[str]
- evaluation_sample_rate: float
- evaluation_timeout_ms: int

Decorator embeds evaluation config into span attributes.

## 4.2 Evaluator Interface

    class Evaluator(Protocol):
        name: str
        def evaluate(self, task: EvaluationTask) -> EvaluationResult

Evaluation must be:

- Pluggable
- Backend-agnostic
- Synchronous in V4

## 4.3 Evaluation Types

- Extensible registry
- Cardinality capped (max 20 types)
- Built-in defaults:
    - toxicity
    - hallucination

Faithfulness (RAG-dependent) deferred to V5.

## 4.4 EvaluationTask Schema

Contains:

- trace_id
- parent_span_id
- service
- env
- model
- provider
- agent
- prompt_text (already redacted if enabled)
- completion_text (already redacted if enabled)
- evaluation_types

## 4.5 Evaluation Queue

- Separate bounded in-memory queue
- Configurable size
- Drop policy when full

Metrics:

- rastir_evaluation_queue_size
- rastir_evaluation_dropped_total

## 4.6 Worker Execution

- ThreadPoolExecutor-based
- Configurable concurrency
- Timeout enforcement per evaluation type
- Isolation from ingestion thread

Evaluation must never block ingestion.

## 4.7 Trace Correlation

Evaluation spans must:

- Use same trace_id as original span
- parent_span_id = original LLM span_id
- span_type = "evaluation"

Evaluation span attributes:

- evaluation_type
- score
- status

## 4.8 Evaluation Metrics

Expose:

- rastir_evaluation_runs_total
- rastir_evaluation_failures_total
- rastir_evaluation_latency_seconds
- rastir_evaluation_score

Labels:

- service
- env
- model
- provider
- evaluation_type

No high-cardinality labels allowed.

---

# 5. Sampling Rules

Sampling order:

1. Ingestion sampling
2. If sampled → redaction
3. If evaluation enabled → evaluation sampling
4. If evaluation sampled → enqueue task

Evaluation sampling must not affect trace sampling.

---

# 6. Deployment Scope (V4)

- Same-process evaluation worker only
- In-memory queue only
- No Redis dependency
- No external evaluation service
- No distributed evaluation workers

---

# 7. Non-Goals (V4)

- No policy enforcement engine
- No dataset storage
- No UI for evaluation feedback
- No distributed trace store
- No RAG faithfulness evaluation

---

# 8. Strategic Outcome

V4 positions Rastir as:

- AI observability layer
- Guardrail-aware analytics engine
- Async evaluation-capable
- PII-safe via centralized trace redaction
- Production-ready within Prom + Tempo + Grafana stack

End of V4 Requirements.
