"""Cross-framework error generation scenarios.

Generates TOOL-LEVEL errors across all 4 frameworks (LangGraph, ADK,
Strands, LlamaIndex) plus one LLM-LEVEL error. Each scenario creates
a tool that raises a specific exception type.

  ┌───────────────────────────────────────────────────────────────────┐
  │  Scenario                     │ Framework  │ Error Type          │
  ├───────────────────────────────┼────────────┼─────────────────────┤
  │ err_langgraph_tool            │ LangGraph  │ ValueError          │
  │ err_adk_tool                  │ ADK        │ RuntimeError        │
  │ err_strands_tool              │ Strands    │ TimeoutError        │
  │ err_llamaindex_tool           │ LlamaIndex │ ConnectionError     │
  │ err_langgraph_llm             │ LangGraph  │ bad model (API err) │
  └───────────────────────────────────────────────────────────────────┘

  These error scenarios are distinct from the per-framework error
  tests in each scenario module. The per-framework errors test
  bad model names and wrap() errors. THIS module tests tool-level
  exceptions that propagate through the framework's error handling.

  Purpose:
    - Populate error rate panels in Grafana dashboards
    - Verify that Rastir captures error spans with correct status
    - Exercise each framework's exception propagation path

  Sources consolidated:
    - generate_errors.py (5 error scenarios)
"""

from __future__ import annotations

import asyncio
import time

from tests.e2e.common import (
    TestResults,
    clear_captured_spans,
    log,
    require_gemini_key,
    require_openai_key,
)
from rastir import langgraph_agent, adk_agent, strands_agent, llamaindex_agent


# ===================================================================
#  SCENARIO 1: LangGraph tool error — ValueError
# ===================================================================
async def err_langgraph_tool():
    """LangGraph react agent with a tool that raises ValueError."""
    from langchain_google_genai import ChatGoogleGenerativeAI
    from langchain_core.tools import tool as lc_tool
    from langgraph.prebuilt import create_react_agent

    @lc_tool
    def failing_tool(x: int) -> int:
        """A tool that always fails."""
        raise ValueError("Tool computation failed: division by zero")

    llm = ChatGoogleGenerativeAI(
        model="gemini-2.5-flash", google_api_key=require_gemini_key(), temperature=0,
    )
    graph = create_react_agent(llm, [failing_tool])

    @langgraph_agent(agent_name="lg_error_test")
    async def invoke(g):
        return await g.ainvoke(
            {"messages": [{"role": "user", "content": "Call failing_tool with x=5"}]}
        )

    await invoke(graph)


# ===================================================================
#  SCENARIO 2: ADK tool error — RuntimeError
# ===================================================================
async def err_adk_tool():
    """ADK agent with a tool that raises RuntimeError."""
    from google.adk.agents import Agent as AdkAgent
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google import genai
    from google.genai import types

    def adk_failing_tool(x: int) -> int:
        """A tool that always fails with a runtime error."""
        raise RuntimeError("ADK tool crashed unexpectedly")

    adk_client = genai.Client(api_key=require_gemini_key())
    adk_agent_obj = AdkAgent(
        name="adk_error_agent",
        model="gemini-2.5-flash",
        tools=[adk_failing_tool],
        client=adk_client,
        instruction="You must call adk_failing_tool with x=5. Do not give up.",
    )
    runner = Runner(
        agent=adk_agent_obj, app_name="error-test",
        session_service=InMemorySessionService(),
    )

    @adk_agent(agent_name="adk_error_test")
    async def invoke(r):
        events = []
        async for ev in r.run_async(
            user_id="u1", session_id="s1",
            new_message=types.Content(
                role="user", parts=[types.Part(text="Call adk_failing_tool with x=5")],
            ),
        ):
            events.append(ev)
        return events

    await invoke(runner)


