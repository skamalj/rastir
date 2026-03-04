"""Send 5 successful LangGraph requests (react + manual) to reduce error %.

Runs 3 manual-graph and 2 react-agent invocations, all expected to succeed.
No intentional errors are generated.

Run:
    GOOGLE_API_KEY=... conda run -n llmobserve PYTHONPATH=src \
        python tests/e2e/run_success_requests.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from typing import Annotated

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "") or os.environ.get("GEMINI_API_KEY", "")
if not GOOGLE_API_KEY:
    print("ERROR: GOOGLE_API_KEY or GEMINI_API_KEY not set"); sys.exit(1)

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import AIMessage, HumanMessage, BaseMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, create_react_agent
import uvicorn

# ---------------------------------------------------------------------------
# Rastir
# ---------------------------------------------------------------------------
import rastir
from rastir import configure, langgraph_agent

configure(
    service="langgraph-e2e-test",
    push_url="http://localhost:8080",
    enable_cost_calculation=True,
)

from rastir.config import get_pricing_registry
_pr = get_pricing_registry()
if _pr is not None:
    _pr.register("gemini", "gemini-2.5-flash", input_price=0.15, output_price=0.60)

# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------
MCP_PORT = 19879
MCP_URL = f"http://127.0.0.1:{MCP_PORT}/mcp"


def _start_server():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "mcp_test_server",
        os.path.join(os.path.dirname(__file__), "mcp_test_server.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    app = mod.create_app(MCP_PORT)
    config = uvicorn.Config(app, host="127.0.0.1", port=MCP_PORT, log_level="warning")
    server = uvicorn.Server(config)
    server.run()


def _wait_for_server(url: str, timeout: float = 10):
    import httpx
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
# Manual graph builder
# ---------------------------------------------------------------------------
from typing import TypedDict


class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


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


# ---------------------------------------------------------------------------
# Prompts — varied simple queries (all should succeed)
# ---------------------------------------------------------------------------
MANUAL_PROMPTS = [
    "What is the weather in London?",
    "Tell me the population of Tokyo and the timezone of Berlin.",
    "Convert 30 celsius to fahrenheit.",
]

REACT_PROMPTS = [
    "What is the weather in Paris and the timezone of Sydney?",
    "Tell me the population of New York and convert 100 fahrenheit to celsius.",
]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def run():
    print("=" * 60)
    print("Sending 5 successful LangGraph requests (no errors)")
    print("=" * 60)

    # Start MCP server
    print("\n1. Starting MCP server...")
    server_thread = threading.Thread(target=_start_server, daemon=True)
    server_thread.start()
    if not _wait_for_server(MCP_URL):
        print("   FAILED: MCP server did not start"); sys.exit(1)
    print(f"   MCP server ready on {MCP_URL}")

    # Setup LLM + tools
    llm = ChatGoogleGenerativeAI(
        model="gemini-2.5-flash",
        temperature=0,
        google_api_key=GOOGLE_API_KEY,
    )

    mcp_client = MultiServerMCPClient({
        "tools": {"url": MCP_URL, "transport": "streamable_http"},
    })
    tools = await mcp_client.get_tools()
    print(f"   {len(tools)} tools available")

    # Build graphs
    manual_graph = build_manual_graph(llm, tools)
    react_graph = create_react_agent(llm, tools)

    # --- Manual graph requests ---
    for i, prompt in enumerate(MANUAL_PROMPTS, 1):
        print(f"\n[{i}/5] Manual graph: {prompt[:60]}...")

        @langgraph_agent(agent_name="manual_graph_agent")
        async def invoke_manual(g, mc):
            return await g.ainvoke({"messages": [HumanMessage(content=prompt)]})

        try:
            result = await invoke_manual(manual_graph, mcp_client)
            answer = result["messages"][-1].content
            print(f"   ✓ OK  ({len(answer)} chars)")
        except Exception as e:
            print(f"   ✗ FAILED: {e}")

    # --- React agent requests ---
    for j, prompt in enumerate(REACT_PROMPTS, len(MANUAL_PROMPTS) + 1):
        print(f"\n[{j}/5] React agent: {prompt[:60]}...")

        @langgraph_agent(agent_name="langgraph_e2e_agent")
        async def invoke_react(g, mc):
            return await g.ainvoke({"messages": [("user", prompt)]})

        try:
            result = await invoke_react(react_graph, mcp_client)
            answer = result["messages"][-1].content
            print(f"   ✓ OK  ({len(answer)} chars)")
        except Exception as e:
            print(f"   ✗ FAILED: {e}")

    print("\n" + "=" * 60)
    print("Done! Waiting 3s for spans to flush...")
    print("=" * 60)
    await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(run())
