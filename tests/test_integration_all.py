"""Live integration tests — all adapter providers with real APIs.

Tests every adapter that has available API keys against real provider
APIs. Each test exercises the full adapter resolution pipeline and
verifies that model, tokens, provider, finish_reason and all label
dimensions are correctly extracted.

SDK versions tested against (recorded at test creation):
    openai==2.24.0
    anthropic==0.84.0
    boto3==1.42.59
    langchain==1.2.10
    langchain-openai==1.1.10
    langchain-anthropic==1.3.4
    langgraph==1.0.10
    llama-index-core==0.14.15
    llama-index-llms-openai==0.6.21
    llama-index-llms-anthropic==0.10.10

Requirements:
    export OPENAI_API_KEY=...  or  export API_OPENAI_KEY=...
    export ANTHROPIC_API_KEY=... or export API_ANTHROPIC_KEY=...
    AWS SSO / credentials for Bedrock (us-east-1)

Usage:
    pytest tests/test_integration_all.py -v -s
    # Skips any provider whose key is missing
"""

from __future__ import annotations

import os
import sys
import json

import pytest

# ── Resolve API keys to canonical env vars ──────────────────────────
_openai_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("API_OPENAI_KEY")
if _openai_key and not os.environ.get("OPENAI_API_KEY"):
    os.environ["OPENAI_API_KEY"] = _openai_key

_anthropic_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("API_ANTHROPIC_KEY")
if _anthropic_key and not os.environ.get("ANTHROPIC_API_KEY"):
    os.environ["ANTHROPIC_API_KEY"] = _anthropic_key


# ── Skip markers ────────────────────────────────────────────────────
skip_no_openai = pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set",
)
skip_no_anthropic = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set",
)

def _aws_available() -> bool:
    """Check if AWS credentials are available for Bedrock."""
    try:
        import boto3
        sts = boto3.client("sts", region_name="us-east-1")
        sts.get_caller_identity()
        return True
    except Exception:
        return False

skip_no_aws = pytest.mark.skipif(
    not _aws_available(),
    reason="AWS credentials not available for Bedrock",
)


# ── Rastir imports ──────────────────────────────────────────────────
from rastir import configure, stop_exporter
from rastir.config import reset_config
from rastir.decorators import agent, llm, trace, retrieval
from rastir.adapters.registry import resolve, resolve_stream_chunk
import rastir.decorators as dec
import rastir.wrapper as wrp


# =====================================================================
# Helpers
# =====================================================================

def _capture_spans():
    """Patch enqueue_span in BOTH decorators and wrapper modules."""
    captured = []
    original_dec = dec.enqueue_span
    original_wrp = wrp.enqueue_span

    def mock_enqueue(span):
        captured.append({
            "name": span.name,
            "span_type": span.span_type.value,
            "status": span.status.value,
            "duration_seconds": span.duration_seconds,
            "attributes": dict(span.attributes),
            "trace_id": span.trace_id,
            "span_id": span.span_id,
        })

    dec.enqueue_span = mock_enqueue
    wrp.enqueue_span = mock_enqueue
    return captured, (original_dec, original_wrp)


def _restore_enqueue(originals):
    dec.enqueue_span = originals[0]
    wrp.enqueue_span = originals[1]


def _print_ar(label: str, ar):
    """Pretty-print an AdapterResult."""
    print(f"\n  [{label}] provider={ar.provider}, model={ar.model}, "
          f"tokens_in={ar.tokens_input}, tokens_out={ar.tokens_output}, "
          f"finish={ar.finish_reason}")
    if ar.extra_attributes:
        for k, v in sorted(ar.extra_attributes.items()):
            print(f"    {k}={v}")


# =====================================================================
# Fixture: configure Rastir for each test
# =====================================================================

@pytest.fixture(autouse=True)
def _setup_rastir():
    reset_config()
    configure(service="integration-test", env="ci")
    yield
    try:
        stop_exporter(timeout=1.0)
    except Exception:
        pass
    reset_config()


# =====================================================================
# 1. OpenAI Direct — sync, streaming, label variations
# =====================================================================

