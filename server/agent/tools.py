from __future__ import annotations

from collections.abc import Callable, Collection
from threading import Lock
from typing import Annotated

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from engine.retrieval.video_retrieval import normalize_hybrid_retrieval_weights

from ..constants.retrieval import AGENT_RETRIEVAL_STRATEGY_VERSION
from ..schema import VideoChunkSearchResult
from .context import project_search_result
from .output import validate_result
from .state import (
    AgentModelInvocationError, AgentToolInvocationError, ExecutionControl,
    ModelContextBudget, RetrievalCall, RetrievalChannelWeights, RunInputs, context_json,
)


class RetrievalArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=20_000),
        Field(
            description=(
                "Standalone semantic/visual description, or a literal quote or "
                "distinctive phrase in the stored transcript language for a "
                "Transcript-heavy call."
            )
        ),
    ]
    top_k: int = Field(
        ge=1,
        le=100,
        description="Maximum fused candidates requested for this call; Server limits still apply.",
    )
    channel_weights: RetrievalChannelWeights = Field(
        description=(
            "Relative Transcript, Semantic and Visual influence for this call. "
            "Values are normalized by the Server before Weighted RRF fusion."
        ),
    )


def build_retrieval_tool(
    search: Callable[..., VideoChunkSearchResult],
    *,
    inputs: RunInputs,
    control: ExecutionControl,
    calls: list[RetrievalCall],
    maximum_top_k: int,
    max_tool_calls: int,
    context_budget: ModelContextBudget,
    previously_returned_video_chunk_ids: Collection[str] = (),
) -> StructuredTool:
    """Bind fixed scope and records to one Turn, never to shared model state."""
    attempts = 0
    invocation_lock = Lock()

    def retrieve(
        query: str,
        top_k: int,
        channel_weights: RetrievalChannelWeights,
    ) -> dict:
        nonlocal attempts
        # One model response may request several tools concurrently. Serializing
        # only this Turn's retrieval keeps the budget and record order consistent.
        with invocation_lock:
            control.check()
            channel_weights = RetrievalChannelWeights.model_validate(channel_weights)
            attempts += 1
            if attempts > max_tool_calls:
                raise AgentModelInvocationError("Query Agent tool-call budget exceeded")
            bounded_top_k = min(top_k, inputs["requested_top_k"], maximum_top_k)
            transcript_weight, semantic_weight, visual_weight = (
                normalize_hybrid_retrieval_weights(
                    channel_weights.transcript,
                    channel_weights.semantic,
                    channel_weights.visual,
                )
            )
            effective_weights = RetrievalChannelWeights(
                transcript=transcript_weight,
                semantic=semantic_weight,
                visual=visual_weight,
            )
            try:
                result = search(
                    query,
                    workspace_id=inputs["workspace_id"],
                    series_ids=list(inputs["series_ids"]),
                    top_k=bounded_top_k,
                    transcript_weight=transcript_weight,
                    text_weight=semantic_weight,
                    vision_weight=visual_weight,
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
            previously_returned_ids = set(previously_returned_video_chunk_ids) | {
                hit.candidate.video_chunk_id
                for call in calls
                for hit in call.result.hits
            }
            projected = project_search_result(
                result,
                previously_returned_video_chunk_ids=previously_returned_ids,
            )
            projected["retrievalStrategy"] = {
                "version": AGENT_RETRIEVAL_STRATEGY_VERSION,
                "requestedWeights": channel_weights.model_dump(mode="json"),
                "effectiveWeights": effective_weights.model_dump(mode="json"),
            }
            context_budget.consume("retrieval tool result", context_json(projected))
            calls.append(RetrievalCall(
                query=query,
                top_k=bounded_top_k,
                requested_weights=channel_weights.model_copy(deep=True),
                effective_weights=effective_weights,
                result=result,
            ))
            return projected

    return StructuredTool.from_function(
        func=retrieve,
        name="search_video_chunk_evidence",
        description=(
            "Search the fixed Series Scope of the current Kick for video evidence. "
            "Use query for characters, dialogue, actions and constraints; top_k limits hits. "
            "Choose channel_weights for every call: transcript is literal ASR wording, "
            "semantic is Pro-parsed event/dialogue meaning, and visual is actual video "
            "appearance and action. Use a quote or distinctive phrase for transcript-heavy "
            "calls, written in the stored ASR language indicated by the Series context. "
            "Returns real videoChunkId/videoId, startMs/endMs and compact evidence. "
            "Later calls return full evidence only for new chunks; reusedHits identifies "
            "chunks whose full evidence is already present in an earlier Tool result. "
            "The scope cannot be changed. First turn must call this tool at least once."
        ),
        args_schema=RetrievalArguments,
    )
