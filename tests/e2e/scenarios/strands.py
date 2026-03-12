"""Strands (AWS Strands Agents) e2e scenarios.

Covers ALL Strands variations tested across the old e2e files:

  ┌────────────────────────────────────────────────────────────────────┐
  │  Scenario                     │ Provider │ Tools  │ Special       │
  ├───────────────────────────────┼──────────┼────────┼───────────────┤
  │ strands_bedrock_local         │ Bedrock  │ local  │ math ops      │
  │ strands_bedrock_mcp           │ Bedrock  │ MCP    │ city data     │
  │ strands_streaming_ttft        │ Bedrock  │ local  │ TTFT + 5 runs │
  └────────────────────────────────────────────────────────────────────┘

  Strands agent pattern:
    1. Create BedrockModel with model_id (Claude Sonnet 4)
    2. Create Agent with model + tools + system_prompt
    3. Call agent(prompt) synchronously — Strands is sync-only

  For TTFT (Time-to-First-Token):
    - Strands uses Bedrock streaming by default
    - Rastir's enable_ttft=True records rastir_ttft_seconds_bucket
    - The TTFT scenario sends 5 varied prompts to produce multiple
      histogram data points for Prometheus

  MCP-backed tools use HTTP POST with traceparent headers, same
  pattern as CrewAI and ADK.

  Sources consolidated:
    - test_strands_e2e.py             (basic local tools)
    - test_strands_streaming_ttft.py  (TTFT with 4 tools, 5 prompts)
    - run_all_combinations.py         (Strands combos 13-14)
"""

from __future__ import annotations

import asyncio
import time

from strands import Agent as StrandsAgent
from strands.models.bedrock import BedrockModel
import strands

from tests.e2e.common import (
    TestResults,
    call_mcp_tool,
    clear_captured_spans,
    log,
    start_mcp_server,
)
from rastir import strands_agent


# ---------------------------------------------------------------------------
# Local tools — @strands.tool decorated functions
# ---------------------------------------------------------------------------
@strands.tool
def strands_add_numbers(a: int, b: int) -> str:
    """Add two numbers together and return the result."""
    return str(a + b)


@strands.tool
def strands_multiply_numbers(a: int, b: int) -> str:
    """Multiply two numbers together and return the result."""
    return str(a * b)


# ---------------------------------------------------------------------------
# TTFT-specific local tools — used by the streaming scenario
# ---------------------------------------------------------------------------
@strands.tool
def strands_get_weather_local(city: str) -> str:
    """Get the weather for a city (local mock, no MCP)."""
    weathers = {
        "london": "15°C, cloudy",
        "paris": "18°C, sunny",
        "tokyo": "22°C, humid",
        "new york": "20°C, partly cloudy",
    }
    return weathers.get(city.lower(), f"25°C, clear in {city}")


@strands.tool
def strands_convert_temperature(value: float, from_unit: str, to_unit: str) -> str:
    """Convert temperature between celsius and fahrenheit."""
    if from_unit.lower().startswith("c") and to_unit.lower().startswith("f"):
        result = value * 9 / 5 + 32
    elif from_unit.lower().startswith("f") and to_unit.lower().startswith("c"):
        result = (value - 32) * 5 / 9
    else:
        result = value
    return f"{result:.1f} {to_unit}"


# ---------------------------------------------------------------------------
# MCP-backed tools — HTTP POST to MCP test server with traceparent
# ---------------------------------------------------------------------------
_MCP_URL: str = ""


def _set_mcp_url(url: str):
    global _MCP_URL
    _MCP_URL = url


@strands.tool
def strands_get_weather(city: str) -> str:
    """Get the current weather for a city."""
    return call_mcp_tool(_MCP_URL, "get_weather", {"city": city})


@strands.tool
def strands_get_population(city: str) -> str:
    """Get the approximate population of a city."""
    return call_mcp_tool(_MCP_URL, "get_population", {"city": city})


# ---------------------------------------------------------------------------
# Shared model constructor
# ---------------------------------------------------------------------------
def _bedrock_model():
    return BedrockModel(model_id="apac.anthropic.claude-sonnet-4-20250514-v1:0")


# ===================================================================
#  SCENARIO FUNCTIONS
# ===================================================================

