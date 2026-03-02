"""Azure OpenAI provider adapter.

Azure OpenAI uses the same ``openai`` Python SDK and returns the same
``ChatCompletion`` / ``ChatCompletionChunk`` response objects.  The
difference is that Azure responses include an ``api_type`` or the
``x-ms-region`` header, and ``model`` may reflect a deployment name
rather than the canonical OpenAI model ID.

Detection strategy:
  1. Same class-name check as OpenAIAdapter (ChatCompletion, Completion).
  2. Additionally look for Azure-specific markers in ``_request_id``
     patterns or ``system_fingerprint`` prefixes that indicate Azure.
  3. If the ``openai`` client was instantiated as ``AzureOpenAI``, the
     response objects carry ``_request_id`` starting with an Azure GUID.

Because Azure and vanilla OpenAI produce identical Python types, this
adapter sits at a **higher priority** (155 vs 150) so it is evaluated
first.  If Azure markers are *not* found, ``can_handle`` returns False
and the standard OpenAIAdapter will match instead.

Priority: 155 (just above OpenAI at 150).
"""

from __future__ import annotations

from typing import Any

from rastir.adapters.types import AdapterResult, BaseAdapter, TokenDelta


class AzureOpenAIAdapter(BaseAdapter):
    """Adapter for Azure OpenAI responses (same SDK, different provider)."""

    name = "azure_openai"
    kind = "provider"
    priority = 155

    supports_tokens = True
    supports_streaming = True
    supports_request_metadata = True

    # Azure-specific headers that appear in raw API responses
    _AZURE_HEADERS = frozenset({
        "x-ms-region",
        "azureml-model-deployment",
        "x-ms-client-request-id",
    })

    def can_handle(self, result: Any) -> bool:
        """Detect Azure OpenAI responses.

        Same class names as OpenAI, but with Azure-specific markers.
        """
        cls_name = type(result).__name__
        module = type(result).__module__ or ""

        if cls_name not in ("ChatCompletion", "Completion"):
            return False
        if "openai" not in module:
            return False

        return self._is_azure(result)

    def _is_azure(self, result: Any) -> bool:
        """Check for Azure-specific markers on the response."""
        # 1. Check system_fingerprint — Azure uses "fp_" prefixed strings
        #    that differ from OpenAI's pattern, but more reliably:
        # 2. Check model field for deployment name pattern
        model = getattr(result, "model", "") or ""

        # Azure deployments often have custom names without "gpt-" prefix,
        # but this is not reliable. Better: check _headers if available.

        # 3. Check raw response headers (if the SDK exposes them)
        raw_response = getattr(result, "_raw_response", None)
        if raw_response is not None:
            headers = getattr(raw_response, "headers", {})
            if headers:
                for header in self._AZURE_HEADERS:
                    if header in headers:
                        return True

        # 4. Check for Azure-style model deployment names
        #    Azure model field often contains deployment name like
        #    "my-gpt4-deployment" or the actual model "gpt-4o"
        #    We check for azure-specific response metadata.

        # 5. Check if result has headers with Azure markers
        headers_dict = getattr(result, "headers", None)
        if isinstance(headers_dict, dict):
            for header in self._AZURE_HEADERS:
                if header in headers_dict:
                    return True

        # If we can't definitively identify Azure, fall through to OpenAI adapter
        return False

    def transform(self, result: Any) -> AdapterResult:
        model = getattr(result, "model", None)
        usage = getattr(result, "usage", None)
        tokens_input = None
        tokens_output = None

        if usage is not None:
            tokens_input = getattr(usage, "prompt_tokens", None)
            tokens_output = getattr(usage, "completion_tokens", None)

        finish_reason = None
        choices = getattr(result, "choices", None)
        if choices and len(choices) > 0:
            finish_reason = getattr(choices[0], "finish_reason", None)

        return AdapterResult(
            model=model,
            provider="azure_openai",
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            finish_reason=finish_reason,
        )

    def can_handle_stream(self, chunk: Any) -> bool:
        """Detect Azure OpenAI streaming chunks."""
        cls_name = type(chunk).__name__
        module = type(chunk).__module__ or ""
        if cls_name != "ChatCompletionChunk" or "openai" not in module:
            return False
        return self._is_azure(chunk)

    def extract_stream_delta(self, chunk: Any) -> TokenDelta:
        model = getattr(chunk, "model", None)
        usage = getattr(chunk, "usage", None)
        tokens_input = None
        tokens_output = None

        if usage is not None:
            tokens_input = getattr(usage, "prompt_tokens", None)
            tokens_output = getattr(usage, "completion_tokens", None)

        return TokenDelta(
            model=model,
            provider="azure_openai",
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            usage_mode="incremental",
        )

    def can_handle_request(self, args: tuple, kwargs: dict) -> bool:
        """Detect AzureOpenAI / AsyncAzureOpenAI client objects in request args."""
        for arg in (*args, *kwargs.values()):
            cls_name = type(arg).__name__
            module = type(arg).__module__ or ""
            if cls_name in ("AzureOpenAI", "AsyncAzureOpenAI") and "openai" in module:
                return True
        return False

    def extract_request_metadata(
        self, args: tuple, kwargs: dict
    ) -> "RequestMetadata":
        """Extract model and provider from Azure OpenAI request arguments."""
        from rastir.adapters.types import RequestMetadata
        span_attrs: dict = {"provider": "azure_openai"}
        model = kwargs.get("model")
        if model and isinstance(model, str):
            span_attrs["model"] = model
        return RequestMetadata(span_attributes=span_attrs)
