"""Manual verification: LlamaIndex with 4 combinations.

TEST 1: ReActAgent + local tools (no MCP)
TEST 2: ReActAgent + MCP tools
TEST 3: FunctionAgent + local tools (no MCP)
TEST 4: FunctionAgent + MCP tools

Run:
    conda run -n llmobserve env PYTHONPATH=src \
        python tests/e2e/verify_llamaindex.py
"""
from __future__ import annotations
import asyncio, os, sys, threading, time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

OPENAI_API_KEY = (
    os.environ.get("API_OPENAI_KEY") or os.environ.get("OPENAI_API_KEY", "")
)
if not OPENAI_API_KEY:
    print("ERROR: set API_OPENAI_KEY or OPENAI_API_KEY"); sys.exit(1)

import uvicorn, httpx
from llama_index.llms.openai import OpenAI as LlamaOpenAI
from llama_index.core.agent import ReActAgent, FunctionAgent
from llama_index.core.tools import FunctionTool
from llama_index.tools.mcp import BasicMCPClient, McpToolSpec

import rastir
from rastir import configure, llamaindex_agent
from rastir.remote import traceparent_headers

configure(
    service="llamaindex-verify",
    push_url="http://localhost:8080",
    enable_cost_calculation=True,
)
from rastir.config import get_pricing_registry
pr = get_pricing_registry()
if pr:
    pr.register("openai", "gpt-4o-mini", input_price=0.15, output_price=0.60)
    pr.register("openai", "gpt-4o-mini-2024-07-18", input_price=0.15, output_price=0.60)

# ----- MCP server (background) -----
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
    uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=MCP_PORT, log_level="warning")
    ).run()


def _wait(url, timeout=10):
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


# ----- Local tools -----
def add_numbers(a: int, b: int) -> int:
    """Add two numbers together."""
    return a + b


def multiply_numbers(a: int, b: int) -> int:
    """Multiply two numbers together."""
    return a * b


def _print_spans(spans, label):
    print(f"\n   Spans ({len(spans)}):")
    for s in spans:
        model = s.attributes.get("model", "")
        provider = s.attributes.get("provider", "")
        ti = s.attributes.get("tokens_input", "")
        to = s.attributes.get("tokens_output", "")
        cost = s.attributes.get("cost_usd", "")
        tool_in = s.attributes.get("tool.input", "")
        tool_out = s.attributes.get("tool.output", "")
        agent = s.attributes.get("agent", s.attributes.get("agent_name", ""))

        extras = []
        if model:
            extras.append(f"model={model}")
        if provider:
            extras.append(f"prov={provider}")
        if ti:
            extras.append(f"ti={ti}")
        if to:
            extras.append(f"to={to}")
        if cost:
            extras.append(f"cost=${cost:.6f}")
        if tool_in:
            extras.append(f"in={str(tool_in)[:60]}")
        if tool_out:
            extras.append(f"out={str(tool_out)[:60]}")
        extra_str = " ".join(extras)
        print(f"     {s.name} ({s.span_type.value}) {extra_str}")

    agent_spans = [s for s in spans if s.span_type.value == "agent"]
    llm_spans = [s for s in spans if s.span_type.value == "llm"]
    tool_spans = [s for s in spans if s.span_type.value == "tool"]

    ok = True
    if not agent_spans:
        print(f"   ✗ [{label}] No agent span"); ok = False
    else:
        print(f"   ✓ [{label}] Agent span: {agent_spans[0].name}")

    if not llm_spans:
        print(f"   ✗ [{label}] No LLM spans"); ok = False
    else:
        all_have_tokens = all(
            s.attributes.get("tokens_input") for s in llm_spans
        )
        all_have_provider = all(
            s.attributes.get("provider") and s.attributes.get("provider") != "unknown"
            for s in llm_spans
        )
        print(f"   {'✓' if True else '✗'} [{label}] {len(llm_spans)} LLM spans")
        all_have_input = all(
            s.attributes.get("input") for s in llm_spans
        )
        all_have_output = all(
            s.attributes.get("output") for s in llm_spans
        )
        print(f"   {'✓' if all_have_tokens else '✗'} [{label}] LLM tokens: {'all present' if all_have_tokens else 'MISSING'}")
        print(f"   {'✓' if all_have_provider else '✗'} [{label}] LLM provider: {'all present' if all_have_provider else 'MISSING'}")
        print(f"   {'✓' if all_have_input else '✗'} [{label}] LLM input: {'all present' if all_have_input else 'MISSING'}")
        print(f"   {'✓' if all_have_output else '✗'} [{label}] LLM output: {'all present' if all_have_output else 'MISSING'}")
        for ls in llm_spans:
            inp = str(ls.attributes.get("input", "—"))[:80]
            out = str(ls.attributes.get("output", "—"))[:60]
            print(f"     input={inp}")
            print(f"     output={out}")
        if not all_have_tokens or not all_have_input or not all_have_output:
            ok = False

    if tool_spans:
        print(f"   ✓ [{label}] {len(tool_spans)} tool spans")
        for ts in tool_spans:
            ti = ts.attributes.get("tool.input", "—")
            to = ts.attributes.get("tool.output", "—")
            print(f"     tool.input={str(ti)[:80]}")
            print(f"     tool.output={str(to)[:60]}")
    else:
        print(f"   ✗ [{label}] No tool spans"); ok = False

    cost_spans = [s for s in llm_spans if s.attributes.get("cost_usd", 0) > 0]
    if cost_spans:
        total = sum(s.attributes.get("cost_usd", 0) for s in cost_spans)
        print(f"   ✓ [{label}] Cost: ${total:.6f} across {len(cost_spans)} spans")
    else:
        print(f"   ✗ [{label}] No cost data")

    return ok


