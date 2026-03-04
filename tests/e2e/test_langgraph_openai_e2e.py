"""E2E test: LangGraph manual StateGraph + OpenAI GPT-4o-mini + MCP tools.

Uses the same MCP test server as the Gemini tests but with OpenAI as the LLM
provider.  This gives us a second model/provider in the SRE dashboard so we
can verify per-LLM error budget and cost budget panels.

Requirements:
    API_OPENAI_KEY env var, langchain-openai, langgraph, langchain-mcp-adapters,
    mcp packages.

Run:
    conda run -n llmobserve PYTHONPATH=src \
        python tests/e2e/test_langgraph_openai_e2e.py
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
OPENAI_API_KEY = os.environ.get("API_OPENAI_KEY", "") or os.environ.get("OPENAI_API_KEY", "")
if not OPENAI_API_KEY:
    print("ERROR: API_OPENAI_KEY or OPENAI_API_KEY not set")
    sys.exit(1)

try:
    from langchain_openai import ChatOpenAI
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
    service="langgraph-openai-e2e",
    push_url="http://localhost:8080",
    enable_cost_calculation=True,
)

# Register OpenAI pricing (USD per 1M tokens)
from rastir.config import get_pricing_registry

_pr = get_pricing_registry()
if _pr is not None:
    _pr.register("openai", "gpt-4o-mini", input_price=0.15, output_price=0.60)

captured_spans: list = []
_orig_enqueue = None


def _capture_enqueue(span):
    """Intercept enqueue_span to capture spans for verification."""
    captured_spans.append(span)
    if _orig_enqueue:
        _orig_enqueue(span)


import rastir.queue as _queue

_orig_enqueue = _queue.enqueue_span
_queue.enqueue_span = _capture_enqueue

# ---------------------------------------------------------------------------
# MCP Server — run in background thread (different port from other tests)
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
# State definition for the manual graph
# ---------------------------------------------------------------------------
from typing import TypedDict


class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


# ---------------------------------------------------------------------------
# Build the graph manually
# ---------------------------------------------------------------------------
def build_manual_graph(llm, tools):
    """Build a LangGraph graph with explicit agent/tools nodes and edges."""

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
    print("LangGraph Manual Graph + OpenAI GPT-4o-mini + MCP E2E Test")
    print("=" * 60)

    # Start MCP server in background
    print("\n1. Starting MCP server...")
    server_thread = threading.Thread(target=_start_server, daemon=True)
    server_thread.start()

    if not _wait_for_server(MCP_URL):
        print("   FAILED: MCP server did not start")
        sys.exit(1)
    print("   MCP server ready on", MCP_URL)

    # Create the agent
    print("\n2. Setting up LangGraph with OpenAI GPT-4o-mini...")
    llm = ChatOpenAI(
        model="gpt-4o-mini",
        temperature=0,
        api_key=OPENAI_API_KEY,
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

    @langgraph_agent(agent_name="openai_agent")
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

    # Print result
    final_msg = result["messages"][-1].content
    print(f"\n4. Agent response:\n   {final_msg[:300]}...")

    # Analyze spans
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

    # Categorise spans
    agent_spans = [s for s in captured_spans if s.span_type.value == "agent"]
    llm_spans = [s for s in captured_spans if s.span_type.value == "llm"]
    tool_spans = [s for s in captured_spans if s.span_type.value == "tool"]

    # Check agent propagation
    spans_with_agent = [
        s for s in captured_spans
        if s.attributes.get("agent") and s.span_type.value != "agent"
    ]

    print(f"\n6. Verification:")

    if agent_spans:
        print(f"   ✓ Agent span: {agent_spans[0].name}")
    else:
        print("   ✗ No agent span found")

    print(f"\n   LLM spans: {len(llm_spans)}")
    for ls in llm_spans:
        model = ls.attributes.get("model", "MISSING")
        provider = ls.attributes.get("provider", "MISSING")
        tokens_in = ls.attributes.get("tokens_input", "MISSING")
        tokens_out = ls.attributes.get("tokens_output", "MISSING")
        agent_attr = ls.attributes.get("agent", "MISSING")
        cost = ls.attributes.get("cost_usd", 0)
        print(
            f"     - model={model}, provider={provider}, "
            f"tokens_in={tokens_in}, tokens_out={tokens_out}, "
            f"agent={agent_attr}, cost=${cost:.6f}"
        )

    # Agent propagation check
    print(f"\n   Agent propagation: {len(spans_with_agent)}/{len(captured_spans)-len(agent_spans)} child spans have agent attribute")
    if spans_with_agent:
        print("   ✓ Agent propagated to child spans!")
    else:
        print("   ✗ Agent NOT propagated to child spans")

    # Cost check
    spans_with_cost = [s for s in llm_spans if s.attributes.get("cost_usd", 0) > 0]
    if spans_with_cost:
        total_cost = sum(s.attributes.get("cost_usd", 0) for s in spans_with_cost)
        print(f"   ✓ Cost calculated: ${total_cost:.6f} across {len(spans_with_cost)} LLM spans")
    else:
        print("   ✗ No cost data on LLM spans")

    # ---------------------------------------------------------------
    # 7. Generate intentional LLM errors for SRE dashboard testing
    # ---------------------------------------------------------------
    print("\n7. Generating intentional LLM errors...")
    from rastir import wrap
    from rastir.context import set_current_agent, reset_current_agent

    # Set agent context so errors are attributed to this agent
    agent_token = set_current_agent("openai_agent")
    try:
        # Error: OpenAI with invalid model name
        bad_llm = ChatOpenAI(
            model="gpt-nonexistent-model",
            temperature=0,
            api_key=OPENAI_API_KEY,
        )
        wrapped_bad = wrap(bad_llm, span_type="llm")
        for attempt in range(3):
            try:
                await wrapped_bad.ainvoke("test")
            except Exception as e:
                print(f"   ✓ Error {attempt+1} captured: {type(e).__name__}: {str(e)[:80]}")
    finally:
        reset_current_agent(agent_token)

    print("\n" + "=" * 60)
    print("Test complete! Waiting 3s for spans to flush to collector...")
    print("=" * 60)
    await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(run_test())