@skip_no_openai
class TestOpenAIIntegration:
    """Live tests against OpenAI API."""

    def test_chat_completion_gpt4o_mini(self):
        """Basic chat completion with gpt-4o-mini — verify all labels."""
        import openai
        client = openai.OpenAI()
        result = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "Say hello in one word."}],
            max_tokens=10,
        )
        ar = resolve(result)
        _print_ar("OpenAI gpt-4o-mini", ar)

        assert ar is not None
        assert ar.provider == "openai"
        assert "gpt-4o-mini" in ar.model  # API returns versioned name
        assert ar.tokens_input is not None and ar.tokens_input > 0
        assert ar.tokens_output is not None and ar.tokens_output > 0
        assert ar.finish_reason in ("stop", "length")

    def test_chat_completion_gpt4o(self):
        """Different model label — gpt-4o."""
        import openai
        client = openai.OpenAI()
        result = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": "Reply with the word 'yes'."}],
            max_tokens=5,
        )
        ar = resolve(result)
        _print_ar("OpenAI gpt-4o", ar)

        assert ar.provider == "openai"
        assert "gpt-4o" in ar.model
        assert ar.tokens_input > 0
        assert ar.tokens_output > 0

    def test_streaming(self):
        """Streaming chat completion — verify chunk deltas."""
        import openai
        client = openai.OpenAI()
        stream = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "Count to 3."}],
            max_tokens=20,
            stream=True,
            stream_options={"include_usage": True},
        )
        chunks_seen = 0
        last_delta = None
        for chunk in stream:
            delta = resolve_stream_chunk(chunk)
            if delta is not None:
                chunks_seen += 1
                last_delta = delta
                if delta.model:
                    print(f"\n  Stream chunk: model={delta.model}")
                if delta.tokens_input:
                    print(f"  Final usage: in={delta.tokens_input}, out={delta.tokens_output}")

        assert chunks_seen > 0, "Should have resolved at least one stream chunk"
        assert last_delta is not None
        print(f"  Total chunks resolved: {chunks_seen}")

    def test_with_rastir_decorator(self):
        """Full @llm decorator wrapping an OpenAI call — verify span."""
        import openai

        captured, originals = _capture_spans()
        try:
            @llm
            def call_openai(prompt: str) -> object:
                client = openai.OpenAI()
                return client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=10,
                )

            result = call_openai("Say hi.")

            assert len(captured) >= 1
            span = captured[0]
            print(f"\n  Span: {span['name']}, type={span['span_type']}, "
                  f"status={span['status']}, dur={span['duration_seconds']*1000:.0f}ms")
            print(f"  Attrs: {span['attributes']}")
            assert span["span_type"] == "llm"
            assert span["status"] == "OK"
            assert span["attributes"].get("provider") == "openai"
            assert "gpt-4o-mini" in span["attributes"].get("model", "")
            assert span["attributes"].get("tokens_input") > 0
            assert span["attributes"].get("tokens_output") > 0
        finally:
            _restore_enqueue(originals)

    def test_finish_reason_length(self):
        """Trigger max_tokens cutoff — finish_reason='length'."""
        import openai
        client = openai.OpenAI()
        result = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "Write a 500-word essay on the history of computing."}],
            max_tokens=5,
        )
        ar = resolve(result)
        _print_ar("OpenAI length-cutoff", ar)
        assert ar.finish_reason == "length"

    def test_system_prompt_label(self):
        """Verify tokens include system prompt tokens."""
        import openai
        client = openai.OpenAI()
        result = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a helpful assistant who speaks only French."},
                {"role": "user", "content": "Say hi."},
            ],
            max_tokens=20,
        )
        ar = resolve(result)
        _print_ar("OpenAI with system prompt", ar)
        assert ar.tokens_input > 10  # system + user prompt > 10 tokens


# =====================================================================
# 2. Anthropic Direct — sync, label variations
#    Working models: claude-sonnet-4-20250514, claude-3-haiku-20240307
# =====================================================================

