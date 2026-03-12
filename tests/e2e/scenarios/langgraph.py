"""LangGraph e2e scenarios.

Covers ALL LangGraph variations tested across the old e2e files:

  ┌──────────────────────────────────────────────────────────────────┐
  │  Scenario                     │ Agent Type │ Provider │ Tools   │
  ├───────────────────────────────┼────────────┼──────────┼─────────┤
  │ lg_react_gemini_mcp           │ react      │ gemini   │ MCP     │
  │ lg_react_gemini_local         │ react      │ gemini   │ local   │
  │ lg_manual_gemini_mcp          │ manual     │ gemini   │ MCP     │
  │ lg_manual_gemini_local        │ manual     │ gemini   │ local   │
  │ lg_manual_openai_mcp          │ manual     │ openai   │ MCP     │
  │ lg_manual_bedrock_mcp         │ manual     │ bedrock  │ MCP     │
  │ lg_error_bad_model            │ react      │ gemini   │ MCP     │
  │ lg_error_wrap_openai          │ wrap()     │ openai   │ —       │
  │ lg_error_wrap_bedrock         │ wrap()     │ bedrock  │ —       │
  └──────────────────────────────────────────────────────────────────┘

  Sources consolidated:
    - test_langgraph_e2e.py        (react + Gemini + MCP)
    - test_langgraph_manual_e2e.py (manual + Gemini + MCP)
    - test_langgraph_openai_e2e.py (manual + OpenAI + MCP)
    - test_langgraph_bedrock_e2e.py(manual + Bedrock + MCP)
    - test_langgraph_error_e2e.py  (react + bad model Gemini)
    - run_all_combinations.py      (react+manual × local+MCP)
    - run_success_requests.py      (5 success requests)
"""

from __future__ import annotations

import asyncio
import time
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.tools import tool as langchain_tool
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, create_react_agent

from tests.e2e.common import (
    TestResults,
    call_mcp_tool,
    clear_captured_spans,
    captured_spans,
    log,
    print_spans,
    require_gemini_key,
    require_openai_key,
    start_mcp_server,
)
from rastir import langgraph_agent


# ---------------------------------------------------------------------------
# State definition (shared by all manual StateGraph variants)
# ---------------------------------------------------------------------------
class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


# ---------------------------------------------------------------------------
# Local tools (LangChain @tool wrappers around simple functions)
# ---------------------------------------------------------------------------
@langchain_tool
def lc_add_numbers(a: int, b: int) -> int:
    """Add two numbers together."""
    return a + b


@langchain_tool
def lc_multiply_numbers(a: int, b: int) -> int:
    """Multiply two numbers together."""
    return a * b


# ---------------------------------------------------------------------------
# Manual graph builder — explicit agent/tools nodes with conditional edges.
# This is the "non-react" LangGraph pattern where the user controls
# the graph topology.
# ---------------------------------------------------------------------------
def build_manual_graph(llm, tools):
    """Build a LangGraph StateGraph with explicit agent + tools nodes."""
    llm_with_tools = llm.bind_tools(tools)

    async def agent_node(state: AgentState) -> AgentState:
        response = await llm_with_tools.ainvoke(state["messages"])
        return {"messages": [response]}

    def should_continue(state: AgentState) -> str:
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and getattr(last, "tool_calls", None):
            return "tools"
        return END

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", ToolNode(tools))
    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")
    return graph.compile()


# ---------------------------------------------------------------------------
# Provider-specific LLM constructors
# ---------------------------------------------------------------------------
def _gemini_llm():
    from langchain_google_genai import ChatGoogleGenerativeAI
    return ChatGoogleGenerativeAI(
        model="gemini-2.5-flash", temperature=0,
        google_api_key=require_gemini_key(),
    )


def _openai_llm():
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(
        model="gpt-4o-mini", temperature=0,
        api_key=require_openai_key(),
    )


def _bedrock_llm():
    from langchain_aws import ChatBedrockConverse
    return ChatBedrockConverse(
        model="us.anthropic.claude-sonnet-4-20250514-v1:0",
        region_name="us-east-1", temperature=0,
    )


async def _get_mcp_tools(mcp_url: str):
    """Get LangChain-compatible tools from MCP server."""
    from langchain_mcp_adapters.client import MultiServerMCPClient
    client = MultiServerMCPClient({"tools": {"url": mcp_url, "transport": "streamable_http"}})
    return await client.get_tools(), client


# ===================================================================
#  SCENARIO FUNCTIONS — each returns the agent result or raises
# ===================================================================

