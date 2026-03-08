"""Tests for rastir.config module."""

import os
import pytest
from rastir.config import configure, get_config, reset_config, GlobalConfig


@pytest.fixture(autouse=True)
def _clean_config():
    """Reset config before and after each test."""
    reset_config()
    yield
    reset_config()


class TestConfigure:
    def test_basic_configure(self):
        cfg = configure(service="test-svc", env="staging")
        assert isinstance(cfg, GlobalConfig)
        assert cfg.service == "test-svc"
        assert cfg.env == "staging"
        assert cfg.version is None
        assert cfg.exporter.enabled is False  # no push_url

    def test_configure_with_push(self):
        cfg = configure(
            service="svc",
            env="prod",
            version="1.0.0",
            push_url="http://localhost:9464/v1/telemetry",
            api_key="secret",
            batch_size=50,
            flush_interval=10,
            timeout=3,
        )
        assert cfg.exporter.enabled is True
        assert cfg.exporter.push_url == "http://localhost:9464/v1/telemetry"
        assert cfg.exporter.api_key == "secret"
        assert cfg.exporter.batch_size == 50
        assert cfg.exporter.flush_interval == 10
        assert cfg.exporter.timeout == 3

    def test_configure_with_retry_and_shutdown(self):
        cfg = configure(
            service="svc",
            env="prod",
            max_retries=5,
            retry_backoff=1.0,
            shutdown_timeout=10.0,
        )
        assert cfg.exporter.max_retries == 5
        assert cfg.exporter.retry_backoff == 1.0
        assert cfg.exporter.shutdown_timeout == 10.0

    def test_configure_retry_defaults(self):
        cfg = configure(service="svc", env="prod")
        assert cfg.exporter.max_retries == 3
        assert cfg.exporter.retry_backoff == 0.5
        assert cfg.exporter.shutdown_timeout == 5.0

    def test_configure_is_immutable(self):
        cfg = configure(service="svc", env="prod")
        with pytest.raises(AttributeError):
            cfg.service = "other"  # type: ignore[misc]

    def test_configure_called_twice_raises(self):
        configure(service="svc", env="prod")
        with pytest.raises(RuntimeError, match="already been called"):
            configure(service="svc2", env="dev")

    def test_global_labels(self):
        cfg = configure(service="svc", env="prod", version="2.0")
        labels = cfg.global_labels
        assert labels == {"service": "svc", "env": "prod", "version": "2.0"}

    def test_global_labels_no_version(self):
        cfg = configure(service="svc", env="dev")
        labels = cfg.global_labels
        assert labels == {"service": "svc", "env": "dev"}


class TestEnvVarFallback:
    def test_env_vars_used_when_no_explicit(self, monkeypatch):
        monkeypatch.setenv("RASTIR_SERVICE", "env-svc")
        monkeypatch.setenv("RASTIR_ENV", "env-prod")
        monkeypatch.setenv("RASTIR_VERSION", "3.0")
        monkeypatch.setenv("RASTIR_PUSH_URL", "http://host:8080")
        monkeypatch.setenv("RASTIR_BATCH_SIZE", "200")

        cfg = configure()
        assert cfg.service == "env-svc"
        assert cfg.env == "env-prod"
        assert cfg.version == "3.0"
        assert cfg.exporter.push_url == "http://host:8080"
        assert cfg.exporter.batch_size == 200

    def test_explicit_overrides_env(self, monkeypatch):
        monkeypatch.setenv("RASTIR_SERVICE", "env-svc")
        cfg = configure(service="explicit-svc")
        assert cfg.service == "explicit-svc"

    def test_invalid_int_env_uses_default(self, monkeypatch):
        monkeypatch.setenv("RASTIR_BATCH_SIZE", "not-a-number")
        cfg = configure(service="svc", env="dev")
        assert cfg.exporter.batch_size == 100  # default

    def test_retry_env_vars(self, monkeypatch):
        monkeypatch.setenv("RASTIR_MAX_RETRIES", "7")
        monkeypatch.setenv("RASTIR_RETRY_BACKOFF", "2.5")
        monkeypatch.setenv("RASTIR_SHUTDOWN_TIMEOUT", "15.0")

        cfg = configure(service="svc", env="dev")
        assert cfg.exporter.max_retries == 7
        assert cfg.exporter.retry_backoff == 2.5
        assert cfg.exporter.shutdown_timeout == 15.0

    def test_invalid_float_env_uses_default(self, monkeypatch):
        monkeypatch.setenv("RASTIR_RETRY_BACKOFF", "not-a-float")
        cfg = configure(service="svc", env="dev")
        assert cfg.exporter.retry_backoff == 0.5  # default

    def test_explicit_retry_overrides_env(self, monkeypatch):
        monkeypatch.setenv("RASTIR_MAX_RETRIES", "10")
        cfg = configure(service="svc", env="dev", max_retries=2)
        assert cfg.exporter.max_retries == 2


class TestGetConfig:
    def test_get_config_after_configure(self):
        configure(service="svc", env="prod")
        cfg = get_config()
        assert cfg.service == "svc"

    def test_get_config_auto_initializes(self):
        cfg = get_config()
        assert cfg.service == "unknown"
        assert cfg.env == "development"
        assert cfg.exporter.enabled is False

    def test_get_config_auto_init_reads_env(self, monkeypatch):
        monkeypatch.setenv("RASTIR_SERVICE", "auto-svc")
        cfg = get_config()
        assert cfg.service == "auto-svc"


class TestEvaluationConfig:
    def test_evaluation_defaults(self):
        cfg = configure(service="svc", env="dev")
        assert cfg.evaluation.enabled is False
        assert cfg.evaluation.evaluation_types == ("hallucination", "relevance")
        assert cfg.evaluation.capture_prompt is True
        assert cfg.evaluation.capture_completion is True

    def test_evaluation_enabled(self):
        cfg = configure(service="svc", env="dev", evaluation_enabled=True)
        assert cfg.evaluation.enabled is True

    def test_evaluation_types_explicit(self):
        cfg = configure(
            service="svc", env="dev",
            evaluation_types=["toxicity", "bias"],
        )
        assert cfg.evaluation.evaluation_types == ("toxicity", "bias")

    def test_evaluation_types_env_var(self, monkeypatch):
        monkeypatch.setenv("RASTIR_EVALUATION_TYPES", "hallucination, custom")
        cfg = configure(service="svc", env="dev")
        assert cfg.evaluation.evaluation_types == ("hallucination", "custom")

    def test_evaluation_types_explicit_overrides_env(self, monkeypatch):
        monkeypatch.setenv("RASTIR_EVALUATION_TYPES", "env_type")
        cfg = configure(
            service="svc", env="dev",
            evaluation_types=["explicit_type"],
        )
        assert cfg.evaluation.evaluation_types == ("explicit_type",)

    def test_evaluation_enabled_env_var(self, monkeypatch):
        monkeypatch.setenv("RASTIR_EVALUATION_ENABLED", "true")
        cfg = configure(service="svc", env="dev")
        assert cfg.evaluation.enabled is True