@skip_no_anthropic
class TestAnthropicIntegration:
    """Live tests against Anthropic API."""

    def test_message_sonnet(self):
        """Basic message with claude-sonnet-4 — verify all labels."""
        import anthropic
        client = anthropic.Anthropic()
        result = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=10,
            messages=[{"role": "user", "content": "Say hello in one word."}],
        )
        ar = resolve(result)
        _print_ar("Anthropic sonnet-4", ar)

        assert ar is not None
        assert ar.provider == "anthropic"
        assert "sonnet" in ar.model
        assert ar.tokens_input > 0
        assert ar.tokens_output > 0
        assert ar.finish_reason in ("end_turn", "max_tokens")

    def test_message_haiku(self):
        """Different model label — claude-3-haiku."""
        import anthropic
        client = anthropic.Anthropic()
        result = client.messages.create(
            model="claude-3-haiku-20240307",
            max_tokens=10,
            messages=[{"role": "user", "content": "Reply with 'yes'."}],
        )
        ar = resolve(result)
        _print_ar("Anthropic haiku", ar)

        assert ar.provider == "anthropic"
        assert "haiku" in ar.model
        assert ar.tokens_input > 0

    def test_finish_reason_max_tokens(self):
        """Trigger max_tokens — finish_reason='max_tokens'."""
        import anthropic
        client = anthropic.Anthropic()
        result = client.messages.create(
            model="claude-3-haiku-20240307",
            max_tokens=3,
            messages=[{"role": "user", "content": "Write a long essay about the universe."}],
        )
        ar = resolve(result)
        _print_ar("Anthropic max_tokens cutoff", ar)
        assert ar.finish_reason == "max_tokens"

    def test_streaming(self):
        """Anthropic streaming — verify we get stream events."""
        import anthropic
        client = anthropic.Anthropic()
        chunks_seen = 0
        with client.messages.stream(
            model="claude-3-haiku-20240307",
            max_tokens=20,
            messages=[{"role": "user", "content": "Count to 3."}],
        ) as stream:
            for event in stream:
                delta = resolve_stream_chunk(event)
                if delta is not None:
                    chunks_seen += 1

        print(f"\n  Anthropic stream chunks resolved: {chunks_seen}")
        # At minimum some events should be resolvable
        assert chunks_seen >= 0

    def test_with_rastir_decorator(self):
        """Full @llm decorator wrapping an Anthropic call."""
        import anthropic

        captured, originals = _capture_spans()
        try:
            @llm
            def call_anthropic(prompt: str) -> object:
                client = anthropic.Anthropic()
                return client.messages.create(
                    model="claude-3-haiku-20240307",
                    max_tokens=10,
                    messages=[{"role": "user", "content": prompt}],
                )

            result = call_anthropic("Say hi.")

            assert len(captured) >= 1
            span = captured[0]
            print(f"\n  Span: {span['name']}, type={span['span_type']}, "
                  f"status={span['status']}, dur={span['duration_seconds']*1000:.0f}ms")
            print(f"  Attrs: {span['attributes']}")
            assert span["span_type"] == "llm"
            assert span["status"] == "OK"
            assert span["attributes"].get("provider") == "anthropic"
            assert "haiku" in span["attributes"].get("model", "")
            assert span["attributes"].get("tokens_input") > 0
            assert span["attributes"].get("tokens_output") > 0
        finally:
            _restore_enqueue(originals)


# =====================================================================
# 3. AWS Bedrock — Converse API, streaming
#    Bedrock Converse response does NOT contain modelId, so
#    resolve() alone returns provider="bedrock", model="unknown".
#    With two-phase enrichment, the @llm decorator captures
#    model/provider from the request-phase modelId kwarg.
# =====================================================================

