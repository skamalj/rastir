"""Bounded evaluator registry with cardinality protection.

Allows registration of custom evaluator implementations while enforcing
a maximum number of evaluation types to protect Prometheus label
cardinality.
"""

from __future__ import annotations

import logging
from typing import Optional

from rastir.server.evaluators.types import Evaluator

logger = logging.getLogger("rastir.server")


class EvaluatorRegistry:
    """Thread-safe registry for evaluator plugins.

    Enforces a maximum number of registered evaluation types
    (default 20) to protect Prometheus label cardinality.
    """

    def __init__(self, max_types: int = 20) -> None:
        self._evaluators: dict[str, Evaluator] = {}
        self._max_types = max_types

    def register(self, evaluator: Evaluator) -> None:
        """Register an evaluator.

        Args:
            evaluator: An object implementing the Evaluator protocol.

        Raises:
            ValueError: If max_types would be exceeded or name is duplicate.
        """
        name = evaluator.name
        if name in self._evaluators:
            logger.warning("Replacing existing evaluator %r", name)
            self._evaluators[name] = evaluator
            return

        if len(self._evaluators) >= self._max_types:
            raise ValueError(
                f"Cannot register evaluator {name!r}: "
                f"max {self._max_types} evaluation types reached. "
                f"Registered: {list(self._evaluators.keys())}"
            )

        self._evaluators[name] = evaluator
        logger.info("Registered evaluator: %s", name)

    def get(self, name: str) -> Optional[Evaluator]:
        """Look up an evaluator by name. Returns None if not found."""
        return self._evaluators.get(name)

    def list_types(self) -> list[str]:
        """Return all registered evaluation type names."""
        return list(self._evaluators.keys())

    def __len__(self) -> int:
        return len(self._evaluators)

    def __contains__(self, name: str) -> bool:
        return name in self._evaluators
