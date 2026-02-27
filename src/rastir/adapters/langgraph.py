"""LangGraph framework adapter.

Detects LangGraph state dicts and StateSnapshot objects returned by
compiled graph execution (`graph.invoke()`, `graph.ainvoke()`,
`graph.get_state()`).  Unwraps the last AIMessage from the state's
``messages`` list so the LangChain adapter → provider adapter pipeline
can extract model, tokens, and provider metadata.

Also extracts graph-level metadata (message count, node names from
streaming updates) into ``extra_attributes``.

Because LangGraph wraps LangChain objects (which in turn wrap
provider-native responses), this adapter sits *above* the
LangChain adapter in the priority chain.

Priority: 260 (framework range 200-300, above LangChain at 250).
"""

from __future__ import annotations

from typing import Any

from rastir.adapters.types import AdapterResult, BaseAdapter, TokenDelta


class LangGraphAdapter(BaseAdapter):
    """Adapter for LangGraph state dicts and StateSnapshot objects."""

    name = "langgraph"
    kind = "framework"
    priority = 260  # Above LangChain (250)

    def can_handle(self, result: Any) -> bool:
        """Detect LangGraph response patterns.

        Matches:
        1. StateSnapshot (NamedTuple from langgraph.types) — has ``values``,
           ``next``, ``tasks`` fields and module contains 'langgraph'.
        2. Dict with a ``messages`` key containing LangChain message objects
           (objects whose module contains 'langchain').
        """
        # Case 1: StateSnapshot
        if self._is_state_snapshot(result):
            return True

        # Case 2: State dict from graph.invoke()
        if isinstance(result, dict) and "messages" in result:
            messages = result["messages"]
            if isinstance(messages, (list, tuple)) and len(messages) > 0:
                last = messages[-1]
                module = getattr(type(last), "__module__", "") or ""
                cls_name = type(last).__name__
                if "langchain" in module and cls_name in (
                    "AIMessage",
                    "AIMessageChunk",
                    "HumanMessage",
                    "SystemMessage",
                    "ToolMessage",
                    "FunctionMessage",
                ):
                    return True

        return False

    def transform(self, result: Any) -> AdapterResult:
        """Extract the last AIMessage and graph-level metadata.

        The unwrapped AIMessage is passed to the LangChain adapter
        (priority 250) in the next framework resolution pass.
        """
        extras: dict[str, Any] = {}

        # Handle StateSnapshot
        if self._is_state_snapshot(result):
            return self._transform_snapshot(result)

        # Handle state dict
        if isinstance(result, dict) and "messages" in result:
            return self._transform_state_dict(result)

        return AdapterResult(extra_attributes=extras)

    def can_handle_stream(self, chunk: Any) -> bool:
        """Detect LangGraph streaming updates.

        In ``stream_mode="updates"``, chunks are dicts keyed by node
        name.  In ``stream_mode="messages"``, chunks are
        ``(BaseMessageChunk, metadata_dict)`` tuples.
        """
        # stream_mode="messages" → tuple (BaseMessageChunk, dict)
        if isinstance(chunk, tuple) and len(chunk) == 2:
            msg, meta = chunk
            module = getattr(type(msg), "__module__", "") or ""
            if "langchain" in module and isinstance(meta, dict):
                return True

        return False

    def extract_stream_delta(self, chunk: Any) -> TokenDelta:
        """Extract token delta from LangGraph streaming chunks.

        For ``stream_mode="messages"`` tuples, extracts model and usage
        from the metadata dict.
        """
        if isinstance(chunk, tuple) and len(chunk) == 2:
            msg, meta = chunk
            model = None
            tokens_input = None
            tokens_output = None
            provider = None

            if isinstance(meta, dict):
                # LangGraph message streaming metadata
                model = meta.get("model_name") or meta.get("model")
                ls_meta = meta.get("ls_model_name")
                if ls_meta and not model:
                    model = ls_meta

                # Provider detection from model name or metadata
                ls_provider = meta.get("ls_provider")
                if ls_provider:
                    provider = ls_provider

            # Check if the message chunk itself has usage_metadata
            usage_meta = getattr(msg, "usage_metadata", None)
            if usage_meta is not None:
                if isinstance(usage_meta, dict):
                    tokens_input = usage_meta.get("input_tokens")
                    tokens_output = usage_meta.get("output_tokens")
                else:
                    tokens_input = getattr(usage_meta, "input_tokens", None)
                    tokens_output = getattr(usage_meta, "output_tokens", None)

            return TokenDelta(
                model=model,
                provider=provider,
                tokens_input=tokens_input,
                tokens_output=tokens_output,
            )

        return TokenDelta()

    # ---- Private helpers ----

    @staticmethod
    def _is_state_snapshot(result: Any) -> bool:
        """Detect langgraph.types.StateSnapshot (NamedTuple)."""
        cls_name = type(result).__name__
        module = getattr(type(result), "__module__", "") or ""
        return cls_name == "StateSnapshot" and "langgraph" in module

    def _transform_snapshot(self, snapshot: Any) -> AdapterResult:
        """Extract metadata from a StateSnapshot."""
        extras: dict[str, Any] = {}

        # StateSnapshot fields: values, next, config, metadata, tasks, ...
        values = getattr(snapshot, "values", None)
        next_nodes = getattr(snapshot, "next", None)
        tasks = getattr(snapshot, "tasks", None)
        metadata = getattr(snapshot, "metadata", None)

        if next_nodes:
            extras["langgraph_next_nodes"] = list(next_nodes)

        if tasks:
            extras["langgraph_task_count"] = len(tasks)
            task_names = []
            for t in tasks:
                name = getattr(t, "name", None)
                if name:
                    task_names.append(name)
            if task_names:
                extras["langgraph_task_names"] = task_names

        if isinstance(metadata, dict):
            step = metadata.get("step")
            if step is not None:
                extras["langgraph_step"] = step
            source = metadata.get("source")
            if source:
                extras["langgraph_source"] = source

        # Try to unwrap AIMessage from values
        unwrapped = None
        if isinstance(values, dict) and "messages" in values:
            unwrapped = self._extract_last_ai_message(values["messages"])

        return AdapterResult(
            unwrapped_result=unwrapped,
            extra_attributes=extras,
        )

    def _transform_state_dict(self, state: dict) -> AdapterResult:
        """Extract metadata from a LangGraph state dict."""
        extras: dict[str, Any] = {}
        messages = state.get("messages", [])

        if isinstance(messages, (list, tuple)):
            extras["langgraph_message_count"] = len(messages)

            # Count message types
            ai_count = 0
            tool_count = 0
            for msg in messages:
                cls_name = type(msg).__name__
                if cls_name in ("AIMessage", "AIMessageChunk"):
                    ai_count += 1
                elif cls_name == "ToolMessage":
                    tool_count += 1
            if ai_count:
                extras["langgraph_ai_message_count"] = ai_count
            if tool_count:
                extras["langgraph_tool_message_count"] = tool_count

        # Unwrap the last AIMessage for the LangChain adapter
        unwrapped = self._extract_last_ai_message(messages)

        return AdapterResult(
            unwrapped_result=unwrapped,
            extra_attributes=extras,
        )

    @staticmethod
    def _extract_last_ai_message(messages: Any) -> Any:
        """Find the last AIMessage in a messages list."""
        if not isinstance(messages, (list, tuple)):
            return None

        for msg in reversed(messages):
            cls_name = type(msg).__name__
            if cls_name in ("AIMessage", "AIMessageChunk"):
                return msg

        return None
