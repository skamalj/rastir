"""Integration test — LangGraph + OpenAI with Rastir instrumentation.

Builds a real LangGraph agent with two tools (calculator + weather),
runs it against OpenAI, and verifies that Rastir adapters correctly
extract model, tokens, provider, and graph metadata from the live
LangGraph state dict.

Requirements:
    pip install langgraph langchain-openai
    export OPENAI_API_KEY=sk-...

Usage:
    python tests/test_langgraph_integration.py
    # or
    pytest tests/test_langgraph_integration.py -v -s
"""

from __future__ import annotations

import json
import os
import sys

import pytest

# ── Resolve API key ──────────────────────────────────────────────────
_api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("API_OPENAI_KEY")
if _api_key and not os.environ.get("OPENAI_API_KEY"):
    os.environ["OPENAI_API_KEY"] = _api_key

skip_no_key = pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set — skipping live integration test",
)

# ── Imports ──────────────────────────────────────────────────────────
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
from langchain_core.tools import tool as lc_tool
from langgraph.graph import StateGraph, MessagesState, START, END
from langgraph.prebuilt import ToolNode

from rastir import configure, stop_exporter
from rastir.config import reset_config
from rastir.decorators import agent, llm, tool
from rastir.adapters.registry import resolve, resolve_stream_chunk


# =====================================================================
# Tools (plain Python functions registered as LangChain tools)
# =====================================================================

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
    # Fake weather data
    weather = {
        "london": "15°C, cloudy",
        "paris": "18°C, sunny",
        "tokyo": "22°C, rainy",
        "new york": "12°C, windy",
    }
    return weather.get(city.lower(), f"Weather data not available for {city}")


# =====================================================================
# Build the LangGraph agent
# =====================================================================

def build_agent(model_name: str = "gpt-4o-mini"):
    """Build a LangGraph ReAct agent with tools."""
    tools = [calculator, get_weather]
    model = ChatOpenAI(model=model_name, temperature=0).bind_tools(tools)

    def chatbot(state: MessagesState):
        """LLM node — calls the model with tool bindings."""
        return {"messages": [model.invoke(state["messages"])]}

    def should_continue(state: MessagesState):
        """Routing function — check if the last message has tool calls."""
        last = state["messages"][-1]
        if hasattr(last, "tool_calls") and last.tool_calls:
            return "tools"
        return END

    # Build graph
    graph = StateGraph(MessagesState)
    graph.add_node("chatbot", chatbot)
    graph.add_node("tools", ToolNode(tools))
    graph.add_edge(START, "chatbot")
    graph.add_conditional_edges("chatbot", should_continue, {"tools": "tools", END: END})
    graph.add_edge("tools", "chatbot")

    return graph.compile()


# =====================================================================
# Tests
# =====================================================================