def strands_bedrock_local():
    """Strands Agent + Bedrock Claude + local math tools."""
    model = _bedrock_model()
    agent = StrandsAgent(
        model=model,
        tools=[strands_add_numbers, strands_multiply_numbers],
        system_prompt="You are a math assistant. Use the tools to solve problems. Be concise.",
    )

    @strands_agent(agent_name="strands_bedrock_local")
    def invoke(a):
        return a("Add 12 and 18, then multiply the result by 3.")

    return invoke(agent)


def strands_bedrock_mcp():
    """Strands Agent + Bedrock Claude + MCP tools (weather, population)."""
    model = _bedrock_model()
    agent = StrandsAgent(
        model=model,
        tools=[strands_get_weather, strands_get_population],
        system_prompt="You are a city researcher. Use the tools to look up city data. Be concise.",
    )

    @strands_agent(agent_name="strands_bedrock_mcp")
    def invoke(a):
        return a("What is the weather in Paris and the population of New York?")

    return invoke(agent)


def strands_streaming_ttft():
    """Strands Agent + Bedrock stream + TTFT measurement.

    Sends 5 varied prompts to exercise tools and generate multiple
    rastir_ttft_seconds_bucket histogram data points. Uses local mock
    tools (not MCP) so it can run independently.

    Prompts are designed to trigger different tool combinations:
      1. Math (add + multiply)
      2. Weather lookup (2 cities)
      3. Temperature conversion
      4. Math + weather
      5. Math + temperature conversion
    """
    model = _bedrock_model()
    agent = StrandsAgent(
        model=model,
        tools=[
            strands_add_numbers, strands_multiply_numbers,
            strands_get_weather_local, strands_convert_temperature,
        ],
        system_prompt="You are a helpful assistant. Use the tools to solve problems. Be concise.",
    )

    prompts = [
        "Add 42 and 58, then multiply the result by 7.",
        "What's the weather in London and Tokyo?",
        "Convert 100 fahrenheit to celsius.",
        "Add 15 and 25, then tell me the weather in Paris.",
        "Multiply 12 by 8, then convert 30 celsius to fahrenheit.",
    ]

    for i, prompt in enumerate(prompts, 1):
        log(f"  TTFT [{i}/{len(prompts)}] {prompt[:60]}...")

        @strands_agent(agent_name="strands_ttft_agent")
        def invoke(ag, p):
            return ag(p)

        t0 = time.monotonic()
        try:
            result = invoke(agent, prompt)
            elapsed = time.monotonic() - t0
            text = str(getattr(result, "message", result))
            log(f"  TTFT [{i}/{len(prompts)}] ({elapsed:.1f}s) OK: {text[:100]}")
        except Exception as e:
            elapsed = time.monotonic() - t0
            log(f"  TTFT [{i}/{len(prompts)}] ({elapsed:.1f}s) ERROR: {e}")

        time.sleep(1)  # pause between invocations


# ===================================================================
#  PUBLIC RUNNER
# ===================================================================

async def run_all(results: TestResults, *, include_errors: bool = True):
    """Run all Strands e2e scenarios (local + MCP + TTFT)."""
    mcp_url = start_mcp_server()
    _set_mcp_url(mcp_url)
    log("Strands: MCP server ready")

    # Strands is synchronous — run in thread
    for label, fn in [
        ("Strands Bedrock + Local tools",   strands_bedrock_local),
        ("Strands Bedrock + MCP tools",     strands_bedrock_mcp),
    ]:
        clear_captured_spans()
        log(f"START: {label}")
        t0 = time.monotonic()
        try:
            answer = await asyncio.to_thread(fn)
            elapsed = time.monotonic() - t0
            text = str(getattr(answer, "message", answer))
            log(f"DONE:  {label} ({elapsed:.1f}s) ✓ {text[:120]}")
            results.passed(label)
        except Exception as e:
            elapsed = time.monotonic() - t0
            log(f"FAIL:  {label} ({elapsed:.1f}s) ✗ {e}")
            results.failed(label, e)
        await asyncio.sleep(1)


async def run_ttft(results: TestResults):
    """Run the TTFT streaming scenario (5 prompts)."""
    label = "Strands TTFT Streaming (5 prompts)"
    clear_captured_spans()
    log(f"START: {label}")
    t0 = time.monotonic()
    try:
        await asyncio.to_thread(strands_streaming_ttft)
        elapsed = time.monotonic() - t0
        log(f"DONE:  {label} ({elapsed:.1f}s) ✓")
        results.passed(label)
    except Exception as e:
        elapsed = time.monotonic() - t0
        log(f"FAIL:  {label} ({elapsed:.1f}s) ✗ {e}")
        results.failed(label, e)
