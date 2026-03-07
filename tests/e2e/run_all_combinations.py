"""Run ALL framework × agent-type × tool-type combinations.

10 combinations total:
  LangGraph  (4): React+MCP, React+Local, Manual+MCP, Manual+Local
  CrewAI     (2): Agent+MCP, Agent+Local
  LlamaIndex (4): ReAct+MCP, ReAct+Local, Function+MCP, Function+Local

Run:
    conda run -n llmobserve env PYTHONPATH=src \
        python tests/e2e/run_all_combinations.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from typing import Annotated, TypedDict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------
GEMINI_KEY = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")
OPENAI_KEY = os.environ.get("API_OPENAI_KEY") or os.environ.get("OPENAI_API_KEY", "")
os.environ["OPENAI_API_KEY"] = OPENAI_KEY  # CrewAI / LlamaIndex need this

if not GEMINI_KEY:
    print("ERROR: GEMINI_API_KEY not set"); sys.exit(1)
if not OPENAI_KEY:
    print("ERROR: API_OPENAI_KEY or OPENAI_API_KEY not set"); sys.exit(1)

# ---------------------------------------------------------------------------
# Rastir
# ---------------------------------------------------------------------------
import rastir
from rastir import configure, langgraph_agent, crew_kickoff, llamaindex_agent
from rastir.remote import traceparent_headers

configure(
    service="all-combos-e2e",
    push_url="http://localhost:8080",
    enable_cost_calculation=True,
)
from rastir.config import get_pricing_registry
pr = get_pricing_registry()
if pr:
    pr.register("gemini", "gemini-2.5-flash", input_price=0.15, output_price=0.60)
    pr.register("openai", "gpt-4o-mini", input_price=0.15, output_price=0.60)
    pr.register("openai", "gpt-4o-mini-2024-07-18", input_price=0.15, output_price=0.60)

# ---------------------------------------------------------------------------
# MCP server (background)
# ---------------------------------------------------------------------------
import uvicorn
import httpx

MCP_PORT = 19890
MCP_URL = f"http://127.0.0.1:{MCP_PORT}/mcp"


def _start_mcp_server():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "mcp_test_server",
        os.path.join(os.path.dirname(__file__), "mcp_test_server.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    app = mod.create_app(MCP_PORT)
    uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=MCP_PORT, log_level="warning")
    ).run()


def _wait_for_server(url: str, timeout: float = 10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with httpx.Client(timeout=2) as c:
                r = c.get(url.replace("/mcp", "/"))
                if r.status_code < 500:
                    return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


# ---------------------------------------------------------------------------
# Local tools (plain functions)
# ---------------------------------------------------------------------------
def add_numbers(a: int, b: int) -> int:
    """Add two numbers together."""
    return a + b


def multiply_numbers(a: int, b: int) -> int:
    """Multiply two numbers together."""
    return a * b


# ===================================================================
#  LANGGRAPH helpers
# ===================================================================
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import AIMessage, HumanMessage, BaseMessage
from langchain_core.tools import tool as langchain_tool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, create_react_agent


class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


@langchain_tool
def lc_add_numbers(a: int, b: int) -> int:
    """Add two numbers together."""
    return a + b


@langchain_tool
def lc_multiply_numbers(a: int, b: int) -> int:
    """Multiply two numbers together."""
    return a * b


def build_manual_graph(llm, tools):
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


# ===================================================================
#  CREWAI helpers
# ===================================================================
from crewai import Agent as CrewAgent, Task, Crew, LLM
from crewai.tools import tool as crewai_tool


@crewai_tool
def crew_add_numbers(a: int, b: int) -> str:
    """Add two numbers together and return the result."""
    return str(a + b)


@crewai_tool
def crew_multiply_numbers(a: int, b: int) -> str:
    """Multiply two numbers and return the result."""
    return str(a * b)


@crewai_tool
def crew_get_weather(city: str) -> str:
    """Get the current weather for a city."""
    hdrs = {"Accept": "application/json", **traceparent_headers()}
    with httpx.Client(timeout=10) as c:
        r = c.post(MCP_URL, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": "get_weather", "arguments": {"city": city}}},
                   headers=hdrs)
        data = r.json()
        content = data.get("result", {}).get("content", [{}])
        return content[0].get("text", str(data)) if content else str(data)


@crewai_tool
def crew_get_population(city: str) -> str:
    """Get the approximate population of a city."""
    hdrs = {"Accept": "application/json", **traceparent_headers()}
    with httpx.Client(timeout=10) as c:
        r = c.post(MCP_URL, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": "get_population", "arguments": {"city": city}}},
                   headers=hdrs)
        data = r.json()
        content = data.get("result", {}).get("content", [{}])
        return content[0].get("text", str(data)) if content else str(data)


# ===================================================================
#  LLAMAINDEX helpers
# ===================================================================
from llama_index.llms.openai import OpenAI as LlamaOpenAI
from llama_index.core.agent import ReActAgent, FunctionAgent
from llama_index.core.tools import FunctionTool
from llama_index.tools.mcp import BasicMCPClient, McpToolSpec

# ===================================================================
#  RESULTS
# ===================================================================
results: dict[str, str] = {}


async def run_combo(label: str, coro):
    """Run a single combination and record pass/fail."""
    print(f"\n{'─' * 60}")
    print(f"  {label}")
    print(f"{'─' * 60}")
    try:
        answer = await coro
        text = str(getattr(answer, "raw", answer))
        # LangGraph returns dict with messages
        if isinstance(answer, dict) and "messages" in answer:
            text = str(answer["messages"][-1].content)
        print(f"  ✓ Result: {text[:200]}")
        results[label] = "PASS ✓"
    except Exception as e:
        print(f"  ✗ FAILED: {e}")
        results[label] = f"FAIL ✗ ({e})"
    await asyncio.sleep(1)


# ===================================================================
#  MAIN
# ===================================================================
async def main():
    print("=" * 60)
    print("  ALL-COMBINATIONS E2E TEST (10 combos)")
    print("=" * 60)

    # ── MCP server ───────────────────────────────────────────────
    print("\nStarting MCP test server...")
    threading.Thread(target=_start_mcp_server, daemon=True).start()
    if not _wait_for_server(MCP_URL):
        print("FAILED: MCP server did not start"); sys.exit(1)
    print(f"MCP server ready: {MCP_URL}")

    # ── Shared resources ─────────────────────────────────────────
    gemini_llm = ChatGoogleGenerativeAI(
        model="gemini-2.5-flash", temperature=0, google_api_key=GEMINI_KEY,
    )
    mcp_client = MultiServerMCPClient({
        "tools": {"url": MCP_URL, "transport": "streamable_http"},
    })
    mcp_tools_lc = await mcp_client.get_tools()
    local_tools_lc = [lc_add_numbers, lc_multiply_numbers]

    # LlamaIndex
    mcp_li_client = BasicMCPClient(MCP_URL, headers={})
    mcp_li_spec = McpToolSpec(client=mcp_li_client)
    mcp_tools_li = await mcp_li_spec.to_tool_list_async()
    local_tools_li = [
        FunctionTool.from_defaults(fn=add_numbers),
        FunctionTool.from_defaults(fn=multiply_numbers),
    ]

    combo_num = 0

    # ══════════════════════════════════════════════════════════════
    #  LANGGRAPH (4 combos)
    # ══════════════════════════════════════════════════════════════
    print("\n" + "═" * 60)
    print("  LANGGRAPH")
    print("═" * 60)

    # 1. React + MCP
    combo_num += 1
    react_mcp_graph = create_react_agent(gemini_llm, mcp_tools_lc)

    @langgraph_agent(agent_name="lg_react_mcp")
    async def lg_react_mcp(g, mc):
        return await g.ainvoke({"messages": [("user", "What is the weather in Tokyo and the population of London?")]})

    await run_combo(f"[{combo_num}/10] LangGraph React + MCP tools", lg_react_mcp(react_mcp_graph, mcp_client))

    # 2. React + Local
    combo_num += 1
    react_local_graph = create_react_agent(gemini_llm, local_tools_lc)

    @langgraph_agent(agent_name="lg_react_local")
    async def lg_react_local(g, mc):
        return await g.ainvoke({"messages": [("user", "Add 15 and 27, then multiply the result by 3.")]})

    await run_combo(f"[{combo_num}/10] LangGraph React + Local tools", lg_react_local(react_local_graph, mcp_client))

    # 3. Manual + MCP
    combo_num += 1
    manual_mcp_graph = build_manual_graph(gemini_llm, mcp_tools_lc)

    @langgraph_agent(agent_name="lg_manual_mcp")
    async def lg_manual_mcp(g, mc):
        return await g.ainvoke({"messages": [HumanMessage(content="Convert 30 celsius to fahrenheit and tell me the timezone of Paris.")]})

    await run_combo(f"[{combo_num}/10] LangGraph Manual + MCP tools", lg_manual_mcp(manual_mcp_graph, mcp_client))

    # 4. Manual + Local
    combo_num += 1
    manual_local_graph = build_manual_graph(gemini_llm, local_tools_lc)

    @langgraph_agent(agent_name="lg_manual_local")
    async def lg_manual_local(g, mc):
        return await g.ainvoke({"messages": [HumanMessage(content="What is 8 + 13 and what is 7 * 9?")]})

    await run_combo(f"[{combo_num}/10] LangGraph Manual + Local tools", lg_manual_local(manual_local_graph, mcp_client))

    # ══════════════════════════════════════════════════════════════
    #  CREWAI (2 combos)
    # ══════════════════════════════════════════════════════════════
    print("\n" + "═" * 60)
    print("  CREWAI")
    print("═" * 60)

    crew_llm = LLM(model="openai/gpt-4o-mini", api_key=OPENAI_KEY, temperature=0)

    # 5. CrewAI + Local tools
    combo_num += 1
    math_agent = CrewAgent(
        role="Math Assistant", goal="Solve math problems using tools",
        backstory="You are a helpful math assistant.", llm=crew_llm,
        tools=[crew_add_numbers, crew_multiply_numbers], verbose=False, max_iter=4,
    )
    math_task = Task(
        description="Calculate: (12 + 18) and (6 * 14). Report both results.",
        expected_output="The sum of 12+18 and the product of 6*14.",
        agent=math_agent,
    )
    math_crew = Crew(agents=[math_agent], tasks=[math_task], verbose=False)

    @crew_kickoff(agent_name="crew_local")
    def crew_local_run(crew):
        return crew.kickoff()

    await run_combo(f"[{combo_num}/10] CrewAI Agent + Local tools",
                    asyncio.to_thread(crew_local_run, math_crew))

    # 6. CrewAI + MCP tools
    combo_num += 1
    city_agent = CrewAgent(
        role="City Researcher", goal="Look up city data",
        backstory="You research city facts.", llm=crew_llm,
        tools=[crew_get_weather, crew_get_population], verbose=False, max_iter=4,
    )
    city_task = Task(
        description="What is the weather in Paris and the population of New York?",
        expected_output="Paris weather and New York population.",
        agent=city_agent,
    )
    city_crew = Crew(agents=[city_agent], tasks=[city_task], verbose=False)

    @crew_kickoff(agent_name="crew_mcp")
    def crew_mcp_run(crew):
        return crew.kickoff()

    await run_combo(f"[{combo_num}/10] CrewAI Agent + MCP tools",
                    asyncio.to_thread(crew_mcp_run, city_crew))

    # ══════════════════════════════════════════════════════════════
    #  LLAMAINDEX (4 combos)
    # ══════════════════════════════════════════════════════════════
    print("\n" + "═" * 60)
    print("  LLAMAINDEX")
    print("═" * 60)

    # 7. ReAct + Local
    combo_num += 1
    li_llm1 = LlamaOpenAI(model="gpt-4o-mini", temperature=0, api_key=OPENAI_KEY)
    react_local = ReActAgent(name="react-local", llm=li_llm1, tools=list(local_tools_li), streaming=False)

    @llamaindex_agent(agent_name="li_react_local")
    async def li_react_local_run(agent):
        return await agent.run(user_msg="What is 3 + 5, then multiply the result by 4?")

    await run_combo(f"[{combo_num}/10] LlamaIndex ReAct + Local tools", li_react_local_run(react_local))

    # 8. ReAct + MCP
    combo_num += 1
    li_llm2 = LlamaOpenAI(model="gpt-4o-mini", temperature=0, api_key=OPENAI_KEY)
    react_mcp = ReActAgent(name="react-mcp", llm=li_llm2, tools=list(mcp_tools_li), streaming=False)

    @llamaindex_agent(agent_name="li_react_mcp")
    async def li_react_mcp_run(agent, mc):
        return await agent.run(user_msg="What is the weather in Sydney?")

    await run_combo(f"[{combo_num}/10] LlamaIndex ReAct + MCP tools", li_react_mcp_run(react_mcp, mcp_li_client))

    # 9. Function + Local
    combo_num += 1
    li_llm3 = LlamaOpenAI(model="gpt-4o-mini", temperature=0, api_key=OPENAI_KEY)
    func_local = FunctionAgent(name="func-local", llm=li_llm3, tools=list(local_tools_li), streaming=False)

    @llamaindex_agent(agent_name="li_func_local")
    async def li_func_local_run(agent):
        return await agent.run(user_msg="What is 7 + 9, then multiply the result by 3?")

    await run_combo(f"[{combo_num}/10] LlamaIndex Function + Local tools", li_func_local_run(func_local))

    # 10. Function + MCP
    combo_num += 1
    li_llm4 = LlamaOpenAI(model="gpt-4o-mini", temperature=0, api_key=OPENAI_KEY)
    func_mcp = FunctionAgent(name="func-mcp", llm=li_llm4, tools=list(mcp_tools_li), streaming=False)

    @llamaindex_agent(agent_name="li_func_mcp")
    async def li_func_mcp_run(agent, mc):
        return await agent.run(user_msg="What is the population of Tokyo?")

    await run_combo(f"[{combo_num}/10] LlamaIndex Function + MCP tools", li_func_mcp_run(func_mcp, mcp_li_client))

    # ══════════════════════════════════════════════════════════════
    #  SUMMARY
    # ══════════════════════════════════════════════════════════════
    print("\n" + "═" * 60)
    print("  SUMMARY")
    print("═" * 60)
    passed = 0
    for label, status in results.items():
        print(f"  {label}: {status}")
        if "PASS" in status:
            passed += 1
    print(f"\n  {passed}/{len(results)} passed")
    print("═" * 60)
    print("Waiting 5s for spans to flush...")
    await asyncio.sleep(5)
    print("Done!")


if __name__ == "__main__":
    asyncio.run(main())