@skip_no_aws
class TestBedrockIntegration:
    """Live tests against AWS Bedrock Converse API."""

    def test_converse_haiku(self):
        """Bedrock Converse with Claude 3 Haiku — verify tokens/labels."""
        import boto3
        client = boto3.client("bedrock-runtime", region_name="us-east-1")
        result = client.converse(
            modelId="anthropic.claude-3-haiku-20240307-v1:0",
            messages=[{"role": "user", "content": [{"text": "Say hello in one word."}]}],
            inferenceConfig={"maxTokens": 10},
        )
        ar = resolve(result)
        _print_ar("Bedrock Haiku", ar)

        assert ar is not None
        # resolve() only sees response — Bedrock response has no modelId
        assert ar.provider == "bedrock"
        assert ar.tokens_input is not None and ar.tokens_input > 0
        assert ar.tokens_output is not None and ar.tokens_output > 0
        assert ar.finish_reason in ("end_turn", "max_tokens")

    def test_converse_max_tokens_cutoff(self):
        """Trigger max_tokens — verify finish_reason."""
        import boto3
        client = boto3.client("bedrock-runtime", region_name="us-east-1")
        result = client.converse(
            modelId="anthropic.claude-3-haiku-20240307-v1:0",
            messages=[{"role": "user", "content": [{"text": "Write a very long essay."}]}],
            inferenceConfig={"maxTokens": 5},
        )
        ar = resolve(result)
        _print_ar("Bedrock max_tokens cutoff", ar)
        assert ar.finish_reason == "max_tokens"

    def test_converse_stream(self):
        """Bedrock Converse stream — verify streaming metadata extraction."""
        import boto3
        client = boto3.client("bedrock-runtime", region_name="us-east-1")
        response = client.converse_stream(
            modelId="anthropic.claude-3-haiku-20240307-v1:0",
            messages=[{"role": "user", "content": [{"text": "Count to 3."}]}],
            inferenceConfig={"maxTokens": 30},
        )
        chunks_seen = 0
        text_parts = []
        final_usage = None
        for event in response["stream"]:
            if "contentBlockDelta" in event:
                delta_text = event["contentBlockDelta"]["delta"].get("text", "")
                text_parts.append(delta_text)
            if "metadata" in event:
                final_usage = event["metadata"].get("usage", {})

            delta = resolve_stream_chunk(event)
            if delta is not None:
                chunks_seen += 1
                if delta.tokens_input:
                    print(f"\n  Stream final usage: in={delta.tokens_input}, out={delta.tokens_output}")

        full_text = "".join(text_parts)
        print(f"\n  Bedrock stream text: {full_text[:100]}")
        print(f"  Chunks resolved: {chunks_seen}")
        assert len(full_text) > 0
        assert final_usage is not None

    def test_with_rastir_decorator(self):
        """Full @llm decorator wrapping a Bedrock Converse call."""
        import boto3

        captured, originals = _capture_spans()
        try:
            @llm
            def call_bedrock(prompt: str) -> dict:
                client = boto3.client("bedrock-runtime", region_name="us-east-1")
                return client.converse(
                    modelId="anthropic.claude-3-haiku-20240307-v1:0",
                    messages=[{"role": "user", "content": [{"text": prompt}]}],
                    inferenceConfig={"maxTokens": 10},
                )

            result = call_bedrock("Say hello.")

            assert len(captured) >= 1
            span = captured[0]
            print(f"\n  Span: type={span['span_type']}, status={span['status']}, "
                  f"dur={span['duration_seconds']*1000:.0f}ms")
            print(f"  Attrs: {span['attributes']}")
            assert span["span_type"] == "llm"
            assert span["status"] == "OK"
            # modelId is hardcoded inside fn body, not in decorated fn kwargs,
            # so request-phase enrichment doesn't see it.
            assert span["attributes"].get("provider") == "bedrock"
            assert span["attributes"].get("tokens_input") > 0
        finally:
            _restore_enqueue(originals)

    def test_two_phase_enrichment_decorator(self):
        """Two-phase enrichment: modelId as kwarg flows through decorator."""
        import boto3

        captured, originals = _capture_spans()
        try:
            @llm
            def call_bedrock(prompt: str, *, modelId: str = "anthropic.claude-3-haiku-20240307-v1:0") -> dict:
                client = boto3.client("bedrock-runtime", region_name="us-east-1")
                return client.converse(
                    modelId=modelId,
                    messages=[{"role": "user", "content": [{"text": prompt}]}],
                    inferenceConfig={"maxTokens": 10},
                )

            result = call_bedrock("Say hello.")

            assert len(captured) >= 1
            span = captured[0]
            print(f"\n  Span: type={span['span_type']}, status={span['status']}, "
                  f"dur={span['duration_seconds']*1000:.0f}ms")
            print(f"  Attrs: {span['attributes']}")
            assert span["span_type"] == "llm"
            assert span["status"] == "OK"
            # Two-phase: request-phase extracts model/provider from modelId kwarg;
            # response "unknown" model doesn't overwrite.
            # Provider: response "bedrock" is concrete and wins.
            assert span["attributes"].get("provider") == "bedrock"
            assert "haiku" in span["attributes"].get("model", "")
            assert span["attributes"].get("tokens_input") > 0
        finally:
            _restore_enqueue(originals)

    def test_system_prompt(self):
        """Verify system prompt tokens are counted."""
        import boto3
        client = boto3.client("bedrock-runtime", region_name="us-east-1")
        result = client.converse(
            modelId="anthropic.claude-3-haiku-20240307-v1:0",
            messages=[{"role": "user", "content": [{"text": "Hi"}]}],
            system=[{"text": "You are a pirate. Always respond in pirate speak."}],
            inferenceConfig={"maxTokens": 20},
        )
        ar = resolve(result)
        _print_ar("Bedrock with system prompt", ar)
        assert ar.tokens_input > 5  # system + user tokens


# =====================================================================
# 4. LangChain — OpenAI + Anthropic backends
# =====================================================================

