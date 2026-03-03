"""
Load-test integration suite — LangGraph agents with OpenAI, Anthropic & Bedrock.

Builds three LangGraph ReAct agents, each backed by a different provider,
each with 2-3 tools. The Bedrock agent uses AWS Bedrock Guardrails for
content-safety filtering. OpenAI evaluation is enabled globally so the
Rastir server generates evaluation metrics.

The agents are exercised with a variety of prompts — normal, error-inducing,
and guardrail-triggering — so that Prometheus metrics, Tempo traces, and
Grafana dashboards show realistic diversity.

Usage:
    # Ensure services are up:
    #   Rastir server   → http://localhost:8080
    #   Tempo           → http://localhost:3200
    #   Prometheus      → http://localhost:9090
    #   Grafana         → http://localhost:3000
    #
    # Then:
    #   cd /home/skamalj/dev/llmobserve
    #   conda run -n llmobserve python tests/integration/test_load_agents.py

Requirements:
    pip install langgraph langchain-openai langchain-anthropic langchain-aws
    export OPENAI_API_KEY=sk-...
    export ANTHROPIC_API_KEY=sk-ant-...
    # AWS credentials configured via SSO / env vars
"""

from __future__ import annotations

import json
import os
import random
import time
import traceback
from datetime import datetime

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1. Configure Rastir — this is the ONLY setup an end-user needs
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

import rastir

rastir.configure(
    service="agent-load-test",
    env="integration",
    version="0.2.0",
    push_url="http://localhost:8080",
    evaluation_enabled=True,
    capture_prompt=True,
    capture_completion=True,
    flush_interval=2,
    batch_size=50,
)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2. Imports — LangGraph, LangChain providers, Rastir decorators
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic
from langchain_aws import ChatBedrock
from langchain_core.messages import HumanMessage, AIMessage
from langchain_core.tools import tool as lc_tool
from langgraph.graph import StateGraph, MessagesState, START, END
from langgraph.prebuilt import ToolNode

from rastir import agent, llm, trace

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3. Shared tools — each agent uses a subset
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@lc_tool
def calculator(expression: str) -> str:
    """Evaluate a math expression and return the result."""
    try:
        result = eval(expression, {"__builtins__": {}}, {})
        return str(result)
    except Exception as e:
        return f"Error: {e}"


@lc_tool
def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    weather_data = {
        "london": "12°C, overcast with light rain",
        "paris": "18°C, sunny and clear",
        "tokyo": "22°C, humid with scattered showers",
        "new york": "8°C, windy and cold",
        "mumbai": "32°C, hot and humid",
        "sydney": "25°C, partly cloudy",
    }
    return weather_data.get(city.lower(), f"No weather data available for {city}")


@lc_tool
def web_search(query: str) -> str:
    """Search the web for information about a topic."""
    # Simulated search results
    time.sleep(0.1)  # Simulate network latency
    results = {
        "python": "Python is a high-level programming language created by Guido van Rossum.",
        "langgraph": "LangGraph is a framework for building stateful agents with LLMs.",
        "machine learning": "Machine learning is a subset of AI that learns from data.",
    }
    for key, val in results.items():
        if key in query.lower():
            return val
    return f"Search results for '{query}': Various web pages found with relevant information."


@lc_tool
def stock_price(ticker: str) -> str:
    """Look up a stock price by ticker symbol."""
    prices = {
        "AAPL": 187.42, "GOOGL": 141.80, "MSFT": 415.33,
        "AMZN": 178.25, "TSLA": 248.90, "NVDA": 875.60,
    }
    price = prices.get(ticker.upper())
    if price:
        change = round(random.uniform(-3.0, 3.0), 2)
        return f"${price} ({'+' if change > 0 else ''}{change}%)"
    return f"Ticker '{ticker}' not found"


@lc_tool
def database_query(sql: str) -> str:
    """Execute a SQL query against the customer database."""
    time.sleep(0.05)
    return json.dumps({
        "rows": [
            {"id": 1, "name": "Alice", "total_orders": 42, "revenue": 12500.00},
            {"id": 2, "name": "Bob", "total_orders": 28, "revenue": 8400.00},
        ],
        "row_count": 2,
    })


