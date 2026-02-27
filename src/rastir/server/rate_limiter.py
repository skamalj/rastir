"""Simple in-memory rate limiter for the Rastir collector.

Provides per-IP and per-service request rate limiting using a sliding
window counter approach (fixed 60-second windows).  Designed to be
lightweight — no external dependencies or shared state.

Rate-limited requests receive HTTP 429 responses and increment the
``rastir_rate_limited_total`` counter.
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Optional

from prometheus_client import Counter, CollectorRegistry


class _WindowCounter:
    """Fixed-window counter with automatic rotation."""

    __slots__ = ("_limit", "_window", "_current", "_count")

    def __init__(self, limit: int, window: float = 60.0) -> None:
        self._limit = limit
        self._window = window
        self._current: float = 0.0
        self._count: int = 0

    def allow(self) -> bool:
        """Return ``True`` if the request is allowed, else ``False``."""
        now = time.monotonic()
        bucket = now // self._window
        if bucket != self._current:
            self._current = bucket
            self._count = 0
        self._count += 1
        return self._count <= self._limit


class RateLimiter:
    """Per-IP and per-service rate limiter.

    Args:
        per_ip_rpm: Max requests per minute per client IP.
        per_service_rpm: Max requests per minute per ``service`` label.
        registry: Optional Prometheus registry for the rejection counter.
    """

    def __init__(
        self,
        per_ip_rpm: int = 600,
        per_service_rpm: int = 3000,
        registry: Optional[CollectorRegistry] = None,
    ) -> None:
        self._per_ip_rpm = per_ip_rpm
        self._per_service_rpm = per_service_rpm
        self._ip_counters: dict[str, _WindowCounter] = defaultdict(
            lambda: _WindowCounter(self._per_ip_rpm)
        )
        self._service_counters: dict[str, _WindowCounter] = defaultdict(
            lambda: _WindowCounter(self._per_service_rpm)
        )
        self.rate_limited = Counter(
            "rastir_rate_limited_total",
            "Requests rejected by rate limiter",
            ["dimension"],  # "ip" or "service"
            registry=registry,
        )

    def check(self, client_ip: str, service: str) -> Optional[str]:
        """Check if the request should be rate-limited.

        Returns:
            ``None`` if allowed; otherwise a string indicating the
            dimension that triggered the limit (``"ip"`` or ``"service"``).
        """
        if not self._ip_counters[client_ip].allow():
            self.rate_limited.labels(dimension="ip").inc()
            return "ip"
        if not self._service_counters[service].allow():
            self.rate_limited.labels(dimension="service").inc()
            return "service"
        return None
