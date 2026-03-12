"""Evaluation pipeline e2e scenarios.

Tests the server-side evaluation pipeline by sending LangGraph requests
with evaluation_enabled=True. The pipeline:

  1. Client sends LLM spans with prompt_text and completion_text
  2. Server samples ~20% (configurable) for judge evaluation
  3. Judge LLM scores the response for accuracy/relevance
  4. Evaluation results appear in Grafana evaluation dashboard

  ┌────────────────────────────────────────────────────────────────────┐
  │  Mode                         │ Requests │ Providers │ Sampling  │
  ├───────────────────────────────┼──────────┼───────────┼───────────┤
  │ full (default, --count 50)    │ 50       │ Gemini +  │ 20%       │
  │                               │          │ OpenAI    │ (server)  │
  │ quick (--count 2)             │ 2        │ Gemini +  │ 100%      │
  │                               │          │ OpenAI    │ (sanity)  │
  └────────────────────────────────────────────────────────────────────┘

  Request matrix (50 total for full mode):
    - Requests  1-13: Gemini 2.5 Flash, prompt-only
    - Requests 14-25: GPT-4o-mini, prompt-only
    - Requests 26-38: Gemini 2.5 Flash, context + prompt
    - Requests 39-50: GPT-4o-mini, context + prompt

  Server-side configuration expected:
    evaluation:
      enabled: true
      default_sample_rate: 0.2
      judge_model: "gemini-2.0-pro"
      judge_provider: "gemini"

  Sources consolidated:
    - test_evaluation_e2e.py   (50 requests, 2 providers)
    - test_evaluation_quick.py (2 requests, 100% sampling)
"""

from __future__ import annotations

import asyncio
import time
from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages

from tests.e2e.common import (
    TestResults,
    captured_spans,
    clear_captured_spans,
    require_gemini_key,
    require_openai_key,
)
from rastir import langgraph_agent


# ---------------------------------------------------------------------------
# Graph state + builder (LLM-only, no tools — evaluation is about
# prompt/completion text, not tool usage)
# ---------------------------------------------------------------------------
class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


def build_eval_graph(llm):
    """Build a simple LLM-only StateGraph for evaluation testing."""
    llm_bound = llm.bind_tools([])

    async def llm_node(state: AgentState) -> AgentState:
        response = await llm_bound.ainvoke(state["messages"])
        return {"messages": [response]}

    graph = StateGraph(AgentState)
    graph.add_node("agent", llm_node)
    graph.set_entry_point("agent")
    graph.add_edge("agent", END)
    return graph.compile()


# ---------------------------------------------------------------------------
# Prompts — 25 prompt-only + 25 context-and-prompt
# These cover a range of topics: science, tech, history, programming
# ---------------------------------------------------------------------------
PROMPT_ONLY = [
    "What are the three laws of thermodynamics?",
    "Explain how photosynthesis works in simple terms.",
    "What is the difference between TCP and UDP?",
    "Describe the water cycle in four steps.",
    "What causes aurora borealis?",
    "How does a transistor work?",
    "What is the Pythagorean theorem and why is it important?",
    "Explain the concept of supply and demand in economics.",
    "What is CRISPR and how does it edit genes?",
    "How do vaccines work to protect against disease?",
    "What are the main differences between Python and JavaScript?",
    "Explain the greenhouse effect in three sentences.",
    "What is machine learning and how does it differ from traditional programming?",
    "How does GPS determine your location?",
    "What causes tides in the ocean?",
    "Explain what an API is to a non-technical person.",
    "What is the difference between DNA and RNA?",
    "How does a blockchain work?",
    "What are the four fundamental forces of nature?",
    "Explain the concept of recursion with a simple example.",
    "What is quantum entanglement?",
    "How do antibiotics work against bacteria?",
    "What is the difference between a compiler and an interpreter?",
    "Explain how solar panels convert sunlight to electricity.",
    "What are the stages of the software development lifecycle?",
]

