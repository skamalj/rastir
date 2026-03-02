"""Tests for the V7 SRE Engine (sre_engine.py) and SRE config model."""

from __future__ import annotations

import asyncio
import math
import time
from unittest.mock import patch

import pytest
from prometheus_client import CollectorRegistry

from rastir.server.config import (
    SREAgentConfig,
    SRESection,
    load_config,
    validate_config,
)
from rastir.server.sre_engine import (
    SREEngine,
    _AgentAccumulator,
    _Bucket,
    _LONG_WINDOW_SECONDS,
    _MONTHLY_WINDOW_SECONDS,
    _SHORT_WINDOW_SECONDS,
    _WEEKLY_WINDOW_SECONDS,
    _current_month_start_epoch,
    _current_week_start_epoch,
    _elapsed_days_in_period,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def registry():
    return CollectorRegistry()


@pytest.fixture()
def default_cfg():
    return SRESection(
        enabled=True,
        update_interval_seconds=60,
        default_slo_error_rate=0.01,
        default_cost_budget_usd=100.0,
        agents={},
    )


@pytest.fixture()
def engine(default_cfg, registry):
    return SREEngine(cfg=default_cfg, registry=registry)


# ===========================================================================
# A. Config dataclass tests
# ===========================================================================


class TestSREAgentConfig:
    def test_defaults(self):
        ac = SREAgentConfig()
        assert ac.slo_error_rate is None
        assert ac.cost_budget_usd is None

    def test_overrides(self):
        ac = SREAgentConfig(slo_error_rate=0.05, cost_budget_usd=500.0)
        assert ac.slo_error_rate == 0.05
        assert ac.cost_budget_usd == 500.0


class TestSRESection:
    def test_defaults(self):
        s = SRESection()
        assert s.enabled is False
        assert s.update_interval_seconds == 60
        assert s.default_slo_error_rate == 0.01
        assert s.default_cost_budget_usd == 0.0  # 0 = disabled by default
        assert s.agents == {}

    def test_custom_agents(self):
        s = SRESection(
            enabled=True,
            agents={"chat": SREAgentConfig(slo_error_rate=0.02)},
        )
        assert s.agents["chat"].slo_error_rate == 0.02


# ===========================================================================
# B. Config validation tests
# ===========================================================================


class TestSREConfigValidation:
    """Validate that validate_config catches bad SRE values."""

    def _make_cfg_with_sre(self, **sre_kwargs):
        """Load default config then replace SRE section."""
        cfg = load_config()
        # Create a mutated copy (frozen dataclass — use object.__setattr__)
        sre = SRESection(**{**{
            "enabled": True,
            "update_interval_seconds": 60,
            "default_slo_error_rate": 0.01,
            "default_cost_budget_usd": 100.0,
            "agents": {},
        }, **sre_kwargs})
        object.__setattr__(cfg, "sre", sre)
        return cfg

    def test_valid_sre_config(self):
        cfg = self._make_cfg_with_sre()
        validate_config(cfg)  # should not raise

    def test_bad_update_interval(self):
        cfg = self._make_cfg_with_sre(update_interval_seconds=0)
        with pytest.raises(Exception, match="update_interval_seconds"):
            validate_config(cfg)

    def test_bad_slo_error_rate_zero(self):
        cfg = self._make_cfg_with_sre(default_slo_error_rate=0.0)
        with pytest.raises(Exception, match="default_slo_error_rate"):
            validate_config(cfg)

    def test_bad_slo_error_rate_above_one(self):
        cfg = self._make_cfg_with_sre(default_slo_error_rate=1.5)
        with pytest.raises(Exception, match="default_slo_error_rate"):
            validate_config(cfg)

    def test_slo_error_rate_one_is_valid(self):
        cfg = self._make_cfg_with_sre(default_slo_error_rate=1.0)
        validate_config(cfg)  # boundary — should pass

    def test_bad_cost_budget_negative(self):
        cfg = self._make_cfg_with_sre(default_cost_budget_usd=-1.0)
        with pytest.raises(Exception, match="default_cost_budget_usd"):
            validate_config(cfg)

    def test_bad_agent_slo(self):
        cfg = self._make_cfg_with_sre(
            agents={"bad_agent": SREAgentConfig(slo_error_rate=2.0)}
        )
        with pytest.raises(Exception, match="bad_agent.*slo_error_rate"):
            validate_config(cfg)

    def test_bad_agent_cost(self):
        cfg = self._make_cfg_with_sre(
            agents={"bad_agent": SREAgentConfig(cost_budget_usd=-5.0)}
        )
        with pytest.raises(Exception, match="bad_agent.*cost_budget_usd"):
            validate_config(cfg)


# ===========================================================================
# C. Accumulator unit tests
# ===========================================================================


class TestAgentAccumulator:
    def test_record_creates_bucket(self):
        acc = _AgentAccumulator()
        now = time.time()
        acc.record(now, is_error=False, cost=0.5)
        assert len(acc.buckets) == 1
        assert acc.buckets[0].requests == 1
        assert acc.buckets[0].errors == 0
        assert acc.buckets[0].cost == 0.5

    def test_record_error(self):
        acc = _AgentAccumulator()
        now = time.time()
        acc.record(now, is_error=True, cost=0.0)
        assert acc.buckets[0].errors == 1
        assert acc.week_errors == 1
        assert acc.month_errors == 1

    def test_calendar_accumulators_increment(self):
        acc = _AgentAccumulator()
        now = time.time()
        acc.record(now, is_error=False, cost=1.0)
        acc.record(now, is_error=True, cost=2.0)
        assert acc.week_requests == 2
        assert acc.month_requests == 2
        assert acc.week_cost == 3.0
        assert acc.month_cost == 3.0

    def test_same_minute_reuses_bucket(self):
        acc = _AgentAccumulator()
        # Align to start of a bucket so +10s stays in same bucket
        now = time.time()
        aligned = now - (now % 60) + 5  # 5s into a minute bucket
        acc.record(aligned, is_error=False, cost=0.0)
        acc.record(aligned + 10, is_error=False, cost=0.0)  # 15s into same minute
        assert len(acc.buckets) == 1
        assert acc.buckets[0].requests == 2

    def test_different_minute_creates_new_bucket(self):
        acc = _AgentAccumulator()
        now = time.time()
        acc.record(now, is_error=False, cost=0.0)
        acc.record(now + 61, is_error=False, cost=0.0)
        assert len(acc.buckets) == 2

    def test_prune_drops_old_buckets(self):
        acc = _AgentAccumulator()
        old = time.time() - _MONTHLY_WINDOW_SECONDS - 120
        acc.record(old, is_error=False, cost=0.0)
        recent = time.time()
        acc.record(recent, is_error=False, cost=0.0)
        acc.prune(recent)
        assert len(acc.buckets) == 1
        assert acc.buckets[0].timestamp >= recent - 60

    def test_rolling_requests_window(self):
        acc = _AgentAccumulator()
        now = time.time()
        # Record 5 events at "now"
        for _ in range(5):
            acc.record(now, is_error=False, cost=0.0)
        # Record 3 events 2 hours ago
        old = now - 2 * 3600
        for _ in range(3):
            acc.record(old, is_error=False, cost=0.0)

        assert acc.rolling_requests(now, _SHORT_WINDOW_SECONDS) == 5  # 1h window
        assert acc.rolling_requests(now, _LONG_WINDOW_SECONDS) == 8   # 6h window

    def test_rolling_errors_window(self):
        acc = _AgentAccumulator()
        now = time.time()
        acc.record(now, is_error=True, cost=0.0)
        acc.record(now - 2 * 3600, is_error=True, cost=0.0)
        assert acc.rolling_errors(now, _SHORT_WINDOW_SECONDS) == 1
        assert acc.rolling_errors(now, _LONG_WINDOW_SECONDS) == 2

    def test_rolling_cost_window(self):
        acc = _AgentAccumulator()
        now = time.time()
        acc.record(now, is_error=False, cost=1.5)
        acc.record(now - 7200, is_error=False, cost=2.5)
        assert acc.rolling_cost(now, _SHORT_WINDOW_SECONDS) == 1.5
        assert acc.rolling_cost(now, _LONG_WINDOW_SECONDS) == 4.0

    def test_maybe_reset_week(self):
        acc = _AgentAccumulator()
        now = time.time()
        acc.record(now, is_error=True, cost=10.0)
        assert acc.week_requests == 1

        # Simulate a new week boundary
        acc.maybe_reset_week(now + 1)
        assert acc.week_requests == 0
        assert acc.week_errors == 0
        assert acc.week_cost == 0.0
        assert acc.week_start == now + 1

    def test_maybe_reset_month(self):
        acc = _AgentAccumulator()
        now = time.time()
        acc.record(now, is_error=False, cost=5.0)
        acc.maybe_reset_month(now + 1)
        assert acc.month_requests == 0
        assert acc.month_cost == 0.0

    def test_reset_idempotent(self):
        acc = _AgentAccumulator()
        now = time.time()
        acc.maybe_reset_week(now)
        acc.record(now, is_error=False, cost=1.0)
        acc.maybe_reset_week(now)  # same boundary — should NOT reset
        assert acc.week_requests == 1


# ===========================================================================
# D. Calendar helper tests
# ===========================================================================


class TestCalendarHelpers:
    def test_week_start_is_monday(self):
        from datetime import datetime, timezone
        epoch = _current_week_start_epoch()
        dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
        assert dt.weekday() == 0  # Monday
        assert dt.hour == 0 and dt.minute == 0 and dt.second == 0

    def test_month_start_is_first(self):
        from datetime import datetime, timezone
        epoch = _current_month_start_epoch()
        dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
        assert dt.day == 1
        assert dt.hour == 0 and dt.minute == 0 and dt.second == 0

    def test_elapsed_days_min(self):
        assert _elapsed_days_in_period(100.0, 100.0) == 0.01

    def test_elapsed_days_one_day(self):
        start = 1000.0
        now = start + 86400.0
        assert abs(_elapsed_days_in_period(start, now) - 1.0) < 0.001


# ===========================================================================
# E. SRE Engine core tests
# ===========================================================================


class TestSREEngineInit:
    def test_creates_gauges(self, engine):
        assert engine.expected_volume is not None
        assert engine.error_budget_total is not None
        assert engine.error_budget_remaining is not None
        assert engine.error_budget_consumed_pct is not None
        assert engine.error_burn_rate_short is not None
        assert engine.error_burn_rate_long is not None
        assert engine.error_days_to_exhaustion is not None
        assert engine.cost_budget_total is not None
        assert engine.cost_budget_remaining is not None
        assert engine.cost_budget_consumed_pct is not None
        assert engine.cost_burn_rate_daily is not None
        assert engine.cost_days_to_exhaustion is not None
        assert engine.sla_status is not None

    def test_all_gauges_registered(self, registry):
        """All 13 SRE gauges registered in the collector registry."""
        cfg = SRESection(enabled=True)
        eng = SREEngine(cfg=cfg, registry=registry)
        # Collect all metric family names
        names = {m.name for m in registry.collect()}
        expected = {
            "rastir_expected_volume",
            "rastir_error_budget_total",
            "rastir_error_budget_remaining",
            "rastir_error_budget_consumed_percent",
            "rastir_error_burn_rate_short",
            "rastir_error_burn_rate_long",
            "rastir_error_days_to_exhaustion",
            "rastir_cost_budget_total",
            "rastir_cost_budget_remaining",
            "rastir_cost_budget_consumed_percent",
            "rastir_cost_burn_rate_daily",
            "rastir_cost_days_to_exhaustion",
            "rastir_sla_status",
        }
        assert expected.issubset(names)


class TestSREEngineRecordEvent:
    def test_record_basic(self, engine):
        engine.record_event("svc", "prod", "chat", False, 0.5)
        key = ("svc", "prod", "chat")
        assert key in engine._accumulators
        acc = engine._accumulators[key]
        assert acc.week_requests == 1
        assert acc.week_cost == 0.5

    def test_record_error(self, engine):
        engine.record_event("svc", "prod", "chat", True, 0.0)
        acc = engine._accumulators[("svc", "prod", "chat")]
        assert acc.week_errors == 1
        assert acc.month_errors == 1

    def test_cardinality_guard(self, engine):
        """Once _MAX_AGENT_KEYS is exceeded, new keys are silently dropped."""
        from rastir.server.sre_engine import _MAX_AGENT_KEYS
        for i in range(_MAX_AGENT_KEYS):
            engine.record_event("svc", "prod", f"agent_{i}", False, 0.0)
        assert len(engine._seen_keys) == _MAX_AGENT_KEYS

        # One more should be dropped
        engine.record_event("svc", "prod", "overflow_agent", False, 1.0)
        assert ("svc", "prod", "overflow_agent") not in engine._accumulators


class TestSREEngineRecompute:
    def test_recompute_basic_gauges(self, engine, registry):
        """After recording events and recomputing, gauges reflect state."""
        # Record 100 OK events + 2 errors
        for _ in range(100):
            engine.record_event("svc", "prod", "chat", False, 0.5)
        for _ in range(2):
            engine.record_event("svc", "prod", "chat", True, 0.0)

        engine._recompute()

        # Expected volume (rolling 7d): should be 102
        w_lbl = {"service": "svc", "env": "prod", "agent": "chat", "period": "week"}
        m_lbl = {"service": "svc", "env": "prod", "agent": "chat", "period": "month"}

        assert engine.expected_volume.labels(**w_lbl)._value.get() == 102
        assert engine.expected_volume.labels(**m_lbl)._value.get() == 102

    def test_error_budget_computation(self, engine):
        """Error budget = expected_volume × slo_error_rate."""
        for _ in range(1000):
            engine.record_event("svc", "prod", "chat", False, 0.0)
        for _ in range(5):
            engine.record_event("svc", "prod", "chat", True, 0.0)

        engine._recompute()

        lbl = {"service": "svc", "env": "prod", "agent": "chat", "period": "week"}
        # Expected volume = 1005, slo = 0.01 → budget_total = 10.05
        budget_total = engine.error_budget_total.labels(**lbl)._value.get()
        assert abs(budget_total - 10.05) < 0.1

        # Remaining = budget_total - 5 (period errors)
        remaining = engine.error_budget_remaining.labels(**lbl)._value.get()
        assert abs(remaining - (budget_total - 5)) < 0.01

        # Consumed percent = (5 / budget_total) * 100
        consumed = engine.error_budget_consumed_pct.labels(**lbl)._value.get()
        expected_pct = (5 / budget_total) * 100.0
        assert abs(consumed - expected_pct) < 1.0

    def test_sla_status_healthy(self, engine):
        for _ in range(100):
            engine.record_event("svc", "prod", "chat", False, 0.0)
        engine._recompute()
        lbl = {"service": "svc", "env": "prod", "agent": "chat", "period": "week"}
        assert engine.sla_status.labels(**lbl)._value.get() == 1

    def test_sla_status_breached(self, engine):
        """When errors far exceed budget, SLA = 0."""
        # 100 requests, slo=0.01 → budget=1 error, record 20 errors to clearly breach
        for _ in range(80):
            engine.record_event("svc", "prod", "chat", False, 0.0)
        for _ in range(20):
            engine.record_event("svc", "prod", "chat", True, 0.0)

        engine._recompute()
        lbl = {"service": "svc", "env": "prod", "agent": "chat", "period": "week"}
        # budget_total = 100 * 0.01 = 1.0, remaining = 1.0 - 20 = -19 → SLA=0
        assert engine.sla_status.labels(**lbl)._value.get() == 0

    def test_cost_budget_gauges(self, engine):
        for _ in range(10):
            engine.record_event("svc", "prod", "chat", False, 5.0)

        engine._recompute()
        lbl = {"service": "svc", "env": "prod", "agent": "chat", "period": "week"}

        # cost_budget_total = 100.0 (from default_cfg fixture)
        assert engine.cost_budget_total.labels(**lbl)._value.get() == 100.0

        # week_cost should be 50.0 (10 events × $5)
        # cost_budget_remaining = 100.0 - 50.0 = 50.0
        remaining = engine.cost_budget_remaining.labels(**lbl)._value.get()
        assert abs(remaining - 50.0) < 0.01

        # cost_consumed_pct = (50 / 100) * 100 = 50%
        consumed = engine.cost_budget_consumed_pct.labels(**lbl)._value.get()
        assert abs(consumed - 50.0) < 0.01

    def test_cost_days_to_exhaustion(self, engine):
        """With steady cost burn, days-to-exhaustion should be finite."""
        for _ in range(10):
            engine.record_event("svc", "prod", "chat", False, 5.0)

        engine._recompute()
        lbl = {"service": "svc", "env": "prod", "agent": "chat", "period": "week"}

        days_val = engine.cost_days_to_exhaustion.labels(**lbl)._value.get()
        # Should be finite (remaining > 0 and daily_burn > 0)
        assert days_val > 0.0
        assert days_val < 9999.0  # not infinite

    def test_burn_rate_short(self, engine):
        """Short burn rate = error_rate_1h / slo."""
        # 100 requests in 1h window, 10 errors → error_rate=0.1, slo=0.01 → burn=10
        now = time.time()
        acc = engine._accumulators[("svc", "prod", "chat")]
        for _ in range(90):
            acc.record(now, False, 0.0)
        for _ in range(10):
            acc.record(now, True, 0.0)

        engine._recompute()
        lbl = {"service": "svc", "env": "prod", "agent": "chat"}
        burn = engine.error_burn_rate_short.labels(**lbl)._value.get()
        assert abs(burn - 10.0) < 0.01

    def test_burn_rate_long(self, engine):
        """Long burn rate uses 6h window."""
        now = time.time()
        acc = engine._accumulators[("svc", "prod", "chat")]

        # Events within 6h window
        for _ in range(200):
            acc.record(now - 3600, False, 0.0)  # 1h ago
        for _ in range(4):
            acc.record(now - 3600, True, 0.0)

        engine._recompute()
        lbl = {"service": "svc", "env": "prod", "agent": "chat"}
        burn = engine.error_burn_rate_long.labels(**lbl)._value.get()
        # error_rate = 4/204 ≈ 0.0196, slo = 0.01 → burn ≈ 1.96
        assert 1.5 < burn < 2.5

    def test_error_days_to_exhaustion_infinite(self, engine):
        """No errors → days_to_exhaustion = 9999 (capped inf)."""
        for _ in range(100):
            engine.record_event("svc", "prod", "chat", False, 0.0)
        engine._recompute()
        lbl = {"service": "svc", "env": "prod", "agent": "chat", "period": "week"}
        days = engine.error_days_to_exhaustion.labels(**lbl)._value.get()
        assert days == 9999.0

    def test_multiple_agents(self, engine):
        """Different agents get independent metrics."""
        engine.record_event("svc", "prod", "chat", False, 1.0)
        engine.record_event("svc", "prod", "search", True, 2.0)
        engine._recompute()

        chat_lbl = {"service": "svc", "env": "prod", "agent": "chat", "period": "week"}
        search_lbl = {"service": "svc", "env": "prod", "agent": "search", "period": "week"}

        assert engine.expected_volume.labels(**chat_lbl)._value.get() == 1
        assert engine.expected_volume.labels(**search_lbl)._value.get() == 1
        assert engine.sla_status.labels(**chat_lbl)._value.get() == 1


class TestSREEngineAgentOverrides:
    def test_per_agent_slo(self, registry):
        """Per-agent SLO overrides default_slo_error_rate."""
        cfg = SRESection(
            enabled=True,
            default_slo_error_rate=0.01,
            default_cost_budget_usd=100.0,
            agents={"chat": SREAgentConfig(slo_error_rate=0.10)},
        )
        eng = SREEngine(cfg=cfg, registry=registry)

        # 100 requests, 5 errors → default slo=0.01 would breach, but agent slo=0.10 → ok
        for _ in range(95):
            eng.record_event("svc", "prod", "chat", False, 0.0)
        for _ in range(5):
            eng.record_event("svc", "prod", "chat", True, 0.0)

        eng._recompute()
        lbl = {"service": "svc", "env": "prod", "agent": "chat", "period": "week"}
        # budget_total = 100 * 0.10 = 10.0
        budget_total = eng.error_budget_total.labels(**lbl)._value.get()
        assert abs(budget_total - 10.0) < 0.1
        # remaining = budget_total - 5
        remaining = eng.error_budget_remaining.labels(**lbl)._value.get()
        assert abs(remaining - (budget_total - 5)) < 0.01
        assert eng.sla_status.labels(**lbl)._value.get() == 1

    def test_per_agent_cost_budget(self, registry):
        cfg = SRESection(
            enabled=True,
            default_cost_budget_usd=100.0,
            agents={"expensive": SREAgentConfig(cost_budget_usd=500.0)},
        )
        eng = SREEngine(cfg=cfg, registry=registry)
        eng.record_event("svc", "prod", "expensive", False, 10.0)
        eng._recompute()
        lbl = {"service": "svc", "env": "prod", "agent": "expensive", "period": "week"}
        assert eng.cost_budget_total.labels(**lbl)._value.get() == 500.0

    def test_default_used_for_unknown_agent(self, registry):
        cfg = SRESection(
            enabled=True,
            default_slo_error_rate=0.05,
            default_cost_budget_usd=200.0,
            agents={"chat": SREAgentConfig(slo_error_rate=0.10)},
        )
        eng = SREEngine(cfg=cfg, registry=registry)
        eng.record_event("svc", "prod", "unknown_agent", False, 0.0)
        eng._recompute()
        lbl = {"service": "svc", "env": "prod", "agent": "unknown_agent", "period": "week"}
        # Should use default slo=0.05 → budget_total = 1 * 0.05
        assert abs(eng.error_budget_total.labels(**lbl)._value.get() - 0.05) < 0.001


# ===========================================================================
# F. Async lifecycle tests
# ===========================================================================


class TestSREEngineLifecycle:
    @pytest.mark.asyncio
    async def test_start_stop(self, engine):
        await engine.start()
        assert engine._task is not None
        assert not engine._task.done()

        await engine.stop()
        assert engine._stopped is True

    @pytest.mark.asyncio
    async def test_recompute_runs_periodically(self, registry):
        """Engine recomputes on schedule (use very short interval)."""
        cfg = SRESection(enabled=True, update_interval_seconds=0.05)
        eng = SREEngine(cfg=cfg, registry=registry)

        eng.record_event("svc", "prod", "chat", False, 1.0)
        await eng.start()
        await asyncio.sleep(0.15)  # wait for ~3 recompute cycles
        await eng.stop()

        lbl = {"service": "svc", "env": "prod", "agent": "chat", "period": "week"}
        # Gauge should have been updated
        val = eng.expected_volume.labels(**lbl)._value.get()
        assert val == 1

    @pytest.mark.asyncio
    async def test_stop_without_start(self, engine):
        """stop() is safe to call even if start() was never called."""
        await engine.stop()  # should not raise


# ===========================================================================
# G. Edge case tests
# ===========================================================================


class TestEdgeCases:
    def test_zero_requests_no_division_error(self, engine):
        """Recompute with empty accumulators must not crash."""
        engine._recompute()  # should be a no-op

    def test_zero_budget_no_division_error(self, registry):
        """cost_budget_usd=0 → consumed_pct=0 (not div-by-zero)."""
        cfg = SRESection(enabled=True, default_cost_budget_usd=0.0)
        eng = SREEngine(cfg=cfg, registry=registry)
        eng.record_event("svc", "prod", "chat", False, 5.0)
        eng._recompute()  # should not raise

        lbl = {"service": "svc", "env": "prod", "agent": "chat", "period": "week"}
        assert eng.cost_budget_consumed_pct.labels(**lbl)._value.get() == 0.0

    def test_all_errors(self, engine):
        """100% error rate should breach SLA."""
        for _ in range(50):
            engine.record_event("svc", "prod", "bot", True, 0.0)
        engine._recompute()

        lbl = {"service": "svc", "env": "prod", "agent": "bot", "period": "week"}
        # expected_volume=50, slo=0.01 → budget_total=0.5, errors=50
        budget_total = engine.error_budget_total.labels(**lbl)._value.get()
        assert budget_total > 0  # 50 * 0.01 = 0.5
        consumed = engine.error_budget_consumed_pct.labels(**lbl)._value.get()
        assert consumed > 100.0  # 50/0.5 * 100 = 10000%
        assert engine.sla_status.labels(**lbl)._value.get() == 0

    def test_large_cost_exceeds_budget(self, engine):
        engine.record_event("svc", "prod", "chat", False, 200.0)
        engine._recompute()
        lbl = {"service": "svc", "env": "prod", "agent": "chat", "period": "week"}
        # budget=100, spent=200 → remaining = -100
        remaining = engine.cost_budget_remaining.labels(**lbl)._value.get()
        assert remaining < 0
        days = engine.cost_days_to_exhaustion.labels(**lbl)._value.get()
        assert days == 0.0  # already exhausted

    def test_inf_capped_to_9999(self, engine):
        """When daily_error_rate is 0, days_to_exhaustion → 9999."""
        for _ in range(10):
            engine.record_event("svc", "prod", "chat", False, 0.0)
        engine._recompute()
        lbl = {"service": "svc", "env": "prod", "agent": "chat", "period": "week"}
        assert engine.error_days_to_exhaustion.labels(**lbl)._value.get() == 9999.0
        assert engine.cost_days_to_exhaustion.labels(**lbl)._value.get() == 9999.0
