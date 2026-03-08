"""Quick evaluation sanity test — 2 requests with 100% server-side sampling.

Sends just 2 LangGraph requests (1 Gemini, 1 OpenAI) so every span
gets evaluated by the judge.  Use this to validate the evaluation
dashboard before running the full 50-request suite.

Requirements:
    GOOGLE_API_KEY and API_OPENAI_KEY env vars.
    Port-forward to the Rastir server on localhost:8080.

Run:
    conda run -n llmobserve PYTHONPATH=src \
        python tests/e2e/test_evaluation_quick.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "") or os.environ.get("GEMINI_API_KEY", "")
OPENAI_API_KEY = os.environ.get("API_OPENAI_KEY", "") or os.environ.get("OPENAI_API_KEY", "")

missing = []
if not GOOGLE_API_KEY:
    missing.append("GOOGLE_API_KEY or GEMINI_API_KEY")
if not OPENAI_API_KEY:
    missing.append("API_OPENAI_KEY or OPENAI_API_KEY")
if missing:
    print(f"ERROR: Missing env vars: {', '.join(missing)}")
    sys.exit(1)

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from typing import Annotated, TypedDict
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages

from rastir import configure, langgraph_agent
from rastir.config import get_pricing_registry

configure(
    service="evaluation-quick-test",
    push_url="http://localhost:8080",
    evaluation_enabled=True,
    enable_cost_calculation=True,
)

_pr = get_pricing_registry()
if _pr is not None:
    _pr.register("gemini", "gemini-2.5-flash", input_price=0.15, output_price=0.60)
    _pr.register("openai", "gpt-4o-mini", input_price=0.15, output_price=0.60)

# Span capture
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


def _ts():
    return time.strftime("%H:%M:%S")


def _log(msg: str):
    print(f"[{_ts()}] {msg}", flush=True)


class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


def build_graph(llm):
    llm_bound = llm.bind_tools([])

    async def llm_node(state: AgentState) -> AgentState:
        response = await llm_bound.ainvoke(state["messages"])
        return {"messages": [response]}

    graph = StateGraph(AgentState)
    graph.add_node("agent", llm_node)
    graph.set_entry_point("agent")
    graph.add_edge("agent", END)
    return graph.compile()


REQUESTS = [
    {
        "model_key": "gemini",
        "label": "gemini-context-1",
        "messages": [
            SystemMessage(content="Use ONLY the following context.\n\nContext:\n"
                "The Amazon rainforest covers 5.5 million sq km and produces "
                "about 20% of the world's oxygen. It is home to 10% of all "
                "known species on Earth."),
            HumanMessage(content="What percentage of Earth's species live in the Amazon?"),
        ],
    },
    {
        "model_key": "openai",
        "label": "openai-prompt-1",
        "messages": [
            HumanMessage(content="What are the three laws of thermodynamics?"),
        ],
    },
]


async def run_test():
    test_start = time.monotonic()
    print("=" * 60, flush=True)
    _log("Evaluation Quick Test — 2 requests (expect 100% sampling)")
    print("=" * 60, flush=True)

    gemini_llm = ChatGoogleGenerativeAI(
        model="gemini-2.5-flash", temperature=0.3, google_api_key=GOOGLE_API_KEY,
    )
    openai_llm = ChatOpenAI(
        model="gpt-4o-mini", temperature=0.3, api_key=OPENAI_API_KEY,
    )
    graphs = {"gemini": build_graph(gemini_llm), "openai": build_graph(openai_llm)}

    for idx, item in enumerate(REQUESTS, 1):
        graph = graphs[item["model_key"]]
        label = item["label"]

        @langgraph_agent(agent_name="eval_quick_agent")
        async def invoke(g, msgs):
            return await g.ainvoke({"messages": msgs})

        captured_spans.clear()
        req_start = time.monotonic()
        _log(f"  [{idx}/2] → {label} ...")

        try:
            result = await invoke(graph, item["messages"])
            elapsed = time.monotonic() - req_start
            final = result["messages"][-1].content

            llm_spans = [s for s in captured_spans if s.span_type.value == "llm"]
            eval_ok = sum(
                1 for s in llm_spans
                if s.attributes.get("evaluation_enabled") and s.attributes.get("evaluation_types")
            )
            has_prompt = any(s.attributes.get("prompt_text") for s in llm_spans)
            has_completion = any(s.attributes.get("completion_text") for s in llm_spans)

            _log(
                f"  [{idx}/2] ✓ {label:<25s} {elapsed:5.1f}s  "
                f"spans={len(captured_spans)} llm={len(llm_spans)} "
                f"eval={eval_ok}/{len(llm_spans)} "
                f"prompt={'✓' if has_prompt else '✗'} "
                f"completion={'✓' if has_completion else '✗'} "
                f'"{final[:50].replace(chr(10), " ")}..."'
            )
        except Exception as e:
            elapsed = time.monotonic() - req_start
            _log(f"  [{idx}/2] ✗ {label:<25s} {elapsed:5.1f}s  ERROR: {e}")

        await asyncio.sleep(0.5)

    total = time.monotonic() - test_start
    _log(f"Done in {total:.1f}s. Waiting 5s for spans to flush...")
    await asyncio.sleep(5)
    _log("Complete. Check the Evaluation dashboard in Grafana.")


if __name__ == "__main__":
    asyncio.run(run_test())