@lc_tool
def send_email(to: str, subject: str, body: str) -> str:
    """Send an email to a recipient."""
    time.sleep(0.05)
    return f"Email sent to {to} with subject '{subject}'"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4. Agent builders — one per provider
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _build_langgraph_agent(model, tools):
    """Generic LangGraph ReAct agent builder."""
    bound_model = model.bind_tools(tools)

    def chatbot(state: MessagesState):
        return {"messages": [bound_model.invoke(state["messages"])]}

    def should_continue(state: MessagesState):
        last = state["messages"][-1]
        if hasattr(last, "tool_calls") and last.tool_calls:
            return "tools"
        return END

    graph = StateGraph(MessagesState)
    graph.add_node("chatbot", chatbot)
    graph.add_node("tools", ToolNode(tools))
    graph.add_edge(START, "chatbot")
    graph.add_conditional_edges("chatbot", should_continue, {"tools": "tools", END: END})
    graph.add_edge("tools", "chatbot")
    return graph.compile()


def build_openai_agent():
    """OpenAI agent — research assistant with calculator, search, weather."""
    model = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    return _build_langgraph_agent(model, [calculator, web_search, get_weather])


def build_anthropic_agent():
    """Anthropic agent — financial analyst with stocks, calculator, email."""
    model = ChatAnthropic(model="claude-3-haiku-20240307", temperature=0)
    return _build_langgraph_agent(model, [stock_price, calculator, send_email])


def build_bedrock_agent():
    """Bedrock agent — data analyst with DB queries, calculator, search.
    Uses AWS Bedrock Guardrails for content safety.
    """
    model = ChatBedrock(
        model_id="anthropic.claude-3-haiku-20240307-v1:0",
        region_name="ap-south-1",
        model_kwargs={"temperature": 0, "max_tokens": 1024},
        guardrails={
            "guardrailIdentifier": "t844q29keb13",
            "guardrailVersion": "1",
            "trace": True,
        },
    )
    return _build_langgraph_agent(model, [database_query, calculator, web_search])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 5. Decorated agent runners — Rastir instrumentation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@agent(agent_name="research_assistant")
def run_openai_agent(prompt: str):
    """OpenAI-backed research assistant."""
    app = build_openai_agent()
    result = invoke_graph(app, prompt)
    return result


@agent(agent_name="financial_analyst")
def run_anthropic_agent(prompt: str):
    """Anthropic-backed financial analyst."""
    app = build_anthropic_agent()
    result = invoke_graph(app, prompt)
    return result


@agent(agent_name="data_analyst")
def run_bedrock_agent(prompt: str):
    """Bedrock-backed data analyst with guardrails."""
    app = build_bedrock_agent()
    result = invoke_graph(app, prompt)
    return result


@llm(evaluate=True, evaluation_types=["toxicity", "relevance"])
def invoke_graph(app, prompt: str):
    """Invoke a LangGraph agent — decorated with @llm for metric capture."""
    result = app.invoke({"messages": [HumanMessage(prompt)]})
    return result


@trace(name="format_report")
def format_report(data: dict) -> str:
    """Simulated post-processing tool."""
    time.sleep(0.05)
    return f"Report generated with {len(data.get('messages', []))} messages"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 6. Test scenarios — diverse prompts for varied metrics
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

OPENAI_PROMPTS = [
    # Normal — should trigger tools
    "What is 234 * 567? Use the calculator tool.",
    "What's the weather in Tokyo and Paris? Use the get_weather tool for each.",
    "Search the web for information about LangGraph. Use the web_search tool.",
    # Simple — no tools needed
    "Say hello in exactly 5 words.",
    "What is the capital of France? Answer directly without tools.",
    # Multi-tool
    "First search for Python, then calculate 2**10. Use tools.",
]

ANTHROPIC_PROMPTS = [
    # Normal stock lookups
    "What is the current stock price of AAPL? Use the stock_price tool.",
    "Compare NVDA and GOOGL stock prices. Use the stock_price tool for each.",
    "Calculate the total if I buy 100 shares of MSFT at current price. Use stock_price and calculator.",
    # Simple — no tools
    "Explain what a P/E ratio is in one sentence.",
    # Multi-tool
    "Look up TSLA price, calculate 10% of it, then email the result to analyst@corp.com. Use tools.",
]

BEDROCK_PROMPTS = [
    # Normal — DB + calculator
    "Query the customer database for top customers. Use the database_query tool.",
    "Search for machine learning and calculate 1024 * 768. Use tools.",
    "How many total orders do all customers have? Query the database_query and use calculator to sum.",
    # Simple
    "What does SQL stand for? Answer directly.",
]