async def lg_react_gemini_mcp(mcp_url: str):
    """LangGraph react agent + Gemini + MCP tools."""
    llm = _gemini_llm()
    tools, client = await _get_mcp_tools(mcp_url)
    graph = create_react_agent(llm, tools)

    @langgraph_agent(agent_name="lg_react_gemini_mcp")
    async def invoke(g, mc):
        return await g.ainvoke({
            "messages": [("user",
                "What is the weather in Tokyo and the population of London?")]
        })
    return await invoke(graph, client)


async def lg_react_gemini_local():
    """LangGraph react agent + Gemini + local math tools."""
    llm = _gemini_llm()
    graph = create_react_agent(llm, [lc_add_numbers, lc_multiply_numbers])

    @langgraph_agent(agent_name="lg_react_gemini_local")
    async def invoke(g, mc):
        return await g.ainvoke({
            "messages": [("user", "Add 15 and 27, then multiply the result by 3.")]
        })
    return await invoke(graph, None)


async def lg_manual_gemini_mcp(mcp_url: str):
    """LangGraph manual StateGraph + Gemini + MCP tools."""
    llm = _gemini_llm()
    tools, client = await _get_mcp_tools(mcp_url)
    graph = build_manual_graph(llm, tools)

    @langgraph_agent(agent_name="lg_manual_gemini_mcp")
    async def invoke(g, mc):
        return await g.ainvoke({
            "messages": [HumanMessage(content=(
                "What is the weather in London and the population of Paris? "
                "Also tell me the timezone of New York."
            ))]
        })
    return await invoke(graph, client)


async def lg_manual_gemini_local():
    """LangGraph manual StateGraph + Gemini + local math tools."""
    llm = _gemini_llm()
    graph = build_manual_graph(llm, [lc_add_numbers, lc_multiply_numbers])

    @langgraph_agent(agent_name="lg_manual_gemini_local")
    async def invoke(g, mc):
        return await g.ainvoke({
            "messages": [HumanMessage(content="What is 8 + 13 and what is 7 * 9?")]
        })
    return await invoke(graph, None)


async def lg_manual_openai_mcp(mcp_url: str):
    """LangGraph manual StateGraph + OpenAI GPT-4o-mini + MCP tools."""
    llm = _openai_llm()
    tools, client = await _get_mcp_tools(mcp_url)
    graph = build_manual_graph(llm, tools)

    @langgraph_agent(agent_name="lg_manual_openai_mcp")
    async def invoke(g, mc):
        return await g.ainvoke({
            "messages": [HumanMessage(content=(
                "What is the weather in London and the population of Paris? "
                "Also tell me the timezone of New York."
            ))]
        })
    return await invoke(graph, client)


async def lg_manual_bedrock_mcp(mcp_url: str):
    """LangGraph manual StateGraph + Bedrock Claude Sonnet 4 + MCP tools."""
    llm = _bedrock_llm()
    tools, client = await _get_mcp_tools(mcp_url)
    graph = build_manual_graph(llm, tools)

    @langgraph_agent(agent_name="lg_manual_bedrock_mcp")
    async def invoke(g, mc):
        return await g.ainvoke({
            "messages": [HumanMessage(content=(
                "What is the weather in London and the population of Paris? "
                "Also tell me the timezone of New York."
            ))]
        })
    return await invoke(graph, client)


# ---------------------------------------------------------------------------
# Error scenarios
# ---------------------------------------------------------------------------

async def lg_error_bad_model(mcp_url: str):
    """LangGraph react agent with a non-existent Gemini model name.

    This triggers an LLM-level error (model not found). The agent span
    should still be created with error status.
    """
    from langchain_google_genai import ChatGoogleGenerativeAI
    llm = ChatGoogleGenerativeAI(
        model="gemini-nonexistent-model-xyz", temperature=0,
        google_api_key=require_gemini_key(),
    )
    tools, client = await _get_mcp_tools(mcp_url)
    graph = create_react_agent(llm, tools)

    @langgraph_agent(agent_name="lg_error_bad_model")
    async def invoke(g, mc):
        return await g.ainvoke({
            "messages": [("user", "What is the weather in Tokyo?")]
        })
    return await invoke(graph, client)


