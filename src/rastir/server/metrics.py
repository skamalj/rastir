"""Prometheus metrics derived from ingested spans.

Creates and manages a dedicated ``CollectorRegistry`` so that the
server's own metrics don't collide with the default global registry
(useful in testing and when embedding with other Prometheus
instrumentations).

Metrics are updated synchronously by the ingestion worker — every call
to ``record_span()`` increments the relevant counters/histograms in
O(1) time with no disk I/O.
"""

from __future__ import annotations

import logging
import resource
import time
from typing import Optional

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    openmetrics,
)

logger = logging.getLogger("rastir.server")

# Overflow sentinel for cardinality-guarded labels
_OVERFLOW_SENTINEL = "__cardinality_overflow__"

# Per-dimension cardinality defaults (V2)
_DEFAULT_CARDINALITY_CAPS: dict[str, int] = {
    "model": 50,
    "provider": 10,
    "tool_name": 200,
    "agent": 200,
    "error_type": 50,
    "guardrail_id": 100,
    "pricing_profile": 20,
}

# Default histogram buckets optimised for LLM workloads
_DEFAULT_DURATION_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0)
_DEFAULT_TOKENS_BUCKETS = (10, 50, 100, 250, 500, 1000, 2000, 4000, 8000, 16000, 32000)
_DEFAULT_COST_BUCKETS = (
    0.0001, 0.0005, 0.001, 0.002, 0.005, 0.01, 0.02, 0.05,
    0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100,
)
_DEFAULT_TTFT_BUCKETS = (0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10)
_MAX_BUCKET_COUNT = 20

# Canonical span types — anything else is mapped to "system"
_VALID_SPAN_TYPES = frozenset({"agent", "llm", "tool", "retrieval", "evaluation", "system", "infra"})

# Bounded enums for guardrail labels (defence-in-depth; adapter also validates)
_VALID_GUARDRAIL_CATEGORIES = frozenset({
    "CONTENT_POLICY",
    "SENSITIVE_INFORMATION_POLICY",
    "WORD_POLICY",
    "TOPIC_POLICY",
    "CONTEXTUAL_GROUNDING_POLICY",
    "DENIED_TOPIC",
})

_VALID_GUARDRAIL_ACTIONS = frozenset({
    "GUARDRAIL_INTERVENED",
    "NONE",
})

# --------------------------------------------------------------------------
# Error-type normalisation: raw exception → fixed category
# --------------------------------------------------------------------------

_ERROR_TYPE_MAP: dict[str, str] = {
    # Timeout variants
    "TimeoutError": "timeout",
    "asyncio.TimeoutError": "timeout",
    "httpx.TimeoutException": "timeout",
    "httpx.ReadTimeout": "timeout",
    "httpx.ConnectTimeout": "timeout",
    "requests.exceptions.Timeout": "timeout",
    "openai.APITimeoutError": "timeout",
    # Rate-limit variants
    "RateLimitError": "rate_limit",
    "openai.RateLimitError": "rate_limit",
    "anthropic.RateLimitError": "rate_limit",
    # Validation variants
    "ValueError": "validation_error",
    "TypeError": "validation_error",
    "ValidationError": "validation_error",
    "pydantic.ValidationError": "validation_error",
    # Provider errors
    "openai.APIError": "provider_error",
    "openai.APIConnectionError": "provider_error",
    "openai.APIStatusError": "provider_error",
    "anthropic.APIError": "provider_error",
    "anthropic.APIConnectionError": "provider_error",
    "anthropic.APIStatusError": "provider_error",
    "botocore.exceptions.ClientError": "provider_error",
    # Internal
    "RuntimeError": "internal_error",
    "Exception": "internal_error",
}

# Allowed normalised error categories
_VALID_ERROR_TYPES = frozenset(
    {"timeout", "rate_limit", "validation_error", "provider_error", "internal_error", "unknown"}
)


