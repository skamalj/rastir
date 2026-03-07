"""Generate error spans across multiple frameworks for dashboard testing."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

GEMINI_KEY = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")
OPENAI_KEY = os.environ.get("API_OPENAI_KEY") or os.environ.get("OPENAI_API_KEY", "")
os.environ["OPENAI_API_KEY"] = OPENAI_KEY

import rastir
from rastir import configure, langgraph_agent, adk_agent, strands_agent, llamaindex_agent

configure(service="error-test", push_url="http://localhost:8080")

results = []


async def main():
    # 1. LangGraph with a tool that raises (tool error)
    print("  [1] LangGraph tool error...")
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI
        from langchain_core.tools import tool as lc_tool
        from langgraph.prebuilt import create_react_agent

        @lc_tool
        def failing_tool(x: int) -> int:
            """A tool that always fails."""
            raise ValueError("Tool computation failed: division by zero")

        llm = ChatGoogleGenerativeAI(
            model="gemini-2.5-flash", google_api_key=GEMINI_KEY, temperature=0
        )
        graph = create_react_agent(llm, [failing_tool])

        @langgraph_agent(agent_name="lg_error_test")
        async def lg_err(g):
            return await g.ainvoke(
                {"messages": [{"role": "user", "content": "Call failing_tool with x=5"}]}
            )

        await lg_err(graph)
        results.append(("[1] LangGraph tool error", "completed (error in tool span)"))
    except Exception as e:
        results.append(("[1] LangGraph tool error", f"ERROR: {type(e).__name__}"))

    # 2. ADK with a tool that raises (tool error)
    print("  [2] ADK tool error...")
    try:
        from google.adk.agents import Agent as AdkAgent
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService
        from google import genai

        def adk_failing_tool(x: int) -> int:
            """A tool that always fails with a runtime error."""
            raise RuntimeError("ADK tool crashed unexpectedly")

        adk_client = genai.Client(api_key=GEMINI_KEY)
        adk_agent_obj = AdkAgent(
            name="adk_error_agent",
            model="gemini-2.5-flash",
            tools=[adk_failing_tool],
            client=adk_client,
            instruction="You must call adk_failing_tool with x=5. Do not give up.",
        )
        runner = Runner(
            agent=adk_agent_obj,
            app_name="error-test",
            session_service=InMemorySessionService(),
        )

        @adk_agent(agent_name="adk_error_test")
        async def adk_err(r):
            from google.genai import types

            events = []
            async for ev in r.run_async(
                user_id="u1",
                session_id="s1",
                new_message=types.Content(
                    role="user",
                    parts=[types.Part(text="Call adk_failing_tool with x=5")],
                ),
            ):
                events.append(ev)
            return events

        await adk_err(runner)
        results.append(("[2] ADK tool error", "completed (error in tool span)"))
    except Exception as e:
        results.append(("[2] ADK tool error", f"ERROR: {type(e).__name__}"))

    # 3. Strands with a tool that raises (tool error)
    print("  [3] Strands tool error...")
    try:
        from strands import Agent as StrandsAgentCls
        from strands.models.bedrock import BedrockModel
        from strands.tools import tool as strands_tool

        @strands_tool
        def strands_failing_tool(x: int) -> int:
            """A tool that always fails with a timeout."""
            raise TimeoutError("Strands tool timed out")

        strands_model = BedrockModel(
            model_id="apac.anthropic.claude-sonnet-4-20250514-v1:0",
            region_name="ap-south-1",
        )
        strands_err_agent = StrandsAgentCls(
            model=strands_model,
            tools=[strands_failing_tool],
            system_prompt="You must call strands_failing_tool with x=5.",
        )

        @strands_agent(agent_name="strands_error_test")
        def strands_err(a):
            return a("Call strands_failing_tool with x=5")

        strands_err(strands_err_agent)
        results.append(("[3] Strands tool error", "completed (error in tool span)"))
    except Exception as e:
        results.append(("[3] Strands tool error", f"ERROR: {type(e).__name__}"))

    # 4. LlamaIndex with a tool that raises (tool error)
    print("  [4] LlamaIndex tool error...")
    try:
        from llama_index.core.agent.workflow import FunctionAgent
        from llama_index.core.tools import FunctionTool
        from llama_index.llms.openai import OpenAI as LlamaOpenAI

        def li_failing_tool(x: int) -> int:
            """A tool that always fails with a connection error."""
            raise ConnectionError("LlamaIndex tool connection refused")

        li_llm = LlamaOpenAI(model="gpt-4o-mini", api_key=OPENAI_KEY, temperature=0)
        li_tool = FunctionTool.from_defaults(fn=li_failing_tool)
        li_agent_obj = FunctionAgent(
            name="li-error", llm=li_llm, tools=[li_tool], streaming=False
        )

        @llamaindex_agent(agent_name="li_error_test")
        async def li_err(a):
            handler = a.run(user_msg="Call li_failing_tool with x=5")
            async for ev in handler.stream_events():
                pass
            return await handler

        await li_err(li_agent_obj)
        results.append(("[4] LlamaIndex tool error", "completed (error in tool span)"))
    except Exception as e:
        results.append(("[4] LlamaIndex tool error", f"ERROR: {type(e).__name__}"))

    # 5. LangGraph with bad model name (LLM error)
    print("  [5] LangGraph LLM error (bad model)...")
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI
        from langgraph.prebuilt import create_react_agent
        from langchain_core.tools import tool as lc_tool2

        @lc_tool2
        def dummy_tool(x: int) -> int:
            """A dummy tool."""
            return x

        bad_llm = ChatGoogleGenerativeAI(
            model="nonexistent-model-xyz", google_api_key=GEMINI_KEY, temperature=0
        )
        bad_graph = create_react_agent(bad_llm, [dummy_tool])

        @langgraph_agent(agent_name="lg_llm_error_test")
        async def lg_llm_err(g):
            return await g.ainvoke(
                {"messages": [{"role": "user", "content": "call dummy_tool with x=1"}]}
            )

        await lg_llm_err(bad_graph)
        results.append(("[5] LangGraph LLM error", "NO ERROR (unexpected)"))
    except Exception as e:
        results.append(("[5] LangGraph LLM error", f"ERROR: {type(e).__name__}"))

    await asyncio.sleep(2)  # let spans flush
    print("\n  RESULTS:")
    for label, status in results:
        marker = "✓" if "error" in status.lower() or "ERROR" in status else "?"
        print(f"  {marker} {label}: {status}")


asyncio.run(main())
