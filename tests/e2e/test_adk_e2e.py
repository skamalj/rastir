"""E2E test: ADK (Google Agent Development Kit) + Gemini + local tools.

Creates an ADK agent with Gemini and local FunctionTools,
exercises the tools, and verifies Rastir spans.

Requirements:
    GEMINI_API_KEY env var, google-adk package.

Run:
    GEMINI_API_KEY=... conda run -n llmobserve PYTHONPATH=src \
        python tests/e2e/test_adk_e2e.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

GEMINI_KEY = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")
if not GEMINI_KEY:
    print("ERROR: GEMINI_API_KEY not set"); sys.exit(1)

from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import FunctionTool
from google.genai import types

import rastir
from rastir import configure, adk_agent

configure(
    service="adk-e2e-test",
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
_orig_enqueue = _queue.enqueue_span
_queue.enqueue_span = _capture_enqueue


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
def add_numbers(a: int, b: int) -> int:
    """Add two numbers together and return the result."""
    return a + b


def multiply_numbers(a: int, b: int) -> int:
    """Multiply two numbers together and return the result."""
    return a * b


# ---------------------------------------------------------------------------
# Main test
# ---------------------------------------------------------------------------
async def run_test():
    print("=" * 60)
    print("ADK + Gemini + Local Tools E2E Test")
    print("=" * 60)

    agent = Agent(
        name="math_agent",
        model="gemini-2.5-flash",
        tools=[FunctionTool(add_numbers), FunctionTool(multiply_numbers)],
        instruction="You are a math assistant. Use the tools to solve problems.",
    )
    runner = Runner(
        agent=agent, app_name="adk-e2e",
        session_service=InMemorySessionService(),
    )

    @adk_agent(agent_name="adk_e2e_agent")
    async def invoke(runner):
        session = await runner.session_service.create_session(
            app_name="adk-e2e", user_id="user1",
        )
        events = []
        async for event in runner.run_async(
            user_id="user1", session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text="Add 15 and 27, then multiply the result by 3.")]),
        ):
            events.append(event)
        for ev in reversed(events):
            parts = getattr(getattr(ev, "content", None), "parts", [])
            for p in parts:
                if hasattr(p, "text") and p.text:
                    return p.text
        return str(events[-1]) if events else "no result"

    captured_spans.clear()
    result = await invoke(runner)

    print(f"\nResult: {result}")
    print(f"\nCaptured {len(captured_spans)} spans:")
    if captured_spans:
        t0 = min(s.start_time for s in captured_spans)
    else:
        t0 = 0
    for s in captured_spans:
        dur = (s.end_time - s.start_time) * 1000 if s.end_time else 0
        print(f"  - {s.name} ({s.span_type.value}) dur={dur:.1f}ms")

    agent_spans = [s for s in captured_spans if s.span_type.value == "agent"]
    tool_spans = [s for s in captured_spans if s.span_type.value == "tool"]

    print(f"\nAgent spans: {len(agent_spans)}")
    print(f"Tool spans: {len(tool_spans)}")

    if agent_spans:
        print("✓ Agent span found")
    else:
        print("✗ No agent span")

    print("\nWaiting 3s for flush...")
    await asyncio.sleep(3)
    print("Done!")


if __name__ == "__main__":
    asyncio.run(run_test())
