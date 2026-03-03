#!/usr/bin/env python3
"""Load-test script for Rastir observability dashboards.

Generates substantial, varied traffic against a running Rastir server
so that Prometheus scrapes and Grafana dashboards show real trends.

Exercises all span types: trace, agent, llm, tool, retrieval.
Varies models, agent names, tool names to produce multi-dimensional metrics.
Includes intentional errors and email/phone in prompts (for redaction).

Usage:
    RASTIR_PUSH_URL=http://localhost:8080 python scripts/load_test.py
"""

from __future__ import annotations

import asyncio
import os
import random
import sys
import time

import openai

# ── Ensure rastir is importable ───────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import rastir
from rastir import agent, configure, llm, retrieval, stop_exporter, trace

# ── Configuration ─────────────────────────────────────────────────
PUSH_URL = os.environ.get("RASTIR_PUSH_URL", "http://localhost:8080")
# Number of full rounds.  Each round sends ~15-20 spans.
ROUNDS = int(os.environ.get("LOAD_ROUNDS", "12"))
# Pause between rounds (seconds) — lets Prometheus scrape in between
ROUND_PAUSE = float(os.environ.get("ROUND_PAUSE", "6"))


# ── Initialise Rastir client ─────────────────────────────────────
configure(
    service="loadtest-svc",
    env="staging",
    version="0.5.0-load",
    push_url=PUSH_URL,
    batch_size=20,
    flush_interval=2,
    evaluation_enabled=True,
    capture_prompt=True,
    capture_completion=True,
)

# ── OpenAI client (for real LLM calls) ───────────────────────────
_oai = openai.OpenAI()

MODELS = ["gpt-4o-mini", "gpt-4o-mini"]  # cheap model, repeat to vary labels
AGENTS = ["planner-agent", "researcher-agent", "writer-agent"]
TOOLS = ["web_search", "calculator", "db_lookup", "file_reader", "api_call"]

# Prompts intentionally include PII patterns (email, phone) to trigger redaction
PROMPTS = [
    "Summarise the quarterly report for john.doe@example.com in 3 bullet points.",
    "What's the weather forecast for next week? Contact 555-123-4567 for details.",
    "Translate the following to French: Hello, how are you?",
    "Write a haiku about machine learning.",
    "Explain the difference between TCP and UDP in one sentence.",
    "My SSN is 123-45-6789, can you verify my account?",
    "List 5 benefits of renewable energy.",
    "Describe quantum computing in simple terms.",
    "What is the capital of France?",
    "Generate a short poem about stars.",
]


# ── Decorated functions ───────────────────────────────────────────


@trace(name="load_test_workflow")
def run_workflow(round_id: int) -> None:
    """Top-level trace span for each round."""
    agent_name = AGENTS[round_id % len(AGENTS)]
    run_agent_task(agent_name, round_id)


@agent(agent_name="dynamic_agent")
def run_agent_task(ag_name: str, round_id: int) -> None:
    """Agent span that orchestrates LLM + tool + retrieval calls."""
    # 1. An LLM call with evaluation enabled
    prompt = PROMPTS[round_id % len(PROMPTS)]
    model = MODELS[round_id % len(MODELS)]
    call_llm_chat(prompt, model)

    # 2. A second LLM call (different prompt) — with evaluation
    prompt2 = PROMPTS[(round_id + 3) % len(PROMPTS)]
    call_llm_chat_eval(prompt2, model)

    # 3. Tool calls
    for i in range(2):
        tool_name = TOOLS[(round_id + i) % len(TOOLS)]
        run_tool(tool_name, f"input_{round_id}_{i}")

    # 4. Retrieval call
    do_retrieval(f"query about {ag_name}")

    # 5. Occasional error span
    if round_id % 4 == 0:
        try:
            failing_llm_call()
        except Exception:
            pass


@llm(model="gpt-4o-mini", provider="openai")
def call_llm_chat(prompt: str, model: str = "gpt-4o-mini"):
    """Real OpenAI LLM call — returns raw response for adapter extraction."""
    resp = _oai.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=60,
    )
    return resp


@llm(
    model="gpt-4o-mini",
    provider="openai",
    evaluate=True,
    evaluation_types=["toxicity"],
)
def call_llm_chat_eval(prompt: str, model: str = "gpt-4o-mini"):
    """Real OpenAI LLM call with evaluation enabled — returns raw response."""
    resp = _oai.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=60,
    )
    return resp


@llm(model="gpt-4o-mini", provider="openai")
def failing_llm_call() -> str:
    """Intentionally fails to generate error metrics."""
    raise RuntimeError("Simulated LLM provider timeout")


@trace(name="run_tool")
def run_tool(tool_name: str, input_data: str) -> str:
    """Simulated tool execution."""
    time.sleep(random.uniform(0.01, 0.08))
    return f"result from {tool_name} on {input_data}"


@retrieval(name="vector_search")
def do_retrieval(query: str) -> list[str]:
    """Simulated retrieval span."""
    time.sleep(random.uniform(0.02, 0.1))
    return [f"doc_{i}" for i in range(random.randint(2, 8))]


# ── Async variant (exercises async wrappers too) ──────────────────


@trace(name="async_workflow")
async def run_async_workflow(round_id: int) -> None:
    """Top-level async trace span."""
    await async_agent_work(round_id)


@agent(agent_name="async_agent")
async def async_agent_work(round_id: int) -> None:
    prompt = PROMPTS[(round_id + 5) % len(PROMPTS)]
    await async_llm_call(prompt)
    await async_tool_work(f"task_{round_id}")


@llm(
    model="gpt-4o-mini",
    provider="openai",
    evaluate=True,
    evaluation_types=["toxicity", "hallucination"],
)
async def async_llm_call(prompt: str):
    """Async real OpenAI LLM call — returns raw response."""
    resp = await asyncio.to_thread(
        _oai.chat.completions.create,
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=50,
    )
    return resp


@trace(name="async_processor")
async def async_tool_work(task_id: str) -> str:
    await asyncio.sleep(random.uniform(0.02, 0.06))
    return f"processed {task_id}"


# ── Main loop ─────────────────────────────────────────────────────


def main() -> None:
    print(f"╔══════════════════════════════════════════════════╗")
    print(f"║  Rastir Load Test — {ROUNDS} rounds, {ROUND_PAUSE}s pause      ║")
    print(f"║  push_url = {PUSH_URL:<36s} ║")
    print(f"╚══════════════════════════════════════════════════╝")

    for r in range(ROUNDS):
        t0 = time.time()
        print(f"\n── Round {r + 1}/{ROUNDS} ──")

        # Sync workflow
        try:
            run_workflow(r)
        except Exception as e:
            print(f"  [sync] error (expected occasionally): {e}")

        # Async workflow
        try:
            asyncio.run(run_async_workflow(r))
        except Exception as e:
            print(f"  [async] error (expected occasionally): {e}")

        elapsed = time.time() - t0
        print(f"  done in {elapsed:.2f}s — sleeping {ROUND_PAUSE}s")

        if r < ROUNDS - 1:
            time.sleep(ROUND_PAUSE)

    # Flush remaining spans
    print("\nFlushing remaining spans...")
    time.sleep(5)
    stop_exporter()
    print("Load test complete ✓")


if __name__ == "__main__":
    main()