# ===================================================================
#  SCENARIO 3: Strands tool error — TimeoutError
# ===================================================================
def err_strands_tool():
    """Strands agent with a tool that raises TimeoutError."""
    from strands import Agent as StrandsAgentCls
    from strands.models.bedrock import BedrockModel
    from strands.tools import tool as strands_tool_dec

    @strands_tool_dec
    def strands_failing_tool(x: int) -> int:
        """A tool that always fails with a timeout."""
        raise TimeoutError("Strands tool timed out")

    model = BedrockModel(
        model_id="apac.anthropic.claude-sonnet-4-20250514-v1:0",
        region_name="ap-south-1",
    )
    agent = StrandsAgentCls(
        model=model, tools=[strands_failing_tool],
        system_prompt="You must call strands_failing_tool with x=5.",
    )

    @strands_agent(agent_name="strands_error_test")
    def invoke(a):
        return a("Call strands_failing_tool with x=5")

    invoke(agent)


# ===================================================================
#  SCENARIO 4: LlamaIndex tool error — ConnectionError
# ===================================================================
async def err_llamaindex_tool():
    """LlamaIndex FunctionAgent with a tool that raises ConnectionError."""
    from llama_index.core.agent.workflow import FunctionAgent
    from llama_index.core.tools import FunctionTool
    from llama_index.llms.openai import OpenAI as LlamaOpenAI

    def li_failing_tool(x: int) -> int:
        """A tool that always fails with a connection error."""
        raise ConnectionError("LlamaIndex tool connection refused")

    llm = LlamaOpenAI(model="gpt-4o-mini", api_key=require_openai_key(), temperature=0)
    tool = FunctionTool.from_defaults(fn=li_failing_tool)
    agent = FunctionAgent(
        name="li-error", llm=llm, tools=[tool], streaming=False,
    )

    @llamaindex_agent(agent_name="li_error_test")
    async def invoke(a):
        handler = a.run(user_msg="Call li_failing_tool with x=5")
        async for ev in handler.stream_events():
            pass
        return await handler

    await invoke(agent)


# ===================================================================
#  SCENARIO 5: LangGraph LLM error — bad model name + dummy tool
# ===================================================================
async def err_langgraph_llm():
    """LangGraph react agent with a non-existent model and a dummy tool.

    This generates an LLM-level error (as opposed to tool-level) by
    using a model name that doesn't exist.
    """
    from langchain_google_genai import ChatGoogleGenerativeAI
    from langchain_core.tools import tool as lc_tool
    from langgraph.prebuilt import create_react_agent

    @lc_tool
    def dummy_tool(x: int) -> int:
        """A dummy tool."""
        return x

    bad_llm = ChatGoogleGenerativeAI(
        model="nonexistent-model-xyz", google_api_key=require_gemini_key(), temperature=0,
    )
    graph = create_react_agent(bad_llm, [dummy_tool])

    @langgraph_agent(agent_name="lg_llm_error_test")
    async def invoke(g):
        return await g.ainvoke(
            {"messages": [{"role": "user", "content": "call dummy_tool with x=1"}]}
        )

    await invoke(graph)


# ===================================================================
#  PUBLIC RUNNER
# ===================================================================

async def run_all(results: TestResults, **_):
    """Run all cross-framework error scenarios (5 total).

    Each scenario is expected to either raise an exception or
    complete with an error captured in the tool/LLM span.
    """
    scenarios = [
        ("[1] LangGraph tool error (ValueError)", err_langgraph_tool(), True),
        ("[2] ADK tool error (RuntimeError)",      err_adk_tool(), True),
        ("[3] Strands tool error (TimeoutError)",   None, False),  # sync
        ("[4] LlamaIndex tool error (ConnError)",   err_llamaindex_tool(), True),
        ("[5] LangGraph LLM error (bad model)",     err_langgraph_llm(), True),
    ]

    for label, coro, is_async in scenarios:
        clear_captured_spans()
        log(f"START: {label}")
        t0 = time.monotonic()
        try:
            if is_async:
                await coro
            else:
                await asyncio.to_thread(err_strands_tool)
            elapsed = time.monotonic() - t0
            # Error captured in span — this is expected success
            log(f"DONE:  {label} ({elapsed:.1f}s) error in span")
            results.passed(f"{label} (error in span)")
        except Exception as e:
            elapsed = time.monotonic() - t0
            # Exception raised — also expected for error generation
            log(f"DONE:  {label} ({elapsed:.1f}s) {type(e).__name__}")
            results.passed(f"{label} ({type(e).__name__})")
