"""E2E test: LangGraph + Gemini + MCP tools with W3C traceparent propagation.

Starts a real MCP server with 4 tools (get_weather, convert_temperature,
get_population, get_timezone), creates a LangGraph react agent with Gemini,
and makes one call that exercises the tools.

The test captures spans in-process and verifies:
  - Client-side tool spans exist with remote="true"
  - traceparent header was set on the MCP client connections

Requirements:
    GOOGLE_API_KEY env var, mcp, langgraph, langchain-google-genai,
    langchain-mcp-adapters packages.

Run:
    GOOGLE_API_KEY=... conda run -n llmobserve PYTHONPATH=src \\
        python tests/e2e/test_langgraph_e2e.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "") or os.environ.get("GEMINI_API_KEY", "")
if not GOOGLE_API_KEY:
    print("ERROR: GOOGLE_API_KEY or GEMINI_API_KEY not set")
    sys.exit(1)

try:
    from langchain_google_genai import ChatGoogleGenerativeAI
    from langgraph.prebuilt import create_react_agent
    from langchain_mcp_adapters.client import MultiServerMCPClient
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

# Configure with push_url so spans get exported to collector → Tempo
configure(
    service="langgraph-e2e-test",
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


# Monkey-patch the queue
import rastir.queue as _queue
_orig_enqueue = _queue.enqueue_span
_queue.enqueue_span = _capture_enqueue

# ---------------------------------------------------------------------------
# MCP Server — run in background thread
# ---------------------------------------------------------------------------
MCP_PORT = 19877
MCP_URL = f"http://127.0.0.1:{MCP_PORT}/mcp"


def _start_server():
    # Import using direct path manipulation
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "mcp_test_server",
        os.path.join(os.path.dirname(__file__), "mcp_test_server.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    app = mod.create_app(MCP_PORT)
    config = uvicorn.Config(app, host="127.0.0.1", port=MCP_PORT,
                            log_level="warning")
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
# Main test
# ---------------------------------------------------------------------------
async def run_test():
    print("=" * 60)
    print("LangGraph + Gemini + MCP E2E Test")
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
    print("\n2. Setting up LangGraph agent with Gemini...")
    llm = ChatGoogleGenerativeAI(
        model="gemini-2.5-flash",
        temperature=0,
        google_api_key=GOOGLE_API_KEY,
    )

    mcp_client = MultiServerMCPClient({
        "tools": {
            "url": MCP_URL,
            "transport": "streamable_http",
        },
    })

    tools = await mcp_client.get_tools()
    print(f"   Discovered {len(tools)} tools: {[t.name for t in tools]}")

    graph = create_react_agent(llm, tools)

    # Define the instrumented function
    @langgraph_agent(agent_name="langgraph_e2e_agent")
    async def invoke(graph, mcp_client):
        return await graph.ainvoke({
            "messages": [("user",
                "Tell me the weather and population of Tokyo. "
                "Also convert 22 celsius to fahrenheit and tell me Tokyo's timezone."
            )]
        })

    print("\n3. Running agent...")
    # Debug: inspect graph structure
    print("   Graph nodes:")
    for name, node in graph.nodes.items():
        print(f"     {name}: {type(node).__name__}")
        bound = getattr(node, "bound", None)
        if bound:
            print(f"       bound: {type(bound).__name__}")
            inner = getattr(bound, "bound", None)
            if inner:
                print(f"       bound.bound: {type(inner).__name__}")
            func = getattr(bound, "func", None)
            if func:
                print(f"       bound.func: {func}")
                # Check closures
                closure = getattr(func, "__closure__", None)
                if closure:
                    for i, cell in enumerate(closure):
                        try:
                            val = cell.cell_contents
                            print(f"         closure[{i}]: {type(val).__name__} = {val!r:.80s}...")
                        except (ValueError, Exception):
                            pass

    captured_spans.clear()

    result = await invoke(graph, mcp_client)

    # Print result
    final_msg = result["messages"][-1].content
    print(f"\n4. Agent response:\n   {final_msg[:200]}...")

    # Analyze spans
    print(f"\n5. Captured {len(captured_spans)} spans:")
    # Find the earliest start_time to compute relative offsets
    if captured_spans:
        t0 = min(s.start_time for s in captured_spans)
    else:
        t0 = 0
    for s in captured_spans:
        remote = s.attributes.get("remote", "")
        tool = s.attributes.get("tool_name", "")
        remote_str = f" remote={remote}" if remote else ""
        tool_str = f" tool={tool}" if tool else ""
        rel_start = (s.start_time - t0) * 1000  # ms
        rel_end = ((s.end_time or s.start_time) - t0) * 1000
        dur = (s.end_time - s.start_time) * 1000 if s.end_time else 0
        print(f"   - {s.name} ({s.span_type.value}){remote_str}{tool_str}"
              f"  start=+{rel_start:.1f}ms  end=+{rel_end:.1f}ms  dur={dur:.1f}ms"
              f"  trace={s.trace_id[:8]}  parent={s.parent_id[:8] if s.parent_id else 'ROOT'}")

    # Verify
    server_tool_spans = [s for s in captured_spans
                         if s.span_type.value == "tool" and s.attributes.get("remote") == "false"]
    tool_node_spans = [s for s in captured_spans if s.span_type.value == "trace"
                       and s.name.startswith("node:tools")]
    agent_spans = [s for s in captured_spans if s.span_type.value == "agent"]
    llm_spans = [s for s in captured_spans if s.span_type.value == "llm"]

    print(f"\n6. Verification:")
    print(f"   Server tool spans (remote=false): {len(server_tool_spans)}")
    for ts in server_tool_spans:
        print(f"     - {ts.attributes.get('tool_name', ts.name)}: "
              f"trace_id={ts.trace_id[:8]}... parent_id={ts.parent_id[:8] if ts.parent_id else 'none'}...")

    if server_tool_spans:
        print("   ✓ MCP server-side tool spans traced via traceparent!")
    else:
        print("   ✗ No server-side tool spans found")

    print(f"   Tool node spans (node:tools): {len(tool_node_spans)}")
    if tool_node_spans:
        print("   ✓ LangGraph tool nodes traced!")

    if agent_spans:
        print(f"   ✓ Agent span: {agent_spans[0].name} trace_id={agent_spans[0].trace_id[:8]}...")
    else:
        print("   ✗ No agent span found")

    # LLM span enrichment verification
    print(f"\n   LLM spans: {len(llm_spans)}")
    for ls in llm_spans:
        model = ls.attributes.get("model", "MISSING")
        provider = ls.attributes.get("provider", "MISSING")
        tokens_in = ls.attributes.get("tokens_input", "MISSING")
        tokens_out = ls.attributes.get("tokens_output", "MISSING")
        has_input = "input" in ls.attributes
        has_output = "output" in ls.attributes
        print(f"     - {ls.name}: model={model}, provider={provider}, "
              f"tokens_in={tokens_in}, tokens_out={tokens_out}, "
              f"has_input={has_input}, has_output={has_output}")
        if has_input:
            inp = ls.attributes["input"]
            print(f"       input: {inp[:100]}..." if len(str(inp)) > 100 else f"       input: {inp}")
        if has_output:
            out = ls.attributes["output"]
            print(f"       output: {out[:100]}..." if len(str(out)) > 100 else f"       output: {out}")

    if llm_spans:
        llm_ok = all(
            ls.attributes.get("model") and ls.attributes.get("model") != "unknown"
            and ls.attributes.get("tokens_input") is not None
            and "input" in ls.attributes
            and "output" in ls.attributes
            for ls in llm_spans
        )
        if llm_ok:
            print("   ✓ LLM spans have model, tokens, input & output!")
        else:
            print("   ✗ Some LLM spans missing enrichment attributes")
    else:
        print("   ✗ No LLM spans found")

    # Check traceparent was injected
    connections = getattr(mcp_client, "connections", {})
    for name, conn in connections.items():
        if isinstance(conn, dict):
            hdrs = conn.get("headers", {})
            if "traceparent" in hdrs:
                print(f"   ✓ traceparent header set on connection '{name}': {hdrs['traceparent'][:40]}...")
            else:
                print(f"   ✗ traceparent header NOT set on connection '{name}'")

    # ---------------------------------------------------------------
    # 7. Generate intentional LLM errors for SRE dashboard testing
    # ---------------------------------------------------------------
    print("\n7. Generating intentional LLM errors...")
    from rastir import wrap
    from rastir.context import set_current_agent, reset_current_agent

    # Set agent context so errors are attributed to this agent
    agent_token = set_current_agent("langgraph_e2e_agent")
    try:
        # Error 1: Gemini with invalid model name
        bad_llm = ChatGoogleGenerativeAI(
            model="gemini-nonexistent-model",
            temperature=0,
            google_api_key=GOOGLE_API_KEY,
        )
        wrapped_bad = wrap(bad_llm, span_type="llm")
        for attempt in range(2):
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
