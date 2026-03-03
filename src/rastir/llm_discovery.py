"""Auto-discovery of LLM client objects for the ``@llm`` decorator.

Scans a decorated function's **arguments**, **closure cells**, and
**globals** (filtered by ``co_names``) to find known LLM provider
client objects.  When found, the relevant call method is monkey-patched
with an interceptor that captures the full provider response and feeds
it to the adapter pipeline — regardless of what the user's function
returns.

Supported providers:

  - OpenAI (``openai.OpenAI``, ``openai.AsyncOpenAI``)
  - Azure OpenAI (``openai.AzureOpenAI``, ``openai.AsyncAzureOpenAI``)
  - Anthropic (``anthropic.Anthropic``, ``anthropic.AsyncAnthropic``)
  - Google Generative AI (``google.generativeai.GenerativeModel``)
  - Cohere (``cohere.ClientV2``, ``cohere.Client``)
  - Mistral (``mistralai.Mistral``)
  - Groq (``groq.Groq``, ``groq.AsyncGroq``)
  - LangChain chat models (``BaseChatModel`` subclasses)
  - Bedrock runtime (``boto3`` bedrock-runtime client)

The module is intentionally lazy about imports — provider SDKs are only
checked at runtime (duck-typing / class-name matching), so none of them
are hard dependencies.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any, Callable

logger = logging.getLogger("rastir")


# ---------------------------------------------------------------------------
# Recognition helpers — one per provider family
# ---------------------------------------------------------------------------

def _cls_chain(obj: Any) -> list[str]:
    """Return ``["module.ClassName", ...]`` for the MRO."""
    return [
        f"{getattr(c, '__module__', '')}.{c.__name__}"
        for c in type(obj).__mro__
    ]


def _module_name(obj: Any) -> str:
    return getattr(type(obj), "__module__", "") or ""


def _cls_name(obj: Any) -> str:
    return type(obj).__name__


# ---------------------------------------------------------------------------
# Each recognizer returns a list of (target_obj, method_path, is_async)
# where method_path is a dot-separated attribute chain to the callable.
# ---------------------------------------------------------------------------

def _recognize_openai(obj: Any) -> list[tuple[Any, str, bool]]:
    """Recognize ``openai.OpenAI`` or ``openai.AsyncOpenAI``."""
    mod = _module_name(obj)
    name = _cls_name(obj)
    if "openai" not in mod:
        return []
    # Skip Azure variants — handled separately
    if "Azure" in name:
        return []
    if name in ("OpenAI",):
        # obj.chat.completions.create
        chat = getattr(obj, "chat", None)
        if chat is None:
            return []
        completions = getattr(chat, "completions", None)
        if completions is None:
            return []
        if hasattr(completions, "create"):
            return [(completions, "create", False)]
    if name in ("AsyncOpenAI",):
        chat = getattr(obj, "chat", None)
        if chat is None:
            return []
        completions = getattr(chat, "completions", None)
        if completions is None:
            return []
        if hasattr(completions, "create"):
            return [(completions, "create", True)]
    return []


def _recognize_azure_openai(obj: Any) -> list[tuple[Any, str, bool]]:
    """Recognize ``openai.AzureOpenAI`` or ``openai.AsyncAzureOpenAI``."""
    mod = _module_name(obj)
    name = _cls_name(obj)
    if "openai" not in mod:
        return []
    if name == "AzureOpenAI":
        chat = getattr(obj, "chat", None)
        if chat is None:
            return []
        completions = getattr(chat, "completions", None)
        if completions is None:
            return []
        if hasattr(completions, "create"):
            return [(completions, "create", False)]
    if name == "AsyncAzureOpenAI":
        chat = getattr(obj, "chat", None)
        if chat is None:
            return []
        completions = getattr(chat, "completions", None)
        if completions is None:
            return []
        if hasattr(completions, "create"):
            return [(completions, "create", True)]
    return []


def _recognize_anthropic(obj: Any) -> list[tuple[Any, str, bool]]:
    """Recognize ``anthropic.Anthropic`` or ``anthropic.AsyncAnthropic``."""
    mod = _module_name(obj)
    name = _cls_name(obj)
    if "anthropic" not in mod:
        return []
    if name == "Anthropic":
        messages = getattr(obj, "messages", None)
        if messages and hasattr(messages, "create"):
            return [(messages, "create", False)]
    if name == "AsyncAnthropic":
        messages = getattr(obj, "messages", None)
        if messages and hasattr(messages, "create"):
            return [(messages, "create", True)]
    return []


def _recognize_google_genai(obj: Any) -> list[tuple[Any, str, bool]]:
    """Recognize ``google.generativeai.GenerativeModel``."""
    mod = _module_name(obj)
    name = _cls_name(obj)
    if "google" not in mod and "generativeai" not in mod:
        return []
    if name == "GenerativeModel" and hasattr(obj, "generate_content"):
        return [(obj, "generate_content", False)]
    return []


def _recognize_cohere(obj: Any) -> list[tuple[Any, str, bool]]:
    """Recognize ``cohere.ClientV2`` or ``cohere.Client``."""
    mod = _module_name(obj)
    name = _cls_name(obj)
    if "cohere" not in mod:
        return []
    if name in ("ClientV2", "Client") and hasattr(obj, "chat"):
        return [(obj, "chat", False)]
    return []


def _recognize_mistral(obj: Any) -> list[tuple[Any, str, bool]]:
    """Recognize ``mistralai.Mistral``."""
    mod = _module_name(obj)
    name = _cls_name(obj)
    if "mistral" not in mod:
        return []
    if name == "Mistral":
        chat = getattr(obj, "chat", None)
        if chat and hasattr(chat, "complete"):
            return [(chat, "complete", False)]
    return []


def _recognize_groq(obj: Any) -> list[tuple[Any, str, bool]]:
    """Recognize ``groq.Groq`` or ``groq.AsyncGroq``."""
    mod = _module_name(obj)
    name = _cls_name(obj)
    if "groq" not in mod:
        return []
    if name == "Groq":
        chat = getattr(obj, "chat", None)
        if chat is None:
            return []
        completions = getattr(chat, "completions", None)
        if completions and hasattr(completions, "create"):
            return [(completions, "create", False)]
    if name == "AsyncGroq":
        chat = getattr(obj, "chat", None)
        if chat is None:
            return []
        completions = getattr(chat, "completions", None)
        if completions and hasattr(completions, "create"):
            return [(completions, "create", True)]
    return []


def _recognize_langchain(obj: Any) -> list[tuple[Any, str, bool]]:
    """Recognize LangChain ``BaseChatModel`` subclasses."""
    for base in type(obj).__mro__:
        mod = getattr(base, "__module__", "") or ""
        if "langchain" in mod and base.__name__ in (
            "BaseChatModel", "BaseLLM", "BaseLanguageModel",
        ):
            results = []
            if hasattr(obj, "invoke"):
                results.append((obj, "invoke", False))
            if hasattr(obj, "ainvoke"):
                results.append((obj, "ainvoke", True))
            return results
    return []


def _recognize_bedrock(obj: Any) -> list[tuple[Any, str, bool]]:
    """Recognize ``boto3`` bedrock-runtime client."""
    name = _cls_name(obj)
    if name != "BedrockRuntime":
        return []
    # boto3 clients have a meta attribute with service model
    meta = getattr(obj, "meta", None)
    if meta is None:
        return []
    service = getattr(meta, "service_model", None)
    if service is None:
        return []
    sname = getattr(service, "service_name", "")
    if "bedrock" in sname:
        if hasattr(obj, "invoke_model"):
            return [(obj, "invoke_model", False)]
    return []


# Ordered recognition pipeline
_RECOGNIZERS: list[Callable[[Any], list[tuple[Any, str, bool]]]] = [
    _recognize_openai,
    _recognize_azure_openai,
    _recognize_anthropic,
    _recognize_google_genai,
    _recognize_cohere,
    _recognize_mistral,
    _recognize_groq,
    _recognize_langchain,
    _recognize_bedrock,
]


def _recognize_llm_client(obj: Any) -> list[tuple[Any, str, bool]]:
    """Run all recognizers on *obj*. Return targets if any match."""
    for recognizer in _RECOGNIZERS:
        try:
            targets = recognizer(obj)
            if targets:
                return targets
        except Exception:
            continue
    return []


# ---------------------------------------------------------------------------
# Scan logic — mirrors langgraph_support._walk_func_for_wrapping
# ---------------------------------------------------------------------------

def discover_llm_clients(func: Any, args: tuple, kwargs: dict) -> list[tuple[Any, str, bool]]:
    """Scan function arguments, closures, and globals for LLM client objects.

    Returns a list of ``(target_obj, method_name, is_async)`` tuples
    describing each call-method to intercept.
    """
    seen: set[int] = set()
    targets: list[tuple[Any, str, bool]] = []

    def _check(obj: Any) -> None:
        oid = id(obj)
        if oid in seen:
            return
        seen.add(oid)
        found = _recognize_llm_client(obj)
        if found:
            targets.extend(found)
            return
        # For nested objects (e.g. obj.chat.completions), the recognizer
        # already traverses the chain, so we don't recurse further.

    # 1. Function arguments (positional + keyword)
    for arg in args:
        _check(arg)
    for val in kwargs.values():
        _check(val)

    # 2. Closure cells
    closure = getattr(func, "__closure__", None)
    if closure:
        for cell in closure:
            try:
                val = cell.cell_contents
            except ValueError:
                continue
            _check(val)

    # 3. Globals referenced by the function (filtered by co_names)
    code = getattr(func, "__code__", None)
    func_globals = getattr(func, "__globals__", None)
    if code is not None and func_globals is not None:
        for varname in code.co_names:
            val = func_globals.get(varname)
            if val is not None:
                _check(val)

    return targets


# ---------------------------------------------------------------------------
# Interceptor — monkey-patches a method and captures the response
# ---------------------------------------------------------------------------

def _make_interceptor(
    original_method: Any,
    span: Any,
    is_async: bool,
) -> tuple[Any, Any]:
    """Create an interceptor wrapper around *original_method*.

    Returns ``(interceptor, original_method)`` so the caller can restore
    the original later.

    The interceptor calls the original, then feeds the full response to
    ``_extract_llm_metadata`` and ``_capture_completion_text`` on the span.
    """
    from rastir.decorators import _extract_llm_metadata, _capture_completion_text

    if is_async:
        @functools.wraps(original_method)
        async def async_interceptor(*args: Any, **kwargs: Any) -> Any:
            result = await original_method(*args, **kwargs)
            try:
                _extract_llm_metadata(span, result)
                _capture_completion_text(span, result)
            except Exception:
                logger.debug("LLM discovery: metadata extraction failed", exc_info=True)
            return result
        return async_interceptor, original_method
    else:
        @functools.wraps(original_method)
        def sync_interceptor(*args: Any, **kwargs: Any) -> Any:
            result = original_method(*args, **kwargs)
            try:
                _extract_llm_metadata(span, result)
                _capture_completion_text(span, result)
            except Exception:
                logger.debug("LLM discovery: metadata extraction failed", exc_info=True)
            return result
        return sync_interceptor, original_method


def install_interceptors(
    targets: list[tuple[Any, str, bool]],
    span: Any,
) -> list[tuple[Any, str, Any]]:
    """Monkey-patch each target's method with an interceptor.

    Returns a list of ``(target_obj, method_name, original_method)``
    for later restoration.
    """
    originals: list[tuple[Any, str, Any]] = []
    for target_obj, method_name, is_async in targets:
        original = getattr(target_obj, method_name, None)
        if original is None:
            continue
        # Skip if already intercepted
        if getattr(original, "_rastir_intercepted", False):
            continue
        interceptor, orig = _make_interceptor(original, span, is_async)
        interceptor._rastir_intercepted = True  # type: ignore[attr-defined]
        try:
            setattr(target_obj, method_name, interceptor)
            originals.append((target_obj, method_name, orig))
        except (AttributeError, TypeError):
            logger.debug(
                "LLM discovery: cannot patch %s.%s",
                type(target_obj).__name__, method_name,
            )
    return originals


def restore_originals(originals: list[tuple[Any, str, Any]]) -> None:
    """Restore all monkey-patched methods to their originals."""
    for target_obj, method_name, original in originals:
        try:
            setattr(target_obj, method_name, original)
        except (AttributeError, TypeError):
            pass
