"""AWS Bedrock provider adapter.

Handles Bedrock Converse API responses (dict-based). Extracts model,
provider, token usage from the nested Bedrock response structure.

Also provides guardrail observability:
  - Request-level: detects guardrailIdentifier/guardrailVersion in kwargs
  - Response-level: detects GUARDRAIL_INTERVENED action and assessments

Bedrock responses are dicts with keys like:
  - output.message.content
  - usage.inputTokens / outputTokens
  - ResponseMetadata.modelId (e.g., "anthropic.claude-3-sonnet-...")
  - amazon-bedrock-guardrailAction (from trace in response)

Priority: 140 (below direct provider adapters so that if a native
SDK response leaks through, the native adapter matches first).
"""

from __future__ import annotations

from typing import Any

from rastir.adapters.types import AdapterResult, BaseAdapter, RequestMetadata


# Bounded guardrail categories - prevents cardinality explosion
_VALID_GUARDRAIL_CATEGORIES = frozenset({
    "CONTENT_POLICY",
    "SENSITIVE_INFORMATION_POLICY",
    "WORD_POLICY",
    "TOPIC_POLICY",
    "CONTEXTUAL_GROUNDING_POLICY",
    "DENIED_TOPIC",
})

_CARDINALITY_OVERFLOW = "__cardinality_overflow__"
_MAX_GUARDRAIL_IDS = 100

# Track seen guardrail IDs for cardinality control
_seen_guardrail_ids: set[str] = set()


