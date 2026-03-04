"""E2E test: LlamaIndex ReActAgent + OpenAI + MCP tools with W3C traceparent.

Starts a real MCP server with 4 tools (get_weather, convert_temperature,
get_population, get_timezone), creates a LlamaIndex ReActAgent with OpenAI,
and makes one call that exercises the tools.

The test captures spans in-process and verifies:
  - Agent span wraps the entire execution
  - LLM spans exist with model and token counts
  - Tool spans exist
  - traceparent header was set on the MCP client

Requirements:
    API_OPENAI_KEY env var, mcp, llama-index-core, llama-index-llms-openai,
    llama-index-tools-mcp packages.

Run:
    API_OPENAI_KEY=... conda run -n llmobserve PYTHONPATH=src \
        python tests/e2e/test_llamaindex_e2e.py
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
OPENAI_API_KEY = (
    os.environ.get("API_OPENAI_KEY", "")
    or os.environ.get("OPENAI_API_KEY", "")
)
if not OPENAI_API_KEY:
    print("ERROR: API_OPENAI_KEY or OPENAI_API_KEY not set")
    sys.exit(1)

try:
    from llama_index.llms.openai import OpenAI as LlamaOpenAI
    from llama_index.core.agent import ReActAgent
    from llama_index.tools.mcp import BasicMCPClient, McpToolSpec
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
from rastir import configure, llamaindex_agent

configure(
    service="llamaindex-e2e-test",
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
# MCP Server — run in background thread
# ---------------------------------------------------------------------------
MCP_PORT = 19881
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
# Main test
# ---------------------------------------------------------------------------
async def run_test():
    print("=" * 60)
    print("LlamaIndex ReActAgent + OpenAI + MCP E2E Test")
    print("=" * 60)

    # Start MCP server in background
    print("\n1. Starting MCP server...")
    server_thread = threading.Thread(target=_start_server, daemon=True)
    server_thread.start()

    if not _wait_for_server(MCP_URL):
        print("   FAILED: MCP server did not start")
        sys.exit(1)
    print("   MCP server ready on", MCP_URL)

    # Get MCP tools via LlamaIndex's BasicMCPClient + McpToolSpec
    print("\n2. Getting MCP tools via LlamaIndex McpToolSpec...")
    mcp_client = BasicMCPClient(MCP_URL, headers={})
    mcp_tool_spec = McpToolSpec(client=mcp_client)
    tools = await mcp_tool_spec.to_tool_list_async()
    print(f"   Discovered {len(tools)} tools: {[t.metadata.name for t in tools]}")

    # Set up LlamaIndex agent
    print("\n3. Setting up LlamaIndex ReActAgent with OpenAI...")
    llm = LlamaOpenAI(
        model="gpt-4o-mini",
        temperature=0,
        api_key=OPENAI_API_KEY,
    )

    agent = ReActAgent(
        name="LlamaIndex-MCP-Agent",
        llm=llm,
        tools=tools,
        verbose=True,
        early_stopping_method="generate",
    )

    # Define the instrumented function
    @llamaindex_agent(agent_name="llamaindex_e2e_agent")
    async def invoke(agent, mcp_client):
        handler = agent.run(
            user_msg="Tell me the weather in Tokyo.",
            max_iterations=10,
        )
        return await handler

    print("\n4. Running agent...")
    captured_spans.clear()
    result = await invoke(agent, mcp_client)

    # Print result
    result_str = str(result)
    print(f"\n5. Agent response:\n   {result_str[:300]}...")

    # Analyze spans
    print(f"\n6. Captured {len(captured_spans)} spans:")
    if captured_spans:
        t0 = min(s.start_time for s in captured_spans)
    else:
        t0 = 0
    for s in captured_spans:
        agent_attr = s.attributes.get("agent_name", s.attributes.get("agent", ""))
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

    print(f"\n7. Verification:")

    # Agent span
    if agent_spans:
        print(f"   ✓ Agent span: {agent_spans[0].name}")
    else:
        print("   ✗ No agent span found")

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
            for ls in llm_spans
        )
        if llm_ok:
            print("   ✓ LLM spans have model attribute!")
        else:
            print("   ✗ Some LLM spans missing model attribute")
    else:
        print("   ✗ No LLM spans found (may be captured in Grafana only)")

    # Tool spans
    print(f"\n   Tool spans: {len(tool_spans)}")
    for ts in tool_spans:
        ti = ts.attributes.get("tool.input", "—")
        to = ts.attributes.get("tool.output", "—")
        print(f"     - {ts.name}: input={str(ti)[:80]}, output={str(to)[:80]}")

    if tool_spans:
        print(f"   ✓ {len(tool_spans)} tool spans captured!")
    else:
        print("   ✗ No tool spans found")

    # Check traceparent was injected on MCP client
    hdrs = getattr(mcp_client, "headers", None) or {}
    if "traceparent" in hdrs:
        print(
            f"   ✓ traceparent header set on BasicMCPClient: "
            f"{hdrs['traceparent'][:40]}..."
        )
    else:
        print("   ✗ traceparent header NOT set on BasicMCPClient")

    # Agent propagation check
    spans_with_agent = [
        s for s in captured_spans
        if (s.attributes.get("agent_name") or s.attributes.get("agent"))
        and s.span_type.value != "agent"
    ]
    print(
        f"\n   Agent propagation: {len(spans_with_agent)}/{len(captured_spans) - len(agent_spans)} "
        f"child spans have agent attribute"
    )
    if spans_with_agent:
        print("   ✓ Agent propagated to child spans!")
    else:
        print("   ✗ Agent NOT propagated to child spans")

    # Cost check
    spans_with_cost = [s for s in llm_spans if s.attributes.get("cost_usd", 0) > 0]
    if spans_with_cost:
        total_cost = sum(s.attributes.get("cost_usd", 0) for s in spans_with_cost)
        print(
            f"   ✓ Cost calculated: ${total_cost:.6f} across "
            f"{len(spans_with_cost)} LLM spans"
        )
    else:
        print("   ✗ No cost data on LLM spans")

    # ---------------------------------------------------------------
    # 8. Generate intentional LLM errors for SRE dashboard testing
    # ---------------------------------------------------------------
    print("\n8. Generating intentional LLM errors...")
    from rastir import wrap
    from rastir.context import set_current_agent, reset_current_agent
    from llama_index.core.base.llms.types import ChatMessage

    agent_token = set_current_agent("llamaindex_e2e_agent")
    try:
        bad_llm = LlamaOpenAI(
            model="gpt-nonexistent-model",
            temperature=0,
            api_key=OPENAI_API_KEY,
        )
        wrapped_bad = wrap(bad_llm, span_type="llm")
        for attempt in range(2):
            try:
                wrapped_bad.chat([ChatMessage(role="user", content="test")])
            except Exception as e:
                print(
                    f"   ✓ Error {attempt + 1} captured: "
                    f"{type(e).__name__}: {str(e)[:80]}"
                )
    finally:
        reset_current_agent(agent_token)

    print("\n" + "=" * 60)
    print("Test complete! Waiting 3s for spans to flush to collector...")
    print("=" * 60)
    await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(run_test())
