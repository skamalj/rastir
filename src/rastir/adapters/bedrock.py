"""AWS Bedrock provider adapter.

Handles Bedrock Converse API responses (dict-based). Extracts model,
provider, token usage from the nested Bedrock response structure.

Bedrock responses are dicts with keys like:
  - output.message.content
  - usage.inputTokens / outputTokens
  - ResponseMetadata.modelId (e.g., "anthropic.claude-3-sonnet-...")

Priority: 140 (below direct provider adapters so that if a native
SDK response leaks through, the native adapter matches first).
"""

from __future__ import annotations

from typing import Any

from rastir.adapters.types import AdapterResult, BaseAdapter


class BedrockAdapter(BaseAdapter):
    """Adapter for AWS Bedrock Converse API dict responses."""

    name = "bedrock"
    kind = "provider"
    priority = 140

    def can_handle(self, result: Any) -> bool:
        """Detect Bedrock dict responses by looking for characteristic keys."""
        if not isinstance(result, dict):
            return False
        # Bedrock Converse API response markers
        has_output = "output" in result
        has_usage = "usage" in result
        has_metadata = "ResponseMetadata" in result
        return has_output and (has_usage or has_metadata)

    def transform(self, result: Any) -> AdapterResult:
        # Extract model from ResponseMetadata or modelId
        model_id = None
        metadata = result.get("ResponseMetadata", {})
        if isinstance(metadata, dict):
            # Try HTTPHeaders for model info
            headers = metadata.get("HTTPHeaders", {})
            if isinstance(headers, dict):
                model_id = headers.get("x-amzn-bedrock-model-id")

        # Try top-level modelId (some Bedrock APIs include it)
        if not model_id:
            model_id = result.get("modelId")

        # Normalize model/provider from Bedrock model ID
        # e.g., "anthropic.claude-3-sonnet-20240229-v1:0"
        model, provider = self._parse_model_id(model_id)

        # Extract token usage
        usage = result.get("usage", {})
        tokens_input = None
        tokens_output = None
        if isinstance(usage, dict):
            tokens_input = usage.get("inputTokens")
            tokens_output = usage.get("outputTokens")

        # Extract finish reason (stopReason in Bedrock Converse)
        finish_reason = result.get("stopReason")

        return AdapterResult(
            model=model,
            provider=provider,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            finish_reason=finish_reason,
        )

    @staticmethod
    def _parse_model_id(model_id: str | None) -> tuple[str, str]:
        """Parse Bedrock model ID into (model, provider).

        Examples:
            "anthropic.claude-3-sonnet-20240229-v1:0" → ("claude-3-sonnet-20240229-v1:0", "anthropic")
            "amazon.titan-text-express-v1" → ("titan-text-express-v1", "amazon")
            "meta.llama3-70b-instruct-v1:0" → ("llama3-70b-instruct-v1:0", "meta")
        """
        if not model_id:
            return ("unknown", "bedrock")

        parts = model_id.split(".", 1)
        if len(parts) == 2:
            return (parts[1], parts[0])
        return (model_id, "bedrock")
