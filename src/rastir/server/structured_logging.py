"""Structured JSON logging for the Rastir collector.

When enabled, replaces the default text formatter with a JSON formatter
that emits one JSON object per log line.  Fields include ``timestamp``,
``level``, ``logger``, ``message``, and optional contextual keys
(``service``, ``span_type``, ``trace_id``, ``error_type``).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any


class StructuredFormatter(logging.Formatter):
    """Emit log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        obj: dict[str, Any] = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Merge extra contextual fields when present
        for key in ("service", "span_type", "trace_id", "error_type", "queue_pct"):
            val = getattr(record, key, None)
            if val is not None:
                obj[key] = val

        if record.exc_info and record.exc_info[1]:
            obj["exception"] = str(record.exc_info[1])

        return json.dumps(obj, default=str)


def configure_logging(structured: bool = False, level: str = "INFO") -> None:
    """Set up logging for the Rastir server.

    Args:
        structured: If ``True``, use JSON structured format.
        level: Log level name (``DEBUG``, ``INFO``, etc.).
    """
    root = logging.getLogger("rastir")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove existing handlers to avoid duplicates
    root.handlers.clear()

    handler = logging.StreamHandler()
    if structured:
        handler.setFormatter(StructuredFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
        )
    root.addHandler(handler)
