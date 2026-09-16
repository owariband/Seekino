from __future__ import annotations

from collections.abc import Callable
from threading import Lock
from typing import Annotated

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from ..schema import VideoChunkSearchResult
from .context import project_search_result
from .output import validate_result
from .state import (
    AgentModelInvocationError, AgentToolInvocationError, ExecutionControl,
    ModelContextBudget, RetrievalCall, RunInputs, context_json,
)


class RetrievalArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20_000)]
    top_k: int = Field(ge=1, le=100)


def build_retrieval_tool(
    search: Callable[..., VideoChunkSearchResult],
    *,
    inputs: RunInputs,
    control: ExecutionControl,
    calls: list[RetrievalCall],
    maximum_top_k: int,
    max_tool_calls: int,
    context_budget: ModelContextBudget,
) -> StructuredTool:
    """Bind fixed scope and records to one Turn, never to shared model state."""
    attempts = 0
    invocation_lock = Lock()

    def retrieve(query: str, top_k: int) -> dict:
        nonlocal attempts
        # One model response may request several tools concurrently. Serializing
        # only this Turn's retrieval keeps the budget and record order consistent.
        with invocation_lock:
            control.check()
            attempts += 1
            if attempts > max_tool_calls:
                raise AgentModelInvocationError("Query Agent tool-call budget exceeded")
            bounded_top_k = min(top_k, inputs["requested_top_k"], maximum_top_k)
            try:
                result = search(
                    query,
                    workspace_id=inputs["workspace_id"],
                    series_ids=list(inputs["series_ids"]),
                    top_k=bounded_top_k,
                )
            except AgentModelInvocationError:
                raise
            except Exception as exc:
                raise AgentToolInvocationError("Video retrieval tool failed") from exc
            control.check()
            result = VideoChunkSearchResult.model_validate(result).model_copy(deep=True)
            validate_result(result, inputs)
            if result.query != query:
                raise AgentModelInvocationError("Retrieval result query does not match the actual call")
            if len(result.hits) > bounded_top_k:
                raise AgentModelInvocationError("Retrieval result exceeds its top_k budget")
            projected = project_search_result(result)
            context_budget.consume("retrieval tool result", context_json(projected))
            calls.append(RetrievalCall(query=query, top_k=bounded_top_k, result=result))
            return projected

    return StructuredTool.from_function(
        func=retrieve,
        name="search_video_chunk_evidence",
        description=(
            "Search the fixed Series Scope of the current Kick for video evidence. "
            "Use query for characters, dialogue, actions and constraints; top_k limits hits. "
            "Returns real videoChunkId/videoId, startMs/endMs and compact evidence. "
            "The scope cannot be changed. First turn must call this tool at least once."
        ),
        args_schema=RetrievalArguments,
    )
