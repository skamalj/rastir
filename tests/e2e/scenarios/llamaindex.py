"""LlamaIndex e2e scenarios.

Covers ALL LlamaIndex variations tested across the old e2e files:

  ┌────────────────────────────────────────────────────────────────────┐
  │  Scenario                      │ Agent Type │ Provider │ Tools   │
  ├────────────────────────────────┼────────────┼──────────┼─────────┤
  │ li_react_openai_local          │ ReAct      │ OpenAI   │ local   │
  │ li_react_openai_mcp            │ ReAct      │ OpenAI   │ MCP     │
  │ li_func_openai_local           │ Function   │ OpenAI   │ local   │
  │ li_func_openai_mcp             │ Function   │ OpenAI   │ MCP     │
  │ li_react_bedrock_mcp           │ ReAct      │ Bedrock  │ MCP     │
  │ li_error_wrap_bedrock          │ wrap()     │ Bedrock  │ —       │
  └────────────────────────────────────────────────────────────────────┘

  LlamaIndex agent types:
    - ReActAgent: uses a loop of Thought → Action → Observation steps
    - FunctionAgent: directly calls tools via function-calling API

  LlamaIndex MCP integration uses BasicMCPClient + McpToolSpec to
  convert MCP tools into LlamaIndex FunctionTool instances.

  Sources consolidated:
    - test_llamaindex_e2e.py         (ReAct + OpenAI + MCP)
    - test_llamaindex_bedrock_e2e.py (ReAct + Bedrock Nova + MCP)
    - verify_llamaindex.py           (4-combination verification)
    - run_all_combinations.py        (LlamaIndex combos 7-10)
"""

from __future__ import annotations

import asyncio
import time

from llama_index.llms.openai import OpenAI as LlamaOpenAI
from llama_index.core.agent import ReActAgent, FunctionAgent
from llama_index.core.tools import FunctionTool
from llama_index.tools.mcp import BasicMCPClient, McpToolSpec

from tests.e2e.common import (
    TestResults,
    clear_captured_spans,
    log,
    require_openai_key,
    start_mcp_server,
)
from rastir import llamaindex_agent


# ---------------------------------------------------------------------------
# Local tools — plain functions wrapped as LlamaIndex FunctionTool
# ---------------------------------------------------------------------------
def _add_numbers(a: int, b: int) -> int:
    """Add two numbers together."""
    return a + b


def _multiply_numbers(a: int, b: int) -> int:
    """Multiply two numbers together."""
    return a * b


def _local_tools() -> list:
    return [
        FunctionTool.from_defaults(fn=_add_numbers),
        FunctionTool.from_defaults(fn=_multiply_numbers),
    ]


# ---------------------------------------------------------------------------
# MCP tools — convert MCP server tools to LlamaIndex FunctionTools
# ---------------------------------------------------------------------------
async def _mcp_tools(mcp_url: str):
    """Return (tools_list, mcp_client) for LlamaIndex MCP integration."""
    client = BasicMCPClient(mcp_url, headers={})
    spec = McpToolSpec(client=client)
    tools = await spec.to_tool_list_async()
    return tools, client


# ---------------------------------------------------------------------------
# LLM constructors
# ---------------------------------------------------------------------------
def _openai_llm():
    return LlamaOpenAI(model="gpt-4o-mini", temperature=0, api_key=require_openai_key())


def _bedrock_llm():
    from llama_index.llms.bedrock_converse import BedrockConverse
    return BedrockConverse(
        model="us.amazon.nova-pro-v1:0",
        region_name="us-east-1",
        temperature=0,
    )


# ===================================================================
#  SCENARIO FUNCTIONS
# ===================================================================

async def li_react_openai_local():
    """LlamaIndex ReActAgent + OpenAI + local math tools."""
    llm = _openai_llm()
    agent = ReActAgent(
        name="react-local", llm=llm, tools=_local_tools(), streaming=False,
    )

    @llamaindex_agent(agent_name="li_react_openai_local")
    async def invoke(a):
        return await a.run(user_msg="What is 3 + 5, then multiply the result by 4?")

    return await invoke(agent)


async def li_react_openai_mcp(mcp_url: str):
    """LlamaIndex ReActAgent + OpenAI + MCP tools."""
    llm = _openai_llm()
    tools, client = await _mcp_tools(mcp_url)
    agent = ReActAgent(
        name="react-mcp", llm=llm, tools=list(tools),
        streaming=False, early_stopping_method="generate",
    )

    @llamaindex_agent(agent_name="li_react_openai_mcp")
    async def invoke(a, mc):
        return await a.run(user_msg="Tell me the weather in Tokyo.", max_iterations=10)

    return await invoke(agent, client)