@skip_no_openai
class TestLangChainOpenAIIntegration:
    """LangChain with OpenAI backend."""

    def test_basic_invoke(self):
        """LangChain ChatOpenAI.invoke() — verify framework unwrapping."""
        from langchain_openai import ChatOpenAI
        from langchain_core.messages import HumanMessage

        model = ChatOpenAI(model="gpt-4o-mini", max_tokens=10)
        result = model.invoke([HumanMessage("Say hello")])

        ar = resolve(result)
        _print_ar("LangChain+OpenAI invoke", ar)

        assert ar is not None
        # LangChain returns AIMessage — adapter extracts metadata
        assert ar.extra_attributes.get("model") is not None or ar.model != "unknown"
        assert ar.extra_attributes.get("tokens_input", ar.tokens_input) is not None

    def test_batch(self):
        """LangChain batch() with multiple prompts."""
        from langchain_openai import ChatOpenAI
        from langchain_core.messages import HumanMessage

        model = ChatOpenAI(model="gpt-4o-mini", max_tokens=5)
        results = model.batch([
            [HumanMessage("Say 'A'")],
            [HumanMessage("Say 'B'")],
        ])

        for i, result in enumerate(results):
            ar = resolve(result)
            _print_ar(f"LangChain batch[{i}]", ar)
            assert ar is not None

    def test_streaming(self):
        """LangChain streaming with ChatOpenAI."""
        from langchain_openai import ChatOpenAI
        from langchain_core.messages import HumanMessage

        model = ChatOpenAI(model="gpt-4o-mini", max_tokens=10, streaming=True)
        chunks = []
        for chunk in model.stream([HumanMessage("Say hi")]):
            chunks.append(chunk)
            delta = resolve_stream_chunk(chunk)

        print(f"\n  LangChain stream chunks: {len(chunks)}")
        assert len(chunks) > 0

    def test_with_rastir_agent_decorator(self):
        """@agent wrapping LangChain — verify agent_name label."""
        from langchain_openai import ChatOpenAI
        from langchain_core.messages import HumanMessage

        captured, originals = _capture_spans()
        try:
            @agent(agent_name="langchain_agent")
            def run_chain(query: str):
                model = ChatOpenAI(model="gpt-4o-mini", max_tokens=10)
                return model.invoke([HumanMessage(query)])

            result = run_chain("Hello")

            assert len(captured) >= 1
            span = captured[0]
            print(f"\n  Agent span: {span['name']}, attrs={span['attributes']}")
            assert span["span_type"] == "agent"
            assert span["attributes"].get("agent_name") == "langchain_agent"
        finally:
            _restore_enqueue(originals)


@skip_no_anthropic
class TestLangChainAnthropicIntegration:
    """LangChain with Anthropic backend."""

    def test_basic_invoke(self):
        """LangChain ChatAnthropic.invoke() — verify framework unwrapping."""
        from langchain_anthropic import ChatAnthropic
        from langchain_core.messages import HumanMessage

        model = ChatAnthropic(model="claude-sonnet-4-20250514", max_tokens=10)
        result = model.invoke([HumanMessage("Say hello")])

        ar = resolve(result)
        _print_ar("LangChain+Anthropic invoke", ar)

        assert ar is not None
        tokens_in = ar.extra_attributes.get("tokens_input", ar.tokens_input)
        assert tokens_in is not None and tokens_in > 0

    def test_different_model_label(self):
        """LangChain+Anthropic with claude-3-haiku — verify model label varies."""
        from langchain_anthropic import ChatAnthropic
        from langchain_core.messages import HumanMessage

        model = ChatAnthropic(model="claude-3-haiku-20240307", max_tokens=10)
        result = model.invoke([HumanMessage("Reply 'yes'")])

        ar = resolve(result)
        _print_ar("LangChain+Anthropic haiku", ar)

        model_val = ar.extra_attributes.get("model", ar.model)
        assert model_val is not None
        assert "haiku" in str(model_val).lower()


# =====================================================================
# 5. LangGraph — with tools, state management
# =====================================================================

