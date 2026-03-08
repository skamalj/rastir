"""E2E test: Evaluation pipeline with 50 LangGraph requests.

Sends 50 requests split across two LLM providers (Gemini 2.0 Flash and
GPT-4o-mini), 25 prompt-only and 25 context+prompt, with evaluation
enabled globally.  The server-side evaluation pipeline samples ~20% of
eligible spans for judge evaluation.

Request matrix (50 total):
  - Requests 1-13:  Gemini 2.0 Flash, prompt-only
  - Requests 14-25: GPT-4o-mini, prompt-only
  - Requests 26-38: Gemini 2.0 Flash, context+prompt
  - Requests 39-50: GPT-4o-mini, context+prompt

Server-side configuration expected:
  evaluation:
    enabled: true
    default_sample_rate: 0.2       # 20% of spans evaluated by judge
    judge_model: "gemini-2.0-pro"  # or another large model
    judge_provider: "gemini"

Requirements:
    GOOGLE_API_KEY and API_OPENAI_KEY env vars.
    langgraph, langchain-google-genai, langchain-openai packages.

Run:
    conda run -n llmobserve PYTHONPATH=src \
        python tests/e2e/test_evaluation_e2e.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------
GOOGLE_API_KEY = (
    os.environ.get("GOOGLE_API_KEY", "")
    or os.environ.get("GEMINI_API_KEY", "")
)
OPENAI_API_KEY = (
    os.environ.get("API_OPENAI_KEY", "")
    or os.environ.get("OPENAI_API_KEY", "")
)

missing = []
if not GOOGLE_API_KEY:
    missing.append("GOOGLE_API_KEY or GEMINI_API_KEY")
if not OPENAI_API_KEY:
    missing.append("API_OPENAI_KEY or OPENAI_API_KEY")
if missing:
    print(f"ERROR: Missing env vars: {', '.join(missing)}")
    sys.exit(1)

try:
    from langchain_google_genai import ChatGoogleGenerativeAI
    from langchain_openai import ChatOpenAI
    from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
    from typing import Annotated, TypedDict
    from langgraph.graph import StateGraph, END
    from langgraph.graph.message import add_messages
except ImportError as e:
    print(f"ERROR: Missing dependency: {e}")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Rastir setup — evaluation enabled globally
# ---------------------------------------------------------------------------
from rastir import configure, langgraph_agent

configure(
    service="evaluation-e2e-test",
    push_url="http://localhost:8080",
    evaluation_enabled=True,
    enable_cost_calculation=True,
)

# Register pricing (USD per 1M tokens)
from rastir.config import get_pricing_registry

_pr = get_pricing_registry()
if _pr is not None:
    _pr.register("gemini", "gemini-2.5-flash", input_price=0.15, output_price=0.60)
    _pr.register("openai", "gpt-4o-mini", input_price=0.15, output_price=0.60)

# Span capture for local verification
captured_spans: list = []
_orig_enqueue = None


def _capture_enqueue(span):
    captured_spans.append(span)
    if _orig_enqueue:
        _orig_enqueue(span)


import rastir.queue as _queue

_orig_enqueue = _queue.enqueue_span
_queue.enqueue_span = _capture_enqueue

# wrapper.py caches enqueue_span at import time — patch it too
import rastir.wrapper as _wrapper
_wrapper.enqueue_span = _capture_enqueue


# ---------------------------------------------------------------------------
# Prompts — 25 prompt-only + 25 context+prompt
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
    {
        "context": (
            "The Amazon rainforest covers 5.5 million square kilometers and "
            "produces approximately 20% of the world's oxygen. It is home to "
            "10% of all known species on Earth. Deforestation rates have "
            "increased by 30% in the last decade."
        ),
        "prompt": "Based on the context, what percentage of Earth's species live in the Amazon and what is happening to deforestation rates?",
    },
    {
        "context": (
            "HTTP/2 introduced multiplexing, allowing multiple requests over a "
            "single TCP connection. It uses header compression (HPACK) and "
            "supports server push. HTTP/3 replaces TCP with QUIC, reducing "
            "connection setup latency."
        ),
        "prompt": "What are the key improvements HTTP/2 brought over HTTP/1.1 according to the context?",
    },
    {
        "context": (
            "The human brain contains approximately 86 billion neurons, each "
            "connected to thousands of other neurons through synapses. Neural "
            "signals travel at speeds up to 120 meters per second. The brain "
            "uses about 20% of the body's total energy."
        ),
        "prompt": "How many neurons does the brain have and what fraction of body energy does it consume?",
    },
    {
        "context": (
            "Kubernetes orchestrates containerized applications across a cluster "
            "of machines. It handles scaling, load balancing, and self-healing. "
            "A Pod is the smallest deployable unit. Services provide stable "
            "networking endpoints for Pods."
        ),
        "prompt": "What is the smallest deployable unit in Kubernetes and what do Services provide?",
    },
    {
        "context": (
            "The Great Barrier Reef stretches 2,300 km along Australia's coast. "
            "It contains over 1,500 species of fish and 400 types of coral. "
            "Rising ocean temperatures have caused three mass bleaching events "
            "since 2016."
        ),
        "prompt": "How many fish species live in the Great Barrier Reef and what threat does it face?",
    },
    {
        "context": (
            "BERT (Bidirectional Encoder Representations from Transformers) was "
            "introduced by Google in 2018. It uses masked language modeling and "
            "next sentence prediction during pre-training. BERT-Base has 110M "
            "parameters and BERT-Large has 340M parameters."
        ),
        "prompt": "How many parameters does BERT-Large have and what pre-training tasks does BERT use?",
    },
    {
        "context": (
            "The Voyager 1 spacecraft launched in 1977 and entered interstellar "
            "space in 2012. It carries a golden record with sounds and images "
            "from Earth. As of 2024, it is approximately 24 billion kilometers "
            "from Earth."
        ),
        "prompt": "When did Voyager 1 enter interstellar space and how far is it from Earth?",
    },
    {
        "context": (
            "PostgreSQL supports ACID transactions, MVCC concurrency control, "
            "and JSON/JSONB data types. It offers full-text search, CTEs, and "
            "window functions. Extensions like PostGIS add geospatial capabilities."
        ),
        "prompt": "What concurrency control method does PostgreSQL use and what does PostGIS add?",
    },
    {
        "context": (
            "The International Space Station orbits Earth at 28,000 km/h, "
            "completing one orbit every 90 minutes. It has been continuously "
            "occupied since November 2000. The station is about 109 meters wide "
            "and weighs approximately 420,000 kg."
        ),
        "prompt": "How fast does the ISS travel and how long has it been continuously occupied?",
    },
    {
        "context": (
            "React uses a virtual DOM to minimize direct manipulations of the "
            "real DOM. Components can be functional or class-based. React 18 "
            "introduced concurrent rendering and automatic batching. The "
            "useState and useEffect hooks are the most commonly used hooks."
        ),
        "prompt": "What rendering improvement did React 18 introduce and what are the most common hooks?",
    },
    {
        "context": (
            "Mitochondria are double-membraned organelles that generate ATP "
            "through oxidative phosphorylation. They contain their own DNA, "
            "inherited maternally. The electron transport chain in the inner "
            "membrane creates a proton gradient to drive ATP synthase."
        ),
        "prompt": "How is mitochondrial DNA inherited and what drives ATP synthase?",
    },
    {
        "context": (
            "Git uses a directed acyclic graph (DAG) to model commit history. "
            "Each commit stores a snapshot, not a diff. Branches are lightweight "
            "pointers to commits. The three-way merge algorithm compares the "
            "merge base with both branch tips."
        ),
        "prompt": "Does Git store diffs or snapshots and how does the merge algorithm work?",
    },
    {
        "context": (
            "The Mariana Trench reaches a depth of 11,034 meters at the "
            "Challenger Deep. Water pressure at the bottom is over 1,000 "
            "atmospheres. Despite extreme conditions, living organisms including "
            "amphipods and xenophyophores have been found there."
        ),
        "prompt": "What is the maximum depth of the Mariana Trench and what organisms live there?",
    },
    {
        "context": (
            "OAuth 2.0 defines four grant types: authorization code, implicit, "
            "resource owner password credentials, and client credentials. Access "
            "tokens are typically short-lived (minutes to hours). Refresh tokens "
            "can be used to obtain new access tokens without re-authentication."
        ),
        "prompt": "What are the four OAuth 2.0 grant types and what is the purpose of refresh tokens?",
    },
    {
        "context": (
            "Penicillin was discovered by Alexander Fleming in 1928 when he "
            "noticed mold killing bacteria on a petri dish. Mass production "
            "began during World War II. Antibiotic resistance has become a "
            "global health threat, with MRSA being one of the most concerning "
            "resistant strains."
        ),
        "prompt": "How was penicillin discovered and what is MRSA?",
    },
    {
        "context": (
            "MapReduce is a programming model for processing large datasets "
            "in parallel. The Map phase processes key-value pairs into "
            "intermediate pairs. The Reduce phase aggregates intermediate "
            "values by key. Hadoop is the most well-known implementation."
        ),
        "prompt": "What are the two phases of MapReduce and what is the most well-known implementation?",
    },
    {
        "context": (
            "The human genome contains approximately 3 billion base pairs and "
            "about 20,000 protein-coding genes. The Human Genome Project was "
            "completed in 2003 at a cost of $2.7 billion. Modern sequencing "
            "can now decode a genome for under $1,000."
        ),
        "prompt": "How many protein-coding genes does the human genome have and what did the Human Genome Project cost?",
    },
    {
        "context": (
            "WebAssembly (Wasm) is a binary instruction format designed for "
            "stack-based virtual machines. It enables near-native performance "
            "in web browsers. Languages like Rust, C++, and Go can compile "
            "to Wasm. WASI extends Wasm to run outside browsers."
        ),
        "prompt": "What performance does WebAssembly achieve and what does WASI enable?",
    },
    {
        "context": (
            "The ozone layer is found in the stratosphere between 15-35 km "
            "altitude. It absorbs 97-99% of the sun's UV radiation. The "
            "Montreal Protocol of 1987 banned CFCs, and the ozone hole has "
            "been slowly recovering since the early 2000s."
        ),
        "prompt": "What percentage of UV radiation does the ozone layer absorb and when was the Montreal Protocol signed?",
    },
    {
        "context": (
            "Prometheus is a time-series database designed for monitoring. It "
            "uses a pull model to scrape metrics from targets. PromQL is its "
            "query language. AlertManager handles de-duplication, grouping, "
            "and routing of alerts."
        ),
        "prompt": "Does Prometheus use push or pull for metrics collection and what does AlertManager do?",
    },
    {
        "context": (
            "Photovoltaic cells are made from semiconductor materials, "
            "primarily silicon. When photons strike the cell, they knock "
            "electrons loose, creating an electric current. Monocrystalline "
            "panels have 20-22% efficiency while polycrystalline panels "
            "achieve 15-17%."
        ),
        "prompt": "What semiconductor material are most solar cells made from and what efficiency do monocrystalline panels achieve?",
    },
    {
        "context": (
            "TCP uses a three-way handshake (SYN, SYN-ACK, ACK) to establish "
            "connections. It provides reliable, ordered delivery with flow "
            "control via sliding windows. Congestion control algorithms like "
            "CUBIC adjust the sending rate based on packet loss."
        ),
        "prompt": "Describe TCP's connection establishment process and what congestion control algorithm is commonly used?",
    },
    {
        "context": (
            "CRISPR-Cas9 uses guide RNA to locate specific DNA sequences. The "
            "Cas9 enzyme cuts both DNA strands at the target site. The cell's "
            "repair mechanisms then either delete or insert new genetic material. "
            "Off-target effects remain a concern for clinical applications."
        ),
        "prompt": "What enzyme does CRISPR use to cut DNA and what is a major concern for clinical use?",
    },
    {
        "context": (
            "Kafka is a distributed event streaming platform. It uses an "
            "append-only log with topics partitioned across brokers. Consumers "
            "track their position via offsets. Kafka guarantees at-least-once "
            "delivery by default and supports exactly-once semantics with "
            "transactions."
        ),
        "prompt": "What data structure does Kafka use internally and what delivery guarantee does it provide by default?",
    },
    {
        "context": (
            "The James Webb Space Telescope launched in December 2021 and orbits "
            "at L2, 1.5 million km from Earth. Its primary mirror is 6.5 meters "
            "in diameter. It observes in infrared wavelengths, allowing it to "
            "see the earliest galaxies formed after the Big Bang."
        ),
        "prompt": "Where does the James Webb Telescope orbit and what wavelengths does it observe in?",
    },
]

assert len(PROMPT_ONLY) == 25, f"Expected 25 prompt-only, got {len(PROMPT_ONLY)}"
assert len(CONTEXT_AND_PROMPT) == 25, f"Expected 25 context+prompt, got {len(CONTEXT_AND_PROMPT)}"


# ---------------------------------------------------------------------------
# Build messages for each request type
# ---------------------------------------------------------------------------
def _build_prompt_only_messages(prompt: str) -> list:
    return [HumanMessage(content=prompt)]


def _build_context_messages(context: str, prompt: str) -> list:
    return [
        SystemMessage(content=f"Use ONLY the following context to answer.\n\nContext:\n{context}"),
        HumanMessage(content=prompt),
    ]


# ---------------------------------------------------------------------------
# Build the request schedule: 50 items
# ---------------------------------------------------------------------------
RequestItem = dict  # keys: model_key, messages, label


def build_schedule() -> list[RequestItem]:
    schedule: list[RequestItem] = []
    # 1-13: Gemini, prompt-only
    for i, p in enumerate(PROMPT_ONLY[:13]):
        schedule.append({
            "model_key": "gemini",
            "messages": _build_prompt_only_messages(p),
            "label": f"gemini-prompt-{i+1}",
        })
    # 14-25: OpenAI, prompt-only
    for i, p in enumerate(PROMPT_ONLY[13:]):
        schedule.append({
            "model_key": "openai",
            "messages": _build_prompt_only_messages(p),
            "label": f"openai-prompt-{i+14}",
        })
    # 26-38: Gemini, context+prompt
    for i, cp in enumerate(CONTEXT_AND_PROMPT[:13]):
        schedule.append({
            "model_key": "gemini",
            "messages": _build_context_messages(cp["context"], cp["prompt"]),
            "label": f"gemini-context-{i+26}",
        })
    # 39-50: OpenAI, context+prompt
    for i, cp in enumerate(CONTEXT_AND_PROMPT[13:]):
        schedule.append({
            "model_key": "openai",
            "messages": _build_context_messages(cp["context"], cp["prompt"]),
            "label": f"openai-context-{i+39}",
        })
    return schedule


# ---------------------------------------------------------------------------
# Main test
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Graph builder — manual StateGraph so langgraph_agent can discover the LLM
# ---------------------------------------------------------------------------

class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


def build_graph(llm):
    """Build a simple LLM-only StateGraph."""
    # bind_tools([]) creates a RunnableBinding so langgraph_agent's
    # wrapping logic can discover and instrument the chat model.
    llm_bound = llm.bind_tools([])

    async def llm_node(state: AgentState) -> AgentState:
        response = await llm_bound.ainvoke(state["messages"])
        return {"messages": [response]}

    graph = StateGraph(AgentState)
    graph.add_node("agent", llm_node)
    graph.set_entry_point("agent")
    graph.add_edge("agent", END)
    return graph.compile()


def _ts():
    """Return HH:MM:SS timestamp."""
    return time.strftime("%H:%M:%S")


def _log(msg: str):
    """Print with timestamp and flush."""
    print(f"[{_ts()}] {msg}", flush=True)


async def run_test():
    test_start = time.monotonic()
    total_count = int(os.environ.get("TEST_COUNT", 50))
    print("=" * 70, flush=True)
    print(f"[{_ts()}] Evaluation Pipeline E2E Test — {total_count} LangGraph Requests", flush=True)
    print("=" * 70, flush=True)

    # --- Build LLMs -------------------------------------------------------
    gemini_llm = ChatGoogleGenerativeAI(
        model="gemini-2.5-flash",
        temperature=0.3,
        google_api_key=GOOGLE_API_KEY,
    )
    openai_llm = ChatOpenAI(
        model="gpt-4o-mini",
        temperature=0.3,
        api_key=OPENAI_API_KEY,
    )

    # --- Build graphs using manual StateGraph for proper LLM wrapping -----
    graphs = {
        "gemini": build_graph(gemini_llm),
        "openai": build_graph(openai_llm),
    }

    schedule = build_schedule()
    max_requests = int(os.environ.get("TEST_COUNT", len(schedule)))
    schedule = schedule[:max_requests]

    _log(f"Schedule: {len(schedule)} requests")
    _log(f"  Gemini prompt-only:   {sum(1 for s in schedule if s['model_key']=='gemini' and 'prompt' in s['label'] and 'context' not in s['label'])}")
    _log(f"  OpenAI prompt-only:   {sum(1 for s in schedule if s['model_key']=='openai' and 'prompt' in s['label'] and 'context' not in s['label'])}")
    _log(f"  Gemini context+prompt:{sum(1 for s in schedule if s['model_key']=='gemini' and 'context' in s['label'])}")
    _log(f"  OpenAI context+prompt:{sum(1 for s in schedule if s['model_key']=='openai' and 'context' in s['label'])}")

    # --- Run requests sequentially ----------------------------------------
    successes = 0
    failures = 0
    eval_spans_count = 0
    _log("Starting request loop...")

    for idx, item in enumerate(schedule, 1):
        graph = graphs[item["model_key"]]
        label = item["label"]
        messages = item["messages"]

        @langgraph_agent(agent_name="eval_e2e_agent")
        async def invoke(g, msgs):
            return await g.ainvoke({"messages": msgs})

        captured_spans.clear()
        req_start = time.monotonic()
        _log(f"  [{idx:2d}/{len(schedule)}] → {label} ...")
        try:
            result = await invoke(graph, messages)
            req_elapsed = time.monotonic() - req_start
            final_msg = result["messages"][-1].content
            successes += 1

            # Check evaluation attrs on LLM spans
            llm_spans = [
                s for s in captured_spans if s.span_type.value == "llm"
            ]
            eval_ok = 0
            for ls in llm_spans:
                has_eval = ls.attributes.get("evaluation_enabled")
                has_types = ls.attributes.get("evaluation_types")
                has_prompt = ls.attributes.get("prompt_text")
                has_completion = ls.attributes.get("completion_text")
                if has_eval and has_types:
                    eval_ok += 1
                    eval_spans_count += 1

            status = "✓"
            eval_status = f"eval={eval_ok}/{len(llm_spans)}"
            prompt_status = "prompt_text=✓" if any(
                ls.attributes.get("prompt_text") for ls in llm_spans
            ) else "prompt_text=✗"
            completion_status = "completion_text=✓" if any(
                ls.attributes.get("completion_text") for ls in llm_spans
            ) else "completion_text=✗"
            response_preview = final_msg[:60].replace("\n", " ")
            _log(
                f"  [{idx:2d}/{len(schedule)}] {status} {label:<25s} "
                f"{req_elapsed:5.1f}s  spans={len(captured_spans):2d} llm={len(llm_spans)} "
                f"{eval_status} {prompt_status} {completion_status} "
                f'"{ response_preview}..."'
            )
        except Exception as e:
            req_elapsed = time.monotonic() - req_start
            failures += 1
            _log(
                f"  [{idx:2d}/{len(schedule)}] \u2717 {label:<25s} "
                f"{req_elapsed:5.1f}s  ERROR: {type(e).__name__}: {str(e)[:80]}"
            )

        # Small delay to avoid rate limiting
        await asyncio.sleep(0.5)

    # --- Summary ----------------------------------------------------------
    total_elapsed = time.monotonic() - test_start
    print("\n" + "=" * 70, flush=True)
    _log("SUMMARY")
    print("=" * 70, flush=True)
    _log(f"  Total requests:       {len(schedule)}")
    _log(f"  Successes:            {successes}")
    _log(f"  Failures:             {failures}")
    _log(f"  LLM spans with eval:  {eval_spans_count}")
    _log(f"  Expected eval by server (~20%): ~{eval_spans_count * 0.2:.0f} spans")
    _log(f"  Total elapsed:        {total_elapsed:.1f}s ({total_elapsed/60:.1f}m)")
    print(flush=True)
    if failures == 0:
        _log("  ✓ All requests succeeded!")
    else:
        _log(f"  ✗ {failures} requests failed")

    if eval_spans_count > 0:
        _log("  ✓ Evaluation metadata present on LLM spans")
    else:
        _log("  ✗ No evaluation metadata found — check configure(evaluation_enabled=True)")

    _log("Waiting 5s for spans to flush to collector...")
    await asyncio.sleep(5)
    _log("Done.")


if __name__ == "__main__":
    asyncio.run(run_test())