async def lg_error_wrap_openai():
    """Generate LLM errors via wrap() with a non-existent OpenAI model.

    Uses rastir.wrap() to instrument a ChatOpenAI instance with a bad
    model name, then calls ainvoke() to produce error spans.
    """
    from langchain_openai import ChatOpenAI
    from rastir import wrap
    from rastir.context import set_current_agent, reset_current_agent

    agent_token = set_current_agent("lg_error_wrap_openai")
    try:
        bad_llm = ChatOpenAI(
            model="gpt-nonexistent-model-xyz", temperature=0,
            api_key=require_openai_key(),
        )
        wrapped = wrap(bad_llm, span_type="llm")
        for i in range(2):
            try:
                await wrapped.ainvoke("test error")
            except Exception as e:
                print(f"    ✓ Error {i+1}: {type(e).__name__}: {str(e)[:80]}")
    finally:
        reset_current_agent(agent_token)


async def lg_error_wrap_bedrock():
    """Generate LLM errors via wrap() with a non-existent Bedrock model.

    Uses rastir.wrap() to instrument a ChatBedrockConverse instance with
    a bad model name, then calls ainvoke() to produce error spans.
    """
    from langchain_aws import ChatBedrockConverse
    from rastir import wrap
    from rastir.context import set_current_agent, reset_current_agent

    agent_token = set_current_agent("lg_error_wrap_bedrock")
    try:
        bad_llm = ChatBedrockConverse(
            model="us.anthropic.claude-nonexistent-v99:0",
            region_name="us-east-1", temperature=0,
        )
        wrapped = wrap(bad_llm, span_type="llm")
        for i in range(2):
            try:
                await wrapped.ainvoke("test error")
            except Exception as e:
                print(f"    ✓ Error {i+1}: {type(e).__name__}: {str(e)[:80]}")
    finally:
        reset_current_agent(agent_token)


# ===================================================================
#  PUBLIC RUNNER — called from run_e2e.py
# ===================================================================

async def run_all(results: TestResults, *, include_errors: bool = True):
    """Run all LangGraph e2e scenarios.

    Covers 6 success scenarios (react/manual × gemini/openai/bedrock)
    plus 3 error scenarios (bad model, wrap-openai, wrap-bedrock).
    """
    mcp_url = start_mcp_server()
    log("LangGraph: MCP server ready")

    # --- Success scenarios ---
    for label, coro in [
        ("LangGraph React + Gemini + MCP",    lg_react_gemini_mcp(mcp_url)),
        ("LangGraph React + Gemini + Local",   lg_react_gemini_local()),
        ("LangGraph Manual + Gemini + MCP",    lg_manual_gemini_mcp(mcp_url)),
        ("LangGraph Manual + Gemini + Local",  lg_manual_gemini_local()),
        ("LangGraph Manual + OpenAI + MCP",    lg_manual_openai_mcp(mcp_url)),
        ("LangGraph Manual + Bedrock + MCP",   lg_manual_bedrock_mcp(mcp_url)),
    ]:
        clear_captured_spans()
        log(f"START: {label}")
        t0 = time.monotonic()
        try:
            answer = await coro
            elapsed = time.monotonic() - t0
            text = str(answer)
            if isinstance(answer, dict) and "messages" in answer:
                text = str(answer["messages"][-1].content)
            log(f"DONE:  {label} ({elapsed:.1f}s) ✓ {text[:120]}")
            results.passed(label)
        except Exception as e:
            elapsed = time.monotonic() - t0
            log(f"FAIL:  {label} ({elapsed:.1f}s) ✗ {e}")
            results.failed(label, e)
        await asyncio.sleep(1)

    # --- Error scenarios ---
    if include_errors:
        # Bad model — expected to raise
        label = "LangGraph Error: bad model"
        clear_captured_spans()
        log(f"START: {label}")
        t0 = time.monotonic()
        try:
            await lg_error_bad_model(mcp_url)
            elapsed = time.monotonic() - t0
            log(f"DONE:  {label} ({elapsed:.1f}s) error in span")
            results.passed(f"{label} (error in span)")
        except Exception as e:
            elapsed = time.monotonic() - t0
            log(f"DONE:  {label} ({elapsed:.1f}s) {type(e).__name__}")
            results.passed(f"{label} ({type(e).__name__})")

        # Wrap errors — expected to print errors but not raise
        for err_label, err_fn in [
            ("LangGraph Error: wrap() OpenAI",  lg_error_wrap_openai),
            ("LangGraph Error: wrap() Bedrock",  lg_error_wrap_bedrock),
        ]:
            clear_captured_spans()
            log(f"START: {err_label}")
            t0 = time.monotonic()
            try:
                await err_fn()
                elapsed = time.monotonic() - t0
                log(f"DONE:  {err_label} ({elapsed:.1f}s) ✓")
                results.passed(err_label)
            except Exception as e:
                elapsed = time.monotonic() - t0
                log(f"FAIL:  {err_label} ({elapsed:.1f}s) ✗ {e}")
                results.failed(err_label, e)
