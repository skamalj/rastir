"""E2E test: CrewAI + Bedrock Claude + MCP tools.

Uses CrewAI with ChatBedrockConverse (Claude) as the LLM provider via
LiteLLM's bedrock/ prefix. This gives us CrewAI+Bedrock data in dashboards.

Requirements:
    AWS credentials (SSO or env), crewai, langchain-aws, mcp packages.

Run:
    conda run -n llmobserve PYTHONPATH=src \
        python tests/e2e/test_crewai_bedrock_e2e.py
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
    from crewai import Agent, Task, Crew, LLM
    from crewai.tools import tool as crewai_tool
except ImportError as e:
    print(f"ERROR: Missing dependency: {e}")
    sys.exit(1)

try:
    import httpx
    import uvicorn
except ImportError as e:
    print(f"ERROR: Missing dependency: {e}")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Rastir setup
# ---------------------------------------------------------------------------
import rastir
from rastir import configure, crew_kickoff
from rastir.remote import traceparent_headers

configure(
    service="crewai-bedrock-e2e",
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
import rastir.wrapper as _wrapper

_orig_enqueue = _queue.enqueue_span
_queue.enqueue_span = _capture_enqueue
_wrapper.enqueue_span = _capture_enqueue

# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------
MCP_PORT = 19884
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
# CrewAI tools — HTTP calls to MCP test server
# ---------------------------------------------------------------------------

@crewai_tool
def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    with httpx.Client(timeout=10) as c:
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "tools/call",
            "params": {"name": "get_weather", "arguments": {"city": city}},
        }
        hdrs = {"Accept": "application/json", **traceparent_headers()}
        r = c.post(MCP_URL, json=payload, headers=hdrs)
        data = r.json()
        content = data.get("result", {}).get("content", [{}])
        return content[0].get("text", str(data)) if content else str(data)


@crewai_tool
def get_population(city: str) -> str:
    """Get the approximate population of a city."""
    with httpx.Client(timeout=10) as c:
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "tools/call",
            "params": {"name": "get_population", "arguments": {"city": city}},
        }
        hdrs = {"Accept": "application/json", **traceparent_headers()}
        r = c.post(MCP_URL, json=payload, headers=hdrs)
        data = r.json()
        content = data.get("result", {}).get("content", [{}])
        return content[0].get("text", str(data)) if content else str(data)


@crewai_tool
def get_timezone(city: str) -> str:
    """Get the timezone of a city."""
    with httpx.Client(timeout=10) as c:
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "tools/call",
            "params": {"name": "get_timezone", "arguments": {"city": city}},
        }
        hdrs = {"Accept": "application/json", **traceparent_headers()}
        r = c.post(MCP_URL, json=payload, headers=hdrs)
        data = r.json()
        content = data.get("result", {}).get("content", [{}])
        return content[0].get("text", str(data)) if content else str(data)


# ---------------------------------------------------------------------------
# Main test
# ---------------------------------------------------------------------------
async def run_test():
    print("=" * 60)
    print("CrewAI + Bedrock Claude Sonnet 4 + MCP E2E Test")
    print("=" * 60)

    print("\n1. Starting MCP server...")
    server_thread = threading.Thread(target=_start_server, daemon=True)
    server_thread.start()

    if not _wait_for_server(MCP_URL):
        print("   FAILED: MCP server did not start")
        sys.exit(1)
    print("   MCP server ready on", MCP_URL)

    print("\n2. Setting up CrewAI with Bedrock Claude...")
    llm = LLM(
        model="bedrock/us.anthropic.claude-sonnet-4-20250514-v1:0",
        temperature=0,
    )

    agent = Agent(
        role="Geography Research Analyst",
        goal="Provide accurate weather, population, and timezone data for cities",
        backstory="You are a research analyst who uses tools to find factual data.",
        llm=llm,
        tools=[get_weather, get_population, get_timezone],
        verbose=True,
        max_iter=5,
    )

    task = Task(
        description=(
            "Tell me the weather in Paris, the population of Tokyo, "
            "and the timezone of London."
        ),
        expected_output=(
            "A concise report with: (1) Paris weather, "
            "(2) Tokyo population, (3) London timezone."
        ),
        agent=agent,
    )

    crew = Crew(agents=[agent], tasks=[task], verbose=True)

    @crew_kickoff(agent_name="crewai_bedrock_agent")
    def run(crew):
        return crew.kickoff()

    print("\n3. Running crew...")
    captured_spans.clear()
    result = run(crew)

    raw = getattr(result, "raw", str(result))
    print(f"\n4. Crew result:\n   {str(raw)[:300]}...")

    print(f"\n5. Captured {len(captured_spans)} spans:")
    if captured_spans:
        t0 = min(s.start_time for s in captured_spans)
    else:
        t0 = 0
    for s in captured_spans:
        agent_attr = s.attributes.get("agent_name", s.attributes.get("agent", ""))
        agent_str = f" agent={agent_attr}" if agent_attr else ""
        rel_start = (s.start_time - t0) * 1000
        dur = (s.end_time - s.start_time) * 1000 if s.end_time else 0
        print(
            f"   - {s.name} ({s.span_type.value}){agent_str}"
            f"  +{rel_start:.0f}ms  dur={dur:.0f}ms"
        )

    agent_spans = [s for s in captured_spans if s.span_type.value == "agent"]
    llm_spans = [s for s in captured_spans if s.span_type.value == "llm"]
    tool_spans = [s for s in captured_spans if s.span_type.value == "tool"]

    print(f"\n6. Verification:")
    if agent_spans:
        print(f"   ✓ Agent span: {agent_spans[0].name}")
    else:
        print("   ✗ No agent span found")
    print(f"   LLM spans: {len(llm_spans)}")
    print(f"   Tool spans: {len(tool_spans)}")

    # ---------------------------------------------------------------
    # 7. Generate intentional LLM errors
    # ---------------------------------------------------------------
    print("\n7. Generating intentional LLM errors...")
    from rastir import wrap
    from rastir.context import set_current_agent, reset_current_agent
    from langchain_aws import ChatBedrockConverse

    agent_token = set_current_agent("crewai_bedrock_agent")
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
