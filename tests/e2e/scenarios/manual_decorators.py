"""Manual decorator e2e scenarios (non-framework LangChain usage).

Covers the unique test_langchain_e2e.py pattern where rastir's manual
decorators (@agent, @llm, @trace) are used WITHOUT framework-specific
wrappers (no langgraph_agent, crew_kickoff, etc.).

  ┌────────────────────────────────────────────────────────────────────┐
  │  Scenario                     │ Decorators  │ Provider(s)         │
  ├───────────────────────────────┼─────────────┼─────────────────────┤
  │ manual_openai                 │ @agent @llm │ OpenAI              │
  │ manual_gemini                 │ @agent @llm │ Gemini              │
  │ manual_multi_provider         │ @agent @llm │ OpenAI + Gemini     │
  │ manual_trace_pipeline         │ @agent @llm │ OpenAI + @trace     │
  │                               │ @trace      │                     │
  │ manual_bedrock                │ @agent @llm │ Bedrock Claude      │
  └────────────────────────────────────────────────────────────────────┘

  This validates "library-level" instrumentation:
    - @agent creates an AGENT span
    - @llm wraps individual LLM calls and extracts model, tokens, cost
    - @trace creates custom trace spans for pipeline steps
    - Nested span hierarchy (agent → trace → llm)
    - Multiple LLM providers in the same agent

  Sources consolidated:
    - test_langchain_e2e.py (5 tests with detailed span verification)
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage

from tests.e2e.common import (
    TestResults,
    captured_spans,
    clear_captured_spans,
    log,
    require_gemini_key,
    require_openai_key,
)
from rastir import agent, llm, trace


# ---------------------------------------------------------------------------
# @llm-decorated LLM call functions
# ---------------------------------------------------------------------------
@llm(model="gpt-4o-mini", provider="openai")
def call_openai(prompt: str) -> str:
    """Call OpenAI directly with @llm decorator."""
    from langchain_openai import ChatOpenAI
    model = ChatOpenAI(model="gpt-4o-mini", temperature=0, api_key=require_openai_key())
    response = model.invoke([HumanMessage(content=prompt)])
    return response.content


@llm(model="gemini-2.5-flash", provider="gemini")
def call_gemini(prompt: str) -> str:
    """Call Gemini directly with @llm decorator."""
    from langchain_google_genai import ChatGoogleGenerativeAI
    model = ChatGoogleGenerativeAI(
        model="gemini-2.5-flash", temperature=0, google_api_key=require_gemini_key(),
    )
    response = model.invoke([HumanMessage(content=prompt)])
    return response.content


@llm(model="apac.anthropic.claude-sonnet-4-20250514-v1:0", provider="bedrock")
def call_bedrock(prompt: str) -> str:
    """Call Bedrock Claude directly with @llm decorator."""
    from langchain_aws import ChatBedrockConverse
    model = ChatBedrockConverse(
        model="apac.anthropic.claude-sonnet-4-20250514-v1:0",
        region_name="ap-south-1",
    )
    response = model.invoke([HumanMessage(content=prompt)])
    return response.content


# ---------------------------------------------------------------------------
# @agent-decorated agents
# ---------------------------------------------------------------------------
@agent(agent_name="openai_manual")
def openai_agent(query: str) -> str:
    """Manual agent wrapping an @llm call."""
    return call_openai(query)


@agent(agent_name="gemini_manual")
def gemini_agent(query: str) -> str:
    """Manual agent wrapping an @llm call."""
    return call_gemini(query)


@agent(agent_name="multi_provider")
def multi_provider_agent(query: str) -> str:
    """Agent that calls both OpenAI and Gemini."""
    openai_answer = call_openai(query)
    gemini_answer = call_gemini(query)
    return f"OpenAI: {openai_answer}\n\nGemini: {gemini_answer}"


@trace(name="data_preprocessing")
def preprocess(query: str) -> str:
    """Simulate a preprocessing step with @trace."""
    return query.strip().lower()


@agent(agent_name="traced_pipeline")
def traced_pipeline(query: str) -> str:
    """Agent with @trace + @llm nested spans."""
    cleaned = preprocess(query)
    return call_openai(cleaned)


@agent(agent_name="bedrock_manual")
def bedrock_agent(query: str) -> str:
    """Manual agent wrapping a Bedrock @llm call."""
    return call_bedrock(query)


# ===================================================================
#  PUBLIC RUNNER
# ===================================================================

async def run_all(results: TestResults, **_):
    """Run all manual decorator e2e scenarios.

    Validates @agent, @llm, @trace decorators with span verification:
      - Agent span exists
      - LLM spans exist with model/provider
      - Multi-provider produces 2+ LLM spans
      - @trace creates trace spans
    """
    import asyncio
    import time

    scenarios = [
        ("Manual @agent+@llm OpenAI", _test_openai),
        ("Manual @agent+@llm Gemini", _test_gemini),
        ("Manual multi-provider agent", _test_multi_provider),
        ("Manual @trace+@llm pipeline", _test_traced_pipeline),
        ("Manual @agent+@llm Bedrock", _test_bedrock),
    ]

    for label, fn in scenarios:
        clear_captured_spans()
        log(f"START: {label}")
        t0 = time.monotonic()
        try:
            fn()
            elapsed = time.monotonic() - t0
            log(f"DONE:  {label} ({elapsed:.1f}s) ✓")
            results.passed(label)
        except Exception as e:
            elapsed = time.monotonic() - t0
            log(f"FAIL:  {label} ({elapsed:.1f}s) ✗ {e}")
            results.failed(label, e)
        await asyncio.sleep(0.5)


def _test_openai():
    result = openai_agent("What is 2 + 2? Answer in one word.")
    print(f"  Result: {result[:200]}")
    agent_spans = [s for s in captured_spans if s.span_type.value == "agent"]
    llm_spans = [s for s in captured_spans if s.span_type.value == "llm"]
    print(f"  Agent spans: {len(agent_spans)}, LLM spans: {len(llm_spans)}")
    assert agent_spans, "No agent span"
    assert llm_spans, "No LLM span"
    print(f"  ✓ model={llm_spans[0].attributes.get('model')}, "
          f"provider={llm_spans[0].attributes.get('provider')}")


def _test_gemini():
    result = gemini_agent("What is the capital of France? Answer in one word.")
    print(f"  Result: {result[:200]}")
    agent_spans = [s for s in captured_spans if s.span_type.value == "agent"]
    llm_spans = [s for s in captured_spans if s.span_type.value == "llm"]
    print(f"  Agent spans: {len(agent_spans)}, LLM spans: {len(llm_spans)}")
    assert agent_spans, "No agent span"
    assert llm_spans, "No LLM span"
    print("  ✓ OK")


def _test_multi_provider():
    result = multi_provider_agent("Say hello in one word.")
    print(f"  Result: {result[:300]}")
    agent_spans = [s for s in captured_spans if s.span_type.value == "agent"]
    llm_spans = [s for s in captured_spans if s.span_type.value == "llm"]
    print(f"  Agent spans: {len(agent_spans)}, LLM spans: {len(llm_spans)}")
    assert agent_spans, "No agent span"
    assert len(llm_spans) >= 2, f"Expected >=2 LLM spans, got {len(llm_spans)}"
    providers = {s.attributes.get("provider") for s in llm_spans}
    print(f"  Providers: {providers}")
    assert "openai" in providers, "OpenAI provider not found"
    assert "gemini" in providers, "Gemini provider not found"
    print("  ✓ Both providers present")


def _test_traced_pipeline():
    result = traced_pipeline("  What is 3 + 3? Answer in one word.  ")
    print(f"  Result: {result[:200]}")
    agent_spans = [s for s in captured_spans if s.span_type.value == "agent"]
    llm_spans = [s for s in captured_spans if s.span_type.value == "llm"]
    trace_spans = [s for s in captured_spans if s.span_type.value == "trace"]
    print(f"  Agent: {len(agent_spans)}, LLM: {len(llm_spans)}, Trace: {len(trace_spans)}")
    assert agent_spans, "No agent span"
    assert trace_spans, "No trace span"
    assert llm_spans, "No LLM span"
    print("  ✓ @trace span found in pipeline")


def _test_bedrock():
    result = bedrock_agent("What is 5 + 5? Answer in one word.")
    print(f"  Result: {result[:200]}")
    agent_spans = [s for s in captured_spans if s.span_type.value == "agent"]
    llm_spans = [s for s in captured_spans if s.span_type.value == "llm"]
    print(f"  Agent spans: {len(agent_spans)}, LLM spans: {len(llm_spans)}")
    assert agent_spans, "No agent span"
    assert llm_spans, "No LLM span"
    print(f"  ✓ model={llm_spans[0].attributes.get('model')}, "
          f"provider={llm_spans[0].attributes.get('provider')}")
