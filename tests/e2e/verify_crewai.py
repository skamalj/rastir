"""Manual verification: CrewAI with local tools + MCP tools.

Run:
    OPENAI_API_KEY=$API_OPENAI_KEY conda run -n llmobserve env PYTHONPATH=src \
        python tests/e2e/verify_crewai.py
"""
from __future__ import annotations
import asyncio, os, sys, threading, time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

OPENAI_API_KEY = os.environ.get("API_OPENAI_KEY") or os.environ.get("OPENAI_API_KEY", "")
if not OPENAI_API_KEY:
    print("ERROR: set API_OPENAI_KEY or OPENAI_API_KEY"); sys.exit(1)
os.environ["OPENAI_API_KEY"] = OPENAI_API_KEY

import uvicorn, httpx
from crewai import Agent, Task, Crew, LLM
from crewai.tools import tool as crewai_tool

import rastir
from rastir import configure, crew_kickoff
from rastir.remote import traceparent_headers
configure(service="crewai-verify", push_url="http://localhost:8080", enable_cost_calculation=True)

from rastir.config import get_pricing_registry
pr = get_pricing_registry()
if pr: pr.register("openai", "gpt-4o-mini", input_price=0.15, output_price=0.60)

# ---------- MCP server (background) ----------
MCP_PORT = 19881
MCP_URL = f"http://127.0.0.1:{MCP_PORT}/mcp"

def _start_server():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "mcp_test_server", os.path.join(os.path.dirname(__file__), "mcp_test_server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    app = mod.create_app(MCP_PORT)
    uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=MCP_PORT, log_level="warning")).run()

def _wait(url, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with httpx.Client(timeout=2) as c:
                r = c.get(url.replace("/mcp", "/"))
                if r.status_code < 500: return True
        except: pass
        time.sleep(0.3)
    return False

print("Starting MCP server...")
threading.Thread(target=_start_server, daemon=True).start()
assert _wait(MCP_URL), "MCP server failed to start"
print(f"MCP server ready: {MCP_URL}")

# ---------- Local tools ----------
@crewai_tool
def add_numbers(a: int, b: int) -> str:
    """Add two numbers together and return the result."""
    return str(a + b)

@crewai_tool
def multiply_numbers(a: int, b: int) -> str:
    """Multiply two numbers and return the result."""
    return str(a * b)

# ---------- MCP-backed tools (HTTP POST) ----------
@crewai_tool
def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    hdrs = {"Accept": "application/json", **traceparent_headers()}
    with httpx.Client(timeout=10) as c:
        r = c.post(MCP_URL, json={"jsonrpc":"2.0","id":1,"method":"tools/call",
            "params":{"name":"get_weather","arguments":{"city":city}}},
            headers=hdrs)
        data = r.json()
        content = data.get("result",{}).get("content",[{}])
        return content[0].get("text", str(data)) if content else str(data)

@crewai_tool
def get_population(city: str) -> str:
    """Get the approximate population of a city."""
    hdrs = {"Accept": "application/json", **traceparent_headers()}
    with httpx.Client(timeout=10) as c:
        r = c.post(MCP_URL, json={"jsonrpc":"2.0","id":1,"method":"tools/call",
            "params":{"name":"get_population","arguments":{"city":city}}},
            headers=hdrs)
        data = r.json()
        content = data.get("result",{}).get("content",[{}])
        return content[0].get("text", str(data)) if content else str(data)

llm = LLM(model="openai/gpt-4o-mini", api_key=OPENAI_API_KEY, temperature=0)

# =============== TEST 1: Local tools ===============
print("\n" + "="*60)
print("TEST 1: CrewAI with LOCAL tools (add, multiply)")
print("="*60)

math_agent = Agent(
    role="Math Assistant", goal="Solve math problems using tools",
    backstory="You are a helpful math assistant.", llm=llm,
    tools=[add_numbers, multiply_numbers], verbose=False, max_iter=4)
math_task = Task(
    description="Calculate: (15 + 27) and (8 * 13). Report both results.",
    expected_output="The sum of 15+27 and the product of 8*13.",
    agent=math_agent)
math_crew = Crew(agents=[math_agent], tasks=[math_task], verbose=False)

@crew_kickoff(agent_name="math_crew")
def run_math(crew):
    return crew.kickoff()

result1 = run_math(math_crew)
print(f"Result: {getattr(result1, 'raw', str(result1))[:300]}")

# =============== TEST 2: MCP tools ===============
print("\n" + "="*60)
print("TEST 2: CrewAI with MCP-backed tools (weather, population)")
print("="*60)

city_agent = Agent(
    role="City Researcher", goal="Look up city data",
    backstory="You research city facts.", llm=llm,
    tools=[get_weather, get_population], verbose=False, max_iter=4)
city_task = Task(
    description="What is the weather in London and the population of Tokyo?",
    expected_output="London weather and Tokyo population.",
    agent=city_agent)
city_crew = Crew(agents=[city_agent], tasks=[city_task], verbose=False)

@crew_kickoff(agent_name="city_crew")
def run_city(crew):
    return crew.kickoff()

result2 = run_city(city_crew)
print(f"Result: {getattr(result2, 'raw', str(result2))[:300]}")

print("\n" + "="*60)
print("Both tests complete. Waiting 5s for spans to flush...")
print("="*60)
time.sleep(5)
print("Done! Check Tempo / Grafana for traces with service=crewai-verify")