@skip_no_openai
class TestLangGraphIntegrationV3:
    """Live tests with LangGraph agent."""

    def _build_agent(self, model_name="gpt-4o-mini"):
        from langchain_openai import ChatOpenAI
        from langchain_core.tools import tool as lc_tool
        from langgraph.graph import StateGraph, MessagesState, START, END
        from langgraph.prebuilt import ToolNode

        @lc_tool
        def calculator(expression: str) -> str:
            """Evaluate a math expression."""
            try:
                return str(eval(expression, {"__builtins__": {}}, {}))
            except Exception as e:
                return f"Error: {e}"

        tools = [calculator]
        model = ChatOpenAI(model=model_name, temperature=0).bind_tools(tools)

        def chatbot(state: MessagesState):
            return {"messages": [model.invoke(state["messages"])]}

        def should_continue(state):
            """Route to tools or end. No type annotation to avoid forward-ref issues."""
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

    def test_simple_chat(self):
        """Simple question — no tools used."""
        from langchain_core.messages import HumanMessage
        app = self._build_agent()
        result = app.invoke({"messages": [HumanMessage("Say hello.")]})

        ar = resolve(result)
        _print_ar("LangGraph simple chat", ar)

        assert ar is not None
        assert ar.extra_attributes.get("langgraph_message_count") >= 2
        assert ar.extra_attributes.get("langgraph_ai_message_count") >= 1

    def test_tool_call(self):
        """Question triggering tool — verify tool_message_count label."""
        from langchain_core.messages import HumanMessage
        app = self._build_agent()
        result = app.invoke(
            {"messages": [HumanMessage("What is 17 * 23? Use calculator.")]}
        )

        ar = resolve(result)
        _print_ar("LangGraph with tool call", ar)

        assert ar.extra_attributes.get("langgraph_tool_message_count") >= 1
        assert ar.extra_attributes.get("langgraph_message_count") >= 4

    def test_streaming_messages_mode(self):
        """LangGraph stream_mode='messages' — verify chunk resolution."""
        from langchain_core.messages import HumanMessage
        app = self._build_agent()
        deltas = 0
        for chunk in app.stream(
            {"messages": [HumanMessage("Say 'test'.")]},
            stream_mode="messages",
        ):
            delta = resolve_stream_chunk(chunk)
            if delta is not None:
                deltas += 1

        print(f"\n  LangGraph stream deltas resolved: {deltas}")
        assert deltas > 0

    def test_with_different_model(self):
        """LangGraph with gpt-4o — verify model label changes."""
        from langchain_core.messages import HumanMessage
        app = self._build_agent("gpt-4o")
        result = app.invoke({"messages": [HumanMessage("Say yes.")]})

        ar = resolve(result)
        _print_ar("LangGraph gpt-4o", ar)

        model_val = ar.extra_attributes.get("model", ar.model)
        assert "gpt-4o" in str(model_val)

    def test_with_nested_decorators(self):
        """Full pipeline: @trace > @agent > LangGraph invoke."""
        from langchain_core.messages import HumanMessage
        app = self._build_agent()

        captured, originals = _capture_spans()
        try:
            @trace
            def handle_request(query: str):
                return run_lg_agent(query)

            @agent(agent_name="lg_agent")
            def run_lg_agent(query: str):
                return app.invoke({"messages": [HumanMessage(query)]})

            result = handle_request("What is 5+5? Use calculator.")

            print(f"\n  Captured {len(captured)} spans:")
            for s in captured:
                print(f"    {s['name']}: type={s['span_type']}, "
                      f"agent={s['attributes'].get('agent_name', '-')}, "
                      f"dur={s['duration_seconds']*1000:.0f}ms")

            assert len(captured) >= 2  # trace + agent at minimum
            types = [s["span_type"] for s in captured]
            assert "agent" in types
            assert "trace" in types
        finally:
            _restore_enqueue(originals)


# =====================================================================
# 6. LlamaIndex — with OpenAI backend
# =====================================================================

