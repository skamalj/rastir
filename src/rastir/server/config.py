"""Server configuration — YAML file + environment variable overrides.

Loads settings from an optional YAML config file. Environment variables
override any YAML values. Sensible defaults are provided for all fields
so the server can start with zero configuration.

Environment variable mapping (prefix ``RASTIR_SERVER_``):

    RASTIR_SERVER_HOST
    RASTIR_SERVER_PORT
    RASTIR_SERVER_MAX_TRACES
    RASTIR_SERVER_MAX_QUEUE_SIZE
    RASTIR_SERVER_MAX_SPAN_ATTRIBUTES
    RASTIR_SERVER_MAX_LABEL_VALUE_LENGTH
    RASTIR_SERVER_TRACE_STORE_ENABLED
    RASTIR_SERVER_OTLP_ENDPOINT
    RASTIR_SERVER_OTLP_BATCH_SIZE
    RASTIR_SERVER_OTLP_FLUSH_INTERVAL
    RASTIR_SERVER_MULTI_TENANT_ENABLED
    RASTIR_SERVER_TENANT_HEADER
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("rastir.server")


# ---------------------------------------------------------------------------
# Configuration dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ServerSection:
    """Network binding settings."""
    host: str = "0.0.0.0"
    port: int = 8080


@dataclass(frozen=True)
class LimitsSection:
    """Resource limits."""
    max_traces: int = 10_000
    max_queue_size: int = 50_000
    max_span_attributes: int = 100
    max_label_value_length: int = 128
    # Per-dimension cardinality caps
    cardinality_model: int = 50
    cardinality_provider: int = 10
    cardinality_tool_name: int = 200
    cardinality_agent: int = 200
    cardinality_error_type: int = 50


@dataclass(frozen=True)
class HistogramSection:
    """Histogram bucket configuration."""
    duration_buckets: tuple[float, ...] = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0)
    tokens_buckets: tuple[float, ...] = (10, 50, 100, 250, 500, 1000, 2000, 4000, 8000, 16000, 32000)


@dataclass(frozen=True)
class TraceStoreSection:
    """Trace ring-buffer settings."""
    enabled: bool = True
    max_spans_per_trace: int = 500
    ttl_seconds: int = 0  # 0 = disabled (no expiration)


@dataclass(frozen=True)
class ExporterSection:
    """OTLP export settings."""
    otlp_endpoint: Optional[str] = None
    batch_size: int = 200
    flush_interval: int = 5  # seconds

    @property
    def enabled(self) -> bool:
        return self.otlp_endpoint is not None


@dataclass(frozen=True)
class MultiTenantSection:
    """Multi-tenant isolation settings."""
    enabled: bool = False
    header_name: str = "X-Tenant-ID"


@dataclass(frozen=True)
class SamplingSection:
    """Trace sampling controls.

    Sampling affects *only* trace storage and OTLP export — metric
    counters and histograms are always updated regardless of sampling
    decisions.
    """
    enabled: bool = False
    rate: float = 1.0  # 0.0–1.0 head-based sampling percentage
    always_retain_errors: bool = True
    latency_threshold_ms: float = 0.0  # always retain above this (0 = disabled)


@dataclass(frozen=True)
class BackpressureSection:
    """Advanced backpressure controls.

    ``soft_limit_pct``  — queue usage % that triggers a warning.
    ``hard_limit_pct``  — queue usage % that triggers rejection/drop.
    ``mode``            — ``reject`` (default) drops new spans;
                          ``drop_oldest`` evicts head of queue.
    """
    soft_limit_pct: float = 80.0
    hard_limit_pct: float = 95.0
    mode: str = "reject"  # "reject" | "drop_oldest"


@dataclass(frozen=True)
class RateLimitSection:
    """Optional per-IP and per-service rate limiting."""
    enabled: bool = False
    per_ip_rpm: int = 600        # requests per minute per IP
    per_service_rpm: int = 3000  # requests per minute per service


@dataclass(frozen=True)
class ExemplarSection:
    """Prometheus exemplar support."""
    enabled: bool = False  # disabled by default


@dataclass(frozen=True)
class ShutdownSection:
    """Graceful shutdown settings."""
    grace_period_seconds: int = 30
    drain_queue: bool = True


@dataclass(frozen=True)
class LoggingSection:
    """Structured logging settings."""
    structured: bool = False  # enable JSON structured logs
    level: str = "INFO"


@dataclass(frozen=True)
class RedactionSection:
    """Server-side redaction settings.

    Redaction applies to ``prompt_text`` and ``completion_text`` attributes
    after sampling, before store/export/evaluation enqueue.
    """
    enabled: bool = False
    max_text_length: int = 50_000
    custom_patterns: tuple[tuple[str, str], ...] = ()  # (regex, replacement) pairs
    drop_on_failure: bool = True  # drop span if redaction fails (security-first)


@dataclass(frozen=True)
class EvaluationSection:
    """Server-side async evaluation settings."""
    enabled: bool = False
    queue_size: int = 10_000
    drop_policy: str = "drop_new"  # "drop_new" | "drop_oldest"
    worker_concurrency: int = 4
    default_sample_rate: float = 1.0
    default_timeout_ms: int = 30_000
    max_evaluation_types: int = 20
    judge_model: str = "gpt-4o-mini"
    judge_provider: str = "openai"
    judge_api_key: Optional[str] = None
    judge_base_url: Optional[str] = None


@dataclass(frozen=True)
class ServerConfig:
    """Top-level server configuration."""
    server: ServerSection = field(default_factory=ServerSection)
    limits: LimitsSection = field(default_factory=LimitsSection)
    histograms: HistogramSection = field(default_factory=HistogramSection)
    trace_store: TraceStoreSection = field(default_factory=TraceStoreSection)
    exporter: ExporterSection = field(default_factory=ExporterSection)
    multi_tenant: MultiTenantSection = field(default_factory=MultiTenantSection)
    sampling: SamplingSection = field(default_factory=SamplingSection)
    backpressure: BackpressureSection = field(default_factory=BackpressureSection)
    rate_limit: RateLimitSection = field(default_factory=RateLimitSection)
    exemplars: ExemplarSection = field(default_factory=ExemplarSection)
    shutdown: ShutdownSection = field(default_factory=ShutdownSection)
    logging: LoggingSection = field(default_factory=LoggingSection)
    redaction: RedactionSection = field(default_factory=RedactionSection)
    evaluation: EvaluationSection = field(default_factory=EvaluationSection)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _env(name: str) -> Optional[str]:
    """Read an env var, returning None if unset or blank."""
    val = os.environ.get(name)
    if val is not None and val.strip():
        return val.strip()
    return None


def _env_int(name: str, default: int) -> int:
    val = _env(name)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        logger.warning("Invalid integer for %s: %r, using default %d", name, val, default)
        return default


def _env_bool(name: str, default: bool) -> bool:
    val = _env(name)
    if val is None:
        return default
    return val.lower() in ("1", "true", "yes", "on")


def load_config(config_path: Optional[str] = None) -> ServerConfig:
    """Load server configuration from YAML + env overrides.

    Args:
        config_path: Optional path to a YAML config file. If ``None``,
                     looks at ``RASTIR_SERVER_CONFIG`` env var,
                     then falls back to pure defaults + env overrides.

    Returns:
        Frozen ``ServerConfig`` instance.
    """
    yaml_data: dict = {}

    path = config_path or _env("RASTIR_SERVER_CONFIG")
    if path and Path(path).is_file():
        try:
            import yaml  # optional dependency — only needed with YAML config

            with open(path) as f:
                yaml_data = yaml.safe_load(f) or {}
            logger.info("Loaded server config from %s", path)
        except Exception as exc:
            logger.warning("Failed to load config from %s: %s", path, exc)

    # ---- helpers to merge yaml → env → default ----
    def _get(section: str, key: str, default, *, as_type=str):
        """Resolve: env var > yaml > default."""
        env_name = f"RASTIR_SERVER_{section.upper()}_{key.upper()}"
        # Flatten: for top-level section names that match the key prefix
        if section == "server":
            env_name = f"RASTIR_SERVER_{key.upper()}"

        env_val = _env(env_name)
        yaml_val = yaml_data.get(section, {}).get(key) if isinstance(yaml_data.get(section), dict) else None

        if env_val is not None:
            if as_type is int:
                try:
                    return int(env_val)
                except ValueError:
                    return default
            elif as_type is bool:
                return env_val.lower() in ("1", "true", "yes", "on")
            return env_val

        if yaml_val is not None:
            return yaml_val

        return default

    server = ServerSection(
        host=_get("server", "host", "0.0.0.0"),
        port=_get("server", "port", 8080, as_type=int),
    )

    limits = LimitsSection(
        max_traces=_get("limits", "max_traces", 10_000, as_type=int),
        max_queue_size=_get("limits", "max_queue_size", 50_000, as_type=int),
        max_span_attributes=_get("limits", "max_span_attributes", 100, as_type=int),
        max_label_value_length=_get("limits", "max_label_value_length", 128, as_type=int),
        cardinality_model=_get("limits", "cardinality_model", 50, as_type=int),
        cardinality_provider=_get("limits", "cardinality_provider", 10, as_type=int),
        cardinality_tool_name=_get("limits", "cardinality_tool_name", 200, as_type=int),
        cardinality_agent=_get("limits", "cardinality_agent", 200, as_type=int),
        cardinality_error_type=_get("limits", "cardinality_error_type", 50, as_type=int),
    )

    # -- histogram buckets (YAML list or comma-separated env var) --
    def _parse_buckets(section: str, key: str, default: tuple[float, ...]) -> tuple[float, ...]:
        env_name = f"RASTIR_SERVER_{section.upper()}_{key.upper()}"
        env_val = _env(env_name)
        if env_val is not None:
            try:
                return tuple(float(x.strip()) for x in env_val.split(","))
            except ValueError:
                logger.warning("Invalid bucket list for %s, using defaults", env_name)
                return default
        yaml_val = yaml_data.get(section, {}).get(key) if isinstance(yaml_data.get(section), dict) else None
        if yaml_val is not None and isinstance(yaml_val, list):
            try:
                return tuple(float(x) for x in yaml_val)
            except (ValueError, TypeError):
                logger.warning("Invalid bucket list in YAML %s.%s, using defaults", section, key)
                return default
        return default

    histograms = HistogramSection(
        duration_buckets=_parse_buckets(
            "histograms", "duration_buckets",
            HistogramSection.duration_buckets,
        ),
        tokens_buckets=_parse_buckets(
            "histograms", "tokens_buckets",
            HistogramSection.tokens_buckets,
        ),
    )

    trace_store = TraceStoreSection(
        enabled=_get("trace_store", "enabled", True, as_type=bool),
        max_spans_per_trace=_get("trace_store", "max_spans_per_trace", 500, as_type=int),
        ttl_seconds=_get("trace_store", "ttl_seconds", 0, as_type=int),
    )

    exporter = ExporterSection(
        otlp_endpoint=_get("exporter", "otlp_endpoint", None),
        batch_size=_get("exporter", "batch_size", 200, as_type=int),
        flush_interval=_get("exporter", "flush_interval", 5, as_type=int),
    )

    multi_tenant = MultiTenantSection(
        enabled=_get("multi_tenant", "enabled", False, as_type=bool),
        header_name=_get("multi_tenant", "header_name", "X-Tenant-ID"),
    )

    def _get_float(section: str, key: str, default: float) -> float:
        env_name = f"RASTIR_SERVER_{section.upper()}_{key.upper()}"
        env_val = _env(env_name)
        if env_val is not None:
            try:
                return float(env_val)
            except ValueError:
                return default
        yaml_val = yaml_data.get(section, {}).get(key) if isinstance(yaml_data.get(section), dict) else None
        if yaml_val is not None:
            try:
                return float(yaml_val)
            except (ValueError, TypeError):
                return default
        return default

    sampling = SamplingSection(
        enabled=_get("sampling", "enabled", False, as_type=bool),
        rate=_get_float("sampling", "rate", 1.0),
        always_retain_errors=_get("sampling", "always_retain_errors", True, as_type=bool),
        latency_threshold_ms=_get_float("sampling", "latency_threshold_ms", 0.0),
    )

    backpressure = BackpressureSection(
        soft_limit_pct=_get_float("backpressure", "soft_limit_pct", 80.0),
        hard_limit_pct=_get_float("backpressure", "hard_limit_pct", 95.0),
        mode=_get("backpressure", "mode", "reject"),
    )

    rate_limit = RateLimitSection(
        enabled=_get("rate_limit", "enabled", False, as_type=bool),
        per_ip_rpm=_get("rate_limit", "per_ip_rpm", 600, as_type=int),
        per_service_rpm=_get("rate_limit", "per_service_rpm", 3000, as_type=int),
    )

    exemplars = ExemplarSection(
        enabled=_get("exemplars", "enabled", False, as_type=bool),
    )

    shutdown = ShutdownSection(
        grace_period_seconds=_get("shutdown", "grace_period_seconds", 30, as_type=int),
        drain_queue=_get("shutdown", "drain_queue", True, as_type=bool),
    )

    logging_cfg = LoggingSection(
        structured=_get("logging", "structured", False, as_type=bool),
        level=_get("logging", "level", "INFO"),
    )

    # -- redaction --
    custom_patterns_raw = yaml_data.get("redaction", {}).get("custom_patterns", [])
    custom_patterns: list[tuple[str, str]] = []
    if isinstance(custom_patterns_raw, list):
        for item in custom_patterns_raw:
            if isinstance(item, dict) and "pattern" in item and "replacement" in item:
                custom_patterns.append((item["pattern"], item["replacement"]))

    redaction = RedactionSection(
        enabled=_get("redaction", "enabled", False, as_type=bool),
        max_text_length=_get("redaction", "max_text_length", 50_000, as_type=int),
        custom_patterns=tuple(custom_patterns),
        drop_on_failure=_get("redaction", "drop_on_failure", True, as_type=bool),
    )

    # -- evaluation --
    eval_section = EvaluationSection(
        enabled=_get("evaluation", "enabled", False, as_type=bool),
        queue_size=_get("evaluation", "queue_size", 10_000, as_type=int),
        drop_policy=_get("evaluation", "drop_policy", "drop_new"),
        worker_concurrency=_get("evaluation", "worker_concurrency", 4, as_type=int),
        default_sample_rate=_get_float("evaluation", "default_sample_rate", 1.0),
        default_timeout_ms=_get("evaluation", "default_timeout_ms", 30_000, as_type=int),
        max_evaluation_types=_get("evaluation", "max_evaluation_types", 20, as_type=int),
        judge_model=_get("evaluation", "judge_model", "gpt-4o-mini"),
        judge_provider=_get("evaluation", "judge_provider", "openai"),
        judge_api_key=_get("evaluation", "judge_api_key", None),
        judge_base_url=_get("evaluation", "judge_base_url", None),
    )

    return ServerConfig(
        server=server,
        limits=limits,
        histograms=histograms,
        trace_store=trace_store,
        exporter=exporter,
        multi_tenant=multi_tenant,
        sampling=sampling,
        backpressure=backpressure,
        rate_limit=rate_limit,
        exemplars=exemplars,
        shutdown=shutdown,
        logging=logging_cfg,
        redaction=redaction,
        evaluation=eval_section,
    )


# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------

class ConfigValidationError(Exception):
    """Raised when server configuration is invalid or unsafe."""


_MAX_BUCKET_COUNT = 20
_MAX_QUEUE_SIZE = 1_000_000
_MAX_TRACES = 500_000
_MAX_LABEL_VALUE_LENGTH = 1024


def validate_config(cfg: ServerConfig) -> None:
    """Validate configuration at startup.

    Raises :class:`ConfigValidationError` if any setting exceeds safe
    thresholds.  Call this before building server components.
    """
    errors: list[str] = []

    # Bucket count limits
    if len(cfg.histograms.duration_buckets) > _MAX_BUCKET_COUNT:
        errors.append(
            f"duration_buckets has {len(cfg.histograms.duration_buckets)} entries "
            f"(max {_MAX_BUCKET_COUNT})"
        )
    if len(cfg.histograms.tokens_buckets) > _MAX_BUCKET_COUNT:
        errors.append(
            f"tokens_buckets has {len(cfg.histograms.tokens_buckets)} entries "
            f"(max {_MAX_BUCKET_COUNT})"
        )

    # Bucket values must be sorted and positive
    for name, buckets in [
        ("duration_buckets", cfg.histograms.duration_buckets),
        ("tokens_buckets", cfg.histograms.tokens_buckets),
    ]:
        if buckets and any(b <= 0 for b in buckets):
            errors.append(f"{name} contains non-positive values")
        if list(buckets) != sorted(buckets):
            errors.append(f"{name} is not sorted in ascending order")

    # Queue size limits
    if cfg.limits.max_queue_size <= 0:
        errors.append("max_queue_size must be positive")
    elif cfg.limits.max_queue_size > _MAX_QUEUE_SIZE:
        errors.append(
            f"max_queue_size={cfg.limits.max_queue_size} exceeds safe limit "
            f"({_MAX_QUEUE_SIZE})"
        )

    # Trace store limits
    if cfg.limits.max_traces <= 0:
        errors.append("max_traces must be positive")
    elif cfg.limits.max_traces > _MAX_TRACES:
        errors.append(
            f"max_traces={cfg.limits.max_traces} exceeds safe limit "
            f"({_MAX_TRACES})"
        )

    # Label length
    if cfg.limits.max_label_value_length <= 0:
        errors.append("max_label_value_length must be positive")
    elif cfg.limits.max_label_value_length > _MAX_LABEL_VALUE_LENGTH:
        errors.append(
            f"max_label_value_length={cfg.limits.max_label_value_length} exceeds safe limit "
            f"({_MAX_LABEL_VALUE_LENGTH})"
        )

    # Cardinality caps must be positive
    for cap_name in (
        "cardinality_model",
        "cardinality_provider",
        "cardinality_tool_name",
        "cardinality_agent",
        "cardinality_error_type",
    ):
        val = getattr(cfg.limits, cap_name)
        if val <= 0:
            errors.append(f"{cap_name} must be positive (got {val})")

    # Sampling validation
    if cfg.sampling.rate < 0.0 or cfg.sampling.rate > 1.0:
        errors.append(
            f"sampling.rate must be between 0.0 and 1.0 (got {cfg.sampling.rate})"
        )
    if cfg.sampling.latency_threshold_ms < 0.0:
        errors.append(
            f"sampling.latency_threshold_ms must be non-negative "
            f"(got {cfg.sampling.latency_threshold_ms})"
        )

    # Backpressure validation
    if cfg.backpressure.soft_limit_pct < 0.0 or cfg.backpressure.soft_limit_pct > 100.0:
        errors.append(
            f"backpressure.soft_limit_pct must be 0-100 "
            f"(got {cfg.backpressure.soft_limit_pct})"
        )
    if cfg.backpressure.hard_limit_pct < 0.0 or cfg.backpressure.hard_limit_pct > 100.0:
        errors.append(
            f"backpressure.hard_limit_pct must be 0-100 "
            f"(got {cfg.backpressure.hard_limit_pct})"
        )
    if cfg.backpressure.soft_limit_pct >= cfg.backpressure.hard_limit_pct:
        errors.append(
            f"backpressure.soft_limit_pct ({cfg.backpressure.soft_limit_pct}) "
            f"must be less than hard_limit_pct ({cfg.backpressure.hard_limit_pct})"
        )
    if cfg.backpressure.mode not in ("reject", "drop_oldest"):
        errors.append(
            f"backpressure.mode must be 'reject' or 'drop_oldest' "
            f"(got {cfg.backpressure.mode!r})"
        )

    # Rate-limit validation
    if cfg.rate_limit.per_ip_rpm <= 0:
        errors.append(f"rate_limit.per_ip_rpm must be positive (got {cfg.rate_limit.per_ip_rpm})")
    if cfg.rate_limit.per_service_rpm <= 0:
        errors.append(
            f"rate_limit.per_service_rpm must be positive (got {cfg.rate_limit.per_service_rpm})"
        )

    # Trace-store retention
    if cfg.trace_store.max_spans_per_trace <= 0:
        errors.append(
            f"trace_store.max_spans_per_trace must be positive "
            f"(got {cfg.trace_store.max_spans_per_trace})"
        )
    if cfg.trace_store.ttl_seconds < 0:
        errors.append(
            f"trace_store.ttl_seconds must be non-negative "
            f"(got {cfg.trace_store.ttl_seconds})"
        )

    # Shutdown
    if cfg.shutdown.grace_period_seconds < 0:
        errors.append(
            f"shutdown.grace_period_seconds must be non-negative "
            f"(got {cfg.shutdown.grace_period_seconds})"
        )

    # Logging level
    valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    if cfg.logging.level.upper() not in valid_levels:
        errors.append(
            f"logging.level must be one of {valid_levels} (got {cfg.logging.level!r})"
        )

    # Redaction validation
    if cfg.redaction.max_text_length <= 0:
        errors.append(
            f"redaction.max_text_length must be positive (got {cfg.redaction.max_text_length})"
        )

    # Evaluation validation
    if cfg.evaluation.queue_size <= 0:
        errors.append(
            f"evaluation.queue_size must be positive (got {cfg.evaluation.queue_size})"
        )
    if cfg.evaluation.queue_size > _MAX_QUEUE_SIZE:
        errors.append(
            f"evaluation.queue_size={cfg.evaluation.queue_size} exceeds safe limit ({_MAX_QUEUE_SIZE})"
        )
    if cfg.evaluation.drop_policy not in ("drop_new", "drop_oldest"):
        errors.append(
            f"evaluation.drop_policy must be 'drop_new' or 'drop_oldest' "
            f"(got {cfg.evaluation.drop_policy!r})"
        )
    if cfg.evaluation.worker_concurrency <= 0:
        errors.append(
            f"evaluation.worker_concurrency must be positive "
            f"(got {cfg.evaluation.worker_concurrency})"
        )
    if cfg.evaluation.default_sample_rate < 0.0 or cfg.evaluation.default_sample_rate > 1.0:
        errors.append(
            f"evaluation.default_sample_rate must be 0.0-1.0 "
            f"(got {cfg.evaluation.default_sample_rate})"
        )
    if cfg.evaluation.default_timeout_ms <= 0:
        errors.append(
            f"evaluation.default_timeout_ms must be positive "
            f"(got {cfg.evaluation.default_timeout_ms})"
        )
    if cfg.evaluation.max_evaluation_types <= 0:
        errors.append(
            f"evaluation.max_evaluation_types must be positive "
            f"(got {cfg.evaluation.max_evaluation_types})"
        )

    if errors:
        detail = "; ".join(errors)
        raise ConfigValidationError(f"Invalid server configuration: {detail}")
