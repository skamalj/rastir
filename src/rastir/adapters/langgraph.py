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

from rastir.adapters.types import (
    AdapterResult,
    BaseAdapter,
    RequestMetadata,
    TokenDelta,
    detect_provider_from_module,
)


class LangGraphAdapter(BaseAdapter):
    """Adapter for LangGraph state dicts and StateSnapshot objects."""

    name = "langgraph"
    kind = "framework"
    priority = 260

    supports_tokens = True
    supports_streaming = True  # Above LangChain (250)
    supports_request_metadata = True

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
                usage_mode="incremental",
            )

        return TokenDelta()

    # ---- Request-phase metadata ----

    def can_handle_request(self, args: tuple, kwargs: dict) -> bool:
        """Detect CompiledGraph (or CompiledStateGraph) in request args."""
        return self._find_compiled_graph(args, kwargs) is not None

    def extract_request_metadata(
        self, args: tuple, kwargs: dict,
    ) -> RequestMetadata:
        """Walk CompiledGraph nodes to find the underlying chat model.

        Traverses PregelNode → closures / bound runnables to locate
        the chat model, then extracts model name and infers provider
        from the model object's module.
        """
        graph = self._find_compiled_graph(args, kwargs)
        if graph is None:
            return RequestMetadata()

        nodes = getattr(graph, "nodes", None)
        if not isinstance(nodes, dict):
            return RequestMetadata()

        # Walk each node looking for a chat model buried inside.
        for node in nodes.values():
            result = self._extract_model_from_node(node)
            if result:
                return result

        return RequestMetadata()

    def _find_compiled_graph(self, args: tuple, kwargs: dict) -> Any:
        """Find a LangGraph CompiledGraph in args/kwargs."""
        return self._find_in_args(args, kwargs, self._is_compiled_graph)

    @staticmethod
    def _is_compiled_graph(obj: Any) -> bool:
        """Check if obj is a LangGraph CompiledGraph."""
        module = getattr(type(obj), "__module__", "") or ""
        cls_name = type(obj).__name__
        return "langgraph" in module and "Compiled" in cls_name

    def _extract_model_from_node(self, node: Any) -> RequestMetadata | None:
        """Recursively search a graph node for a chat model.

        Supports PregelNode → bound → RunnableBinding → ChatModel
        and PregelNode → func (closure) → __closure__ cells.
        """
        # Try direct attributes: .bound, .func
        for accessor in ("bound", "func"):
            inner = getattr(node, accessor, None)
            if inner is None:
                continue
            result = self._try_extract_from_runnable(inner)
            if result:
                return result

        return None

    def _try_extract_from_runnable(
        self, obj: Any, _seen: set | None = None,
    ) -> RequestMetadata | None:
        """Try to extract model/provider from a Runnable or closure."""
        if _seen is None:
            _seen = set()
        obj_id = id(obj)
        if obj_id in _seen:
            return None
        _seen.add(obj_id)

        # Direct model attribute
        model_name = self._extract_model_attr(obj)
        if model_name:
            module = getattr(type(obj), "__module__", "") or ""
            provider = detect_provider_from_module(module)
            # If provider is unknown (e.g. RunnableBinding from
            # langchain_core), check the inner .bound for a more
            # specific module.
            if provider == "unknown":
                inner = getattr(obj, "bound", None)
                if inner is not None:
                    inner_module = getattr(type(inner), "__module__", "") or ""
                    provider = detect_provider_from_module(inner_module)
            return RequestMetadata(
                span_attributes={"model": model_name, "provider": provider},
            )

        # Traverse chain: .bound, .func, .first (covers RunnableBinding,
        # RunnableCallable, RunnableSequence)
        for attr in ("bound", "func", "first"):
            inner = getattr(obj, attr, None)
            if inner is not None and id(inner) not in _seen:
                result = self._try_extract_from_runnable(inner, _seen)
                if result:
                    return result

        # Closure cells (lambda s: {… bound_model.invoke(…)})
        closure = getattr(obj, "__closure__", None)
        if closure:
            for cell in closure:
                try:
                    cell_val = cell.cell_contents
                except ValueError:
                    continue
                if id(cell_val) not in _seen:
                    result = self._try_extract_from_runnable(cell_val, _seen)
                    if result:
                        return result

        return None

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
