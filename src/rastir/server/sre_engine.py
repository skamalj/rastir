"""V7 SRE Engine — SLO / SLA / Error Budget / Cost Budget / Burn Rate.

This module is entirely server-side.  It maintains in-memory rolling
accumulators fed by the ingestion pipeline and periodically refreshes a
set of derived Prometheus **Gauge** metrics consumed by Grafana and
alerting rules.

Key design choices
------------------
* No histograms — every SRE metric is a ``Gauge``.
* Labels: ``service``, ``env``, ``agent``, ``period`` (where applicable).
* Burn-rate windows are fixed: **1 h** (short), **6 h** (long).
* Rolling estimation windows: **7 d** (weekly), **30 d** (monthly).
* Calendar-based period boundaries for error/cost budget consumption.
* Update interval is configurable (default 60 s).
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from prometheus_client import CollectorRegistry, Gauge

from rastir.server.config import SREAgentConfig, SRESection

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SHORT_WINDOW_SECONDS = 3600       # 1 h
_LONG_WINDOW_SECONDS = 6 * 3600   # 6 h
_WEEKLY_WINDOW_SECONDS = 7 * 86400   # 7 d
_MONTHLY_WINDOW_SECONDS = 30 * 86400  # 30 d
_BUCKET_GRANULARITY = 60  # 1-minute buckets for rolling windows

_PERIODS = ("week", "month")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Bucket:
    """One-minute accumulation bucket."""
    timestamp: float  # start of bucket (epoch)
    requests: int = 0
    errors: int = 0
    cost: float = 0.0


@dataclass
class _AgentAccumulator:
    """Per (service, env, agent) rolling counters."""

    # Deque of _Bucket sorted by timestamp (oldest first).
    # We keep up to 30 days of 1-minute buckets ≈ 43 200 entries.
    buckets: deque[_Bucket] = field(default_factory=deque)

    # Calendar-period accumulators (reset at period boundary).
    week_requests: int = 0
    week_errors: int = 0
    week_cost: float = 0.0
    week_start: float = 0.0   # epoch of current week start

    month_requests: int = 0
    month_errors: int = 0
    month_cost: float = 0.0
    month_start: float = 0.0  # epoch of current month start

    def __post_init__(self) -> None:
        # Initialise period starts to current calendar boundaries so that
        # the first ``_recompute`` does not spuriously reset counters.
        if self.week_start == 0.0:
            self.week_start = _current_week_start_epoch()
        if self.month_start == 0.0:
            self.month_start = _current_month_start_epoch()

    def _current_bucket(self, now: float) -> _Bucket:
        bucket_ts = now - (now % _BUCKET_GRANULARITY)
        if self.buckets and self.buckets[-1].timestamp == bucket_ts:
            return self.buckets[-1]
        b = _Bucket(timestamp=bucket_ts)
        self.buckets.append(b)
        return b

    def record(self, now: float, is_error: bool, cost: float) -> None:
        b = self._current_bucket(now)
        b.requests += 1
        if is_error:
            b.errors += 1
        b.cost += cost

        # Calendar accumulators
        self.week_requests += 1
        self.month_requests += 1
        if is_error:
            self.week_errors += 1
            self.month_errors += 1
        self.week_cost += cost
        self.month_cost += cost

    def prune(self, now: float) -> None:
        """Drop buckets older than 30 days."""
        cutoff = now - _MONTHLY_WINDOW_SECONDS
        while self.buckets and self.buckets[0].timestamp < cutoff:
            self.buckets.popleft()

    # ---- Rolling queries --------------------------------------------------

    def rolling_requests(self, now: float, window_seconds: int) -> int:
        cutoff = now - window_seconds
        return sum(b.requests for b in self.buckets if b.timestamp >= cutoff)

    def rolling_errors(self, now: float, window_seconds: int) -> int:
        cutoff = now - window_seconds
        return sum(b.errors for b in self.buckets if b.timestamp >= cutoff)

    def rolling_cost(self, now: float, window_seconds: int) -> float:
        cutoff = now - window_seconds
        return sum(b.cost for b in self.buckets if b.timestamp >= cutoff)

    # ---- Calendar period resets -------------------------------------------

    def maybe_reset_week(self, week_start_epoch: float) -> None:
        if self.week_start < week_start_epoch:
            self.week_requests = 0
            self.week_errors = 0
            self.week_cost = 0.0
            self.week_start = week_start_epoch

    def maybe_reset_month(self, month_start_epoch: float) -> None:
        if self.month_start < month_start_epoch:
            self.month_requests = 0
            self.month_errors = 0
            self.month_cost = 0.0
            self.month_start = month_start_epoch


# ---------------------------------------------------------------------------
# Calendar helpers
# ---------------------------------------------------------------------------

def _current_week_start_epoch() -> float:
    """Return epoch timestamp of Monday 00:00 UTC for the current week."""
    now = datetime.now(timezone.utc)
    monday = now.replace(hour=0, minute=0, second=0, microsecond=0)
    monday = monday - __import__("datetime").timedelta(days=monday.weekday())
    return monday.timestamp()


def _current_month_start_epoch() -> float:
    """Return epoch timestamp of 1st 00:00 UTC for the current month."""
    now = datetime.now(timezone.utc)
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return first.timestamp()


def _elapsed_days_in_period(period_start: float, now: float) -> float:
    """Return fractional days elapsed since period start (min 0.01)."""
    elapsed = max(now - period_start, 0.0)
    return max(elapsed / 86400.0, 0.01)  # avoid div-by-zero


# ---------------------------------------------------------------------------
# SRE Engine
# ---------------------------------------------------------------------------

_AgentKey = tuple[str, str, str]  # (service, env, agent)

# Cardinality cap for (service, env, agent) combo count
_MAX_AGENT_KEYS = 500


class SREEngine:
    """Server-side SRE layer: rolling accumulators + periodic gauge refresh.

    Instantiated once during server startup and kept alive for the process
    lifetime.  Thread-safe for ``record_event`` (called from ingestion
    worker thread) via the GIL — no lock required for dict/deque ops in
    CPython.
    """

    def __init__(
        self,
        cfg: SRESection,
        registry: CollectorRegistry,
    ) -> None:
        self._cfg = cfg
        self._registry = registry
        self._accumulators: dict[_AgentKey, _AgentAccumulator] = defaultdict(
            _AgentAccumulator,
        )
        self._seen_keys: set[_AgentKey] = set()
        self._task: Optional[asyncio.Task[None]] = None
        self._stopped = False

        # ---- Register Prometheus Gauges -----------------------------------
        _labels_with_period = ["service", "env", "agent", "period"]
        _labels_no_period = ["service", "env", "agent"]

        self.expected_volume = Gauge(
            "rastir_expected_volume",
            "Rolling request volume used to estimate allowed budget",
            _labels_with_period,
            registry=registry,
        )

        # Error budget
        self.error_budget_total = Gauge(
            "rastir_error_budget_total",
            "Total allowed errors for current period",
            _labels_with_period,
            registry=registry,
        )
        self.error_budget_remaining = Gauge(
            "rastir_error_budget_remaining",
            "Remaining allowed errors for current SLA period",
            _labels_with_period,
            registry=registry,
        )
        self.error_budget_consumed_pct = Gauge(
            "rastir_error_budget_consumed_percent",
            "Percentage of error budget consumed",
            _labels_with_period,
            registry=registry,
        )

        # Burn rates (no period label)
        self.error_burn_rate_short = Gauge(
            "rastir_error_burn_rate_short",
            "Short-window (1h) error burn rate",
            _labels_no_period,
            registry=registry,
        )
        self.error_burn_rate_long = Gauge(
            "rastir_error_burn_rate_long",
            "Long-window (6h) error burn rate",
            _labels_no_period,
            registry=registry,
        )

        # Error exhaustion
        self.error_days_to_exhaustion = Gauge(
            "rastir_error_days_to_exhaustion",
            "Estimated days until error budget exhaustion",
            _labels_with_period,
            registry=registry,
        )

        # Cost budget
        self.cost_budget_total = Gauge(
            "rastir_cost_budget_total",
            "Configured cost budget for current period",
            _labels_with_period,
            registry=registry,
        )
        self.cost_budget_remaining = Gauge(
            "rastir_cost_budget_remaining",
            "Remaining cost budget",
            _labels_with_period,
            registry=registry,
        )
        self.cost_budget_consumed_pct = Gauge(
            "rastir_cost_budget_consumed_percent",
            "Percent of cost budget consumed",
            _labels_with_period,
            registry=registry,
        )
        self.cost_burn_rate_daily = Gauge(
            "rastir_cost_burn_rate_daily",
            "Average cost burn per elapsed day in current period",
            _labels_with_period,
            registry=registry,
        )
        self.cost_days_to_exhaustion = Gauge(
            "rastir_cost_days_to_exhaustion",
            "Estimated days until cost budget exhaustion",
            _labels_with_period,
            registry=registry,
        )

        # SLA status
        self.sla_status = Gauge(
            "rastir_sla_status",
            "Current SLA health indicator (1=healthy, 0=breached)",
            _labels_with_period,
            registry=registry,
        )

        logger.info(
            "SRE engine initialised  update_interval=%ds  default_slo=%.4f  "
            "default_cost_budget=$%.2f  agent_overrides=%d",
            cfg.update_interval_seconds,
            cfg.default_slo_error_rate,
            cfg.default_cost_budget_usd,
            len(cfg.agents),
        )

    # ---- Agent SLO / budget resolution ------------------------------------

    def _agent_cfg(self, agent: str) -> SREAgentConfig:
        return self._cfg.agents.get(agent, SREAgentConfig())

    def _slo_error_rate(self, agent: str) -> float:
        ac = self._agent_cfg(agent)
        if ac.slo_error_rate is not None:
            return ac.slo_error_rate
        return self._cfg.default_slo_error_rate

    def _cost_budget(self, agent: str) -> float:
        ac = self._agent_cfg(agent)
        if ac.cost_budget_usd is not None:
            return ac.cost_budget_usd
        return self._cfg.default_cost_budget_usd

    # ---- Event ingestion (called per span) --------------------------------

    def record_event(
        self,
        service: str,
        env: str,
        agent: str,
        is_error: bool,
        cost: float,
    ) -> None:
        """Record a single span event into the rolling accumulators.

        Called from the ingestion worker thread for every processed span.
        Must be fast — O(1) amortised.
        """
        key: _AgentKey = (service, env, agent)

        # Cardinality guard
        if key not in self._seen_keys:
            if len(self._seen_keys) >= _MAX_AGENT_KEYS:
                return  # silently drop to protect memory
            self._seen_keys.add(key)

        acc = self._accumulators[key]
        now = time.time()
        acc.record(now, is_error, cost)

    # ---- Periodic recompute -----------------------------------------------

    async def start(self) -> None:
        """Start the periodic recompute loop."""
        self._stopped = False
        self._task = asyncio.ensure_future(self._loop())
        logger.info("SRE engine recompute loop started")

    async def stop(self) -> None:
        """Cancel the periodic recompute loop."""
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("SRE engine stopped")

    async def _loop(self) -> None:
        interval = self._cfg.update_interval_seconds
        while not self._stopped:
            try:
                self._recompute()
            except Exception:
                logger.exception("SRE recompute failed")
            await asyncio.sleep(interval)

    def _recompute(self) -> None:
        """Refresh all SRE gauges from current accumulator state."""
        now = time.time()
        week_start = _current_week_start_epoch()
        month_start = _current_month_start_epoch()

        for key, acc in list(self._accumulators.items()):
            service, env, agent = key

            # Calendar period resets
            acc.maybe_reset_week(week_start)
            acc.maybe_reset_month(month_start)

            # Prune old buckets
            acc.prune(now)

            slo = self._slo_error_rate(agent)
            cost_budget = self._cost_budget(agent)

            # ---- Burn rates (no period dimension) -------------------------
            self._compute_burn_rates(service, env, agent, acc, now, slo)

            # ---- Per-period metrics ---------------------------------------
            for period in _PERIODS:
                if period == "week":
                    rolling_window = _WEEKLY_WINDOW_SECONDS
                    period_errors = acc.week_errors
                    period_cost = acc.week_cost
                    period_start = week_start
                else:
                    rolling_window = _MONTHLY_WINDOW_SECONDS
                    period_errors = acc.month_errors
                    period_cost = acc.month_cost
                    period_start = month_start

                lbl = dict(service=service, env=env, agent=agent, period=period)
                elapsed_days = _elapsed_days_in_period(period_start, now)

                # Expected volume (rolling)
                expected = acc.rolling_requests(now, rolling_window)
                self.expected_volume.labels(**lbl).set(expected)

                # Error budget
                budget_total = expected * slo
                budget_remaining = budget_total - period_errors
                consumed_pct = (
                    (period_errors / budget_total) * 100.0
                    if budget_total > 0
                    else 0.0
                )
                self.error_budget_total.labels(**lbl).set(budget_total)
                self.error_budget_remaining.labels(**lbl).set(budget_remaining)
                self.error_budget_consumed_pct.labels(**lbl).set(consumed_pct)

                # Error days to exhaustion
                daily_error_rate = period_errors / elapsed_days if elapsed_days > 0 else 0.0
                if daily_error_rate > 0 and budget_remaining > 0:
                    days_to_exhaust = budget_remaining / daily_error_rate
                else:
                    days_to_exhaust = float("inf") if budget_remaining >= 0 else 0.0
                self.error_days_to_exhaustion.labels(**lbl).set(
                    days_to_exhaust if math.isfinite(days_to_exhaust) else 9999.0
                )

                # Cost budget
                self.cost_budget_total.labels(**lbl).set(cost_budget)
                cost_remaining = cost_budget - period_cost
                cost_consumed_pct = (
                    (period_cost / cost_budget) * 100.0
                    if cost_budget > 0
                    else 0.0
                )
                self.cost_budget_remaining.labels(**lbl).set(cost_remaining)
                self.cost_budget_consumed_pct.labels(**lbl).set(cost_consumed_pct)

                # Cost burn rate (daily)
                daily_cost = period_cost / elapsed_days if elapsed_days > 0 else 0.0
                self.cost_burn_rate_daily.labels(**lbl).set(daily_cost)

                # Cost days to exhaustion
                if daily_cost > 0 and cost_remaining > 0:
                    cost_exhaust = cost_remaining / daily_cost
                else:
                    cost_exhaust = float("inf") if cost_remaining >= 0 else 0.0
                self.cost_days_to_exhaustion.labels(**lbl).set(
                    cost_exhaust if math.isfinite(cost_exhaust) else 9999.0
                )

                # SLA status
                sla = 1 if budget_remaining > 0 else 0
                self.sla_status.labels(**lbl).set(sla)

        logger.debug("SRE recompute done  agents=%d", len(self._accumulators))

    def _compute_burn_rates(
        self,
        service: str,
        env: str,
        agent: str,
        acc: _AgentAccumulator,
        now: float,
        slo: float,
    ) -> None:
        lbl = dict(service=service, env=env, agent=agent)

        # Short burn rate (1h)
        req_1h = acc.rolling_requests(now, _SHORT_WINDOW_SECONDS)
        err_1h = acc.rolling_errors(now, _SHORT_WINDOW_SECONDS)
        if req_1h > 0 and slo > 0:
            error_rate_1h = err_1h / req_1h
            burn_short = error_rate_1h / slo
        else:
            burn_short = 0.0
        self.error_burn_rate_short.labels(**lbl).set(burn_short)

        # Long burn rate (6h)
        req_6h = acc.rolling_requests(now, _LONG_WINDOW_SECONDS)
        err_6h = acc.rolling_errors(now, _LONG_WINDOW_SECONDS)
        if req_6h > 0 and slo > 0:
            error_rate_6h = err_6h / req_6h
            burn_long = error_rate_6h / slo
        else:
            burn_long = 0.0
        self.error_burn_rate_long.labels(**lbl).set(burn_long)
