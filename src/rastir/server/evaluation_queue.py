"""Evaluation queue for async server-side evaluation.

Provides a bounded, thread-safe queue for evaluation tasks.
The queue uses ``queue.Queue`` (stdlib) since evaluation workers
run in a ``ThreadPoolExecutor``.

Drop policies:
- ``drop_new``: reject new tasks when full.
- ``drop_oldest``: evict oldest task to make room.
"""

from __future__ import annotations

import logging
import queue
import time
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

logger = logging.getLogger("rastir.server")


@dataclass
class EvaluationTask:
    """Payload for a single evaluation task.

    Created by the ingestion worker from a sampled LLM span.
    All text fields have already been through redaction (if enabled).
    """

    trace_id: str
    parent_span_id: str
    service: str
    env: str
    model: str
    provider: str
    agent: str | None
    prompt_text: str | None
    completion_text: str | None
    evaluation_types: list[str] = field(default_factory=list)
    timeout_ms: int = 30_000
    span_start_time: float | None = None
    span_end_time: float | None = None
    enqueued_at: float = field(default_factory=time.time)


@runtime_checkable
class EvaluationQueue(Protocol):
    """Protocol for evaluation queue backends.

    V4: in-memory only. Future: Redis, etc.
    """

    def put(self, task: EvaluationTask) -> bool:
        """Enqueue a task. Returns False if dropped."""
        ...

    def get(self, timeout: float = 1.0) -> EvaluationTask | None:
        """Blocking get with timeout. Returns None on timeout."""
        ...

    def size(self) -> int:
        """Current number of tasks in the queue."""
        ...

    def full(self) -> bool:
        """Whether the queue is at capacity."""
        ...

    @property
    def maxsize(self) -> int:
        """Maximum queue capacity."""
        ...


class InMemoryEvaluationQueue:
    """Thread-safe in-memory evaluation queue backed by ``queue.Queue``.

    Supports two drop policies when full:
    - ``drop_new``: reject the incoming task.
    - ``drop_oldest``: evict the oldest task to make room.
    """

    def __init__(self, max_size: int = 10_000, drop_policy: str = "drop_new") -> None:
        if drop_policy not in ("drop_new", "drop_oldest"):
            raise ValueError(f"Invalid drop_policy: {drop_policy!r}")
        self._queue: queue.Queue[EvaluationTask] = queue.Queue(maxsize=max_size)
        self._drop_policy = drop_policy
        self._max_size = max_size
        self._dropped: int = 0

    def put(self, task: EvaluationTask) -> bool:
        """Enqueue a task. Returns False if dropped (drop_new policy)."""
        if self._queue.full():
            if self._drop_policy == "drop_oldest":
                try:
                    self._queue.get_nowait()
                    self._dropped += 1
                except queue.Empty:
                    pass
            else:
                # drop_new: reject
                self._dropped += 1
                logger.warning("Evaluation queue full — dropping new task")
                return False

        try:
            self._queue.put_nowait(task)
            return True
        except queue.Full:
            self._dropped += 1
            logger.warning("Evaluation queue full — dropping task")
            return False

    def get(self, timeout: float = 1.0) -> EvaluationTask | None:
        """Blocking get with timeout. Returns None on timeout."""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def size(self) -> int:
        return self._queue.qsize()

    def full(self) -> bool:
        return self._queue.full()

    @property
    def maxsize(self) -> int:
        return self._max_size

    @property
    def dropped_count(self) -> int:
        return self._dropped
