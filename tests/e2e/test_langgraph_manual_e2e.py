"""E2E test: Manually built LangGraph StateGraph + Gemini + MCP tools.

Unlike the react-agent test, this constructs the graph by hand using
StateGraph, with explicit ``agent`` and ``tools`` nodes and conditional
edges.  Exercises the same MCP test server (4 tools).

The test captures spans in-process and verifies:
  - Agent span wraps everything
  - node:agent, node:tools trace spans appear
  - LLM spans have model, tokens, input, output
  - Tool spans have tool.input and tool.output

Requirements:
    GOOGLE_API_KEY env var, mcp, langgraph, langchain-google-genai,
    langchain-mcp-adapters packages.

Run:
    GOOGLE_API_KEY=... conda run -n llmobserve PYTHONPATH=src \\
        python tests/e2e/test_langgraph_manual_e2e.py
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
GOOGLE_API_KEY = (
    os.environ.get("GOOGLE_API_KEY", "")
    or os.environ.get("GEMINI_API_KEY", "")
)
if not GOOGLE_API_KEY:
    print("ERROR: GOOGLE_API_KEY or GEMINI_API_KEY not set")
    sys.exit(1)

try:
    from langchain_google_genai import ChatGoogleGenerativeAI
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
    service="langgraph-manual-e2e",
    push_url="http://localhost:8080",
    enable_cost_calculation=True,
    evaluation_enabled=True,
)

# Register Gemini pricing (USD per 1M tokens)
from rastir.config import get_pricing_registry
_pr = get_pricing_registry()
if _pr is not None:
    _pr.register("gemini", "gemini-2.5-flash", input_price=0.15, output_price=0.60)

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
# MCP Server — run in background thread (reuse same server, different port)
# ---------------------------------------------------------------------------
MCP_PORT = 19878
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

    # Bind tools to the model so it can generate tool_calls
    llm_with_tools = llm.bind_tools(tools)

    # Agent node: call the LLM
    async def agent_node(state: AgentState) -> AgentState:
        response = await llm_with_tools.ainvoke(state["messages"])
        return {"messages": [response]}

    # Route: if the last message has tool_calls, go to tools; else END
    def should_continue(state: AgentState) -> str:
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and getattr(last, "tool_calls", None):
            return "tools"
        return END

    # Build the StateGraph
    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", ToolNode(tools))

    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")

    return graph.compile()


# ---------------------------------------------------------------------------
# Main test
# ---------------------------------------------------------------------------
async def run_test():
    print("=" * 60)
    print("LangGraph MANUAL Graph + Gemini + MCP E2E Test")
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
    print("\n2. Setting up manual LangGraph with Gemini...")
    llm = ChatGoogleGenerativeAI(
        model="gemini-2.5-flash",
        temperature=0,
        google_api_key=GOOGLE_API_KEY,
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

    # Inspect graph structure
    print("   Graph nodes:")
    for name, node in graph.nodes.items():
        print(f"     {name}: {type(node).__name__}")
        bound = getattr(node, "bound", None)
        if bound:
            print(f"       bound: {type(bound).__name__}")

    # Define the instrumented function
    @langgraph_agent(agent_name="manual_graph_agent")
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
        remote = s.attributes.get("remote", "")
        remote_str = f" remote={remote}" if remote else ""
        tool_input = s.attributes.get("tool.input", "")
        tool_output = s.attributes.get("tool.output", "")
        ti_str = f" input={str(tool_input)[:60]}" if tool_input else ""
        to_str = f" output={str(tool_output)[:60]}" if tool_output else ""
        rel_start = (s.start_time - t0) * 1000
        dur = (s.end_time - s.start_time) * 1000 if s.end_time else 0
        print(
            f"   - {s.name} ({s.span_type.value}){remote_str}{ti_str}{to_str}"
            f"  +{rel_start:.0f}ms  dur={dur:.0f}ms"
            f"  trace={s.trace_id[:8]}  parent={s.parent_id[:8] if s.parent_id else 'ROOT'}"
        )

    # Categorise spans
    agent_spans = [s for s in captured_spans if s.span_type.value == "agent"]
    node_spans = [
        s for s in captured_spans
        if s.span_type.value == "trace" and s.name.startswith("node:")
    ]
    llm_spans = [s for s in captured_spans if s.span_type.value == "llm"]
    tool_spans = [s for s in captured_spans if s.span_type.value == "tool"]

    print(f"\n6. Verification:")

    # Agent span
    if agent_spans:
        print(f"   ✓ Agent span: {agent_spans[0].name}")
    else:
        print("   ✗ No agent span found")

    # Node spans
    node_names = sorted(set(s.name for s in node_spans))
    print(f"   Node spans ({len(node_spans)}): {node_names}")
    if any("node:agent" in n for n in node_names):
        print("   ✓ node:agent spans present")
    else:
        print("   ✗ node:agent spans missing")
    if any("node:tools" in n for n in node_names):
        print("   ✓ node:tools spans present")
    else:
        print("   ✗ node:tools spans missing")

    # LLM spans
    print(f"\n   LLM spans: {len(llm_spans)}")
    for ls in llm_spans:
        model = ls.attributes.get("model", "MISSING")
        provider = ls.attributes.get("provider", "MISSING")
        tokens_in = ls.attributes.get("tokens_input", "MISSING")
        tokens_out = ls.attributes.get("tokens_output", "MISSING")
        has_input = "input" in ls.attributes
        has_output = "output" in ls.attributes
        print(
            f"     - {ls.name}: model={model}, provider={provider}, "
            f"tokens_in={tokens_in}, tokens_out={tokens_out}, "
            f"has_input={has_input}, has_output={has_output}"
        )

    if llm_spans:
        llm_ok = all(
            ls.attributes.get("model")
            and ls.attributes.get("model") != "unknown"
            and ls.attributes.get("tokens_input") is not None
            and "input" in ls.attributes
            and "output" in ls.attributes
            for ls in llm_spans
        )
        if llm_ok:
            print("   ✓ LLM spans have model, tokens, input & output!")
        else:
            print("   ✗ Some LLM spans missing enrichment")
    else:
        print("   ✗ No LLM spans found (may be captured only in Grafana)")

    # Tool spans — verify input/output
    print(f"\n   Tool spans: {len(tool_spans)}")
    for ts in tool_spans:
        ti = ts.attributes.get("tool.input", "—")
        to = ts.attributes.get("tool.output", "—")
        print(f"     - {ts.name}: input={str(ti)[:80]}, output={str(to)[:80]}")

    tools_with_io = [
        s for s in tool_spans
        if s.attributes.get("tool.input") and s.attributes.get("tool.output")
    ]
    if tools_with_io:
        print(f"   ✓ {len(tools_with_io)} tool spans have input & output!")
    else:
        print("   ✗ No tool spans with input/output (may be in Grafana only)")

    print("\n" + "=" * 60)
    print("Test complete! Waiting 3s for spans to flush to collector...")
    print("=" * 60)
    await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(run_test())