@skip_no_key
class TestLangGraphIntegration:
    """Live integration tests — requires OpenAI API key."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        """Configure Rastir (no push — just test adapter resolution)."""
        reset_config()
        configure(service="langgraph-test", env="integration")
        yield
        try:
            stop_exporter(timeout=1.0)
        except Exception:
            pass
        reset_config()

    def test_simple_chat_no_tools(self):
        """Simple question that doesn't need tools — single LLM call."""
        app = build_agent()
        result = app.invoke({"messages": [HumanMessage("Say hello in exactly 3 words.")]})

        # Verify it's a dict with messages
        assert isinstance(result, dict)
        assert "messages" in result
        messages = result["messages"]
        assert len(messages) >= 2  # at least HumanMessage + AIMessage

        # Last message should be AIMessage
        last = messages[-1]
        assert isinstance(last, AIMessage)
        print(f"\n  Response: {last.content}")

        # ── Test Rastir adapter resolution ──
        ar = resolve(result)
        assert ar is not None
        print(f"  AdapterResult: provider={ar.provider}, model={ar.model}")
        print(f"  Tokens: in={ar.tokens_input}, out={ar.tokens_output}")
        print(f"  Extra attrs: {ar.extra_attributes}")

        # LangGraph adapter should have extracted message count
        assert ar.extra_attributes.get("langgraph_message_count") >= 2
        assert ar.extra_attributes.get("langgraph_ai_message_count") >= 1

        # Should have model and token info (from LangChain → OpenAI chain)
        # These come through extra_attributes from the LangChain adapter
        has_model = ar.model is not None or ar.extra_attributes.get("model") is not None
        assert has_model, "Model should be extracted from the response"

    def test_tool_call_calculator(self):
        """Question that triggers the calculator tool."""
        app = build_agent()
        result = app.invoke(
            {"messages": [HumanMessage("What is 137 * 42? Use the calculator tool.")]}
        )

        assert isinstance(result, dict)
        messages = result["messages"]
        print(f"\n  Total messages: {len(messages)}")
        for i, msg in enumerate(messages):
            print(f"  [{i}] {type(msg).__name__}: {str(msg.content)[:80]}")

        # Should have: HumanMessage, AIMessage(tool_call), ToolMessage, AIMessage(final)
        assert len(messages) >= 4, f"Expected at least 4 messages in tool-call flow, got {len(messages)}"

        # Check tool message exists
        tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
        assert len(tool_msgs) >= 1, "Expected at least one ToolMessage"
        print(f"  Tool result: {tool_msgs[0].content}")

        # Final answer should contain 5754
        last = messages[-1]
        assert isinstance(last, AIMessage)
        assert "5754" in last.content, f"Expected 5754 in response, got: {last.content}"

        # ── Adapter resolution ──
        ar = resolve(result)
        assert ar is not None
        assert ar.extra_attributes.get("langgraph_message_count") >= 4
        assert ar.extra_attributes.get("langgraph_tool_message_count") >= 1
        print(f"  Adapter: model={ar.model or ar.extra_attributes.get('model')}, "
              f"tokens_in={ar.tokens_input or ar.extra_attributes.get('tokens_input')}")

    def test_tool_call_weather(self):
        """Question that triggers the weather tool."""
        app = build_agent()
        result = app.invoke(
            {"messages": [HumanMessage("What's the weather in London? Use the get_weather tool.")]}
        )

        messages = result["messages"]
        print(f"\n  Total messages: {len(messages)}")
        for i, msg in enumerate(messages):
            print(f"  [{i}] {type(msg).__name__}: {str(msg.content)[:80]}")

        # Should have tool call flow
        tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
        assert len(tool_msgs) >= 1
        # Weather response should mention temperature
        assert "15" in tool_msgs[0].content or "cloudy" in tool_msgs[0].content

        # Adapter resolution
        ar = resolve(result)
        assert ar is not None
        assert ar.extra_attributes.get("langgraph_tool_message_count") >= 1

    def test_multi_tool_call(self):
        """Question that may trigger multiple tools."""
        app = build_agent()
        result = app.invoke(
            {"messages": [HumanMessage(
                "First calculate 99 * 11 using calculator, then tell me the weather in Tokyo using get_weather."
            )]}
        )

        messages = result["messages"]
        print(f"\n  Total messages: {len(messages)}")
        for i, msg in enumerate(messages):
            print(f"  [{i}] {type(msg).__name__}: {str(msg.content)[:80]}")

        tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
        print(f"  Tool calls: {len(tool_msgs)}")
        assert len(tool_msgs) >= 2, f"Expected at least 2 tool calls, got {len(tool_msgs)}"

        ar = resolve(result)
        assert ar is not None
        assert ar.extra_attributes.get("langgraph_tool_message_count") >= 2

    def test_streaming(self):
        """Test streaming with stream_mode='messages'."""
        app = build_agent()
        chunks_received = 0
        deltas_resolved = 0
        last_model = None

        for chunk in app.stream(
            {"messages": [HumanMessage("Say 'test' and nothing else.")]},
            stream_mode="messages",
        ):
            chunks_received += 1
            # Each chunk is (BaseMessage, metadata_dict)
            if isinstance(chunk, tuple) and len(chunk) == 2:
                msg, meta = chunk
                delta = resolve_stream_chunk(chunk)
                if delta is not None:
                    deltas_resolved += 1
                    if delta.model:
                        last_model = delta.model

        print(f"\n  Chunks received: {chunks_received}")
        print(f"  Deltas resolved by adapter: {deltas_resolved}")
        print(f"  Last model seen: {last_model}")
        assert chunks_received > 0
        assert deltas_resolved > 0

    def test_with_rastir_decorators(self):
        """Full end-to-end: Rastir decorators wrapping a LangGraph agent."""
        app = build_agent()

        # Captured spans for inspection
        captured_spans = []

        # Patch the exporter to capture spans instead of sending them
        import rastir.decorators as dec
        original_queue_span = dec.enqueue_span

        def mock_queue_span(span):
            captured_spans.append({
                "name": span.name,
                "span_type": span.span_type.value,
                "status": span.status.value,
                "start_time": span.start_time,
                "end_time": span.end_time,
                "duration_seconds": span.duration_seconds,
                "attributes": dict(span.attributes),
                "trace_id": span.trace_id,
                "span_id": span.span_id,
            })

        dec.enqueue_span = mock_queue_span

        try:
            @agent(agent_name="test_agent")
            def run_agent(query: str):
                return app.invoke({"messages": [HumanMessage(query)]})

            result = run_agent("What is 7 * 8? Use the calculator tool.")

            print(f"\n  Result type: {type(result)}")
            assert isinstance(result, dict)
            assert "messages" in result

            # Check captured spans
            print(f"  Captured spans: {len(captured_spans)}")
            for span in captured_spans:
                print(f"    - {span['name']}: type={span['span_type']}, "
                      f"status={span['status']}, "
                      f"dur={span['duration_seconds']*1000:.0f}ms")

            assert len(captured_spans) >= 1
            agent_span = captured_spans[0]
            assert agent_span["span_type"] == "agent"
            assert agent_span["status"] == "OK"
            assert agent_span["attributes"].get("agent_name") == "test_agent"
            assert agent_span["end_time"] is not None
            assert agent_span["duration_seconds"] > 0
            print(f"  Agent span duration: {agent_span['duration_seconds']*1000:.0f}ms")

        finally:
            dec.enqueue_span = original_queue_span


# =====================================================================
# Direct run
# =====================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
