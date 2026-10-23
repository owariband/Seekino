from __future__ import annotations

from collections.abc import Callable
from threading import Lock
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, model_validator

from engine.rag.contracts import VideoChunkSearchResult
from engine.rag.retrieval.fusion import (
    AGENT_RETRIEVAL_STRATEGY_VERSION,
    normalize_hybrid_retrieval_weights,
)

from ...common.errors import (
    AgentModelInvocationError, AgentToolFeedbackError, AgentToolInvocationError,
    AgentToolOutputError,
)
from ...common.state import ExecutionControl, RunInputs
from ...common.tool import AgentTool, build_agent_tool
from ...common.tool_output import cursor_position, page_items
from ...common.tool_result import TextToolResult, ToolResult
from ..errors import VideoRetrievalError
from ..output import merge_retrieval_calls, retrieval_hit, validate_result
from ..context import retrieval_evidence_text
from ..state import (
    LexicalRetrievalChannelWeights,
    RetrievalCall,
    RetrievalChannelContract,
    RetrievalChannelWeights,
)
from .shared import (
    _OptionalCursor, _ToolCallBudget, _bounded_text, _retrieval_card, _start_tool_call,
)

class RetrieveArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=20_000),
    ] | None = None
    top_k: int | None = Field(default=None, ge=1, strict=True)
    channel_weights: RetrievalChannelWeights | None = None
    cursor: _OptionalCursor = None

    @model_validator(mode="after")
    def validate_initial_or_continuation_call(self) -> RetrieveArguments:
        search_values = (self.query, self.top_k, self.channel_weights)
        if self.cursor is None and any(value is None for value in search_values):
            raise ValueError(
                "query, top_k and channel_weights are required without cursor"
            )
        if self.cursor is not None and any(value is not None for value in search_values):
            raise ValueError("cursor continuation must not repeat search arguments")
        return self


class LexicalRetrieveArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=20_000),
    ] | None = None
    top_k: int | None = Field(default=None, ge=1, strict=True)
    channel_weights: LexicalRetrievalChannelWeights | None = None
    cursor: _OptionalCursor = None

    @model_validator(mode="after")
    def validate_initial_or_continuation_call(self) -> LexicalRetrieveArguments:
        search_values = (self.query, self.top_k, self.channel_weights)
        if self.cursor is None and any(value is None for value in search_values):
            raise ValueError(
                "query, top_k and channel_weights are required without cursor"
            )
        if self.cursor is not None and any(value is not None for value in search_values):
            raise ValueError("cursor continuation must not repeat search arguments")
        return self


class ReadItemArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_chunk_id: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=36),
    ]
    cursor: _OptionalCursor = None

