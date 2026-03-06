"""E2E test: LlamaIndex ReActAgent + Bedrock Nova Pro + MCP tools.

Uses BedrockConverse with Amazon Nova Pro as the LLM provider.
This gives us a Bedrock/Nova data point alongside the OpenAI tests.

Requirements:
    AWS credentials (SSO or env), llama-index-core, llama-index-llms-bedrock-converse,
    llama-index-tools-mcp, mcp packages.

Run:
    conda run -n llmobserve PYTHONPATH=src \
        python tests/e2e/test_llamaindex_bedrock_e2e.py
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
try:
    from llama_index.llms.bedrock_converse import BedrockConverse
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
# Rastir setup
# ---------------------------------------------------------------------------
import rastir
from rastir import configure, llamaindex_agent

configure(
    service="llamaindex-bedrock-e2e",
    push_url="http://localhost:8080",
    enable_cost_calculation=True,
)

from rastir.config import get_pricing_registry

_pr = get_pricing_registry()
if _pr is not None:
    _pr.register("bedrock", "us.amazon.nova-pro-v1:0",
                 input_price=0.80, output_price=3.20)

captured_spans: list = []
_orig_enqueue = None


def _capture_enqueue(span):
    captured_spans.append(span)
    if _orig_enqueue:
        _orig_enqueue(span)


import rastir.queue as _queue
import rastir.wrapper as _wrapper

_orig_enqueue = _queue.enqueue_span
_queue.enqueue_span = _capture_enqueue
_wrapper.enqueue_span = _capture_enqueue

# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------
MCP_PORT = 19883
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
    print("LlamaIndex ReActAgent + Bedrock Nova Pro + MCP E2E Test")
    print("=" * 60)

    print("\n1. Starting MCP server...")
    server_thread = threading.Thread(target=_start_server, daemon=True)
    server_thread.start()

    if not _wait_for_server(MCP_URL):
        print("   FAILED: MCP server did not start")
        sys.exit(1)
    print("   MCP server ready on", MCP_URL)

    print("\n2. Getting MCP tools via LlamaIndex McpToolSpec...")
    mcp_client = BasicMCPClient(MCP_URL, headers={})
    mcp_tool_spec = McpToolSpec(client=mcp_client)
    tools = await mcp_tool_spec.to_tool_list_async()
    print(f"   Discovered {len(tools)} tools: {[t.metadata.name for t in tools]}")

    print("\n3. Setting up LlamaIndex ReActAgent with Bedrock Nova Pro...")
    llm = BedrockConverse(
        model="us.amazon.nova-pro-v1:0",
        region_name="us-east-1",
        temperature=0,
    )

    agent = ReActAgent(
        name="LlamaIndex-Bedrock-Agent",
        llm=llm,
        tools=tools,
        verbose=True,
        streaming=False,
        early_stopping_method="generate",
    )

    @llamaindex_agent(agent_name="llamaindex_bedrock_agent")
    async def invoke(agent, mcp_client):
        handler = agent.run(
            user_msg="Tell me the weather in Tokyo and the population of London.",
            max_iterations=10,
        )
        return await handler

    print("\n4. Running agent...")
    captured_spans.clear()
    result = await invoke(agent, mcp_client)

    print(f"\n5. Agent response:\n   {str(result)[:300]}...")

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

    agent_spans = [s for s in captured_spans if s.span_type.value == "agent"]
    llm_spans = [s for s in captured_spans if s.span_type.value == "llm"]
    tool_spans = [s for s in captured_spans if s.span_type.value == "tool"]

    print(f"\n7. Verification:")
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

    print(f"   Tool spans: {len(tool_spans)}")
    for ts in tool_spans:
        print(f"     - {ts.name}")

    # ---------------------------------------------------------------
    # 8. Generate intentional LLM errors
    # ---------------------------------------------------------------
    print("\n8. Generating intentional LLM errors...")
    from rastir import wrap
    from rastir.context import set_current_agent, reset_current_agent
    from llama_index.core.base.llms.types import ChatMessage

    agent_token = set_current_agent("llamaindex_bedrock_agent")
    try:
        bad_llm = BedrockConverse(
            model="us.amazon.nova-nonexistent-v99:0",
            region_name="us-east-1",
            temperature=0,
        )
        wrapped_bad = wrap(bad_llm, span_type="llm")
        for attempt in range(2):
            try:
                wrapped_bad.chat([ChatMessage(role="user", content="test")])
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