CONTEXT_AND_PROMPT = [
    {"context": "The Amazon rainforest covers 5.5 million square kilometers and produces approximately 20% of the world's oxygen. It is home to 10% of all known species on Earth. Deforestation rates have increased by 30% in the last decade.",
     "prompt": "Based on the context, what percentage of Earth's species live in the Amazon and what is happening to deforestation rates?"},
    {"context": "HTTP/2 introduced multiplexing, allowing multiple requests over a single TCP connection. It uses header compression (HPACK) and supports server push. HTTP/3 replaces TCP with QUIC, reducing connection setup latency.",
     "prompt": "What are the key improvements HTTP/2 brought over HTTP/1.1 according to the context?"},
    {"context": "The human brain contains approximately 86 billion neurons, each connected to thousands of other neurons through synapses. Neural signals travel at speeds up to 120 meters per second. The brain uses about 20% of the body's total energy.",
     "prompt": "How many neurons does the brain have and what fraction of body energy does it consume?"},
    {"context": "Kubernetes orchestrates containerized applications across a cluster of machines. It handles scaling, load balancing, and self-healing. A Pod is the smallest deployable unit. Services provide stable networking endpoints for Pods.",
     "prompt": "What is the smallest deployable unit in Kubernetes and what do Services provide?"},
    {"context": "The Great Barrier Reef stretches 2,300 km along Australia's coast. It contains over 1,500 species of fish and 400 types of coral. Rising ocean temperatures have caused three mass bleaching events since 2016.",
     "prompt": "How many fish species live in the Great Barrier Reef and what threat does it face?"},
    {"context": "BERT (Bidirectional Encoder Representations from Transformers) was introduced by Google in 2018. It uses masked language modeling and next sentence prediction during pre-training. BERT-Base has 110M parameters and BERT-Large has 340M parameters.",
     "prompt": "How many parameters does BERT-Large have and what pre-training tasks does BERT use?"},
    {"context": "The Voyager 1 spacecraft launched in 1977 and entered interstellar space in 2012. It carries a golden record with sounds and images from Earth. As of 2024, it is approximately 24 billion kilometers from Earth.",
     "prompt": "When did Voyager 1 enter interstellar space and how far is it from Earth?"},
    {"context": "PostgreSQL supports ACID transactions, MVCC concurrency control, and JSON/JSONB data types. It offers full-text search, CTEs, and window functions. Extensions like PostGIS add geospatial capabilities.",
     "prompt": "What concurrency control method does PostgreSQL use and what does PostGIS add?"},
    {"context": "The International Space Station orbits Earth at 28,000 km/h, completing one orbit every 90 minutes. It has been continuously occupied since November 2000. The station is about 109 meters wide and weighs approximately 420,000 kg.",
     "prompt": "How fast does the ISS travel and how long has it been continuously occupied?"},
    {"context": "React uses a virtual DOM to minimize direct manipulations of the real DOM. Components can be functional or class-based. React 18 introduced concurrent rendering and automatic batching. The useState and useEffect hooks are the most commonly used hooks.",
     "prompt": "What rendering improvement did React 18 introduce and what are the most common hooks?"},
    {"context": "Mitochondria are double-membraned organelles that generate ATP through oxidative phosphorylation. They contain their own DNA, inherited maternally. The electron transport chain in the inner membrane creates a proton gradient to drive ATP synthase.",
     "prompt": "How is mitochondrial DNA inherited and what drives ATP synthase?"},
    {"context": "Git uses a directed acyclic graph (DAG) to model commit history. Each commit stores a snapshot, not a diff. Branches are lightweight pointers to commits. The three-way merge algorithm compares the merge base with both branch tips.",
     "prompt": "Does Git store diffs or snapshots and how does the merge algorithm work?"},
    {"context": "The Mariana Trench reaches a depth of 11,034 meters at the Challenger Deep. Water pressure at the bottom is over 1,000 atmospheres. Despite extreme conditions, living organisms including amphipods and xenophyophores have been found there.",
     "prompt": "What is the maximum depth of the Mariana Trench and what organisms live there?"},
    {"context": "OAuth 2.0 defines four grant types: authorization code, implicit, resource owner password credentials, and client credentials. Access tokens are typically short-lived (minutes to hours). Refresh tokens can be used to obtain new access tokens without re-authentication.",
     "prompt": "What are the four OAuth 2.0 grant types and what is the purpose of refresh tokens?"},
    {"context": "Penicillin was discovered by Alexander Fleming in 1928 when he noticed mold killing bacteria on a petri dish. Mass production began during World War II. Antibiotic resistance has become a global health threat, with MRSA being one of the most concerning resistant strains.",
     "prompt": "How was penicillin discovered and what is MRSA?"},
    {"context": "MapReduce is a programming model for processing large datasets in parallel. The Map phase processes key-value pairs into intermediate pairs. The Reduce phase aggregates intermediate values by key. Hadoop is the most well-known implementation.",
     "prompt": "What are the two phases of MapReduce and what is the most well-known implementation?"},
    {"context": "The human genome contains approximately 3 billion base pairs and about 20,000 protein-coding genes. The Human Genome Project was completed in 2003 at a cost of $2.7 billion. Modern sequencing can now decode a genome for under $1,000.",
     "prompt": "How many protein-coding genes does the human genome have and what did the Human Genome Project cost?"},
    {"context": "WebAssembly (Wasm) is a binary instruction format designed for stack-based virtual machines. It enables near-native performance in web browsers. Languages like Rust, C++, and Go can compile to Wasm. WASI extends Wasm to run outside browsers.",
     "prompt": "What performance does WebAssembly achieve and what does WASI enable?"},
    {"context": "The ozone layer is found in the stratosphere between 15-35 km altitude. It absorbs 97-99% of the sun's UV radiation. The Montreal Protocol of 1987 banned CFCs, and the ozone hole has been slowly recovering since the early 2000s.",
     "prompt": "What percentage of UV radiation does the ozone layer absorb and when was the Montreal Protocol signed?"},
    {"context": "Prometheus is a time-series database designed for monitoring. It uses a pull model to scrape metrics from targets. PromQL is its query language. AlertManager handles de-duplication, grouping, and routing of alerts.",
     "prompt": "Does Prometheus use push or pull for metrics collection and what does AlertManager do?"},
    {"context": "Photovoltaic cells are made from semiconductor materials, primarily silicon. When photons strike the cell, they knock electrons loose, creating an electric current. Monocrystalline panels have 20-22% efficiency while polycrystalline panels achieve 15-17%.",
     "prompt": "What semiconductor material are most solar cells made from and what efficiency do monocrystalline panels achieve?"},
    {"context": "TCP uses a three-way handshake (SYN, SYN-ACK, ACK) to establish connections. It provides reliable, ordered delivery with flow control via sliding windows. Congestion control algorithms like CUBIC adjust the sending rate based on packet loss.",
     "prompt": "Describe TCP's connection establishment process and what congestion control algorithm is commonly used?"},
    {"context": "CRISPR-Cas9 uses guide RNA to locate specific DNA sequences. The Cas9 enzyme cuts both DNA strands at the target site. The cell's repair mechanisms then either delete or insert new genetic material. Off-target effects remain a concern for clinical applications.",
     "prompt": "What enzyme does CRISPR use to cut DNA and what is a major concern for clinical use?"},
    {"context": "Kafka is a distributed event streaming platform. It uses an append-only log with topics partitioned across brokers. Consumers track their position via offsets. Kafka guarantees at-least-once delivery by default and supports exactly-once semantics with transactions.",
     "prompt": "What data structure does Kafka use internally and what delivery guarantee does it provide by default?"},
    {"context": "The James Webb Space Telescope launched in December 2021 and orbits at L2, 1.5 million km from Earth. Its primary mirror is 6.5 meters in diameter. It observes in infrared wavelengths, allowing it to see the earliest galaxies formed after the Big Bang.",
     "prompt": "Where does the James Webb Telescope orbit and what wavelengths does it observe in?"},
]