class BedrockAdapter(BaseAdapter):
    """Adapter for AWS Bedrock Converse API dict responses."""

    name = "bedrock"
    kind = "provider"
    priority = 140

    supports_tokens = True
    supports_streaming = True
    supports_request_metadata = True
    supports_guardrail_metadata = True

    def can_handle(self, result: Any) -> bool:
        """Detect Bedrock dict responses by looking for characteristic keys."""
        if not isinstance(result, dict):
            return False
        has_output = "output" in result
        has_usage = "usage" in result
        has_metadata = "ResponseMetadata" in result
        return has_output and (has_usage or has_metadata)

    def transform(self, result: Any) -> AdapterResult:
        model_id = None
        metadata = result.get("ResponseMetadata", {})
        if isinstance(metadata, dict):
            headers = metadata.get("HTTPHeaders", {})
            if isinstance(headers, dict):
                model_id = headers.get("x-amzn-bedrock-model-id")

        if not model_id:
            model_id = result.get("modelId")

        model, provider = self._parse_model_id(model_id)

        usage = result.get("usage", {})
        tokens_input = None
        tokens_output = None
        if isinstance(usage, dict):
            tokens_input = usage.get("inputTokens")
            tokens_output = usage.get("outputTokens")

        finish_reason = result.get("stopReason")

        extra: dict[str, Any] = {}
        self._extract_guardrail_response(result, extra)

        return AdapterResult(
            model=model,
            provider=provider,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            finish_reason=finish_reason,
            extra_attributes=extra,
        )

    def can_handle_request(self, args: tuple, kwargs: dict) -> bool:
        """Detect guardrail configuration in Bedrock API call kwargs."""
        return (
            "guardrailIdentifier" in kwargs
            or "guardrailConfig" in kwargs
        )

    def extract_request_metadata(
        self, args: tuple, kwargs: dict
    ) -> RequestMetadata:
        """Extract guardrail config from request kwargs."""
        span_attrs: dict[str, Any] = {}
        extra_attrs: dict[str, Any] = {}

        guardrail_id = kwargs.get("guardrailIdentifier")
        guardrail_version = kwargs.get("guardrailVersion")

        config = kwargs.get("guardrailConfig", {})
        if isinstance(config, dict):
            guardrail_id = guardrail_id or config.get("guardrailIdentifier")
            guardrail_version = guardrail_version or config.get(
                "guardrailVersion"
            )

        if guardrail_id:
            safe_id = self._safe_guardrail_id(guardrail_id)
            span_attrs["guardrail.id"] = safe_id
            span_attrs["guardrail.enabled"] = True
            extra_attrs["guardrail_id"] = safe_id
            extra_attrs["guardrail_enabled"] = True

        if guardrail_version:
            span_attrs["guardrail.version"] = str(guardrail_version)
            extra_attrs["guardrail_version"] = str(guardrail_version)

        return RequestMetadata(
            span_attributes=span_attrs,
            extra_attributes=extra_attrs,
        )

    def can_handle_stream(self, chunk: Any) -> bool:
        """Detect Bedrock streaming chunks (dict with contentBlockDelta)."""
        if not isinstance(chunk, dict):
            return False
        return (
            "contentBlockDelta" in chunk
            or "messageStop" in chunk
            or "metadata" in chunk
            or "amazon-bedrock-guardrailAction" in chunk
        )

    def extract_stream_delta(self, chunk: Any):
        """Extract from Bedrock streaming chunk."""
        from rastir.adapters.types import TokenDelta

        if isinstance(chunk, dict) and "metadata" in chunk:
            meta = chunk["metadata"]
            usage = meta.get("usage", {})
            tokens_input = usage.get("inputTokens")
            tokens_output = usage.get("outputTokens")
            return TokenDelta(
                tokens_input=tokens_input,
                tokens_output=tokens_output,
            )

        return TokenDelta()

    def _extract_guardrail_response(
        self, result: dict, extra: dict[str, Any]
    ) -> None:
        """Extract guardrail intervention from Bedrock response."""
        guardrail_action = result.get("amazon-bedrock-guardrailAction")

        trace = result.get("trace", {})
        if isinstance(trace, dict):
            guardrail_trace = trace.get("guardrail", {})
            if isinstance(guardrail_trace, dict):
                action = guardrail_trace.get("action")
                if action and action != "NONE":
                    guardrail_action = guardrail_action or action

                input_assessment = guardrail_trace.get("inputAssessment", {})
                output_assessments = guardrail_trace.get(
                    "outputAssessments", []
                )

                categories = self._extract_categories(
                    input_assessment, output_assessments
                )
                if categories:
                    extra["guardrail_categories"] = categories

        if guardrail_action and guardrail_action != "NONE":
            extra["guardrail.triggered"] = True
            extra["guardrail.action"] = guardrail_action
            extra["guardrail_action"] = guardrail_action

            categories = extra.get("guardrail_categories", [])
            if categories:
                extra["guardrail.category"] = categories[0]
                extra["guardrail_category"] = self._safe_category(
                    categories[0]
                )

    def _extract_categories(
        self,
        input_assessment: Any,
        output_assessments: Any,
    ) -> list[str]:
        """Extract guardrail categories from assessments."""
        categories: list[str] = []

        if isinstance(input_assessment, dict):
            for policy_type in input_assessment.values():
                if isinstance(policy_type, list):
                    for item in policy_type:
                        if isinstance(item, dict):
                            cat = item.get("type") or item.get("name")
                            if cat:
                                categories.append(str(cat))

        if isinstance(output_assessments, list):
            for assessment in output_assessments:
                if isinstance(assessment, dict):
                    for policy_type in assessment.values():
                        if isinstance(policy_type, list):
                            for item in policy_type:
                                if isinstance(item, dict):
                                    cat = item.get("type") or item.get("name")
                                    if cat:
                                        categories.append(str(cat))

        return categories

    @staticmethod
    def _safe_guardrail_id(guardrail_id: str) -> str:
        """Apply cardinality control to guardrail IDs."""
        if guardrail_id in _seen_guardrail_ids:
            return guardrail_id
        if len(_seen_guardrail_ids) < _MAX_GUARDRAIL_IDS:
            _seen_guardrail_ids.add(guardrail_id)
            return guardrail_id
        return _CARDINALITY_OVERFLOW

    @staticmethod
    def _safe_category(category: str) -> str:
        """Map category to bounded enum or overflow."""
        upper = category.upper()
        if upper in _VALID_GUARDRAIL_CATEGORIES:
            return upper
        return _CARDINALITY_OVERFLOW

    @staticmethod
    def _parse_model_id(model_id: str | None) -> tuple[str, str]:
        """Parse Bedrock model ID into (model, provider)."""
        if not model_id:
            return ("unknown", "bedrock")

        parts = model_id.split(".", 1)
        if len(parts) == 2:
            return (parts[1], parts[0])
        return (model_id, "bedrock")
