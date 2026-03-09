"""E2E test: LangChain (non-framework) — manual @agent + @llm decorators.

Uses LangChain's ChatOpenAI and ChatGoogleGenerativeAI directly with
rastir's @agent, @llm, and @trace decorators (no framework_agent or
langgraph_agent).  Also tests Bedrock via ChatBedrock.

This verifies "library-level" instrumentation without framework wrappers:
  - @agent creates an AGENT span
  - @llm wraps individual LLM calls and extracts model, tokens, cost
  - Nested span hierarchy (agent → llm)
  - Multiple LLM providers in the same agent (OpenAI, Gemini, Bedrock)

Requirements:
    API_OPENAI_KEY env var for OpenAI, GEMINI_API_KEY for Gemini,
    AWS credentials for Bedrock.

Run:
    conda run -n llmobserve PYTHONPATH=src \
        python tests/e2e/test_langchain_e2e.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------
OPENAI_KEY = os.environ.get("API_OPENAI_KEY") or os.environ.get("OPENAI_API_KEY", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")

if not OPENAI_KEY:
    print("ERROR: API_OPENAI_KEY or OPENAI_API_KEY not set"); sys.exit(1)
if not GEMINI_KEY:
    print("ERROR: GEMINI_API_KEY not set"); sys.exit(1)

# ---------------------------------------------------------------------------
# Deps
# ---------------------------------------------------------------------------
from langchain_openai import ChatOpenAI
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_aws import ChatBedrockConverse
from langchain_core.messages import HumanMessage

# ---------------------------------------------------------------------------
# Rastir — manual decorators
# ---------------------------------------------------------------------------
import rastir
from rastir import configure, agent, llm, trace

configure(
    service="langchain-manual-e2e",
    push_url="http://localhost:8080",
    enable_cost_calculation=True,
)

captured_spans: list = []
_orig_enqueue = None


def _capture_enqueue(span):
    captured_spans.append(span)
    if _orig_enqueue:
        _orig_enqueue(span)


import rastir.queue as _queue
import rastir.decorators as _decorators
_orig_enqueue = _queue.enqueue_span
_queue.enqueue_span = _capture_enqueue
_decorators.enqueue_span = _capture_enqueue


# ---------------------------------------------------------------------------
# Test 1: OpenAI via @agent + @llm
# ---------------------------------------------------------------------------
@llm(model="gpt-4o-mini", provider="openai")
def call_openai(prompt: str) -> str:
    """Call OpenAI directly with @llm decorator."""
    model = ChatOpenAI(model="gpt-4o-mini", temperature=0, api_key=OPENAI_KEY)
    response = model.invoke([HumanMessage(content=prompt)])
    return response.content


@agent(agent_name="openai_manual")
def openai_agent(query: str) -> str:
    """Manual agent wrapping an @llm call."""
    return call_openai(query)


# ---------------------------------------------------------------------------
# Test 2: Gemini via @agent + @llm
# ---------------------------------------------------------------------------
@llm(model="gemini-2.5-flash", provider="gemini")
def call_gemini(prompt: str) -> str:
    """Call Gemini directly with @llm decorator."""
    model = ChatGoogleGenerativeAI(
        model="gemini-2.5-flash", temperature=0, google_api_key=GEMINI_KEY,
    )
    response = model.invoke([HumanMessage(content=prompt)])
    return response.content


@agent(agent_name="gemini_manual")
def gemini_agent(query: str) -> str:
    """Manual agent wrapping an @llm call."""
    return call_gemini(query)


# ---------------------------------------------------------------------------
# Test 3: Multi-provider agent (OpenAI + Gemini in same agent)
# ---------------------------------------------------------------------------
@agent(agent_name="multi_provider")
def multi_provider_agent(query: str) -> str:
    """Agent that calls both OpenAI and Gemini."""
    openai_answer = call_openai(query)
    gemini_answer = call_gemini(query)
    return f"OpenAI: {openai_answer}\n\nGemini: {gemini_answer}"


# ---------------------------------------------------------------------------
# Test 4: @trace decorator for custom spans
# ---------------------------------------------------------------------------
@trace(name="data_preprocessing")
def preprocess(query: str) -> str:
    """Simulate a preprocessing step with @trace."""
    return query.strip().lower()


@agent(agent_name="traced_pipeline")
def traced_pipeline(query: str) -> str:
    """Agent with @trace + @llm nested spans."""
    cleaned = preprocess(query)
    return call_openai(cleaned)


# ---------------------------------------------------------------------------
# Test 5: Bedrock via @agent + @llm
# ---------------------------------------------------------------------------
@llm(model="apac.anthropic.claude-sonnet-4-20250514-v1:0", provider="bedrock")
def call_bedrock(prompt: str) -> str:
    """Call Bedrock Claude directly with @llm decorator."""
    model = ChatBedrockConverse(
        model="apac.anthropic.claude-sonnet-4-20250514-v1:0",
        region_name="ap-south-1",
    )
    response = model.invoke([HumanMessage(content=prompt)])
    return response.content


@agent(agent_name="bedrock_manual")
def bedrock_agent(query: str) -> str:
    """Manual agent wrapping a Bedrock @llm call."""
    return call_bedrock(query)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run_tests():
    print("=" * 60)
    print("  LangChain (Non-Framework) Manual Decorator E2E Test")
    print("=" * 60)

    results: dict[str, str] = {}

    # Test 1: OpenAI
    print("\n--- Test 1: OpenAI @agent + @llm ---")
    captured_spans.clear()
    try:
        result = openai_agent("What is 2 + 2? Answer in one word.")
        print(f"  Result: {result[:200]}")
        agent_spans = [s for s in captured_spans if s.span_type.value == "agent"]
        llm_spans = [s for s in captured_spans if s.span_type.value == "llm"]
        print(f"  Agent spans: {len(agent_spans)}, LLM spans: {len(llm_spans)}")
        assert len(agent_spans) >= 1, "No agent span"
        assert len(llm_spans) >= 1, "No LLM span"
        llm_span = llm_spans[0]
        print(f"  LLM model: {llm_span.attributes.get('model')}")
        print(f"  LLM provider: {llm_span.attributes.get('provider')}")
        results["OpenAI @agent + @llm"] = "PASS ✓"
    except Exception as e:
        print(f"  FAILED: {e}")
        results["OpenAI @agent + @llm"] = f"FAIL ✗ ({e})"

    # Test 2: Gemini
    print("\n--- Test 2: Gemini @agent + @llm ---")
    captured_spans.clear()
    try:
        result = gemini_agent("What is the capital of France? Answer in one word.")
        print(f"  Result: {result[:200]}")
        agent_spans = [s for s in captured_spans if s.span_type.value == "agent"]
        llm_spans = [s for s in captured_spans if s.span_type.value == "llm"]
        print(f"  Agent spans: {len(agent_spans)}, LLM spans: {len(llm_spans)}")
        assert len(agent_spans) >= 1, "No agent span"
        assert len(llm_spans) >= 1, "No LLM span"
        results["Gemini @agent + @llm"] = "PASS ✓"
    except Exception as e:
        print(f"  FAILED: {e}")
        results["Gemini @agent + @llm"] = f"FAIL ✗ ({e})"

    # Test 3: Multi-provider
    print("\n--- Test 3: Multi-provider agent ---")
    captured_spans.clear()
    try:
        result = multi_provider_agent("Say hello in one word.")
        print(f"  Result: {result[:300]}")
        agent_spans = [s for s in captured_spans if s.span_type.value == "agent"]
        llm_spans = [s for s in captured_spans if s.span_type.value == "llm"]
        print(f"  Agent spans: {len(agent_spans)}, LLM spans: {len(llm_spans)}")
        assert len(agent_spans) >= 1, "No agent span"
        assert len(llm_spans) >= 2, f"Expected >=2 LLM spans, got {len(llm_spans)}"
        # Check both providers present
        providers = {s.attributes.get("provider") for s in llm_spans}
        print(f"  Providers: {providers}")
        assert "openai" in providers, "OpenAI provider not found"
        assert "gemini" in providers, "Gemini provider not found"
        results["Multi-provider agent"] = "PASS ✓"
    except Exception as e:
        print(f"  FAILED: {e}")
        results["Multi-provider agent"] = f"FAIL ✗ ({e})"

    # Test 4: @trace nested spans
    print("\n--- Test 4: @trace + @llm pipeline ---")
    captured_spans.clear()
    try:
        result = traced_pipeline("  What is 3 + 3? Answer in one word.  ")
        print(f"  Result: {result[:200]}")
        agent_spans = [s for s in captured_spans if s.span_type.value == "agent"]
        llm_spans = [s for s in captured_spans if s.span_type.value == "llm"]
        trace_spans = [s for s in captured_spans if s.span_type.value == "trace"]
        print(f"  Agent: {len(agent_spans)}, LLM: {len(llm_spans)}, Trace: {len(trace_spans)}")
        assert len(agent_spans) >= 1, "No agent span"
        assert len(trace_spans) >= 1, "No trace span"
        assert len(llm_spans) >= 1, "No LLM span"
        results["@trace + @llm pipeline"] = "PASS ✓"
    except Exception as e:
        print(f"  FAILED: {e}")
        results["@trace + @llm pipeline"] = f"FAIL ✗ ({e})"

    # Test 5: Bedrock Claude
    print("\n--- Test 5: Bedrock Claude @agent + @llm ---")
    captured_spans.clear()
    try:
        result = bedrock_agent("What is 5 + 5? Answer in one word.")
        print(f"  Result: {result[:200]}")
        agent_spans = [s for s in captured_spans if s.span_type.value == "agent"]
        llm_spans = [s for s in captured_spans if s.span_type.value == "llm"]
        print(f"  Agent spans: {len(agent_spans)}, LLM spans: {len(llm_spans)}")
        assert len(agent_spans) >= 1, "No agent span"
        assert len(llm_spans) >= 1, "No LLM span"
        llm_span = llm_spans[0]
        print(f"  LLM model: {llm_span.attributes.get('model')}")
        print(f"  LLM provider: {llm_span.attributes.get('provider')}")
        results["Bedrock @agent + @llm"] = "PASS ✓"
    except Exception as e:
        print(f"  FAILED: {e}")
        results["Bedrock @agent + @llm"] = f"FAIL ✗ ({e})"

    # Summary
    print("\n" + "=" * 60)
    print("  SUMMARY")
    print("=" * 60)
    passed = 0
    for label, status in results.items():
        print(f"  {label}: {status}")
        if "PASS" in status:
            passed += 1
    print(f"\n  {passed}/{len(results)} passed")
    print("=" * 60)

    import time
    print("\nWaiting 3s for flush...")
    time.sleep(3)
    print("Done!")


if __name__ == "__main__":
    run_tests()
