#!/usr/bin/env python3
"""Multi-provider load test for Rastir observability dashboards.

Generates traffic across OpenAI, Anthropic, and AWS Bedrock with and
without guardrails so that all Grafana dashboards (system-health,
llm-performance, agent-tool, evaluation, guardrail) show real data.

Usage:
    RASTIR_PUSH_URL=http://localhost:8080 python scripts/load_test_all.py
"""

from __future__ import annotations

import asyncio
import os
import random
import sys
import time

# ── Ensure rastir is importable ───────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import rastir
from rastir import agent, configure, llm, retrieval, stop_exporter, trace

# ── Configuration ─────────────────────────────────────────────────
PUSH_URL = os.environ.get("RASTIR_PUSH_URL", "http://localhost:8080")
ROUNDS = int(os.environ.get("LOAD_ROUNDS", "8"))
ROUND_PAUSE = float(os.environ.get("ROUND_PAUSE", "8"))

# Bedrock guardrail
GUARDRAIL_ID = os.environ.get("BEDROCK_GUARDRAIL_ID", "i3rttxfu7kow")
GUARDRAIL_VERSION = "DRAFT"

# ── Initialise Rastir client ──────────────────────────────────────
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

# ── Provider clients ─────────────────────────────────────────────
import openai
import anthropic
import boto3

_oai = openai.OpenAI()
_ant = anthropic.Anthropic()
_br = boto3.client("bedrock-runtime", region_name="us-east-1")

AGENTS = ["planner-agent", "researcher-agent", "coder-agent"]
TOOLS = ["web_search", "calculator", "db_lookup", "file_reader", "api_call"]

# Prompts — some include PII to trigger server-side redaction
PROMPTS = [
    "Summarise the quarterly report for john.doe@example.com in 3 bullet points.",
    "What's the weather forecast? Contact 555-123-4567 for details.",
    "Translate 'Hello, how are you?' to French.",
    "Write a haiku about machine learning.",
    "Explain TCP vs UDP in one sentence.",
    "My SSN is 123-45-6789, can you verify?",
    "List 5 benefits of renewable energy.",
    "Describe quantum computing simply.",
    "What is the capital of France?",
    "Write a short poem about stars.",
]


# =====================================================================
# OpenAI calls
# =====================================================================

@llm(model="gpt-4o-mini", provider="openai")
def openai_chat(prompt: str):
    """OpenAI chat — no evaluation."""
    return _oai.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=60,
    )


@llm(model="gpt-4o-mini", provider="openai",
     evaluate=True, evaluation_types=["toxicity"])
def openai_chat_eval(prompt: str):
    """OpenAI chat — with toxicity evaluation."""
    return _oai.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=60,
    )


# =====================================================================
# Anthropic calls
# =====================================================================

@llm(model="claude-3-haiku-20240307", provider="anthropic")
def anthropic_chat(prompt: str):
    """Anthropic Claude chat — no evaluation."""
    return _ant.messages.create(
        model="claude-3-haiku-20240307",
        max_tokens=60,
        messages=[{"role": "user", "content": prompt}],
    )


@llm(model="claude-3-haiku-20240307", provider="anthropic",
     evaluate=True, evaluation_types=["hallucination"])
def anthropic_chat_eval(prompt: str):
    """Anthropic Claude chat — with hallucination evaluation."""
    return _ant.messages.create(
        model="claude-3-haiku-20240307",
        max_tokens=60,
        messages=[{"role": "user", "content": prompt}],
    )


# =====================================================================
# Bedrock calls — WITHOUT guardrails
# =====================================================================

@llm(provider="bedrock")
def bedrock_chat(prompt: str, modelId: str = "anthropic.claude-3-haiku-20240307-v1:0"):
    """Bedrock converse — no guardrails."""
    return _br.converse(
        modelId=modelId,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 60},
    )


@llm(provider="bedrock", evaluate=True, evaluation_types=["toxicity"])
def bedrock_chat_eval(prompt: str, modelId: str = "anthropic.claude-3-haiku-20240307-v1:0"):
    """Bedrock converse with evaluation — no guardrails."""
    return _br.converse(
        modelId=modelId,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 60},
    )


# =====================================================================
# Bedrock calls — WITH guardrails
# =====================================================================

@llm(provider="bedrock")
def bedrock_guarded(prompt: str,
                    modelId: str = "anthropic.claude-3-haiku-20240307-v1:0",
                    guardrailIdentifier: str = GUARDRAIL_ID,
                    guardrailVersion: str = GUARDRAIL_VERSION):
    """Bedrock converse WITH guardrail — no evaluation."""
    return _br.converse(
        modelId=modelId,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 60},
        guardrailConfig={
            "guardrailIdentifier": guardrailIdentifier,
            "guardrailVersion": guardrailVersion,
        },
    )


@llm(provider="bedrock", evaluate=True, evaluation_types=["toxicity", "hallucination"])
def bedrock_guarded_eval(prompt: str,
                         modelId: str = "anthropic.claude-3-haiku-20240307-v1:0",
                         guardrailIdentifier: str = GUARDRAIL_ID,
                         guardrailVersion: str = GUARDRAIL_VERSION):
    """Bedrock converse WITH guardrail AND evaluation."""
    return _br.converse(
        modelId=modelId,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 60},
        guardrailConfig={
            "guardrailIdentifier": guardrailIdentifier,
            "guardrailVersion": guardrailVersion,
        },
    )


# =====================================================================
# Error generator
# =====================================================================

