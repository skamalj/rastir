"""E2E test: Strands (AWS Strands Agents) + Bedrock + local tools.

Creates a Strands agent with Bedrock Claude and @strands.tool tools,
exercises the tools, and verifies Rastir spans.

Requirements:
    AWS credentials (for Bedrock), strands-agents package.

Run:
    conda run -n llmobserve PYTHONPATH=src \
        python tests/e2e/test_strands_e2e.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from strands import Agent
from strands.models.bedrock import BedrockModel
import strands

import rastir
from rastir import configure, strands_agent

configure(
    service="strands-e2e-test",
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
@strands.tool
def add_numbers(a: int, b: int) -> str:
    """Add two numbers together and return the result."""
    return str(a + b)


@strands.tool
def multiply_numbers(a: int, b: int) -> str:
    """Multiply two numbers together and return the result."""
    return str(a * b)


# ---------------------------------------------------------------------------
# Main test
# ---------------------------------------------------------------------------
def run_test():
    print("=" * 60)
    print("Strands + Bedrock Claude + Local Tools E2E Test")
    print("=" * 60)

    model = BedrockModel(model_id="apac.anthropic.claude-sonnet-4-20250514-v1:0")
    agent = Agent(
        model=model,
        tools=[add_numbers, multiply_numbers],
        system_prompt="You are a math assistant. Use the tools to solve problems. Be concise.",
    )

    @strands_agent(agent_name="strands_e2e_agent")
    def invoke(agent):
        return agent("Add 12 and 18, then multiply the result by 3.")

    captured_spans.clear()
    result = invoke(agent)

    text = str(getattr(result, "message", result))
    print(f"\nResult: {text[:300]}")
    print(f"\nCaptured {len(captured_spans)} spans:")
    for s in captured_spans:
        dur = (s.end_time - s.start_time) * 1000 if s.end_time else 0
        print(f"  - {s.name} ({s.span_type.value}) dur={dur:.1f}ms")

    agent_spans = [s for s in captured_spans if s.span_type.value == "agent"]
    tool_spans = [s for s in captured_spans if s.span_type.value == "tool"]
    llm_spans = [s for s in captured_spans if s.span_type.value == "llm"]

    print(f"\nAgent spans: {len(agent_spans)}")
    print(f"LLM spans: {len(llm_spans)}")
    print(f"Tool spans: {len(tool_spans)}")

    if agent_spans:
        print("✓ Agent span found")
    else:
        print("✗ No agent span")

    if llm_spans:
        print("✓ LLM spans found")
    else:
        print("✗ No LLM spans")

    import time
    print("\nWaiting 3s for flush...")
    time.sleep(3)
    print("Done!")


if __name__ == "__main__":
    run_test()