# ----- Capture spans -----
import rastir.queue as _queue
import rastir.wrapper as _wrapper

captured_spans: list = []
_orig_enqueue = _queue.enqueue_span


def _capture(span):
    captured_spans.append(span)
    _orig_enqueue(span)


_queue.enqueue_span = _capture
_wrapper.enqueue_span = _capture


# =====================================================================
async def main():
    print("=" * 60)
    print("LlamaIndex 4-Combination Verification")
    print("=" * 60)

    # Start MCP server
    print("\nStarting MCP server...")
    t = threading.Thread(target=_start_server, daemon=True)
    t.start()
    if not _wait(MCP_URL):
        print("FAILED: MCP server did not start"); sys.exit(1)
    print(f"MCP server ready on {MCP_URL}")

    # Get MCP tools
    mcp_client = BasicMCPClient(MCP_URL, headers={})
    mcp_tool_spec = McpToolSpec(client=mcp_client)
    mcp_tools = await mcp_tool_spec.to_tool_list_async()
    print(f"MCP tools: {[t.metadata.name for t in mcp_tools]}")

    local_tools = [
        FunctionTool.from_defaults(fn=add_numbers),
        FunctionTool.from_defaults(fn=multiply_numbers),
    ]

    results = {}

    # ------------------------------------------------------------------
    # TEST 1: ReActAgent + local tools (no MCP)
    # ------------------------------------------------------------------
    print("\n" + "-" * 60)
    print("TEST 1: ReActAgent + local tools (no MCP)")
    print("-" * 60)

    llm1 = LlamaOpenAI(model="gpt-4o-mini", temperature=0, api_key=OPENAI_API_KEY)
    agent1 = ReActAgent(
        name="react-local", llm=llm1, tools=list(local_tools),
        streaming=False,
    )

    @llamaindex_agent(agent_name="react_local_agent")
    async def test1(agent):
        return await agent.run(user_msg="What is 3 + 5, then multiply the result by 2?")

    captured_spans.clear()
    result1 = await test1(agent1)
    print(f"   Result: {str(result1)[:200]}")
    results["TEST1"] = _print_spans(list(captured_spans), "TEST1")
    await asyncio.sleep(2)

    # ------------------------------------------------------------------
    # TEST 2: ReActAgent + MCP tools
    # ------------------------------------------------------------------
    print("\n" + "-" * 60)
    print("TEST 2: ReActAgent + MCP tools")
    print("-" * 60)

    llm2 = LlamaOpenAI(model="gpt-4o-mini", temperature=0, api_key=OPENAI_API_KEY)
    agent2 = ReActAgent(
        name="react-mcp", llm=llm2, tools=list(mcp_tools),
        streaming=False,
    )

    @llamaindex_agent(agent_name="react_mcp_agent")
    async def test2(agent, mcp_client):
        return await agent.run(user_msg="What is the weather in Tokyo?")

    captured_spans.clear()
    result2 = await test2(agent2, mcp_client)
    print(f"   Result: {str(result2)[:200]}")
    results["TEST2"] = _print_spans(list(captured_spans), "TEST2")
    await asyncio.sleep(2)

    # ------------------------------------------------------------------
    # TEST 3: FunctionAgent + local tools (no MCP)
    # ------------------------------------------------------------------
    print("\n" + "-" * 60)
    print("TEST 3: FunctionAgent + local tools (no MCP)")
    print("-" * 60)

    llm3 = LlamaOpenAI(model="gpt-4o-mini", temperature=0, api_key=OPENAI_API_KEY)
    agent3 = FunctionAgent(
        name="func-local", llm=llm3, tools=list(local_tools),
        streaming=False,
    )

    @llamaindex_agent(agent_name="func_local_agent")
    async def test3(agent):
        return await agent.run(user_msg="What is 7 + 9, then multiply the result by 3?")

    captured_spans.clear()
    result3 = await test3(agent3)
    print(f"   Result: {str(result3)[:200]}")
    results["TEST3"] = _print_spans(list(captured_spans), "TEST3")
    await asyncio.sleep(2)

    # ------------------------------------------------------------------
    # TEST 4: FunctionAgent + MCP tools
    # ------------------------------------------------------------------
    print("\n" + "-" * 60)
    print("TEST 4: FunctionAgent + MCP tools")
    print("-" * 60)

    llm4 = LlamaOpenAI(model="gpt-4o-mini", temperature=0, api_key=OPENAI_API_KEY)
    agent4 = FunctionAgent(
        name="func-mcp", llm=llm4, tools=list(mcp_tools),
        streaming=False,
    )

    @llamaindex_agent(agent_name="func_mcp_agent")
    async def test4(agent, mcp_client):
        return await agent.run(user_msg="What is the population of London?")

    captured_spans.clear()
    result4 = await test4(agent4, mcp_client)
    print(f"   Result: {str(result4)[:200]}")
    results["TEST4"] = _print_spans(list(captured_spans), "TEST4")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for name, ok in results.items():
        print(f"  {name}: {'PASS ✓' if ok else 'FAIL ✗'}")
    print("=" * 60)
    print("Waiting 3s for spans to flush to collector...")
    await asyncio.sleep(3)
    print("Done. Check Tempo for traces.")


if __name__ == "__main__":
    asyncio.run(main())
