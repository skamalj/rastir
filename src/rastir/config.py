"""Configuration management for Rastir.

Provides the `configure()` API, environment variable fallback, and the
immutable GlobalConfig singleton.

Precedence: configure() > environment variables > defaults.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("rastir")

_config_lock = threading.Lock()
_global_config: Optional[GlobalConfig] = None
_initialized = False


@dataclass(frozen=True)
class ExporterConfig:
    """Transport/exporter settings."""

    push_url: Optional[str] = None
    api_key: Optional[str] = None
    batch_size: int = 100
    flush_interval: int = 5  # seconds
    timeout: int = 5  # seconds (HTTP request timeout)
    max_retries: int = 3  # retries on transient failures (5xx, 429, conn errors)
    retry_backoff: float = 0.5  # initial backoff in seconds (doubles each retry)
    shutdown_timeout: float = 5.0  # max seconds to wait for exporter thread on shutdown

    @property
    def enabled(self) -> bool:
        """Push is enabled only when a push_url is configured."""
        return self.push_url is not None


@dataclass(frozen=True)
class GlobalConfig:
    """Immutable global configuration for Rastir.

    Once created, this object cannot be modified. This ensures thread-safe
    access from decorators and the exporter without locks.
    """

    service: str = "unknown"
    env: str = "development"
    version: Optional[str] = None
    exporter: ExporterConfig = field(default_factory=ExporterConfig)

    @property
    def global_labels(self) -> dict[str, str]:
        """Labels injected into all Prometheus metrics."""
        labels = {
            "service": self.service,
            "env": self.env,
        }
        if self.version:
            labels["version"] = self.version
        return labels


def configure(
    service: str | None = None,
    env: str | None = None,
    version: str | None = None,
    push_url: str | None = None,
    api_key: str | None = None,
    batch_size: int | None = None,
    flush_interval: int | None = None,
    timeout: int | None = None,
    max_retries: int | None = None,
    retry_backoff: float | None = None,
    shutdown_timeout: float | None = None,
) -> GlobalConfig:
    """Initialize Rastir configuration.

    Must be called once at application startup. After initialization,
    configuration is locked and cannot be changed.

    Priority: explicit args > environment variables > defaults.

    Args:
        service: Logical service name.
        env: Deployment environment (e.g., production, staging).
        version: Application version string.
        push_url: Telemetry collector endpoint URL.
        api_key: Authentication key for the collector.
        batch_size: Max spans per push batch.
        flush_interval: Seconds between batch flushes.
        timeout: HTTP request timeout in seconds.
        max_retries: Max retry attempts on transient failures (default 3).
        retry_backoff: Initial backoff seconds, doubles each retry (default 0.5).
        shutdown_timeout: Max seconds to wait for exporter thread on shutdown (default 5.0).

    Returns:
        The frozen GlobalConfig instance.

    Raises:
        RuntimeError: If configure() is called more than once.
    """
    global _global_config, _initialized

    with _config_lock:
        if _initialized:
            raise RuntimeError(
                "rastir.configure() has already been called. "
                "Configuration is immutable after initialization."
            )

        resolved_service = _resolve(service, "RASTIR_SERVICE", "unknown")
        resolved_env = _resolve(env, "RASTIR_ENV", "development")
        resolved_version = _resolve(version, "RASTIR_VERSION", None)
        resolved_push_url = _resolve(push_url, "RASTIR_PUSH_URL", None)
        resolved_api_key = _resolve(api_key, "RASTIR_API_KEY", None)
        resolved_batch_size = _resolve_int(batch_size, "RASTIR_BATCH_SIZE", 100)
        resolved_flush_interval = _resolve_int(flush_interval, "RASTIR_FLUSH_INTERVAL", 5)
        resolved_timeout = _resolve_int(timeout, "RASTIR_TIMEOUT", 5)
        resolved_max_retries = _resolve_int(max_retries, "RASTIR_MAX_RETRIES", 3)
        resolved_retry_backoff = _resolve_float(retry_backoff, "RASTIR_RETRY_BACKOFF", 0.5)
        resolved_shutdown_timeout = _resolve_float(shutdown_timeout, "RASTIR_SHUTDOWN_TIMEOUT", 5.0)

        exporter = ExporterConfig(
            push_url=resolved_push_url,
            api_key=resolved_api_key,
            batch_size=resolved_batch_size,
            flush_interval=resolved_flush_interval,
            timeout=resolved_timeout,
            max_retries=resolved_max_retries,
            retry_backoff=resolved_retry_backoff,
            shutdown_timeout=resolved_shutdown_timeout,
        )

        _global_config = GlobalConfig(
            service=resolved_service,
            env=resolved_env,
            version=resolved_version,
            exporter=exporter,
        )

        _initialized = True

        if exporter.enabled:
            logger.info(
                "Rastir configured: service=%s, env=%s, push_url=%s",
                resolved_service,
                resolved_env,
                resolved_push_url,
            )
            # Start background exporter (lazy import to avoid circular deps)
            from rastir.transport import start_exporter
            start_exporter(_global_config)
        else:
            logger.info(
                "Rastir configured: service=%s, env=%s (push disabled)",
                resolved_service,
                resolved_env,
            )

        return _global_config


def get_config() -> GlobalConfig:
    """Return the current global configuration.

    If configure() has not been called, auto-initializes from environment
    variables and defaults (with push disabled unless RASTIR_PUSH_URL
    is set).
    """
    global _global_config, _initialized

    if _global_config is not None:
        return _global_config

    with _config_lock:
        # Double-check after acquiring lock
        if _global_config is not None:
            return _global_config

        # Auto-initialize from env vars / defaults
        _global_config = GlobalConfig(
            service=_resolve(None, "RASTIR_SERVICE", "unknown"),
            env=_resolve(None, "RASTIR_ENV", "development"),
            version=_resolve(None, "RASTIR_VERSION", None),
            exporter=ExporterConfig(
                push_url=_resolve(None, "RASTIR_PUSH_URL", None),
                api_key=_resolve(None, "RASTIR_API_KEY", None),
                batch_size=_resolve_int(None, "RASTIR_BATCH_SIZE", 100),
                flush_interval=_resolve_int(None, "RASTIR_FLUSH_INTERVAL", 5),
                timeout=_resolve_int(None, "RASTIR_TIMEOUT", 5),
                max_retries=_resolve_int(None, "RASTIR_MAX_RETRIES", 3),
                retry_backoff=_resolve_float(None, "RASTIR_RETRY_BACKOFF", 0.5),
                shutdown_timeout=_resolve_float(None, "RASTIR_SHUTDOWN_TIMEOUT", 5.0),
            ),
        )
        _initialized = True
        logger.debug("Rastir auto-configured from environment/defaults")
        return _global_config


def reset_config() -> None:
    """Reset configuration. Intended for testing only."""
    global _global_config, _initialized

    with _config_lock:
        # Stop background exporter if running
        try:
            from rastir.transport import stop_exporter
            stop_exporter()
        except ImportError:
            pass

        _global_config = None
        _initialized = False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve(explicit: str | None, env_var: str, default: str | None) -> str | None:
    """Resolve a string config value: explicit > env > default."""
    if explicit is not None:
        return explicit
    env_val = os.environ.get(env_var)
    if env_val is not None and env_val.strip():
        return env_val.strip()
    return default


def _resolve_int(explicit: int | None, env_var: str, default: int) -> int:
    """Resolve an integer config value: explicit > env > default."""
    if explicit is not None:
        return explicit
    env_val = os.environ.get(env_var)
    if env_val is not None and env_val.strip():
        try:
            return int(env_val.strip())
        except ValueError:
            logger.warning("Invalid integer for %s: %r, using default %d", env_var, env_val, default)
            return default
    return default


def _resolve_float(explicit: float | None, env_var: str, default: float) -> float:
    """Resolve a float config value: explicit > env > default."""
    if explicit is not None:
        return explicit
    env_val = os.environ.get(env_var)
    if env_val is not None and env_val.strip():
        try:
            return float(env_val.strip())
        except ValueError:
            logger.warning("Invalid float for %s: %r, using default %s", env_var, env_val, default)
            return default
    return default
