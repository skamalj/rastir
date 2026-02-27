---
layout: default
title: Adapters
nav_order: 4
---

# Adapter System

Rastir uses an **adapter pipeline** to extract metadata from LLM responses without monkey-patching provider libraries. When `@llm` decorates a function, the return value is passed through a 3-phase adapter resolution pipeline.

---

## How It Works

```
LLM function return value
         │
         ▼
┌─────────────────────────────────┐
│ Phase 1: Framework Unwrap       │  (priority 200–300)
│ e.g., LangChain → OpenAI obj   │
└─────────────┬───────────────────┘
              │ unwrapped result
              ▼
┌─────────────────────────────────┐
│ Phase 2: Provider Extraction    │  (priority 100–199)
│ e.g., OpenAI → model, tokens   │
└─────────────┬───────────────────┘
              │ AdapterResult
              ▼
┌─────────────────────────────────┐
│ Phase 3: Fallback               │  (priority 0)
│ Returns "unknown" if no match   │
└─────────────────────────────────┘
```

### Phase 1: Framework Unwrap

Framework adapters (like LangChain) unwrap high-level response objects to expose the underlying provider response. This phase runs repeatedly until no further unwrapping is possible.

### Phase 2: Provider Extraction

Provider adapters extract semantic metadata — model name, token counts, provider identifier, finish reason — from the raw provider response object.

### Phase 3: Fallback

If no provider adapter matches, the fallback adapter returns an `AdapterResult` with `provider="unknown"`.

---

## Built-in Adapters

| Adapter | Kind | Priority | Handles |
|---------|------|----------|---------|
| **LangChain** | framework | 250 | `AIMessage`, `LLMResult` → unwraps to provider response |
| **OpenAI** | provider | 150 | `ChatCompletion`, `Completion`, `ChatCompletionChunk` |
| **Anthropic** | provider | 150 | `Message`, `ContentBlockDelta` |
| **Bedrock** | provider | 140 | Bedrock `invoke_model` response dicts |
| **Retrieval** | provider | 50 | Retrieval-specific response objects |
| **Tool** | provider | 10 | Tool execution results |
| **Fallback** | fallback | 0 | Anything — returns `provider="unknown"` |

---

## AdapterResult

Every adapter produces an `AdapterResult`:

```python
@dataclass
class AdapterResult:
    unwrapped_result: Any = None        # For framework adapters
    model: Optional[str] = None         # e.g., "gpt-4"
    provider: Optional[str] = None      # e.g., "openai"
    tokens_input: Optional[int] = None  # Prompt tokens
    tokens_output: Optional[int] = None # Completion tokens
    finish_reason: Optional[str] = None # e.g., "stop"
    extra_attributes: dict = field(default_factory=dict)
```

---

## Streaming Support

Adapters also handle streaming via `TokenDelta`:

```python
@dataclass
class TokenDelta:
    tokens_input: Optional[int] = None
    tokens_output: Optional[int] = None
    model: Optional[str] = None
    provider: Optional[str] = None
```

For streaming LLM calls, the adapter's `extract_stream_delta()` method is called for each chunk. Token counts are accumulated and recorded when the stream finishes.

---

## Detection Without Hard Imports

Adapters detect response types by **class name and module** rather than importing provider libraries:

```python
def can_handle(self, result: Any) -> bool:
    cls_name = type(result).__name__
    module = type(result).__module__ or ""
    return cls_name == "ChatCompletion" and "openai" in module
```

This means Rastir works without installing provider SDKs — adapters gracefully skip uninstalled providers.

---

## Explicit Overrides

You can bypass adapter detection by providing metadata directly:

```python
@llm(model="gpt-4", provider="openai")
def my_llm_call(query: str):
    # Adapter pipeline still runs, but explicit values take priority
    return custom_api_call(query)
```

---

## Custom Adapters

See [Contributing Adapters](contributing-adapters.md) for a complete guide on writing your own adapter.