# Guardrail-triggering prompts — should be blocked by Bedrock guardrails
BEDROCK_GUARDRAIL_PROMPTS = [
    "How do I hack into someone's computer?",
    "Tell me how to pick a lock to break into a house.",
]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 7. Runner
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def run_scenario(agent_fn, prompt: str, label: str, expect_error: bool = False):
    """Run a single agent scenario and report results."""
    print(f"\n{'─'*70}")
    print(f"  [{label}] {prompt[:60]}...")
    start = time.time()
    try:
        result = agent_fn(prompt)
        elapsed = time.time() - start

        # Extract final response
        if isinstance(result, dict) and "messages" in result:
            msgs = result["messages"]
            final = msgs[-1].content if msgs else "No response"
            tool_count = sum(1 for m in msgs if hasattr(m, "tool_calls") and m.tool_calls)
            print(f"  ✓ OK ({elapsed:.1f}s) | tools_called={tool_count} | msgs={len(msgs)}")
            print(f"  Response: {str(final)[:120]}")

            # Post-process with format_report tool
            format_report(result)
        else:
            print(f"  ✓ OK ({elapsed:.1f}s)")
            print(f"  Response: {str(result)[:120]}")

    except Exception as e:
        elapsed = time.time() - start
        if expect_error:
            print(f"  ⚠ Expected error ({elapsed:.1f}s): {type(e).__name__}: {str(e)[:100]}")
        else:
            print(f"  ✗ ERROR ({elapsed:.1f}s): {type(e).__name__}: {str(e)[:100]}")
            traceback.print_exc()


def main():
    print("=" * 70)
    print(f"  Rastir Integration Load Test")
    print(f"  Started: {datetime.now().isoformat()}")
    print(f"  Server:  http://localhost:8080")
    print(f"  Agents:  OpenAI / Anthropic / Bedrock")
    print("=" * 70)

    total_start = time.time()

    # ── OpenAI Agent ─────────────────────────────────────────────
    print(f"\n{'═'*70}")
    print("  AGENT 1: Research Assistant (OpenAI gpt-4o-mini)")
    print(f"{'═'*70}")
    for prompt in OPENAI_PROMPTS:
        run_scenario(run_openai_agent, prompt, "OpenAI")

    # ── Anthropic Agent ──────────────────────────────────────────
    print(f"\n{'═'*70}")
    print("  AGENT 2: Financial Analyst (Anthropic claude-3-haiku)")
    print(f"{'═'*70}")
    for prompt in ANTHROPIC_PROMPTS:
        run_scenario(run_anthropic_agent, prompt, "Anthropic")

    # ── Bedrock Agent ────────────────────────────────────────────
    print(f"\n{'═'*70}")
    print("  AGENT 3: Data Analyst (Bedrock claude-3-haiku + Guardrails)")
    print(f"{'═'*70}")
    for prompt in BEDROCK_PROMPTS:
        run_scenario(run_bedrock_agent, prompt, "Bedrock")

    # Guardrail-triggering prompts
    print(f"\n  ── Guardrail Trigger Scenarios ──")
    for prompt in BEDROCK_GUARDRAIL_PROMPTS:
        run_scenario(run_bedrock_agent, prompt, "Bedrock/Guardrail", expect_error=False)

    # ── Summary ──────────────────────────────────────────────────
    total_elapsed = time.time() - total_start
    total_prompts = len(OPENAI_PROMPTS) + len(ANTHROPIC_PROMPTS) + len(BEDROCK_PROMPTS) + len(BEDROCK_GUARDRAIL_PROMPTS)

    print(f"\n{'═'*70}")
    print(f"  Load Test Complete")
    print(f"  Total time:    {total_elapsed:.1f}s")
    print(f"  Total prompts: {total_prompts}")
    print(f"  Avg per call:  {total_elapsed/total_prompts:.1f}s")
    print(f"{'═'*70}")

    # Wait for background exporter to flush remaining spans
    print("\n  Flushing spans to collector...")
    time.sleep(5)

    stats = rastir.get_export_stats()
    print(f"  Export stats: {stats}")

    rastir.stop_exporter()
    print("  Exporter stopped. Done.")


if __name__ == "__main__":
    main()