def build_retrieve_tool(
    search: Callable[..., VideoChunkSearchResult],
    *,
    inputs: RunInputs,
    control: ExecutionControl,
    calls: list[RetrievalCall],
    maximum_result_bytes: int,
    maximum_catalog_items: int,
    retrieval_channel: RetrievalChannelContract = "transcript",
    budget: _ToolCallBudget | None = None,
    role: Literal["query", "supervisor"] = "query",
) -> AgentTool:
    """Return source evidence directly and retain immutable calls for selection checks."""

    if retrieval_channel not in ("transcript", "lexical"):
        raise ValueError("retrieval_channel must be transcript or lexical")

    catalogs: dict[str, tuple[list[dict[str, Any]], dict[str, Any]]] = {}
    invocation_lock = Lock()

    def retrieve(
        query: str | None = None,
        top_k: int | None = None,
        channel_weights: (
            RetrievalChannelWeights | LexicalRetrievalChannelWeights | None
        ) = None,
        cursor: str | None = None,
    ) -> ToolResult:
        with invocation_lock:
            _start_tool_call(control, budget, "retrieve")
            if cursor is not None:
                try:
                    position = cursor_position(cursor, namespace="retrieve")
                    catalog = catalogs.get(position.resource_id)
                    if catalog is None:
                        raise AgentToolInvocationError("Unknown or expired retrieve cursor")
                    items, metadata = catalog
                    return TextToolResult(
                        success=True,
                        content=page_items(
                            items,
                            namespace="retrieve",
                            resource_id=position.resource_id,
                            cursor=cursor,
                            maximum_items=maximum_catalog_items,
                            maximum_bytes=maximum_result_bytes,
                            metadata=metadata,
                        ),
                    )
                except AgentToolOutputError:
                    raise
                except AgentToolInvocationError as exc:
                    raise AgentToolFeedbackError(
                        "INVALID_CATALOG_CURSOR", str(exc),
                        "Call retrieve again with query, top_k and channel_weights.",
                    ) from exc
            if query is None or top_k is None or channel_weights is None:
                raise AgentToolInvocationError(
                    "retrieve requires query, top_k and channel_weights"
                )
            if retrieval_channel == "lexical":
                requested_weights = LexicalRetrievalChannelWeights.model_validate(
                    channel_weights
                )
                first_weight = requested_weights.lexical
            else:
                requested_weights = RetrievalChannelWeights.model_validate(
                    channel_weights
                )
                first_weight = requested_weights.transcript
            first_effective, semantic_weight, visual_weight = (
                normalize_hybrid_retrieval_weights(
                    first_weight,
                    requested_weights.semantic,
                    requested_weights.visual,
                )
            )
            if retrieval_channel == "lexical":
                effective_weights: (
                    RetrievalChannelWeights | LexicalRetrievalChannelWeights
                ) = LexicalRetrievalChannelWeights(
                    lexical=first_effective,
                    semantic=semantic_weight,
                    visual=visual_weight,
                )
                first_weight_argument = {"lexical_weight": first_effective}
            else:
                effective_weights = RetrievalChannelWeights(
                    transcript=first_effective,
                    semantic=semantic_weight,
                    visual=visual_weight,
                )
                first_weight_argument = {"transcript_weight": first_effective}
            try:
                result = search(
                    query,
                    tenant_id=inputs["tenant_id"],
                    series_ids=list(inputs["series_ids"]),
                    top_k=top_k,
                    text_weight=semantic_weight,
                    vision_weight=visual_weight,
                    **first_weight_argument,
                )
            except AgentModelInvocationError:
                raise
            except Exception as exc:
                raise VideoRetrievalError("Video retrieval tool failed") from exc
            control.check()
            try:
                result = VideoChunkSearchResult.model_validate(result).model_copy(deep=True)
            except ValidationError as exc:
                raise AgentModelInvocationError("Retrieval returned invalid source evidence") from exc
            validate_result(result, inputs)
            if result.query != query:
                raise AgentModelInvocationError(
                    "Retrieval result query does not match the actual call"
                )
            if len(result.hits) > top_k:
                raise AgentModelInvocationError("Retrieval result exceeds the requested top_k")

            call = RetrievalCall(
                query=query,
                top_k=top_k,
                requested_weights=requested_weights.model_copy(deep=True),
                effective_weights=effective_weights,
                result=result,
                role=role,
            )
            merge_retrieval_calls(inputs, [*calls, call])
            calls.append(call)
            resource_id = f"retrieval-{len(catalogs) + 1}"
            items = [_retrieval_card(hit, max(1, (maximum_result_bytes - 1024) // 8)) for hit in result.hits]
            metadata = {
                "query": query,
                "requested_top_k": top_k,
                "result_count": len(items),
                "partial": result.partial,
                "warnings": [_bounded_text(value, 500) for value in result.warnings[:8]],
                "retrieval_strategy": {
                    "version": AGENT_RETRIEVAL_STRATEGY_VERSION,
                    "requested_weights": requested_weights.model_dump(mode="json"),
                    "effective_weights": effective_weights.model_dump(mode="json"),
                },
            }
            catalogs[resource_id] = (items, metadata)
            return TextToolResult(
                success=True,
                content=page_items(
                    items,
                    namespace="retrieve",
                    resource_id=resource_id,
                    cursor=None,
                    maximum_items=maximum_catalog_items,
                    maximum_bytes=maximum_result_bytes,
                    metadata=metadata,
                ),
            )

    return build_agent_tool(
        func=retrieve,
        name="retrieve",
        description=(
            "Search the fixed Series scope and return source evidence with video_chunk_id and canonical times. "
            "Use the returned evidence to assess each claim; ranking alone is not proof. "
            "If evidence_truncated or more context is needed, use read_retrieval_evidence with that ID. "
            "When next_cursor is present, call retrieve with cursor only."
        ),
        args_schema=(
            LexicalRetrieveArguments
            if retrieval_channel == "lexical"
            else RetrieveArguments
        ),
        maximum_result_bytes=maximum_result_bytes,
    )


def build_read_retrieval_evidence_tool(
    *, calls: list[RetrievalCall], control: ExecutionControl,
    maximum_result_bytes: int, maximum_read_chars: int = 16_384,
    budget: _ToolCallBudget | None = None,
) -> AgentTool:
    def read_retrieval_evidence(video_chunk_id: str, cursor: str | None = None) -> ToolResult:
        _start_tool_call(control, budget, "read_retrieval_evidence")
        content = retrieval_evidence_text(retrieval_hit(calls, video_chunk_id))
        part_size = max(1, min(maximum_read_chars, (maximum_result_bytes - 384) // 8))
        parts = [{"text": content[start:start + part_size]}
                 for start in range(0, len(content), part_size)]
        try:
            return TextToolResult(success=True, content=page_items(
                parts, namespace="retrieval-evidence", resource_id=video_chunk_id,
                cursor=cursor, maximum_items=1, maximum_bytes=maximum_result_bytes,
                metadata={"video_chunk_id": video_chunk_id},
            ))
        except AgentToolInvocationError as exc:
            raise AgentToolFeedbackError(
                "INVALID_EVIDENCE_CURSOR", str(exc),
                "Read this video_chunk_id again without cursor, then use its returned next_cursor.",
            ) from exc

    return build_agent_tool(
        func=read_retrieval_evidence, name="read_retrieval_evidence",
        description=(
            "Read complete source evidence for a retrieved video_chunk_id. "
            "Only use when the search excerpt is insufficient. "
            "Continue with the same ID and returned next_cursor; no character ranges are needed."
        ), args_schema=ReadItemArguments, maximum_result_bytes=maximum_result_bytes,
    )
