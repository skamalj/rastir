"""Server-side trace redaction for PII and sensitive data.

Redaction is a standalone pipeline stage that runs after sampling and
before span storage, OTLP export, and evaluation enqueue.  It applies
only to ``prompt_text`` and ``completion_text`` span attributes.

Redaction is synchronous, CPU-bound, deterministic, and side-effect free.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

logger = logging.getLogger("rastir.server")

# Default max text length before truncation
_DEFAULT_MAX_TEXT_LENGTH = 50_000

# Truncation marker appended when text exceeds max length
_TRUNCATED_MARKER = "[TRUNCATED]"


@dataclass
class RedactionContext:
    """Context passed to the redactor for context-aware redaction.

    Provides service/env/model/provider so redaction rules can vary
    by deployment or provider if needed.
    """

    service: str
    env: str
    model: str | None = None
    provider: str | None = None


@runtime_checkable
class Redactor(Protocol):
    """Protocol for pluggable redaction implementations."""

    def redact(self, text: str, context: RedactionContext) -> str:
        """Redact sensitive content from text.

        Must be synchronous, deterministic, and side-effect free.
        Must not raise — callers handle failures by dropping the span.
        """
        ...


class NoOpRedactor:
    """Redactor that passes text through unchanged."""

    def redact(self, text: str, context: RedactionContext) -> str:
        return text


class RegexRedactor:
    """Default redactor using regex patterns for common PII types.

    Masks emails, phone numbers, SSNs, and credit card numbers.
    Custom patterns can be added at construction time.

    Also enforces ``max_text_length`` — text exceeding this limit
    is truncated with a ``[TRUNCATED]`` marker.
    """

    # Built-in patterns: (name, compiled_regex, replacement)
    _BUILTIN_PATTERNS: list[tuple[str, re.Pattern, str]] = [
        (
            "email",
            re.compile(
                r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Z|a-z]{2,}\b"
            ),
            "[EMAIL_REDACTED]",
        ),
        (
            "phone",
            re.compile(
                r"(?<!\d)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}(?!\d)"
            ),
            "[PHONE_REDACTED]",
        ),
        (
            "ssn",
            re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
            "[SSN_REDACTED]",
        ),
        (
            "credit_card",
            re.compile(
                r"\b(?:\d{4}[-\s]?){3}\d{4}\b"
            ),
            "[CC_REDACTED]",
        ),
    ]

    def __init__(
        self,
        extra_patterns: list[tuple[str, str]] | None = None,
        max_text_length: int = _DEFAULT_MAX_TEXT_LENGTH,
    ) -> None:
        """Initialize the regex redactor.

        Args:
            extra_patterns: Additional ``(regex_pattern, replacement)`` pairs.
            max_text_length: Maximum text length before truncation.
        """
        self._max_text_length = max_text_length

        # Build ordered pattern list: builtins + custom
        self._patterns: list[tuple[str, re.Pattern, str]] = list(self._BUILTIN_PATTERNS)
        if extra_patterns:
            for i, (pattern, replacement) in enumerate(extra_patterns):
                try:
                    compiled = re.compile(pattern)
                    self._patterns.append((f"custom_{i}", compiled, replacement))
                except re.error:
                    logger.warning(
                        "Invalid custom redaction pattern %r — skipping", pattern
                    )

    def redact(self, text: str, context: RedactionContext) -> str:
        """Apply all regex patterns and enforce max text length."""
        # 1. Payload guard: truncate if needed
        if len(text) > self._max_text_length:
            text = text[: self._max_text_length] + _TRUNCATED_MARKER

        # 2. Apply patterns
        for _name, pattern, replacement in self._patterns:
            text = pattern.sub(replacement, text)

        return text


def redact_span(
    span: dict,
    redactor: Redactor,
    service: str,
    env: str,
) -> bool:
    """Apply redaction to a span's prompt_text and completion_text.

    Modifies the span dict in-place.

    Args:
        span: Mutable span dict.
        redactor: Redactor implementation to use.
        service: Service name for RedactionContext.
        env: Environment for RedactionContext.

    Returns:
        ``True`` if redaction succeeded, ``False`` if it failed
        (caller should drop the span).
    """
    attrs = span.get("attributes", {})
    model = attrs.get("model")
    provider = attrs.get("provider")
    ctx = RedactionContext(service=service, env=env, model=model, provider=provider)

    try:
        prompt = attrs.get("prompt_text")
        if prompt is not None and isinstance(prompt, str):
            span["attributes"]["prompt_text"] = redactor.redact(prompt, ctx)

        completion = attrs.get("completion_text")
        if completion is not None and isinstance(completion, str):
            span["attributes"]["completion_text"] = redactor.redact(completion, ctx)

        return True
    except Exception:
        logger.error("Redaction failed for span %s", span.get("span_id", "?"), exc_info=True)
        return False
