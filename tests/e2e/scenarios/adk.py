"""ADK (Google Agent Development Kit) e2e scenarios.

Covers ALL ADK variations tested across the old e2e files:

  ┌───────────────────────────────────────────────────────────────┐
  │  Scenario                   │ Provider │ Tools  │ Notes      │
  ├─────────────────────────────┼──────────┼────────┼────────────┤
  │ adk_gemini_local            │ Gemini   │ local  │ math ops   │
  │ adk_gemini_mcp              │ Gemini   │ MCP    │ city data  │
  └───────────────────────────────────────────────────────────────┘

  ADK agent pattern:
    1. Create Agent with model + tools + instruction
    2. Create Runner with InMemorySessionService
    3. Call runner.run_async() which yields events
    4. Extract the final text response from the last event

  ADK uses FunctionTool to wrap plain Python functions. For "MCP" tools,
  ADK wraps functions that make HTTP POST calls to the MCP test server
  with traceparent headers for distributed tracing.

  Sources consolidated:
    - test_adk_e2e.py            (local tools + span verification)
    - run_all_combinations.py    (ADK combos 11-12)
"""

from __future__ import annotations

import asyncio
import time

from google.adk.agents import Agent as AdkAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import FunctionTool as AdkFunctionTool
from google.genai import types

from tests.e2e.common import (
    TestResults,
    call_mcp_tool,
    clear_captured_spans,
    log,
    require_gemini_key,
    start_mcp_server,
)
from rastir import adk_agent


# ---------------------------------------------------------------------------
# Local tools — simple math functions
# ---------------------------------------------------------------------------
def adk_add_numbers(a: int, b: int) -> int:
    """Add two numbers together and return the result."""
    return a + b


def adk_multiply_numbers(a: int, b: int) -> int:
    """Multiply two numbers together and return the result."""
    return a * b


# ---------------------------------------------------------------------------
# MCP-backed tools — HTTP POST to MCP test server with traceparent
# ---------------------------------------------------------------------------
_MCP_URL: str = ""


def _set_mcp_url(url: str):
    global _MCP_URL
    _MCP_URL = url


def adk_get_weather(city: str) -> str:
    """Get the current weather for a city."""
    return call_mcp_tool(_MCP_URL, "get_weather", {"city": city})


def adk_get_population(city: str) -> str:
    """Get the approximate population of a city."""
    return call_mcp_tool(_MCP_URL, "get_population", {"city": city})


# ---------------------------------------------------------------------------
# Helper to extract text from ADK runner events
# ---------------------------------------------------------------------------
async def _run_adk_agent(runner: Runner, app_name: str, prompt: str) -> str:
    """Run an ADK agent and return the final text response."""
    session = await runner.session_service.create_session(
        app_name=app_name, user_id="user1",
    )
    events = []
    async for event in runner.run_async(
        user_id="user1",
        session_id=session.id,
        new_message=types.Content(
            role="user", parts=[types.Part(text=prompt)]
        ),
    ):
        events.append(event)

    # Walk events in reverse to find the last text response
    for ev in reversed(events):
        parts = getattr(getattr(ev, "content", None), "parts", [])
        for p in parts:
            if hasattr(p, "text") and p.text:
                return p.text
    return str(events[-1]) if events else "no result"


# ===================================================================
#  SCENARIO FUNCTIONS
# ===================================================================

async def adk_gemini_local():
    """ADK Agent + Gemini + local math tools."""
    require_gemini_key()
    agent = AdkAgent(
        name="adk_math",
        model="gemini-2.5-flash",
        tools=[AdkFunctionTool(adk_add_numbers), AdkFunctionTool(adk_multiply_numbers)],
        instruction="You are a math assistant. Use the tools to solve problems.",
    )
    runner = Runner(
        agent=agent, app_name="adk-local-e2e",
        session_service=InMemorySessionService(),
    )

    @adk_agent(agent_name="adk_gemini_local")
    async def invoke(r):
        return await _run_adk_agent(r, "adk-local-e2e",
                                    "Add 15 and 27, then multiply the result by 3.")

    return await invoke(runner)


async def adk_gemini_mcp():
    """ADK Agent + Gemini + MCP tools (weather, population)."""
    require_gemini_key()
    agent = AdkAgent(
        name="adk_city",
        model="gemini-2.5-flash",
        tools=[AdkFunctionTool(adk_get_weather), AdkFunctionTool(adk_get_population)],
        instruction="You are a city information assistant. Use the tools to look up city data.",
    )
    runner = Runner(
        agent=agent, app_name="adk-mcp-e2e",
        session_service=InMemorySessionService(),
    )

    @adk_agent(agent_name="adk_gemini_mcp")
    async def invoke(r):
        return await _run_adk_agent(r, "adk-mcp-e2e",
                                    "What is the weather in Tokyo and the population of London?")

    return await invoke(runner)


# ===================================================================
#  PUBLIC RUNNER
# ===================================================================

async def run_all(results: TestResults, *, include_errors: bool = True):
    """Run all ADK e2e scenarios (2 scenarios: local + MCP)."""
    mcp_url = start_mcp_server()
    _set_mcp_url(mcp_url)
    log("ADK: MCP server ready")

    for label, coro in [
        ("ADK Gemini + Local tools",  adk_gemini_local()),
        ("ADK Gemini + MCP tools",    adk_gemini_mcp()),
    ]:
        clear_captured_spans()
        log(f"START: {label}")
        t0 = time.monotonic()
        try:
            answer = await coro
            elapsed = time.monotonic() - t0
            log(f"DONE:  {label} ({elapsed:.1f}s) ✓ {str(answer)[:120]}")
            results.passed(label)
        except Exception as e:
            elapsed = time.monotonic() - t0
            log(f"FAIL:  {label} ({elapsed:.1f}s) ✗ {e}")
            results.failed(label, e)
        await asyncio.sleep(1)