assert len(PROMPT_ONLY) == 25
assert len(CONTEXT_AND_PROMPT) == 25


# ---------------------------------------------------------------------------
# Schedule builder
# ---------------------------------------------------------------------------
def _build_schedule(max_count: int = 50) -> list[dict]:
    """Build the request schedule: up to 50 items split across providers."""
    schedule = []

    # 1-13: Gemini, prompt-only
    for i, p in enumerate(PROMPT_ONLY[:13]):
        schedule.append({"model_key": "gemini", "label": f"gemini-prompt-{i+1}",
                         "messages": [HumanMessage(content=p)]})

    # 14-25: OpenAI, prompt-only
    for i, p in enumerate(PROMPT_ONLY[13:]):
        schedule.append({"model_key": "openai", "label": f"openai-prompt-{i+14}",
                         "messages": [HumanMessage(content=p)]})

    # 26-38: Gemini, context+prompt
    for i, cp in enumerate(CONTEXT_AND_PROMPT[:13]):
        schedule.append({"model_key": "gemini", "label": f"gemini-context-{i+26}",
                         "messages": [
                             SystemMessage(content=f"Use ONLY the following context to answer.\n\nContext:\n{cp['context']}"),
                             HumanMessage(content=cp["prompt"]),
                         ]})

    # 39-50: OpenAI, context+prompt
    for i, cp in enumerate(CONTEXT_AND_PROMPT[13:]):
        schedule.append({"model_key": "openai", "label": f"openai-context-{i+39}",
                         "messages": [
                             SystemMessage(content=f"Use ONLY the following context to answer.\n\nContext:\n{cp['context']}"),
                             HumanMessage(content=cp["prompt"]),
                         ]})

    return schedule[:max_count]