@llm(model="gpt-4o-mini", provider="openai")
def failing_llm_call():
    raise RuntimeError("Simulated provider timeout")


# =====================================================================
# Tools & Retrieval
# =====================================================================

@trace(name="run_tool")
def run_tool(tool_name: str, input_data: str) -> str:
    time.sleep(random.uniform(0.01, 0.05))
    return f"result from {tool_name}: {input_data}"


@retrieval(name="vector_search")
def do_retrieval(query: str) -> list[str]:
    time.sleep(random.uniform(0.02, 0.08))
    return [f"doc_{i}" for i in range(random.randint(2, 8))]


# =====================================================================
# Workflows
# =====================================================================

@trace(name="openai_workflow")
def workflow_openai(round_id: int) -> None:
    """OpenAI-focused workflow."""
    _run_openai_agent(round_id)


@agent(agent_name="openai_agent")
def _run_openai_agent(round_id: int) -> None:
    p1 = PROMPTS[round_id % len(PROMPTS)]
    p2 = PROMPTS[(round_id + 3) % len(PROMPTS)]
    openai_chat(p1)
    openai_chat_eval(p2)
    run_tool(TOOLS[round_id % len(TOOLS)], f"oai_{round_id}")
    do_retrieval(f"openai query {round_id}")


@trace(name="anthropic_workflow")
def workflow_anthropic(round_id: int) -> None:
    """Anthropic-focused workflow."""
    _run_anthropic_agent(round_id)


@agent(agent_name="anthropic_agent")
def _run_anthropic_agent(round_id: int) -> None:
    p1 = PROMPTS[(round_id + 1) % len(PROMPTS)]
    p2 = PROMPTS[(round_id + 4) % len(PROMPTS)]
    anthropic_chat(p1)
    anthropic_chat_eval(p2)
    run_tool(TOOLS[(round_id + 2) % len(TOOLS)], f"ant_{round_id}")
    do_retrieval(f"anthropic query {round_id}")


@trace(name="bedrock_workflow")
def workflow_bedrock(round_id: int) -> None:
    """Bedrock workflow — with and without guardrails."""
    _run_bedrock_agent(round_id)


@agent(agent_name="bedrock_agent")
def _run_bedrock_agent(round_id: int) -> None:
    p1 = PROMPTS[(round_id + 2) % len(PROMPTS)]
    p2 = PROMPTS[(round_id + 5) % len(PROMPTS)]

    # Without guardrails
    bedrock_chat(p1)
    bedrock_chat_eval(p2)

    # With guardrails
    p3 = PROMPTS[(round_id + 6) % len(PROMPTS)]
    p4 = PROMPTS[(round_id + 7) % len(PROMPTS)]
    bedrock_guarded(p3)
    bedrock_guarded_eval(p4)

    run_tool(TOOLS[(round_id + 1) % len(TOOLS)], f"br_{round_id}")
    do_retrieval(f"bedrock query {round_id}")


@trace(name="mixed_workflow")
def workflow_mixed(round_id: int) -> None:
    """Mixed provider workflow — exercises all three."""
    _run_mixed_agent(round_id)


@agent(agent_name="multi_provider_agent")
def _run_mixed_agent(round_id: int) -> None:
    p = PROMPTS[round_id % len(PROMPTS)]
    # One call from each provider
    openai_chat(p)
    anthropic_chat(p)
    bedrock_guarded(p)  # guarded bedrock call in mixed workflow
    run_tool(TOOLS[(round_id + 3) % len(TOOLS)], f"mix_{round_id}")
    # Occasional error
    if round_id % 3 == 0:
        try:
            failing_llm_call()
        except Exception:
            pass


# =====================================================================
# Main
# =====================================================================

def main() -> None:
    print(f"╔══════════════════════════════════════════════════════════╗")
    print(f"║  Multi-Provider Load Test                               ║")
    print(f"║  {ROUNDS} rounds × 4 workflows, {ROUND_PAUSE}s pause             ║")
    print(f"║  OpenAI + Anthropic + Bedrock (±guardrails)             ║")
    print(f"║  push_url = {PUSH_URL:<42s} ║")
    print(f"╚══════════════════════════════════════════════════════════╝")

    for r in range(ROUNDS):
        t0 = time.time()
        print(f"\n── Round {r + 1}/{ROUNDS} ──")

        # 1. OpenAI workflow
        try:
            workflow_openai(r)
            print("  ✓ OpenAI")
        except Exception as e:
            print(f"  ✗ OpenAI: {e}")

        # 2. Anthropic workflow
        try:
            workflow_anthropic(r)
            print("  ✓ Anthropic")
        except Exception as e:
            print(f"  ✗ Anthropic: {e}")

        # 3. Bedrock workflow (with + without guardrails)
        try:
            workflow_bedrock(r)
            print("  ✓ Bedrock")
        except Exception as e:
            print(f"  ✗ Bedrock: {e}")

        # 4. Mixed provider workflow
        try:
            workflow_mixed(r)
            print("  ✓ Mixed")
        except Exception as e:
            print(f"  ✗ Mixed: {e}")

        elapsed = time.time() - t0
        print(f"  Total: {elapsed:.1f}s — sleeping {ROUND_PAUSE}s")

        if r < ROUNDS - 1:
            time.sleep(ROUND_PAUSE)

    # Flush
    print("\nFlushing remaining spans...")
    time.sleep(5)
    stop_exporter()
    print("Load test complete ✓")


if __name__ == "__main__":
    main()
