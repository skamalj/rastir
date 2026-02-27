# Adapter Subsystem --- Requirements

## 1. Objective

Define the architecture, contracts, and operational rules for the
Adapter subsystem responsible for:

-   Normalizing provider-specific LLM responses
-   Unwrapping framework-wrapped results
-   Extracting structured metadata (model, provider, tokens, etc.)
-   Supporting streaming and non-streaming responses
-   Guaranteeing deterministic behavior with graceful degradation

The adapter system must be extensible, deterministic, and safe for
production workloads.

------------------------------------------------------------------------

## 2. Design Principles

1.  Deterministic resolution (no heuristics based on text content)
2.  Layered adapter phases (Framework → Provider → Fallback)
3.  No runtime monkey-patching
4.  No network calls inside adapters
5.  Low computational overhead
6.  Explicit priority-based conflict resolution
7.  Graceful degradation if metadata unavailable

------------------------------------------------------------------------

## 3. Adapter Categories

### 3.1 Framework Adapters

Purpose: - Detect framework-wrapped response objects - Extract and
unwrap provider-native responses - Normalize metadata location

Examples: - LangChainAdapter - LangGraphAdapter - CrewAIAdapter

Framework adapters may recursively unwrap nested objects.

------------------------------------------------------------------------

### 3.2 Provider Adapters

Purpose: - Extract semantic LLM metadata from provider-native responses

Must extract (when available): - model - provider - input token count -
output token count - finish reason

Examples: - OpenAIAdapter - AnthropicAdapter - BedrockAdapter

------------------------------------------------------------------------

### 3.3 Fallback Adapter

Purpose: - Handle unmatched responses - Ensure span emission never fails

Behavior: - model = "unknown" - provider = "unknown" - tokens omitted -
latency still emitted

------------------------------------------------------------------------

## 4. Base Adapter Interface

All adapters must implement:

-   name: str
-   kind: "framework" \| "provider" \| "fallback"
-   priority: int
-   can_handle(result) -\> bool
-   transform(result) -\> AdapterResult

Adapters must not raise exceptions during normal extraction flow.

------------------------------------------------------------------------

## 5. AdapterResult Contract

AdapterResult must contain:

-   unwrapped_result (optional)
-   model (optional)
-   provider (optional)
-   tokens_input (optional)
-   tokens_output (optional)
-   finish_reason (optional)
-   extra_attributes (dict)

Framework adapters primarily set unwrapped_result. Provider adapters
populate semantic fields.

------------------------------------------------------------------------

## 6. Resolution Phases

### Phase 1: Framework Unwrap

-   Evaluate all framework adapters
-   Sorted by descending priority
-   First match transforms result
-   `extra_attributes` are captured from every matching framework
    adapter, regardless of whether `unwrapped_result` is returned
-   If unwrapped_result returned, restart framework phase

Goal: fully unwrap response to provider-native form. When unwrapping is
not possible (e.g., LangChain without a raw provider object), framework
attributes still propagate to the final `AdapterResult` via the
fallback path.

**Implementation note:** Framework `extra_attributes` (e.g., token
counts, model name extracted from LangChain `response_metadata`) are
merged into the final result regardless of which phase resolves it.
This enables LangChain metadata to reach the fallback adapter when no
native provider response is available for unwrapping.

------------------------------------------------------------------------

### Phase 2: Provider Extraction

-   Evaluate provider adapters
-   Sorted by descending priority
-   First matching adapter extracts metadata
-   Stop after first match

------------------------------------------------------------------------

### Phase 3: Fallback

If no provider adapter matches: - Use fallback adapter - Emit minimal
metadata

------------------------------------------------------------------------

## 7. Priority Rules

-   Higher priority evaluated first
-   Framework adapters: 200--300 range
-   Provider adapters: 100--199 range
-   Utility adapters: 10--99 range (implemented as `kind="provider"`;
    see below)
-   Fallback adapter: 0

**Utility adapters (Retrieval, Tool):** The `BaseAdapter` interface
defines three `kind` values: `"framework"`, `"provider"`, and
`"fallback"`. Utility adapters are implemented as `kind="provider"`
but occupy a dedicated priority range (10–99) below the standard
provider range. This ensures they only match after all real provider
adapters have been tried.

**Implemented priorities:**

| Adapter | Kind | Priority |
|---------|------|----------|
| LangChain | framework | 250 |
| OpenAI | provider | 150 |
| Anthropic | provider | 150 |
| Bedrock | provider | 140 |
| Retrieval | provider (utility) | 50 |
| Tool | provider (utility) | 10 |
| Fallback | fallback | 0 |

If multiple adapters match at same priority: - Deterministic ordering
based on registration order - Log debug warning

------------------------------------------------------------------------

## 8. Streaming Support

Provider adapters may optionally implement:

-   can_handle_stream(chunk) -\> bool
-   extract_stream_delta(chunk) -\> TokenDelta

Streaming resolution must occur once per session. Chunk-level processing
must be lightweight.

------------------------------------------------------------------------

## 9. Error Handling

Adapters must:

-   Never crash application execution
-   Catch internal extraction errors
-   Return partial metadata if possible
-   Log debug information only

If metadata extraction fails: - model/provider may be "unknown" - token
metrics omitted

------------------------------------------------------------------------

## 10. Performance Constraints

-   Resolution complexity O(N) where N = number of adapters
-   Adapter registry built at startup
-   No reflection-heavy runtime scanning
-   No per-call dynamic registration

Target overhead per call: \< 1ms for adapter resolution.

------------------------------------------------------------------------

## 11. Extensibility

System must support:

-   Third-party adapter registration
-   Custom provider adapters
-   Custom framework adapters
-   Optional streaming adapters

Adapters must be pluggable via registry API.

------------------------------------------------------------------------

## 12. Testing Requirements

Each adapter must have:

-   Positive detection test
-   Negative detection test
-   Conflict resolution test
-   Streaming extraction test (if applicable)

Test suite must validate: - Correct priority handling - Recursive
unwrapping behavior - Graceful fallback behavior

------------------------------------------------------------------------

## 13. Non-Goals

Adapters must NOT:

-   Infer tokens from text length
-   Parse prompts heuristically
-   Make external API calls
-   Store state across requests
-   Introduce high-cardinality labels

------------------------------------------------------------------------

## 14. Summary

The Adapter subsystem provides:

-   Deterministic response normalization
-   Clear separation of framework and provider logic
-   Safe fallback behavior
-   Streaming-aware extraction
-   Extensible, priority-driven resolution

End of Adapter Requirements.
