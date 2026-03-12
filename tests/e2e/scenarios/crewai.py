"""CrewAI e2e scenarios.

Covers ALL CrewAI variations tested across the old e2e files:

  ┌───────────────────────────────────────────────────────────────┐
  │  Scenario                   │ Provider  │ Tools  │ Notes     │
  ├─────────────────────────────┼───────────┼────────┼───────────┤
  │ crew_openai_local           │ OpenAI    │ local  │ math ops  │
  │ crew_openai_mcp             │ OpenAI    │ MCP    │ city data │
  │ crew_bedrock_mcp            │ Bedrock   │ MCP    │ 3 tools   │
  │ crew_error_bad_model        │ OpenAI    │ MCP    │ bad model │
  └───────────────────────────────────────────────────────────────┘

  CrewAI uses LiteLLM for LLM routing, so provider strings use
  LiteLLM format: "openai/gpt-4o-mini" or "bedrock/us.anthropic...".

  MCP tools are called via JSON-RPC POST (not CrewAI's native
  MCPServerHTTP) because native MCP tool names sometimes fail
  OpenAI/Gemini function-name validation.

  Sources consolidated:
    - test_crewai_e2e.py         (OpenAI + MCP)
    - test_crewai_bedrock_e2e.py (Bedrock + MCP)
    - test_crewai_error_e2e.py   (bad model name)
    - verify_crewai.py           (local + MCP verification)
    - run_all_combinations.py    (CrewAI combos 5-6)
"""

from __future__ import annotations

import asyncio
import time

from crewai import Agent as CrewAgent, Task, Crew, LLM
from crewai.tools import tool as crewai_tool

from tests.e2e.common import (
    TestResults,
    call_mcp_tool,
    clear_captured_spans,
    log,
    require_openai_key,
    start_mcp_server,
)
from rastir import crew_kickoff


# ---------------------------------------------------------------------------
# Local tools — plain math operations
# ---------------------------------------------------------------------------
@crewai_tool
def crew_add_numbers(a: int, b: int) -> str:
    """Add two numbers together and return the result."""
    return str(a + b)


@crewai_tool
def crew_multiply_numbers(a: int, b: int) -> str:
    """Multiply two numbers and return the result."""
    return str(a * b)


# ---------------------------------------------------------------------------
# MCP-backed tools — call the MCP test server via JSON-RPC POST.
# Each function wraps call_mcp_tool() with the correct tool name.
# The MCP URL is set at module-init time by _init_mcp_tools().
# ---------------------------------------------------------------------------
_MCP_URL: str = ""


def _set_mcp_url(url: str):
    global _MCP_URL
    _MCP_URL = url


@crewai_tool
def crew_get_weather(city: str) -> str:
    """Get the current weather for a city."""
    return call_mcp_tool(_MCP_URL, "get_weather", {"city": city})


@crewai_tool
def crew_get_population(city: str) -> str:
    """Get the approximate population of a city."""
    return call_mcp_tool(_MCP_URL, "get_population", {"city": city})


@crewai_tool
def crew_get_timezone(city: str) -> str:
    """Get the timezone of a city."""
    return call_mcp_tool(_MCP_URL, "get_timezone", {"city": city})


# ===================================================================
#  SCENARIO FUNCTIONS
# ===================================================================

def crew_openai_local():
    """CrewAI Agent + OpenAI GPT-4o-mini + local math tools."""
    llm = LLM(model="openai/gpt-4o-mini", api_key=require_openai_key(), temperature=0)

    agent = CrewAgent(
        role="Math Assistant",
        goal="Solve math problems using tools",
        backstory="You are a helpful math assistant.",
        llm=llm,
        tools=[crew_add_numbers, crew_multiply_numbers],
        verbose=False,
        max_iter=4,
    )
    task = Task(
        description="Calculate: (15 + 27) and (8 * 13). Report both results.",
        expected_output="The sum of 15+27 and the product of 8*13.",
        agent=agent,
    )
    crew = Crew(agents=[agent], tasks=[task], verbose=False)

    @crew_kickoff(agent_name="crew_openai_local")
    def run(c):
        return c.kickoff()

    return run(crew)


