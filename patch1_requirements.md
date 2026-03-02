
# Rastir Patch Requirements — Streaming Token Usage Normalization
## (Adapter-Level Fix + Mandatory Test Coverage)

---

# 1. Problem Statement

Certain LLM providers (e.g., Gemini) emit cumulative token usage during streaming responses.

Example (cumulative mode):

Chunk 1:
    input_tokens = 100
    output_tokens = 10

Chunk 2:
    input_tokens = 100
    output_tokens = 25

Chunk 3:
    input_tokens = 100
    output_tokens = 40

Incorrect aggregation logic:
    total_input += chunk.input_tokens
    total_output += chunk.output_tokens

This produces inflated totals.

Correct behavior:
    For cumulative providers → use the latest chunk value only.
    For incremental providers → sum the deltas.

This MUST be corrected before cost + SRE calculations are considered reliable.

---

# 2. Scope

Applies to:

- All provider adapters
- All streaming execution paths
- Token aggregation logic
- Cost calculation paths dependent on tokens

Fix MUST occur inside adapter layer only.

---

# 3. Adapter Contract Update

All adapters MUST normalize usage before returning values upstream.

Required normalized adapter output:

{
    "tokens_input": <final_total_input>,
    "tokens_output": <final_total_output>,
    "usage_mode": "cumulative" | "incremental"
}

Decorators must assume tokens are already normalized.

---

# 4. Adapter Behavior Requirements

## 4.1 Cumulative Providers

- Overwrite totals on each chunk
- Final value must equal last cumulative value
- No summation allowed

## 4.2 Incremental Providers

- Sum chunk deltas
- Final value equals accumulated total

---

# 5. Provider Audit (Mandatory)

Developer MUST audit:

- OpenAI adapter
- Anthropic adapter
- Bedrock adapter
- Gemini adapter
- Azure OpenAI (if present)
- Any other adapters

For each adapter document:

1. Streaming support?
2. Usage emission style (cumulative / incremental / final-only)?
3. Current behavior?
4. Required correction?

Audit findings must be included in PR.

---

# 6. Required Test Additions

## 6.1 Cumulative Streaming Test

Simulate cumulative chunks:
- Verify final tokens equal last chunk values
- Ensure no overcounting

## 6.2 Incremental Streaming Test

Simulate incremental chunks:
- Verify final tokens equal sum of deltas

## 6.3 Final-Only Usage Test

Simulate provider emitting usage only in final chunk
- Verify correct totals

## 6.4 Cost Integrity Test

- Simulate known token totals
- Verify derived cost matches expected
- Ensure no inflation

## 6.5 Regression Guard Test

Explicitly fail test if cumulative chunks are summed.

---

# 7. Acceptance Criteria

Patch accepted only if:

- All adapters audited
- Streaming paths normalized
- New tests added (cumulative + incremental + cost integrity)
- All tests pass
- No regression introduced

---

# 8. Architectural Rule

All future adapters MUST:

- Declare streaming usage mode
- Normalize tokens before span emission
- Include streaming correctness tests

Failure blocks merge.

---

End of Patch Requirements.
