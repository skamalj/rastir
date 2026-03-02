---
layout: default
title: Contributing Adapters
nav_order: 11
---

# Contributing New Adapters

Rastir's adapter system is designed to be extensible. You can add support for new LLM providers, frameworks, or response formats by implementing a simple adapter class.

---

## Overview

An adapter is a class that:
1. **Detects** whether it can handle a given response object (`can_handle`)
2. **Extracts** metadata from that object (`transform`)
3. Optionally handles **streaming chunks** (`can_handle_stream`, `extract_stream_delta`)

Adapters are registered in a global registry and resolved in priority order through a 3-phase pipeline:
- **Phase 1: Framework** (priority 200–300) — unwraps high-level wrappers
- **Phase 2: Provider** (priority 100–199) — extracts model/token metadata
- **Phase 3: Fallback** (priority 0) — catch-all

---

## Step-by-Step Guide

### 1. Create the Adapter File

Create a new file in `src/rastir/adapters/`. For example, to add a Cohere adapter:

```python
# src/rastir/adapters/cohere.py

"""Cohere provider adapter.

Handles Cohere chat and generate responses.
Extracts model, provider, token usage, finish reason.

Priority: 150 (standard provider range).
"""

from __future__ import annotations
from typing import Any

from rastir.adapters.types import AdapterResult, BaseAdapter, TokenDelta


class CohereAdapter(BaseAdapter):
    """Adapter for Cohere API responses."""

    name = "cohere"
    kind = "provider"    # "framework" | "provider" | "fallback"
    priority = 150       # Higher = evaluated first

    def can_handle(self, result: Any) -> bool:
        """Detect Cohere response objects by class name.

        IMPORTANT: Use class name + module inspection instead of
        importing the provider library. This keeps Rastir lightweight
        and avoids hard dependencies.
        """
        cls_name = type(result).__name__
        module = type(result).__module__ or ""
        return cls_name in ("ChatResponse", "Generation") and "cohere" in module

    def transform(self, result: Any) -> AdapterResult:
        """Extract metadata from the Cohere response."""
        model = getattr(result, "model", None) or "unknown"

        # Token extraction — adapt to the provider's response structure
        tokens_input = None
        tokens_output = None

        meta = getattr(result, "meta", None)
        if meta is not None:
            billed_units = getattr(meta, "billed_units", None)
            if billed_units:
                tokens_input = getattr(billed_units, "input_tokens", None)
                tokens_output = getattr(billed_units, "output_tokens", None)

        finish_reason = getattr(result, "finish_reason", None)

        return AdapterResult(
            model=model,
            provider="cohere",
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            finish_reason=finish_reason,
        )
```

### 2. Register the Adapter

Open `src/rastir/adapters/__init__.py` and add your adapter:

```python
# Add import
from rastir.adapters.cohere import CohereAdapter

# Add registration (order doesn't matter — priority-based)
register(CohereAdapter())  # provider, priority 150
```

The complete file would look like:

```python
"""Rastir adapters — response normalization and metadata extraction."""

from rastir.adapters.anthropic import AnthropicAdapter
from rastir.adapters.bedrock import BedrockAdapter
from rastir.adapters.cohere import CohereAdapter       # ← new
from rastir.adapters.fallback import FallbackAdapter
from rastir.adapters.langchain import LangChainAdapter
from rastir.adapters.openai import OpenAIAdapter
from rastir.adapters.registry import register
from rastir.adapters.retrieval import RetrievalAdapter
from rastir.adapters.tool import ToolAdapter

register(LangChainAdapter())   # framework, priority 250
register(OpenAIAdapter())      # provider, priority 150
register(AnthropicAdapter())   # provider, priority 150
register(CohereAdapter())      # provider, priority 150  ← new
register(BedrockAdapter())     # provider, priority 140
register(RetrievalAdapter())   # provider, priority 50
register(ToolAdapter())        # provider, priority 10
register(FallbackAdapter())    # fallback, priority 0
```

### 3. Add Streaming Support (Optional)

If the provider supports streaming, implement `can_handle_stream` and `extract_stream_delta`:

```python
class CohereAdapter(BaseAdapter):
    # ... (previous code)

    def can_handle_stream(self, chunk: Any) -> bool:
        """Detect Cohere streaming events."""
        cls_name = type(chunk).__name__
        module = type(chunk).__module__ or ""
        return cls_name == "StreamedChatResponse" and "cohere" in module

    def extract_stream_delta(self, chunk: Any) -> TokenDelta:
        """Extract token delta from a streaming chunk."""
        # Adapt to the provider's streaming format
        model = getattr(chunk, "model", None)

        # Many providers include usage only in the final chunk
        tokens_input = None
        tokens_output = None

        if getattr(chunk, "is_finished", False):
            meta = getattr(chunk, "meta", None)
            if meta:
                billed = getattr(meta, "billed_units", None)
                if billed:
                    tokens_input = getattr(billed, "input_tokens", None)
                    tokens_output = getattr(billed, "output_tokens", None)

        return TokenDelta(
            model=model,
            provider="cohere",
            tokens_input=tokens_input,
            tokens_output=tokens_output,
        )
```

### 4. Write Tests

Create `tests/test_adapter_cohere.py`:

```python
"""Tests for the Cohere adapter."""

from dataclasses import dataclass
from rastir.adapters.cohere import CohereAdapter


# Create mock response objects that mimic the provider's structure
@dataclass
class MockBilledUnits:
    input_tokens: int = 100
    output_tokens: int = 50

@dataclass
class MockMeta:
    billed_units: MockBilledUnits = None

@dataclass
class MockChatResponse:
    model: str = "command-r-plus"
    finish_reason: str = "COMPLETE"
    meta: MockMeta = None

    class __class__:
        __name__ = "ChatResponse"
        __module__ = "cohere.types"


class TestCohereAdapter:
    def setup_method(self):
        self.adapter = CohereAdapter()

    def test_can_handle_chat_response(self):
        resp = MockChatResponse()
        # Override type detection
        resp.__class__ = type("ChatResponse", (), {
            "__name__": "ChatResponse",
            "__module__": "cohere.types",
        })
        assert self.adapter.can_handle(resp)

    def test_cannot_handle_other(self):
        assert not self.adapter.can_handle({"key": "value"})
        assert not self.adapter.can_handle("string")

    def test_transform_extracts_metadata(self):
        resp = MockChatResponse(
            model="command-r-plus",
            meta=MockMeta(billed_units=MockBilledUnits(100, 50)),
        )
        # Would need proper type mocking for can_handle
        result = self.adapter.transform(resp)
        assert result.provider == "cohere"
        assert result.model == "command-r-plus"
        assert result.tokens_input == 100
        assert result.tokens_output == 50
```

---

## Adapter Interface Reference

### BaseAdapter

```python
class BaseAdapter:
    name: str = "base"         # Unique adapter name
    kind: str = "provider"     # "framework" | "provider" | "fallback"
    priority: int = 100        # Higher = evaluated first

    def can_handle(self, result: Any) -> bool:
        """Return True if this adapter can handle the given result."""

    def transform(self, result: Any) -> AdapterResult:
        """Extract metadata from the result."""

    def can_handle_stream(self, chunk: Any) -> bool:
        """Return True if this adapter can handle a streaming chunk."""

    def extract_stream_delta(self, chunk: Any) -> TokenDelta:
        """Extract token delta from a single streaming chunk."""
```

### AdapterResult

```python
@dataclass
class AdapterResult:
    unwrapped_result: Any = None        # For framework adapters only
    model: Optional[str] = None         # e.g., "gpt-4", "claude-3-opus"
    provider: Optional[str] = None      # e.g., "openai", "anthropic"
    tokens_input: Optional[int] = None  # Prompt/input tokens
    tokens_output: Optional[int] = None # Completion/output tokens
    finish_reason: Optional[str] = None # e.g., "stop", "length"
    extra_attributes: dict = field(default_factory=dict)  # Custom fields
```

### TokenDelta

```python
@dataclass
class TokenDelta:
    tokens_input: Optional[int] = None
    tokens_output: Optional[int] = None
    model: Optional[str] = None
    provider: Optional[str] = None
```

---

## Design Rules

### DO

- **Detect by class name and module** — use `type(result).__name__` and `type(result).__module__` instead of `isinstance()` or importing the provider library
- **Handle missing fields gracefully** — use `getattr(obj, "field", None)` throughout
- **Return `None` for unknown values** — never guess or hardcode fallback token counts
- **Set `provider` to a fixed string** — e.g., `"cohere"`, `"openai"`, `"anthropic"`
- **Follow the priority convention** — framework (200–300), provider (100–199), fallback (0)
- **Write tests with mock objects** — avoid importing provider SDKs in tests

### DON'T

- **Don't import provider libraries** at module scope — use class-name detection instead
- **Don't raise exceptions** from `can_handle()` — return `False` if uncertain
- **Don't modify the result object** — adapters are read-only
- **Don't add new metrics** — adapters only extract metadata; metrics are handled by the server
- **Don't hardcode token counts** — if the response doesn't include usage, return `None`

---

## Framework Adapters

Framework adapters (like LangChain) are special — their job is to **unwrap** high-level response objects to expose the underlying provider response.

```python
class MyFrameworkAdapter(BaseAdapter):
    name = "my_framework"
    kind = "framework"      # ← framework type
    priority = 250          # ← higher than providers

    def can_handle(self, result: Any) -> bool:
        cls_name = type(result).__name__
        module = type(result).__module__ or ""
        return cls_name == "FrameworkResponse" and "my_framework" in module

    def transform(self, result: Any) -> AdapterResult:
        # Unwrap to the underlying provider response
        inner = getattr(result, "raw_response", None)
        return AdapterResult(
            unwrapped_result=inner,  # ← key field for framework adapters
            extra_attributes={
                "framework": "my_framework",
                "framework_version": getattr(result, "version", None),
            },
        )
```

The pipeline will then pass the `unwrapped_result` to Phase 2 (provider adapters) for metadata extraction.

---

## Existing Adapters as Reference

Study these files for real-world examples:

| File | Type | Provider/Framework |
|------|------|-------------------|
| `src/rastir/adapters/openai.py` | Provider | OpenAI (ChatCompletion, streaming) |
| `src/rastir/adapters/anthropic.py` | Provider | Anthropic (Message, streaming events) |
| `src/rastir/adapters/bedrock.py` | Provider | AWS Bedrock (dict-based responses) |
| `src/rastir/adapters/langchain.py` | Framework | LangChain (AIMessage → unwrap) |
| `src/rastir/adapters/langgraph.py` | Framework | LangGraph (state dicts, StateSnapshot → unwrap) |
| `src/rastir/adapters/fallback.py` | Fallback | Catch-all for unknown responses |

---

## Checklist

Before submitting a new adapter:

- [ ] Adapter file created in `src/rastir/adapters/`
- [ ] Class attributes set: `name`, `kind`, `priority`
- [ ] `can_handle()` uses class name + module detection (no direct imports)
- [ ] `transform()` returns `AdapterResult` with all available fields
- [ ] Streaming support added (if provider supports it)
- [ ] Adapter registered in `src/rastir/adapters/__init__.py`
- [ ] Tests written with mock objects (no provider SDK dependency)
- [ ] All existing tests still pass (`pytest tests/ -v`)