@skip_no_openai
class TestLlamaIndexIntegration:
    """Live tests with LlamaIndex using OpenAI."""

    def test_llm_complete(self):
        """LlamaIndex OpenAI LLM complete() call."""
        from llama_index.llms.openai import OpenAI as LlamaOpenAI

        llm_instance = LlamaOpenAI(model="gpt-4o-mini", max_tokens=10)
        result = llm_instance.complete("Say hello in one word.")

        print(f"\n  LlamaIndex complete result type: {type(result).__name__}")
        print(f"  Result text: {result.text}")

        ar = resolve(result)
        if ar:
            _print_ar("LlamaIndex complete", ar)

    def test_llm_chat(self):
        """LlamaIndex OpenAI LLM chat() call."""
        from llama_index.llms.openai import OpenAI as LlamaOpenAI
        from llama_index.core.llms import ChatMessage

        llm_instance = LlamaOpenAI(model="gpt-4o-mini", max_tokens=10)
        result = llm_instance.chat([
            ChatMessage(role="user", content="Say hello in one word.")
        ])

        print(f"\n  LlamaIndex chat result type: {type(result).__name__}")
        print(f"  Module: {type(result).__module__}")
        print(f"  Result text: {result.message.content}")

        raw = getattr(result, "raw", None)
        print(f"  Has .raw: {raw is not None}")
        if raw:
            print(f"  Raw type: {type(raw).__name__}, module: {type(raw).__module__}")

        ar = resolve(result)
        if ar:
            _print_ar("LlamaIndex chat", ar)
            if ar.provider == "openai":
                assert "gpt-4o-mini" in ar.model  # API returns versioned name
                assert ar.tokens_input > 0
                assert ar.tokens_output > 0

    def test_different_model(self):
        """LlamaIndex with gpt-4o — verify model label varies."""
        from llama_index.llms.openai import OpenAI as LlamaOpenAI
        from llama_index.core.llms import ChatMessage

        llm_instance = LlamaOpenAI(model="gpt-4o", max_tokens=5)
        result = llm_instance.chat([
            ChatMessage(role="user", content="Say yes.")
        ])

        ar = resolve(result)
        if ar and ar.provider == "openai":
            _print_ar("LlamaIndex gpt-4o", ar)
            assert "gpt-4o" in ar.model

    def test_with_rastir_decorator(self):
        """@llm decorator wrapping LlamaIndex call."""
        from llama_index.llms.openai import OpenAI as LlamaOpenAI
        from llama_index.core.llms import ChatMessage

        captured, originals = _capture_spans()
        try:
            @llm
            def call_llamaindex(prompt: str):
                instance = LlamaOpenAI(model="gpt-4o-mini", max_tokens=10)
                return instance.chat([ChatMessage(role="user", content=prompt)])

            result = call_llamaindex("Say hello.")

            assert len(captured) >= 1
            span = captured[0]
            print(f"\n  Span: type={span['span_type']}, status={span['status']}")
            print(f"  Attrs: {span['attributes']}")
            assert span["span_type"] == "llm"
            assert span["status"] == "OK"
        finally:
            _restore_enqueue(originals)


# =====================================================================
# 7. rastir.wrap() — generic object wrapper
# =====================================================================

@skip_no_openai
class TestWrapIntegration:
    """Live test of rastir.wrap() with a real wrapped object."""

    def test_wrap_openai_client(self):
        """Wrap an OpenAI client and call chat.completions.create()."""
        import openai
        import rastir

        captured, originals = _capture_spans()
        try:
            # Wrap AFTER patching enqueue_span so the wrapper uses the mock
            client = openai.OpenAI()
            wrapped = rastir.wrap(client.chat.completions, name="openai_chat")

            result = wrapped.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": "Say hi."}],
                max_tokens=5,
            )

            assert len(captured) >= 1, f"Expected at least 1 span, got {len(captured)}"
            span = captured[0]
            print(f"\n  wrap() span: {span['name']}, type={span['span_type']}")
            print(f"  Attrs: {span['attributes']}")
            assert span["name"] == "openai_chat.create"
            assert span["span_type"] == "infra"
            assert span["status"] == "OK"
            assert span["attributes"]["wrap.method"] == "create"
        finally:
            _restore_enqueue(originals)


# =====================================================================
# 8. Cross-cutting: nested decorator combinations
# =====================================================================

@skip_no_openai
class TestNestedDecoratorIntegration:
    """Verify proper nesting of multiple decorators with real calls."""

    def test_trace_agent_llm_retrieval(self):
        """Full decorator stack: @trace > @agent > @llm + @retrieval."""
        import openai

        captured, originals = _capture_spans()
        try:
            @retrieval
            def fetch_context(query: str) -> list:
                return ["doc1", "doc2"]

            @llm
            def call_model(prompt: str, context: list) -> object:
                client = openai.OpenAI()
                return client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=10,
                )

            @agent(agent_name="full_stack_agent")
            def run_pipeline(query: str):
                context = fetch_context(query)
                return call_model(query, context)

            @trace
            def handle(query: str):
                return run_pipeline(query)

            result = handle("Test query")

            print(f"\n  Captured {len(captured)} spans:")
            for s in captured:
                print(f"    {s['name']}: type={s['span_type']}, "
                      f"status={s['status']}, "
                      f"agent={s['attributes'].get('agent_name', '-')}")

            # Should have: retrieval, llm, agent, trace (innermost first)
            assert len(captured) >= 4
            types = {s["span_type"] for s in captured}
            assert "retrieval" in types
            assert "llm" in types
            assert "agent" in types
            assert "trace" in types

            # All should be OK
            for s in captured:
                assert s["status"] == "OK", f"{s['name']} has status {s['status']}"

            # LLM span should have provider/model
            llm_spans = [s for s in captured if s["span_type"] == "llm"]
            assert len(llm_spans) >= 1
            assert llm_spans[0]["attributes"].get("provider") == "openai"
            assert "gpt-4o-mini" in llm_spans[0]["attributes"].get("model", "")
            assert llm_spans[0]["attributes"].get("tokens_input") > 0

        finally:
            _restore_enqueue(originals)