def _ts():
    return time.strftime("%H:%M:%S")


def _log(msg: str):
    print(f"[{_ts()}] {msg}", flush=True)


# ===================================================================
#  PUBLIC RUNNER
# ===================================================================

async def run_all(results: TestResults, *, count: int = 50, **_):
    """Run the evaluation pipeline e2e test.

    Args:
        count: Number of requests to send (default 50, use 2 for quick mode).
    """
    from langchain_google_genai import ChatGoogleGenerativeAI
    from langchain_openai import ChatOpenAI

    gemini_key = require_gemini_key()
    openai_key = require_openai_key()

    test_start = time.monotonic()
    _log(f"Evaluation Pipeline E2E Test — {count} requests")

    # Build LLMs
    gemini_llm = ChatGoogleGenerativeAI(
        model="gemini-2.5-flash", temperature=0.3, google_api_key=gemini_key,
    )
    openai_llm = ChatOpenAI(
        model="gpt-4o-mini", temperature=0.3, api_key=openai_key,
    )

    # Build graphs (LLM-only, no tools)
    graphs = {
        "gemini": build_eval_graph(gemini_llm),
        "openai": build_eval_graph(openai_llm),
    }

    schedule = _build_schedule(count)
    _log(f"Schedule: {len(schedule)} requests")

    successes = 0
    failures = 0
    eval_spans_count = 0

    for idx, item in enumerate(schedule, 1):
        graph = graphs[item["model_key"]]
        label = item["label"]
        messages = item["messages"]

        @langgraph_agent(agent_name="eval_e2e_agent")
        async def invoke(g, msgs):
            return await g.ainvoke({"messages": msgs})

        clear_captured_spans()
        req_start = time.monotonic()
        _log(f"  [{idx:2d}/{len(schedule)}] -> {label} ...")

        try:
            result = await invoke(graph, messages)
            req_elapsed = time.monotonic() - req_start
            final_msg = result["messages"][-1].content
            successes += 1

            # Check evaluation metadata on LLM spans
            llm_spans = [s for s in captured_spans if s.span_type.value == "llm"]
            eval_ok = sum(
                1 for ls in llm_spans
                if ls.attributes.get("evaluation_enabled") and ls.attributes.get("evaluation_types")
            )
            eval_spans_count += eval_ok

            _log(f"  [{idx:2d}/{len(schedule)}] ✓ {label:<25s} {req_elapsed:5.1f}s "
                 f"spans={len(captured_spans)} llm={len(llm_spans)} eval={eval_ok}")
        except Exception as e:
            req_elapsed = time.monotonic() - req_start
            failures += 1
            _log(f"  [{idx:2d}/{len(schedule)}] ✗ {label:<25s} {req_elapsed:5.1f}s "
                 f"ERROR: {type(e).__name__}: {str(e)[:80]}")

        await asyncio.sleep(0.5)

    # Summary
    total_elapsed = time.monotonic() - test_start
    _log(f"Done: {successes} ok, {failures} failed, {eval_spans_count} eval spans, "
         f"{total_elapsed:.1f}s total")

    if failures == 0:
        results.passed(f"Evaluation ({count} requests)")
    else:
        results.failed(f"Evaluation ({count} requests)", f"{failures} failed")

    _log("Waiting 5s for spans to flush...")
    await asyncio.sleep(5)
