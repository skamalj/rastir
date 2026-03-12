"""E2E test: Strands streaming with TTFT + volume for rate() panels.

Exercises Strands agent with Bedrock Claude streaming to produce:
  - rastir_ttft_seconds_bucket histogram (TTFT from streaming LLM calls)
  - Multiple LLM/tool/agent spans for rate() panel data
  - Varied prompts to give Prometheus multiple data points

Run:
    conda run -n llmobserve PYTHONPATH=src \
        python tests/e2e/test_strands_streaming_ttft.py
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from strands import Agent
from strands.models.bedrock import BedrockModel
import strands

import rastir
from rastir import configure, strands_agent

configure(
    service="strands-ttft-e2e",
    push_url="http://localhost:8080",
    enable_cost_calculation=True,
    enable_ttft=True,
)


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


@strands.tool
def get_weather(city: str) -> str:
    """Get the weather for a city."""
    weathers = {
        "london": "15°C, cloudy",
        "paris": "18°C, sunny",
        "tokyo": "22°C, humid",
        "new york": "20°C, partly cloudy",
    }
    return weathers.get(city.lower(), f"25°C, clear in {city}")


@strands.tool
def convert_temperature(value: float, from_unit: str, to_unit: str) -> str:
    """Convert temperature between celsius and fahrenheit."""
    if from_unit.lower().startswith("c") and to_unit.lower().startswith("f"):
        result = value * 9 / 5 + 32
    elif from_unit.lower().startswith("f") and to_unit.lower().startswith("c"):
        result = (value - 32) * 5 / 9
    else:
        result = value
    return f"{result:.1f} {to_unit}"


# ---------------------------------------------------------------------------
# Prompts — varied queries to exercise tools + generate multiple spans
# ---------------------------------------------------------------------------
PROMPTS = [
    "Add 42 and 58, then multiply the result by 7.",
    "What's the weather in London and Tokyo?",
    "Convert 100 fahrenheit to celsius.",
    "Add 15 and 25, then tell me the weather in Paris.",
    "Multiply 12 by 8, then convert 30 celsius to fahrenheit.",
]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run():
    print("=" * 60)
    print("Strands Streaming TTFT + Volume E2E Test")
    print("=" * 60)

    model = BedrockModel(model_id="apac.anthropic.claude-sonnet-4-20250514-v1:0")
    agent = Agent(
        model=model,
        tools=[add_numbers, multiply_numbers, get_weather, convert_temperature],
        system_prompt="You are a helpful assistant. Use the tools to solve problems. Be concise.",
    )

    for i, prompt in enumerate(PROMPTS, 1):
        print(f"\n[{i}/{len(PROMPTS)}] {prompt[:60]}...")

        @strands_agent(agent_name="strands_ttft_agent")
        def invoke(ag, p):
            return ag(p)

        try:
            result = invoke(agent, prompt)
            text = str(getattr(result, "message", result))
            print(f"  OK: {text[:200]}")
        except Exception as e:
            print(f"  ERROR: {e}")

        # Brief pause between invocations
        time.sleep(1)

    print("\n" + "=" * 60)
    print(f"Sent {len(PROMPTS)} Strands requests (streaming via Bedrock).")
    print("TTFT + span data flows to Rastir server → Prometheus.")
    print("Verify via: python scripts/verify_dashboards.py")
    print("Waiting 5s for span flush...")
    time.sleep(5)
    print("Done!")


if __name__ == "__main__":
    run()