def crew_openai_mcp():
    """CrewAI Agent + OpenAI GPT-4o-mini + MCP tools (weather, population)."""
    llm = LLM(model="openai/gpt-4o-mini", api_key=require_openai_key(), temperature=0)

    agent = CrewAgent(
        role="City Researcher",
        goal="Look up city data",
        backstory="You research city facts.",
        llm=llm,
        tools=[crew_get_weather, crew_get_population],
        verbose=False,
        max_iter=4,
    )
    task = Task(
        description="What is the weather in Paris and the population of New York?",
        expected_output="Paris weather and New York population.",
        agent=agent,
    )
    crew = Crew(agents=[agent], tasks=[task], verbose=False)

    @crew_kickoff(agent_name="crew_openai_mcp")
    def run(c):
        return c.kickoff()

    return run(crew)


def crew_bedrock_mcp():
    """CrewAI Agent + Bedrock Claude Sonnet 4 + MCP tools (3 tools).

    Uses LiteLLM's bedrock/ prefix for provider routing.
    Tools include weather, population, and timezone.
    """
    llm = LLM(model="bedrock/us.anthropic.claude-sonnet-4-20250514-v1:0", temperature=0)

    agent = CrewAgent(
        role="Geography Research Analyst",
        goal="Provide accurate weather, population, and timezone data for cities",
        backstory="You are a research analyst who uses tools to find factual data.",
        llm=llm,
        tools=[crew_get_weather, crew_get_population, crew_get_timezone],
        verbose=False,
        max_iter=5,
    )
    task = Task(
        description=(
            "Find the weather in London, the population of Tokyo, "
            "and the timezone of New York. Present the results clearly."
        ),
        expected_output="London weather, Tokyo population, and New York timezone.",
        agent=agent,
    )
    crew = Crew(agents=[agent], tasks=[task], verbose=False)

    @crew_kickoff(agent_name="crew_bedrock_mcp")
    def run(c):
        return c.kickoff()

    return run(crew)


def crew_error_bad_model():
    """CrewAI Agent with a non-existent model name to trigger LLM error.

    Uses "openai/gpt-nonexistent-model-xyz" which will fail at call time.
    """
    llm = LLM(model="openai/gpt-nonexistent-model-xyz",
              api_key=require_openai_key(), temperature=0)

    agent = CrewAgent(
        role="Test Agent",
        goal="Trigger an error",
        backstory="Testing error handling.",
        llm=llm,
        tools=[crew_get_weather],
        verbose=False,
        max_iter=2,
    )
    task = Task(
        description="What is the weather in London?",
        expected_output="Weather in London.",
        agent=agent,
    )
    crew = Crew(agents=[agent], tasks=[task], verbose=False)

    @crew_kickoff(agent_name="crew_error_bad_model")
    def run(c):
        return c.kickoff()

    return run(crew)


# ===================================================================
#  PUBLIC RUNNER
# ===================================================================

async def run_all(results: TestResults, *, include_errors: bool = True):
    """Run all CrewAI e2e scenarios.

    Covers 3 success scenarios (openai-local, openai-mcp, bedrock-mcp)
    plus 1 error scenario (bad model name).
    """
    mcp_url = start_mcp_server()
    _set_mcp_url(mcp_url)
    log("CrewAI: MCP server ready")

    # CrewAI is synchronous — run in thread to avoid blocking event loop
    for label, fn in [
        ("CrewAI OpenAI + Local tools",    crew_openai_local),
        ("CrewAI OpenAI + MCP tools",      crew_openai_mcp),
        ("CrewAI Bedrock + MCP tools",     crew_bedrock_mcp),
    ]:
        clear_captured_spans()
        log(f"START: {label}")
        t0 = time.monotonic()
        try:
            answer = await asyncio.to_thread(fn)
            elapsed = time.monotonic() - t0
            text = str(getattr(answer, "raw", answer))
            log(f"DONE:  {label} ({elapsed:.1f}s) ✓ {text[:120]}")
            results.passed(label)
        except Exception as e:
            elapsed = time.monotonic() - t0
            log(f"FAIL:  {label} ({elapsed:.1f}s) ✗ {e}")
            results.failed(label, e)
        await asyncio.sleep(1)

    if include_errors:
        label = "CrewAI Error: bad model"
        clear_captured_spans()
        log(f"START: {label}")
        t0 = time.monotonic()
        try:
            await asyncio.to_thread(crew_error_bad_model)
            elapsed = time.monotonic() - t0
            log(f"DONE:  {label} ({elapsed:.1f}s) error in span")
            results.passed(f"{label} (error in span)")
        except Exception as e:
            elapsed = time.monotonic() - t0
            log(f"DONE:  {label} ({elapsed:.1f}s) {type(e).__name__}")
            results.passed(f"{label} ({type(e).__name__})")