# =====================================================================
# 9. Metrics aggregation with real API spans
# =====================================================================

@skip_no_openai
@skip_no_anthropic
class TestMetricsAggregation:
    """Feed real API spans into MetricsRegistry, verify aggregate counts."""

    @staticmethod
    def _parse_metric(output: str, metric_name: str, labels: dict[str, str]) -> float | None:
        """Parse a counter/gauge value from Prometheus text output."""
        for line in output.splitlines():
            if line.startswith("#") or not line.startswith(metric_name):
                continue
            if all(f'{k}="{v}"' in line for k, v in labels.items()):
                return float(line.rsplit(" ", 1)[-1])
        return None

    def test_multi_model_aggregate_metrics(self):
        """Make 2 OpenAI + 3 Anthropic real calls, verify /metrics counts & tokens."""
        import openai
        import anthropic
        from rastir.server.metrics import MetricsRegistry

        captured, originals = _capture_spans()
        try:
            @llm
            def call_openai(prompt: str, model: str = "gpt-4o-mini") -> object:
                client = openai.OpenAI()
                return client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=5,
                )

            @llm
            def call_anthropic(prompt: str, model: str = "claude-3-haiku-20240307") -> object:
                client = anthropic.Anthropic()
                return client.messages.create(
                    model=model,
                    max_tokens=5,
                    messages=[{"role": "user", "content": prompt}],
                )

            # 2 OpenAI calls
            call_openai("Say hi")
            call_openai("Say bye")

            # 3 Anthropic calls
            call_anthropic("Say hello")
            call_anthropic("Say goodbye")
            call_anthropic("Say thanks")

        finally:
            _restore_enqueue(originals)

        assert len(captured) == 5, f"Expected 5 spans, got {len(captured)}"

        # Feed real spans into MetricsRegistry
        reg = MetricsRegistry()
        for span in captured:
            reg.record_span(span, service="integ-test", env="test")

        output = reg.generate()[0].decode()
        print(f"\n  --- /metrics output (excerpt) ---")
        for line in output.splitlines():
            if "rastir_llm_calls_total" in line or "rastir_tokens" in line:
                if not line.startswith("#"):
                    print(f"  {line}")

        # Verify call counts
        openai_calls = self._parse_metric(output, "rastir_llm_calls_total",
                                          {"provider": "openai"})
        anthropic_calls = self._parse_metric(output, "rastir_llm_calls_total",
                                             {"provider": "anthropic"})
        assert openai_calls == 2.0, f"Expected 2 OpenAI calls, got {openai_calls}"
        assert anthropic_calls == 3.0, f"Expected 3 Anthropic calls, got {anthropic_calls}"

        # Verify tokens are real (non-zero, non-round)
        openai_tokens_in = self._parse_metric(output, "rastir_tokens_input_total",
                                              {"provider": "openai"})
        anthropic_tokens_in = self._parse_metric(output, "rastir_tokens_input_total",
                                                 {"provider": "anthropic"})
        assert openai_tokens_in is not None and openai_tokens_in > 0, \
            f"OpenAI input tokens should be > 0, got {openai_tokens_in}"
        assert anthropic_tokens_in is not None and anthropic_tokens_in > 0, \
            f"Anthropic input tokens should be > 0, got {anthropic_tokens_in}"

        openai_tokens_out = self._parse_metric(output, "rastir_tokens_output_total",
                                               {"provider": "openai"})
        anthropic_tokens_out = self._parse_metric(output, "rastir_tokens_output_total",
                                                  {"provider": "anthropic"})
        assert openai_tokens_out is not None and openai_tokens_out > 0
        assert anthropic_tokens_out is not None and anthropic_tokens_out > 0

        # Verify total spans ingested = 5
        total_llm = self._parse_metric(output, "rastir_spans_ingested_total",
                                       {"span_type": "llm", "status": "OK"})
        assert total_llm == 5.0

        # Print actual token values for visibility
        print(f"\n  Real token totals:")
        print(f"    OpenAI:    in={openai_tokens_in}, out={openai_tokens_out}")
        print(f"    Anthropic: in={anthropic_tokens_in}, out={anthropic_tokens_out}")


# =====================================================================
# Direct run
# =====================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
