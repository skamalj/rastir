"""Evaluator protocol and result types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from rastir.server.evaluation_queue import EvaluationTask


@dataclass
class EvaluationResult:
    """Result from a single evaluation run.

    Attributes:
        evaluation_type: The type of evaluation (e.g. "toxicity").
        score: Numeric score between 0.0 and 1.0.
        passed: Whether the evaluation passed (score >= threshold).
        details: Free-form metadata about the evaluation.
        error: Error message if evaluation failed (None on success).
    """

    evaluation_type: str
    score: float = 0.0
    passed: bool = True
    details: dict = field(default_factory=dict)
    error: str | None = None

    @property
    def failed(self) -> bool:
        return self.error is not None


@runtime_checkable
class Evaluator(Protocol):
    """Protocol for pluggable evaluation implementations.

    Evaluators are synchronous in V4 — they run in a ThreadPoolExecutor.
    Each evaluator handles one evaluation type.
    """

    @property
    def name(self) -> str:
        """Unique name matching the evaluation_type label."""
        ...

    def evaluate(self, task: EvaluationTask) -> EvaluationResult:
        """Run evaluation on the task.

        Must be synchronous, thread-safe, and should respect the task's
        timeout_ms (though the worker pool also enforces timeouts externally).

        Args:
            task: The evaluation task containing prompt/completion text.

        Returns:
            An EvaluationResult with score, pass/fail, and optional details.
        """
        ...