async def li_func_openai_local():
    """LlamaIndex FunctionAgent + OpenAI + local math tools."""
    llm = _openai_llm()
    agent = FunctionAgent(
        name="func-local", llm=llm, tools=_local_tools(), streaming=False,
    )

    @llamaindex_agent(agent_name="li_func_openai_local")
    async def invoke(a):
        return await a.run(user_msg="What is 7 + 9, then multiply the result by 3?")

    return await invoke(agent)


async def li_func_openai_mcp(mcp_url: str):
    """LlamaIndex FunctionAgent + OpenAI + MCP tools."""
    llm = _openai_llm()
    tools, client = await _mcp_tools(mcp_url)
    agent = FunctionAgent(
        name="func-mcp", llm=llm, tools=list(tools), streaming=False,
    )

    @llamaindex_agent(agent_name="li_func_openai_mcp")
    async def invoke(a, mc):
        return await a.run(user_msg="What is the population of Tokyo?")

    return await invoke(agent, client)


async def li_react_bedrock_mcp(mcp_url: str):
    """LlamaIndex ReActAgent + Bedrock Nova Pro + MCP tools.

    Uses BedrockConverse with Amazon Nova Pro — gives a Bedrock/Nova
    data point alongside the OpenAI tests.
    """
    llm = _bedrock_llm()
    tools, client = await _mcp_tools(mcp_url)
    agent = ReActAgent(
        name="LlamaIndex-Bedrock-Agent", llm=llm, tools=list(tools),
        streaming=False, early_stopping_method="generate",
    )

    @llamaindex_agent(agent_name="li_react_bedrock_mcp")
    async def invoke(a, mc):
        handler = a.run(
            user_msg="Tell me the weather in Tokyo and the population of London.",
            max_iterations=10,
        )
        return await handler

    return await invoke(agent, client)


# ---------------------------------------------------------------------------
# Error scenario — post-hoc bad model via wrap() + sync chat()
# ---------------------------------------------------------------------------
async def li_error_wrap_bedrock():
    """Generate LLM errors via wrap() with a non-existent Bedrock model.

    Uses rastir.wrap() to instrument a BedrockConverse instance with a
    bad model name, then calls .chat() synchronously to produce error spans.
    """
    from llama_index.llms.bedrock_converse import BedrockConverse
    from llama_index.core.base.llms.types import ChatMessage
    from rastir import wrap
    from rastir.context import set_current_agent, reset_current_agent

    agent_token = set_current_agent("li_error_wrap_bedrock")
    try:
        bad_llm = BedrockConverse(
            model="us.amazon.nova-nonexistent-v99:0",
            region_name="us-east-1",
            temperature=0,
        )
        wrapped = wrap(bad_llm, span_type="llm")
        for i in range(2):
            try:
                wrapped.chat([ChatMessage(role="user", content="test")])
            except Exception as e:
                print(f"    ✓ Error {i+1}: {type(e).__name__}: {str(e)[:80]}")
    finally:
        reset_current_agent(agent_token)


# ===================================================================
#  PUBLIC RUNNER
# ===================================================================

async def run_all(results: TestResults, *, include_errors: bool = True):
    """Run all LlamaIndex e2e scenarios.

    Covers 5 success scenarios (ReAct/Function × Local/MCP + Bedrock)
    plus 1 error scenario (wrap-bedrock).
    """
    mcp_url = start_mcp_server()
    log("LlamaIndex: MCP server ready")

    for label, coro in [
        ("LlamaIndex ReAct + OpenAI + Local",   li_react_openai_local()),
        ("LlamaIndex ReAct + OpenAI + MCP",     li_react_openai_mcp(mcp_url)),
        ("LlamaIndex Function + OpenAI + Local", li_func_openai_local()),
        ("LlamaIndex Function + OpenAI + MCP",   li_func_openai_mcp(mcp_url)),
        ("LlamaIndex ReAct + Bedrock + MCP",     li_react_bedrock_mcp(mcp_url)),
    ]:
        clear_captured_spans()
        log(f"START: {label}")
        t0 = time.monotonic()
        try:
            answer = await coro
            elapsed = time.monotonic() - t0
            text = str(answer)[:120]
            log(f"DONE:  {label} ({elapsed:.1f}s) ✓ {text}")
            results.passed(label)
        except Exception as e:
            elapsed = time.monotonic() - t0
            log(f"FAIL:  {label} ({elapsed:.1f}s) ✗ {e}")
            results.failed(label, e)
        await asyncio.sleep(1)

    if include_errors:
        label = "LlamaIndex Error: wrap() Bedrock"
        clear_captured_spans()
        log(f"START: {label}")
        t0 = time.monotonic()
        try:
            await li_error_wrap_bedrock()
            elapsed = time.monotonic() - t0
            log(f"DONE:  {label} ({elapsed:.1f}s) ✓")
            results.passed(label)
        except Exception as e:
            elapsed = time.monotonic() - t0
            # Expected — bad model name causes early validation error
            log(f"DONE:  {label} ({elapsed:.1f}s) {type(e).__name__} (expected)")
            results.passed(f"{label} ({type(e).__name__})")