class MetricsRegistry:
    """In-process Prometheus metric registry for LLM observability.

    All metrics are scoped under the ``rastir_`` prefix.
    """

    def __init__(
        self,
        max_label_value_length: int = 128,
        cardinality_caps: dict[str, int] | None = None,
        duration_buckets: tuple[float, ...] | None = None,
        tokens_buckets: tuple[float, ...] | None = None,
        exemplars_enabled: bool = False,
    ) -> None:
        self._max_label_len = max_label_value_length
        self._registry = CollectorRegistry()
        self._exemplars_enabled = exemplars_enabled

        # Per-dimension cardinality caps (merge user overrides over defaults)
        caps = dict(_DEFAULT_CARDINALITY_CAPS)
        if cardinality_caps:
            caps.update(cardinality_caps)
        self._cardinality_caps = caps

        # Histogram buckets (validate ≤ _MAX_BUCKET_COUNT)
        dur_b = duration_buckets or _DEFAULT_DURATION_BUCKETS
        tok_b = tokens_buckets or _DEFAULT_TOKENS_BUCKETS
        if len(dur_b) > _MAX_BUCKET_COUNT:
            raise ValueError(
                f"duration_buckets has {len(dur_b)} entries; maximum is {_MAX_BUCKET_COUNT}"
            )
        if len(tok_b) > _MAX_BUCKET_COUNT:
            raise ValueError(
                f"tokens_buckets has {len(tok_b)} entries; maximum is {_MAX_BUCKET_COUNT}"
            )
        self._duration_buckets = dur_b
        self._tokens_buckets = tok_b

        # ---- Counters ----
        self.spans_ingested = Counter(
            "rastir_spans_ingested_total",
            "Total spans ingested by type",
            ["service", "env", "span_type", "status"],
            registry=self._registry,
        )

        self.llm_calls = Counter(
            "rastir_llm_calls_total",
            "Total LLM calls by model and provider",
            ["service", "env", "model", "provider", "agent"],
            registry=self._registry,
        )

        self.tokens_input = Counter(
            "rastir_tokens_input_total",
            "Total input tokens",
            ["service", "env", "model", "provider", "agent"],
            registry=self._registry,
        )

        self.tokens_output = Counter(
            "rastir_tokens_output_total",
            "Total output tokens",
            ["service", "env", "model", "provider", "agent"],
            registry=self._registry,
        )

        self.tool_calls = Counter(
            "rastir_tool_calls_total",
            "Total tool invocations",
            ["service", "env", "tool_name", "agent", "model", "provider"],
            registry=self._registry,
        )

        self.retrieval_calls = Counter(
            "rastir_retrieval_calls_total",
            "Total retrieval operations",
            ["service", "env", "agent", "model", "provider"],
            registry=self._registry,
        )

        self.errors = Counter(
            "rastir_errors_total",
            "Total spans that finished with error status",
            ["service", "env", "span_type", "error_type", "model", "provider", "agent"],
            registry=self._registry,
        )

        # ---- Histograms ----
        self.duration = Histogram(
            "rastir_duration_seconds",
            "Span duration in seconds",
            ["service", "env", "span_type", "model", "provider"],
            buckets=self._duration_buckets,
            registry=self._registry,
        )

        self.tokens_per_call = Histogram(
            "rastir_tokens_per_call",
            "Total tokens (input+output) per LLM call",
            ["service", "env", "model", "provider"],
            buckets=self._tokens_buckets,
            registry=self._registry,
        )

        # ---- Cost metrics (V6) ----
        self.cost_total = Counter(
            "rastir_cost_total",
            "Total accumulated USD cost by model/provider/pricing_profile",
            ["service", "env", "model", "provider", "agent", "pricing_profile"],
            registry=self._registry,
        )

        self.cost_per_call = Histogram(
            "rastir_cost_per_call_usd",
            "Cost per LLM call in USD",
            ["service", "env", "model"],
            buckets=_DEFAULT_COST_BUCKETS,
            registry=self._registry,
        )

        self.pricing_missing = Counter(
            "rastir_pricing_missing_total",
            "Total LLM calls where pricing entry was not found",
            ["service", "env", "model", "provider"],
            registry=self._registry,
        )

        # ---- TTFT metric (V6) ----
        self.ttft = Histogram(
            "rastir_ttft_seconds",
            "Time-To-First-Token for streaming LLM calls",
            ["service", "env", "model", "provider"],
            buckets=_DEFAULT_TTFT_BUCKETS,
            registry=self._registry,
        )

        # ---- Server-internal counters ----
        # ---- Guardrail counters ----
        self.guardrail_requests = Counter(
            "rastir_guardrail_requests_total",
            "Total LLM calls with guardrail configuration enabled",
            ["service", "env", "provider", "model", "agent", "guardrail_id", "guardrail_version"],
            registry=self._registry,
        )

        self.guardrail_violations = Counter(
            "rastir_guardrail_violations_total",
            "Total guardrail interventions (violations)",
            ["service", "env", "provider", "model", "agent",
             "guardrail_id", "guardrail_action", "guardrail_category"],
            registry=self._registry,
        )

        self._seen_guardrail_ids: set[str] = set()

        self.ingestion_rejections = Counter(
            "rastir_ingestion_rejections_total",
            "Total spans rejected due to full queue",
            ["service", "env"],
            registry=self._registry,
        )

        self.export_failures = Counter(
            "rastir_export_failures_total",
            "Total OTLP export failures",
            ["service", "env"],
            registry=self._registry,
        )

        # ---- Gauges ----
        self.queue_size = Gauge(
            "rastir_queue_size",
            "Current number of span batches in the ingestion queue",
            registry=self._registry,
        )

        self.queue_utilization = Gauge(
            "rastir_queue_utilization_percent",
            "Queue utilization as a percentage of max capacity",
            registry=self._registry,
        )

        self.memory_bytes = Gauge(
            "rastir_memory_bytes",
            "Resident memory usage of the server process in bytes",
            registry=self._registry,
        )

        self.trace_store_size = Gauge(
            "rastir_trace_store_size",
            "Total number of spans currently held in the trace store",
            registry=self._registry,
        )

        self.active_traces = Gauge(
            "rastir_active_traces",
            "Number of distinct traces currently in the trace store",
            registry=self._registry,
        )

        # ---- Sampling counters ----
        self.spans_sampled = Counter(
            "rastir_spans_sampled_total",
            "Total spans retained for storage/export after sampling",
            ["service", "env"],
            registry=self._registry,
        )

        self.spans_dropped_by_sampling = Counter(
            "rastir_spans_dropped_by_sampling_total",
            "Total spans dropped by sampling (metrics still recorded)",
            ["service", "env"],
            registry=self._registry,
        )

        # ---- Backpressure counters/gauges ----
        self.backpressure_warnings = Counter(
            "rastir_backpressure_warnings_total",
            "Total times queue exceeded the soft backpressure threshold",
            registry=self._registry,
        )

        self.spans_dropped_by_backpressure = Counter(
            "rastir_spans_dropped_by_backpressure_total",
            "Total span batches evicted from queue head in drop_oldest mode",
            registry=self._registry,
        )

        # ---- Redaction counters ----
        self.redaction_applied = Counter(
            "rastir_redaction_applied_total",
            "Total spans where redaction was successfully applied",
            ["service", "env"],
            registry=self._registry,
        )

        self.redaction_failures = Counter(
            "rastir_redaction_failures_total",
            "Total redaction failures (span dropped if drop_on_failure=True)",
            ["service", "env"],
            registry=self._registry,
        )

        self.ingestion_rate = Gauge(
            "rastir_ingestion_rate",
            "Approximate span ingestion rate (spans per second)",
            registry=self._registry,
        )

        # ---- Evaluation metrics ----
        _eval_labels = ["service", "env", "model", "provider", "evaluation_type",
                        "evaluator_model", "evaluator_provider"]

        self.evaluation_runs = Counter(
            "rastir_evaluation_runs_total",
            "Total evaluation runs by type",
            _eval_labels,
            registry=self._registry,
        )

        self.evaluation_failures = Counter(
            "rastir_evaluation_failures_total",
            "Total evaluation failures (timeout, error, etc.)",
            _eval_labels,
            registry=self._registry,
        )

        self.evaluation_latency = Histogram(
            "rastir_evaluation_latency_seconds",
            "Evaluation latency in seconds",
            _eval_labels,
            buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0),
            registry=self._registry,
        )

        self.evaluation_score = Gauge(
            "rastir_evaluation_score",
            "Latest evaluation score per type (0.0-1.0)",
            _eval_labels,
            registry=self._registry,
        )

        self.evaluation_queue_size = Gauge(
            "rastir_evaluation_queue_size",
            "Current number of tasks in the evaluation queue",
            registry=self._registry,
        )

        self.evaluation_queue_utilization = Gauge(
            "rastir_evaluation_queue_utilization_percent",
            "Evaluation queue utilization as a percentage of max capacity",
            registry=self._registry,
        )

        self.evaluation_dropped = Counter(
            "rastir_evaluation_dropped_total",
            "Total evaluation tasks dropped (queue full)",
            ["service", "env"],
            registry=self._registry,
        )

        # Evaluation type cardinality guard
        self._seen_evaluation_types: set[str] = set()

        # ---- SRE Config Gauges (set once at startup from config) ----
        self.sre_slo_error_rate = Gauge(
            "rastir_slo_error_rate",
            "Configured SLO error rate per agent (e.g. 0.01 = 1%)",
            ["agent"],
            registry=self._registry,
        )
        self.sre_cost_budget_usd = Gauge(
            "rastir_cost_budget_usd",
            "Configured cost budget in USD per agent per period",
            ["agent"],
            registry=self._registry,
        )

        # Ingestion rate tracking state
        self._rate_spans: int = 0
        self._rate_timestamp: float = time.monotonic()

        # Track seen label combos for cardinality guard (per dimension)
        self._seen_models: set[str] = set()
        self._seen_providers: set[str] = set()
        self._seen_tools: set[str] = set()
        self._seen_agents: set[str] = set()
        self._seen_error_types: set[str] = set()
        self._seen_pricing_profiles: set[str] = set()

    # ----- public API ------------------------------------------------------

    @staticmethod
    def _normalise_span_type(raw: str) -> str:
        """Map incoming span_type to V2 canonical types.

        ``trace`` and ``metric`` are mapped to ``system``.
        Unrecognised types are also mapped to ``system``.
        """
        if raw in _VALID_SPAN_TYPES:
            return raw
        return "system"

    def record_span(self, span: dict, service: str, env: str) -> None:
        """Derive and update all Prometheus metrics from a single span dict."""
        raw_type = span.get("span_type", "unknown")
        span_type = self._normalise_span_type(raw_type)
        status = span.get("status", "OK")
        attrs = span.get("attributes", {})
        trace_id = span.get("trace_id", "")

        # Build exemplar dict if enabled and trace_id is present
        exemplar = {"trace_id": trace_id} if (self._exemplars_enabled and trace_id) else None

        # -- universal: ingested counter + duration histogram
        self.spans_ingested.labels(
            service=self._clip(service),
            env=self._clip(env),
            span_type=span_type,
            status=status,
        ).inc()

        # Extract model/provider early so duration & errors can use them.
        # All span types may carry inherited model/provider from context.
        raw_model = attrs.get("model", "")
        raw_provider = attrs.get("provider", "")
        model_label = self._guard_cardinality(raw_model, self._seen_models, "model") if raw_model else ""
        provider_label = self._guard_cardinality(raw_provider, self._seen_providers, "provider") if raw_provider else ""

        duration = span.get("duration_ms")
        if duration is not None:
            self.duration.labels(
                service=self._clip(service),
                env=self._clip(env),
                span_type=span_type,
                model=model_label,
                provider=provider_label,
            ).observe(duration / 1000.0, exemplar=exemplar)

        # -- error counter
        if status == "ERROR":
            error_type = self._normalise_error_type(span)
            error_type = self._guard_cardinality(
                error_type, self._seen_error_types, "error_type"
            )
            agent_label = self._guard_cardinality(
                attrs.get("agent", ""), self._seen_agents, "agent"
            )
            self.errors.labels(
                service=self._clip(service),
                env=self._clip(env),
                span_type=span_type,
                error_type=error_type,
                model=model_label,
                provider=provider_label,
                agent=agent_label,
            ).inc(exemplar=exemplar)

        # -- LLM-specific
        if span_type == "llm":
            # model_label and provider_label already extracted above
            agent = self._guard_cardinality(attrs.get("agent", ""), self._seen_agents, "agent")

            self.llm_calls.labels(
                service=self._clip(service),
                env=self._clip(env),
                model=model_label,
                provider=provider_label,
                agent=agent,
            ).inc(exemplar=exemplar)

            tokens_in = attrs.get("tokens_input", 0) or 0
            tokens_out = attrs.get("tokens_output", 0) or 0

            if tokens_in:
                self.tokens_input.labels(
                    service=self._clip(service),
                    env=self._clip(env),
                    model=model_label,
                    provider=provider_label,
                    agent=agent,
                ).inc(tokens_in)

            if tokens_out:
                self.tokens_output.labels(
                    service=self._clip(service),
                    env=self._clip(env),
                    model=model_label,
                    provider=provider_label,
                    agent=agent,
                ).inc(tokens_out)

            total_tokens = tokens_in + tokens_out
            if total_tokens > 0:
                self.tokens_per_call.labels(
                    service=self._clip(service),
                    env=self._clip(env),
                    model=model_label,
                    provider=provider_label,
                ).observe(total_tokens)

            # -- cost metrics (V6)
            cost_usd = attrs.get("cost_usd")
            if cost_usd is not None and cost_usd > 0:
                pricing_profile = self._guard_cardinality(
                    attrs.get("pricing_profile", "default"),
                    self._seen_pricing_profiles,
                    "pricing_profile",
                )
                self.cost_total.labels(
                    service=self._clip(service),
                    env=self._clip(env),
                    model=model_label,
                    provider=provider_label,
                    agent=agent,
                    pricing_profile=pricing_profile,
                ).inc(cost_usd)

                self.cost_per_call.labels(
                    service=self._clip(service),
                    env=self._clip(env),
                    model=model_label,
                ).observe(cost_usd)

            if attrs.get("pricing_missing"):
                self.pricing_missing.labels(
                    service=self._clip(service),
                    env=self._clip(env),
                    model=model_label,
                    provider=provider_label,
                ).inc()

            # -- TTFT metric (V6)
            ttft_ms = attrs.get("ttft_ms")
            if ttft_ms is not None:
                self.ttft.labels(
                    service=self._clip(service),
                    env=self._clip(env),
                    model=model_label,
                    provider=provider_label,
                ).observe(ttft_ms / 1000.0)  # convert ms → seconds

            # -- guardrail metrics (LLM spans with guardrail attrs)
            guardrail_id = attrs.get("guardrail_id")
            if guardrail_id:
                safe_gr_id = self._guard_cardinality(
                    guardrail_id, self._seen_guardrail_ids, "guardrail_id"
                )
                guardrail_version = str(attrs.get("guardrail_version", ""))
                self.guardrail_requests.labels(
                    service=self._clip(service),
                    env=self._clip(env),
                    provider=provider_label,
                    model=model_label,
                    agent=agent,
                    guardrail_id=safe_gr_id,
                    guardrail_version=guardrail_version,
                ).inc()

            # Guardrail violation (intervention occurred)
            guardrail_action = attrs.get("guardrail_action")
            if guardrail_action and guardrail_action != "NONE":
                safe_gr_id = self._guard_cardinality(
                    attrs.get("guardrail_id", "unknown"),
                    self._seen_guardrail_ids,
                    "guardrail_id",
                )
                guardrail_category = attrs.get(
                    "guardrail_category", "unknown"
                )
                # Defence-in-depth: server-side bounded enum validation
                if guardrail_category not in _VALID_GUARDRAIL_CATEGORIES:
                    guardrail_category = _OVERFLOW_SENTINEL
                safe_action = (
                    guardrail_action
                    if guardrail_action in _VALID_GUARDRAIL_ACTIONS
                    else _OVERFLOW_SENTINEL
                )
                self.guardrail_violations.labels(
                    service=self._clip(service),
                    env=self._clip(env),
                    provider=provider_label,
                    model=model_label,
                    agent=agent,
                    guardrail_id=safe_gr_id,
                    guardrail_action=safe_action,
                    guardrail_category=guardrail_category,
                ).inc()

        # -- tool-specific
        elif span_type == "tool":
            tool_name = self._guard_cardinality(
                span.get("name", "unknown"), self._seen_tools, "tool_name"
            )
            agent = self._guard_cardinality(
                attrs.get("agent", "unknown"), self._seen_agents, "agent"
            )
            tool_model = self._guard_cardinality(
                attrs.get("model", ""), self._seen_models, "model"
            )
            tool_provider = self._guard_cardinality(
                attrs.get("provider", ""), self._seen_providers, "provider"
            )
            self.tool_calls.labels(
                service=self._clip(service),
                env=self._clip(env),
                tool_name=tool_name,
                agent=agent,
                model=tool_model,
                provider=tool_provider,
            ).inc(exemplar=exemplar)

        # -- retrieval-specific
        elif span_type == "retrieval":
            agent = self._guard_cardinality(attrs.get("agent", ""), self._seen_agents, "agent")
            ret_model = self._guard_cardinality(
                attrs.get("model", ""), self._seen_models, "model"
            )
            ret_provider = self._guard_cardinality(
                attrs.get("provider", ""), self._seen_providers, "provider"
            )
            self.retrieval_calls.labels(
                service=self._clip(service),
                env=self._clip(env),
                agent=agent,
                model=ret_model,
                provider=ret_provider,
            ).inc()

    def generate(self) -> tuple[bytes, str]:
        """Render all metrics.

        Returns:
            A tuple of ``(content_bytes, content_type)``.
            When exemplars are enabled, uses OpenMetrics format;
            otherwise classic Prometheus exposition format.
        """
        if self._exemplars_enabled:
            content = openmetrics.exposition.generate_latest(self._registry)
            ct = openmetrics.exposition.CONTENT_TYPE_LATEST
            return content, ct
        return generate_latest(self._registry), "text/plain; version=0.0.4; charset=utf-8"

    def update_operational_gauges(
        self,
        queue_size: int,
        queue_maxsize: int,
        trace_store: Optional[object] = None,
        eval_queue: Optional[object] = None,
    ) -> None:
        """Refresh all operational gauges (called periodically by the worker).

        Args:
            queue_size: Current queue depth.
            queue_maxsize: Maximum queue capacity.
            trace_store: Optional ``TraceStore`` instance for span/trace counts.
            eval_queue: Optional ``EvaluationQueue`` for eval queue gauges.
        """
        self.queue_size.set(queue_size)
        pct = (queue_size / queue_maxsize * 100.0) if queue_maxsize else 0.0
        self.queue_utilization.set(round(pct, 1))

        # RSS memory via getrusage (maxrss is in KB on Linux)
        try:
            rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            self.memory_bytes.set(rss_kb * 1024)
        except Exception:
            pass  # platform without getrusage

        if trace_store is not None:
            self.trace_store_size.set(trace_store.span_count)
            self.active_traces.set(trace_store.trace_count)

        # Evaluation queue gauges
        if eval_queue is not None:
            eq_size = eval_queue.size()
            eq_max = eval_queue.maxsize
            self.evaluation_queue_size.set(eq_size)
            eq_pct = (eq_size / eq_max * 100.0) if eq_max else 0.0
            self.evaluation_queue_utilization.set(round(eq_pct, 1))

        # Update ingestion rate
        self._refresh_ingestion_rate()

    def record_ingested_spans(self, count: int) -> None:
        """Track span count for ingestion-rate calculation."""
        self._rate_spans += count

    def _refresh_ingestion_rate(self) -> None:
        """Compute and set the ingestion rate gauge (spans/sec)."""
        now = time.monotonic()
        elapsed = now - self._rate_timestamp
        if elapsed >= 1.0:
            rate = self._rate_spans / elapsed
            self.ingestion_rate.set(round(rate, 2))
            self._rate_spans = 0
            self._rate_timestamp = now

    @property
    def registry(self) -> CollectorRegistry:
        return self._registry

    # ----- internal --------------------------------------------------------

    def _clip(self, value: str) -> str:
        """Truncate label values to the configured max length."""
        if len(value) > self._max_label_len:
            return value[: self._max_label_len]
        return value

    def _guard_cardinality(self, value: str, seen: set[str], dimension: str = "") -> str:
        """Replace high-cardinality label values with an overflow sentinel.

        Uses per-dimension caps from ``_cardinality_caps``.
        """
        value = self._clip(value)
        if value in seen:
            return value
        cap = self._cardinality_caps.get(dimension, 500)
        if len(seen) >= cap:
            return _OVERFLOW_SENTINEL
        seen.add(value)
        return value

    @staticmethod
    def _normalise_error_type(span: dict) -> str:
        """Extract exception type from span events and normalise to a fixed category.

        Mapping: raw exception class name → one of ``timeout``, ``rate_limit``,
        ``validation_error``, ``provider_error``, ``internal_error``, ``unknown``.
        """
        raw = ""
        for event in span.get("events", []):
            if event.get("name") == "exception":
                raw = event.get("attributes", {}).get("exception.type", "")
                if raw:
                    break

        if not raw:
            return "unknown"

        # 1. Direct lookup (exact match)
        if raw in _ERROR_TYPE_MAP:
            return _ERROR_TYPE_MAP[raw]

        # 2. Suffix-based heuristic for unqualified names
        lower = raw.lower()
        if "timeout" in lower:
            return "timeout"
        if "ratelimit" in lower or "rate_limit" in lower:
            return "rate_limit"
        if "validation" in lower:
            return "validation_error"

        return "unknown"
