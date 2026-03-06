"""E2E test: LangGraph manual StateGraph + Bedrock Claude + MCP tools.

Uses ChatBedrockConverse with Claude Sonnet 4 on Bedrock as the LLM provider.
This gives us a Bedrock/Claude data point in all dashboards alongside
the OpenAI and Gemini data from the other e2e tests.

Requirements:
    AWS credentials (SSO or env), langchain-aws, langgraph,
    langchain-mcp-adapters, mcp packages.

Run:
    conda run -n llmobserve PYTHONPATH=src \
        python tests/e2e/test_langgraph_bedrock_e2e.py
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
# Pre-flight checks
# ---------------------------------------------------------------------------
try:
    from langchain_aws import ChatBedrockConverse
    from langchain_core.messages import AIMessage, HumanMessage, BaseMessage
    from langchain_mcp_adapters.client import MultiServerMCPClient
    from langgraph.graph import StateGraph, END
    from langgraph.graph.message import add_messages
    from langgraph.prebuilt import ToolNode
except ImportError as e:
    print(f"ERROR: Missing dependency: {e}")
    sys.exit(1)

try:
    import uvicorn
except ImportError:
    print("ERROR: uvicorn not installed")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Rastir setup — capture spans in-process
# ---------------------------------------------------------------------------
import rastir
from rastir import configure, langgraph_agent

configure(
    service="langgraph-bedrock-e2e",
    push_url="http://localhost:8080",
    enable_cost_calculation=True,
)

from rastir.config import get_pricing_registry

_pr = get_pricing_registry()
if _pr is not None:
    _pr.register("bedrock", "us.anthropic.claude-sonnet-4-20250514-v1:0",
                 input_price=3.0, output_price=15.0)

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
# MCP Server — run in background thread
# ---------------------------------------------------------------------------
MCP_PORT = 19882
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
    config = uvicorn.Config(
        app, host="127.0.0.1", port=MCP_PORT, log_level="warning"
    )
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
# State definition
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
    graph.add_conditional_edges(
        "agent", should_continue, {"tools": "tools", END: END}
    )
    graph.add_edge("tools", "agent")

    return graph.compile()


# ---------------------------------------------------------------------------
# Main test
# ---------------------------------------------------------------------------
async def run_test():
    print("=" * 60)
    print("LangGraph + Bedrock Claude Sonnet 4 + MCP E2E Test")
    print("=" * 60)

    print("\n1. Starting MCP server...")
    server_thread = threading.Thread(target=_start_server, daemon=True)
    server_thread.start()

    if not _wait_for_server(MCP_URL):
        print("   FAILED: MCP server did not start")
        sys.exit(1)
    print("   MCP server ready on", MCP_URL)

    print("\n2. Setting up LangGraph with Bedrock Claude...")
    llm = ChatBedrockConverse(
        model="us.anthropic.claude-sonnet-4-20250514-v1:0",
        region_name="us-east-1",
        temperature=0,
    )

    mcp_client = MultiServerMCPClient(
        {
            "tools": {
                "url": MCP_URL,
                "transport": "streamable_http",
            },
        }
    )

    tools = await mcp_client.get_tools()
    print(f"   Discovered {len(tools)} tools: {[t.name for t in tools]}")

    graph = build_manual_graph(llm, tools)

    @langgraph_agent(agent_name="bedrock_claude_agent")
    async def invoke(graph, mcp_client):
        return await graph.ainvoke(
            {
                "messages": [
                    HumanMessage(
                        content=(
                            "What is the weather in London and the population of Paris? "
                            "Also tell me the timezone of New York."
                        )
                    )
                ]
            }
        )

    print("\n3. Running agent...")
    captured_spans.clear()
    result = await invoke(graph, mcp_client)

    final_msg = result["messages"][-1].content
    print(f"\n4. Agent response:\n   {str(final_msg)[:300]}...")

    print(f"\n5. Captured {len(captured_spans)} spans:")
    if captured_spans:
        t0 = min(s.start_time for s in captured_spans)
    else:
        t0 = 0
    for s in captured_spans:
        agent_attr = s.attributes.get("agent", "")
        agent_str = f" agent={agent_attr}" if agent_attr else ""
        cost = s.attributes.get("cost_usd", "")
        cost_str = f" cost=${cost:.6f}" if cost else ""
        rel_start = (s.start_time - t0) * 1000
        dur = (s.end_time - s.start_time) * 1000 if s.end_time else 0
        print(
            f"   - {s.name} ({s.span_type.value}){agent_str}{cost_str}"
            f"  +{rel_start:.0f}ms  dur={dur:.0f}ms"
        )

    agent_spans = [s for s in captured_spans if s.span_type.value == "agent"]
    llm_spans = [s for s in captured_spans if s.span_type.value == "llm"]

    print(f"\n6. Verification:")
    if agent_spans:
        print(f"   ✓ Agent span: {agent_spans[0].name}")
    else:
        print("   ✗ No agent span found")

    print(f"   LLM spans: {len(llm_spans)}")
    for ls in llm_spans:
        print(
            f"     - model={ls.attributes.get('model','?')}, "
            f"provider={ls.attributes.get('provider','?')}, "
            f"tokens_in={ls.attributes.get('tokens_input','?')}, "
            f"tokens_out={ls.attributes.get('tokens_output','?')}"
        )

    # ---------------------------------------------------------------
    # 7. Generate intentional LLM errors
    # ---------------------------------------------------------------
    print("\n7. Generating intentional LLM errors...")
    from rastir import wrap
    from rastir.context import set_current_agent, reset_current_agent

    agent_token = set_current_agent("bedrock_claude_agent")
    try:
        bad_llm = ChatBedrockConverse(
            model="us.anthropic.claude-nonexistent-v99:0",
            region_name="us-east-1",
            temperature=0,
        )
        wrapped_bad = wrap(bad_llm, span_type="llm")
        for attempt in range(2):
            try:
                await wrapped_bad.ainvoke("test error")
            except Exception as e:
                print(f"   ✓ Error {attempt+1}: {type(e).__name__}: {str(e)[:80]}")
    finally:
        reset_current_agent(agent_token)

    print("\n" + "=" * 60)
    print("Test complete! Waiting 3s for spans to flush...")
    print("=" * 60)
    await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(run_test())
