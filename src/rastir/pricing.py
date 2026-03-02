"""Client-side pricing registry for LLM cost calculation.

Provides ``PricingRegistry`` — a thread-safe lookup table mapping
``provider:model`` pairs to per-token input/output prices.  Cost is
calculated at span finalization time on the client; the server remains
pricing-agnostic.

Usage::

    from rastir.pricing import PricingRegistry

    registry = PricingRegistry()
    registry.register("openai", "gpt-4o", input_price=2.50, output_price=10.00)

    cost = registry.calculate_cost("openai", "gpt-4o", tokens_in=500, tokens_out=100)
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger("rastir")


@dataclass(frozen=True)
class PricingEntry:
    """Per-million token pricing for a single model."""

    input_price: float   # USD per 1M input tokens
    output_price: float  # USD per 1M output tokens


class PricingRegistry:
    """Thread-safe registry mapping (provider, model) → pricing.

    Prices are expressed in **USD per 1 million tokens**.

    Supports three loading strategies:
    - **inline**: call ``register()`` / pass a dict to the constructor
    - **file**: load from a JSON file via ``load_file()`` or constructor
    - **env**: load from ``RASTIR_PRICING_DATA`` (JSON string)
    """

    def __init__(
        self,
        entries: dict[str, dict[str, dict[str, float]]] | None = None,
        pricing_file: str | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, PricingEntry] = {}

        # Load from env var first (lowest priority)
        env_data = os.environ.get("RASTIR_PRICING_DATA")
        if env_data:
            try:
                self._load_dict(json.loads(env_data))
            except (json.JSONDecodeError, TypeError):
                logger.warning("RASTIR_PRICING_DATA is not valid JSON, ignoring")

        # Load from file (overrides env)
        if pricing_file:
            self.load_file(pricing_file)

        # Load inline entries (highest priority)
        if entries:
            self._load_dict(entries)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register(
        self,
        provider: str,
        model: str,
        input_price: float,
        output_price: float,
    ) -> None:
        """Register pricing for a single provider/model pair.

        Args:
            provider: Provider name (e.g. ``"openai"``).
            model: Model identifier (e.g. ``"gpt-4o"``).
            input_price: USD per 1 million input tokens.
            output_price: USD per 1 million output tokens.
        """
        key = self._key(provider, model)
        with self._lock:
            self._entries[key] = PricingEntry(
                input_price=input_price,
                output_price=output_price,
            )

    def lookup(self, provider: str, model: str) -> Optional[PricingEntry]:
        """Look up pricing for a provider/model pair.

        Returns ``None`` if no entry is registered.
        """
        key = self._key(provider, model)
        with self._lock:
            return self._entries.get(key)

    def calculate_cost(
        self,
        provider: str,
        model: str,
        tokens_in: int,
        tokens_out: int,
    ) -> tuple[float, bool]:
        """Calculate cost in USD for a given token usage.

        Returns:
            A tuple of ``(cost_usd, pricing_missing)``.
            If pricing is not found, returns ``(0.0, True)``.
        """
        entry = self.lookup(provider, model)
        if entry is None:
            return 0.0, True

        cost = (
            (tokens_in * entry.input_price / 1_000_000)
            + (tokens_out * entry.output_price / 1_000_000)
        )
        return cost, False

    def load_file(self, path: str) -> None:
        """Load pricing entries from a JSON file.

        Expected schema::

            {
                "openai": {
                    "gpt-4o": {"input_price": 2.50, "output_price": 10.00},
                    "gpt-4o-mini": {"input_price": 0.15, "output_price": 0.60}
                },
                "anthropic": {
                    "claude-sonnet-4-20250514": {"input_price": 3.00, "output_price": 15.00}
                }
            }
        """
        resolved = os.path.expanduser(path)
        with open(resolved) as f:
            data = json.load(f)
        self._load_dict(data)

    @property
    def model_count(self) -> int:
        """Number of registered provider/model entries."""
        with self._lock:
            return len(self._entries)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load_dict(self, data: dict[str, Any]) -> None:
        """Load from nested dict: {provider: {model: {input_price, output_price}}}."""
        with self._lock:
            for provider, models in data.items():
                if not isinstance(models, dict):
                    logger.warning("Skipping invalid provider entry: %s", provider)
                    continue
                for model_name, prices in models.items():
                    if not isinstance(prices, dict):
                        logger.warning(
                            "Skipping invalid model entry: %s/%s", provider, model_name
                        )
                        continue
                    inp = prices.get("input_price")
                    out = prices.get("output_price")
                    if inp is None or out is None:
                        logger.warning(
                            "Missing input_price/output_price for %s/%s, skipping",
                            provider,
                            model_name,
                        )
                        continue
                    key = self._key(provider, model_name)
                    self._entries[key] = PricingEntry(
                        input_price=float(inp),
                        output_price=float(out),
                    )

    @staticmethod
    def _key(provider: str, model: str) -> str:
        """Build a canonical lookup key."""
        return f"{provider.lower()}:{model.lower()}"
