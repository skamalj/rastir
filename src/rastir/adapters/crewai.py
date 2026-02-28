"""CrewAI framework adapter.

Detects CrewAI result objects (``CrewOutput``, ``TaskOutput``) and
extracts task/agent metadata for observability. CrewAI orchestrates
multi-agent workflows, so we track:
  - Agent roles and task descriptions
  - Token usage aggregated across tasks
  - Raw output for downstream processing

Priority: 245 (framework range 200-300, between LangChain at 250
and LlamaIndex at 240).
"""

from __future__ import annotations

from typing import Any

from rastir.adapters.types import AdapterResult, BaseAdapter, TokenDelta


class CrewAIAdapter(BaseAdapter):
    """Adapter for CrewAI CrewOutput / TaskOutput objects."""

    name = "crewai"
    kind = "framework"
    priority = 245

    supports_tokens = True
    supports_streaming = False  # CrewAI doesn't stream

    _KNOWN_CLASSES = frozenset({
        "CrewOutput",
        "TaskOutput",
    })

    _KNOWN_MODULES = (
        "crewai",
        "crewai.crews",
        "crewai.tasks",
    )

    def can_handle(self, result: Any) -> bool:
        cls_name = type(result).__name__
        module = type(result).__module__ or ""
        return (
            cls_name in self._KNOWN_CLASSES
            and any(m in module for m in self._KNOWN_MODULES)
        )

    def transform(self, result: Any) -> AdapterResult:
        extra: dict[str, Any] = {}
        cls_name = type(result).__name__

        if cls_name == "CrewOutput":
            return self._transform_crew_output(result, extra)
        elif cls_name == "TaskOutput":
            return self._transform_task_output(result, extra)

        return AdapterResult(extra_attributes=extra)

    def _transform_crew_output(
        self, result: Any, extra: dict[str, Any]
    ) -> AdapterResult:
        """Extract metadata from CrewOutput."""
        # Token usage — CrewAI aggregates across tasks
        token_usage = getattr(result, "token_usage", None)
        tokens_input = None
        tokens_output = None
        if isinstance(token_usage, dict):
            tokens_input = token_usage.get("prompt_tokens") or token_usage.get(
                "total_tokens"
            )
            tokens_output = token_usage.get("completion_tokens")
            extra["crewai_total_tokens"] = token_usage.get("total_tokens")
            extra["crewai_successful_requests"] = token_usage.get(
                "successful_requests"
            )

        # Tasks metadata
        tasks_output = getattr(result, "tasks_output", None)
        if tasks_output is not None:
            extra["crewai_task_count"] = len(tasks_output)
            task_summaries = []
            for t in tasks_output:
                desc = getattr(t, "description", None)
                agent = getattr(t, "agent", None) or getattr(t, "name", None)
                if desc or agent:
                    task_summaries.append(
                        {"description": str(desc), "agent": str(agent)}
                    )
            if task_summaries:
                extra["crewai_tasks"] = task_summaries

        # Raw output
        raw = getattr(result, "raw", None)
        if raw and isinstance(raw, str):
            extra["crewai_raw_length"] = len(raw)

        # JSON output if available
        json_output = getattr(result, "json_dict", None)
        if json_output is not None:
            extra["crewai_has_json_output"] = True

        # Pydantic output if available
        pydantic_output = getattr(result, "pydantic", None)
        if pydantic_output is not None:
            extra["crewai_has_pydantic_output"] = True

        return AdapterResult(
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            extra_attributes=extra,
        )

    def _transform_task_output(
        self, result: Any, extra: dict[str, Any]
    ) -> AdapterResult:
        """Extract metadata from TaskOutput (single task result)."""
        description = getattr(result, "description", None)
        if description:
            extra["crewai_task_description"] = str(description)

        agent = getattr(result, "agent", None) or getattr(result, "name", None)
        if agent:
            extra["crewai_agent"] = str(agent)

        raw = getattr(result, "raw", None)
        if raw and isinstance(raw, str):
            extra["crewai_raw_length"] = len(raw)

        # Token usage on individual task
        token_usage = getattr(result, "token_usage", None)
        tokens_input = None
        tokens_output = None
        if isinstance(token_usage, dict):
            tokens_input = token_usage.get("prompt_tokens")
            tokens_output = token_usage.get("completion_tokens")

        return AdapterResult(
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            extra_attributes=extra,
        )

    def can_handle_stream(self, chunk: Any) -> bool:
        return False  # CrewAI doesn't stream

    def extract_stream_delta(self, chunk: Any) -> TokenDelta:
        return TokenDelta()
