"""Tests for rastir.pricing module (PricingRegistry) and V6 config fields."""

import json
import os
import tempfile

import pytest

from rastir.config import configure, get_config, reset_config, get_pricing_registry
from rastir.pricing import PricingRegistry, PricingEntry


@pytest.fixture(autouse=True)
def _clean_config():
    """Reset config before and after each test."""
    reset_config()
    yield
    reset_config()


# -----------------------------------------------------------------------
# PricingRegistry unit tests
# -----------------------------------------------------------------------

class TestPricingRegistry:
    def test_register_and_lookup(self):
        reg = PricingRegistry()
        reg.register("openai", "gpt-4o", input_price=2.50, output_price=10.00)
        entry = reg.lookup("openai", "gpt-4o")
        assert entry is not None
        assert entry.input_price == 2.50
        assert entry.output_price == 10.00

    def test_lookup_missing(self):
        reg = PricingRegistry()
        assert reg.lookup("openai", "nonexistent") is None

    def test_case_insensitive_lookup(self):
        reg = PricingRegistry()
        reg.register("OpenAI", "GPT-4o", input_price=2.50, output_price=10.00)
        entry = reg.lookup("openai", "gpt-4o")
        assert entry is not None
        assert entry.input_price == 2.50

    def test_calculate_cost(self):
        reg = PricingRegistry()
        reg.register("openai", "gpt-4o", input_price=2.50, output_price=10.00)
        cost, missing = reg.calculate_cost("openai", "gpt-4o", tokens_in=1000, tokens_out=500)
        # 1000 * 2.50/1M + 500 * 10.00/1M = 0.0025 + 0.005 = 0.0075
        assert abs(cost - 0.0075) < 1e-9
        assert missing is False

    def test_calculate_cost_missing(self):
        reg = PricingRegistry()
        cost, missing = reg.calculate_cost("openai", "gpt-4o", tokens_in=1000, tokens_out=500)
        assert cost == 0.0
        assert missing is True

    def test_calculate_cost_zero_tokens(self):
        reg = PricingRegistry()
        reg.register("openai", "gpt-4o", input_price=2.50, output_price=10.00)
        cost, missing = reg.calculate_cost("openai", "gpt-4o", tokens_in=0, tokens_out=0)
        assert cost == 0.0
        assert missing is False

    def test_model_count(self):
        reg = PricingRegistry()
        assert reg.model_count == 0
        reg.register("openai", "gpt-4o", input_price=2.50, output_price=10.00)
        assert reg.model_count == 1
        reg.register("openai", "gpt-4o-mini", input_price=0.15, output_price=0.60)
        assert reg.model_count == 2

    def test_register_overwrite(self):
        reg = PricingRegistry()
        reg.register("openai", "gpt-4o", input_price=2.50, output_price=10.00)
        reg.register("openai", "gpt-4o", input_price=3.00, output_price=12.00)
        entry = reg.lookup("openai", "gpt-4o")
        assert entry.input_price == 3.00
        assert entry.output_price == 12.00
        assert reg.model_count == 1

    def test_load_from_dict(self):
        data = {
            "openai": {
                "gpt-4o": {"input_price": 2.50, "output_price": 10.00},
                "gpt-4o-mini": {"input_price": 0.15, "output_price": 0.60},
            },
            "anthropic": {
                "claude-sonnet-4-20250514": {"input_price": 3.00, "output_price": 15.00},
            },
        }
        reg = PricingRegistry(entries=data)
        assert reg.model_count == 3
        entry = reg.lookup("openai", "gpt-4o")
        assert entry.input_price == 2.50
        entry2 = reg.lookup("anthropic", "claude-sonnet-4-20250514")
        assert entry2.output_price == 15.00

    def test_load_from_file(self):
        data = {
            "openai": {
                "gpt-4o": {"input_price": 2.50, "output_price": 10.00},
            },
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            f.flush()
            path = f.name

        try:
            reg = PricingRegistry(pricing_file=path)
            assert reg.model_count == 1
            entry = reg.lookup("openai", "gpt-4o")
            assert entry.input_price == 2.50
        finally:
            os.unlink(path)

    def test_load_from_env(self, monkeypatch):
        data = {
            "openai": {
                "gpt-4o": {"input_price": 5.0, "output_price": 15.0},
            },
        }
        monkeypatch.setenv("RASTIR_PRICING_DATA", json.dumps(data))
        reg = PricingRegistry()
        assert reg.model_count == 1
        entry = reg.lookup("openai", "gpt-4o")
        assert entry.input_price == 5.0

    def test_invalid_env_json(self, monkeypatch):
        monkeypatch.setenv("RASTIR_PRICING_DATA", "not-json{")
        reg = PricingRegistry()
        assert reg.model_count == 0

    def test_priority_inline_over_file(self):
        # File entry
        file_data = {
            "openai": {"gpt-4o": {"input_price": 1.0, "output_price": 5.0}},
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(file_data, f)
            f.flush()
            path = f.name

        # Inline entry overrides
        inline_data = {
            "openai": {"gpt-4o": {"input_price": 99.0, "output_price": 99.0}},
        }
        try:
            reg = PricingRegistry(entries=inline_data, pricing_file=path)
            entry = reg.lookup("openai", "gpt-4o")
            assert entry.input_price == 99.0  # inline wins
        finally:
            os.unlink(path)

    def test_load_dict_skips_invalid(self):
        data = {
            "openai": {
                "gpt-4o": {"input_price": 2.50},  # missing output_price
                "gpt-4o-mini": {"input_price": 0.15, "output_price": 0.60},
            },
            "bad_provider": "not-a-dict",
        }
        reg = PricingRegistry(entries=data)
        assert reg.model_count == 1  # only gpt-4o-mini loaded
        assert reg.lookup("openai", "gpt-4o") is None


# -----------------------------------------------------------------------
# V6 Config fields
# -----------------------------------------------------------------------

class TestCostConfig:
    def test_cost_disabled_by_default(self):
        cfg = configure(service="test")
        assert cfg.cost.enabled is False
        assert cfg.cost.pricing_profile == "default"
        assert cfg.cost.pricing_source is None
        assert cfg.cost.max_cost_per_call_alert is None

    def test_cost_enabled(self):
        cfg = configure(
            service="test",
            enable_cost_calculation=True,
            pricing_profile="enterprise_v2",
        )
        assert cfg.cost.enabled is True
        assert cfg.cost.pricing_profile == "enterprise_v2"

    def test_cost_with_file(self):
        data = {"openai": {"gpt-4o": {"input_price": 2.50, "output_price": 10.00}}}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            f.flush()
            path = f.name

        try:
            cfg = configure(
                service="test",
                enable_cost_calculation=True,
                pricing_source=path,
            )
            assert cfg.cost.enabled is True
            reg = get_pricing_registry()
            assert reg is not None
            assert reg.model_count == 1
        finally:
            os.unlink(path)

    def test_pricing_registry_none_when_disabled(self):
        configure(service="test")
        assert get_pricing_registry() is None

    def test_max_cost_alert(self):
        cfg = configure(
            service="test",
            enable_cost_calculation=True,
            max_cost_per_call_alert=0.50,
        )
        assert cfg.cost.max_cost_per_call_alert == 0.50

    def test_cost_env_vars(self, monkeypatch):
        monkeypatch.setenv("RASTIR_ENABLE_COST_CALCULATION", "true")
        monkeypatch.setenv("RASTIR_PRICING_PROFILE", "staging_q1")
        cfg = configure(service="test")
        assert cfg.cost.enabled is True
        assert cfg.cost.pricing_profile == "staging_q1"


class TestTTFTConfig:
    def test_ttft_enabled_by_default(self):
        cfg = configure(service="test")
        assert cfg.enable_ttft is True

    def test_ttft_disabled(self):
        cfg = configure(service="test", enable_ttft=False)
        assert cfg.enable_ttft is False

    def test_ttft_env_var(self, monkeypatch):
        monkeypatch.setenv("RASTIR_ENABLE_TTFT", "false")
        cfg = configure(service="test")
        assert cfg.enable_ttft is False


# -----------------------------------------------------------------------
# Server-side metrics
# -----------------------------------------------------------------------

class TestCostMetrics:
    def test_cost_metrics_recorded(self):
        from rastir.server.metrics import MetricsRegistry

        reg = MetricsRegistry()
        span = {
            "span_type": "llm",
            "status": "OK",
            "duration_ms": 1200,
            "attributes": {
                "model": "gpt-4o",
                "provider": "openai",
                "tokens_input": 500,
                "tokens_output": 100,
                "cost_usd": 0.0075,
                "pricing_profile": "default_2025_q1",
                "agent": "",
            },
        }
        reg.record_span(span, service="test-svc", env="prod")

        # Verify cost counter
        val = reg.cost_total.labels(
            service="test-svc", env="prod", model="gpt-4o",
            provider="openai", agent="", pricing_profile="default_2025_q1",
        )._value.get()
        assert abs(val - 0.0075) < 1e-9

    def test_pricing_missing_counter(self):
        from rastir.server.metrics import MetricsRegistry

        reg = MetricsRegistry()
        span = {
            "span_type": "llm",
            "status": "OK",
            "duration_ms": 800,
            "attributes": {
                "model": "unknown-model",
                "provider": "openai",
                "pricing_missing": True,
                "cost_usd": 0,
                "agent": "",
            },
        }
        reg.record_span(span, service="test-svc", env="prod")

        val = reg.pricing_missing.labels(
            service="test-svc", env="prod",
            model="unknown-model", provider="openai",
        )._value.get()
        assert val == 1.0

    def test_no_cost_metrics_when_absent(self):
        from rastir.server.metrics import MetricsRegistry

        reg = MetricsRegistry()
        span = {
            "span_type": "llm",
            "status": "OK",
            "duration_ms": 500,
            "attributes": {
                "model": "gpt-4o",
                "provider": "openai",
                "tokens_input": 100,
                "tokens_output": 50,
                "agent": "",
            },
        }
        # Should not raise even without cost attributes
        reg.record_span(span, service="test-svc", env="prod")


class TestTTFTMetrics:
    def test_ttft_histogram_recorded(self):
        from rastir.server.metrics import MetricsRegistry

        reg = MetricsRegistry()
        span = {
            "span_type": "llm",
            "status": "OK",
            "duration_ms": 3000,
            "attributes": {
                "model": "gpt-4o",
                "provider": "openai",
                "streaming": True,
                "ttft_ms": 250.0,
                "agent": "",
            },
        }
        reg.record_span(span, service="test-svc", env="prod")

        # Check that the histogram has a sample (sum should be 0.25 seconds)
        val = reg.ttft.labels(
            service="test-svc", env="prod",
            model="gpt-4o", provider="openai",
        )._sum.get()
        assert abs(val - 0.250) < 1e-6

    def test_no_ttft_when_absent(self):
        from rastir.server.metrics import MetricsRegistry

        reg = MetricsRegistry()
        span = {
            "span_type": "llm",
            "status": "OK",
            "duration_ms": 500,
            "attributes": {
                "model": "gpt-4o",
                "provider": "openai",
                "agent": "",
            },
        }
        # Should not raise
        reg.record_span(span, service="test-svc", env="prod")


# -----------------------------------------------------------------------
# PricingEntry frozen dataclass
# -----------------------------------------------------------------------

class TestPricingEntry:
    def test_frozen(self):
        entry = PricingEntry(input_price=2.50, output_price=10.00)
        with pytest.raises(AttributeError):
            entry.input_price = 5.0
