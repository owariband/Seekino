from __future__ import annotations

from collections.abc import Callable
from threading import Lock
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from engine.retrieval.video_retrieval import normalize_hybrid_retrieval_weights

from ....constants.retrieval import AGENT_RETRIEVAL_STRATEGY_VERSION
from ....schema import VideoChunkSearchResult
from ...common.errors import (
    AgentModelInvocationError, AgentToolFeedbackError, AgentToolInvocationError,
    AgentToolOutputError,
)
from ...common.state import ExecutionControl, RunInputs
from ...common.tool import AgentTool, build_agent_tool
from ...common.tool_output import cursor_position, page_items
from ...common.tool_result import TextToolResult, ToolResult
from ..errors import VideoRetrievalError
from ..output import validate_result
from ..state import RetrievalCall, RetrievalChannelWeights
from ..workspace import AgentWorkspace
from .shared import (
    _OptionalCursor, _ToolCallBudget, _bounded_text, _detail_sections,
    _read_detail_section, _retrieval_card, _start_tool_call,
)

class RetrieveArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=20_000),
    ] | None = None
    top_k: int | None = Field(default=None, ge=1, le=100)
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


class ReadItemArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_chunk_id: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=36),
    ]
    section: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=80),
    ] = "overview"
    start: int = Field(default=0, ge=0, strict=True)
    end: int | None = Field(default=None, ge=0, strict=True)

def build_retrieve_tool(
    search: Callable[..., VideoChunkSearchResult],
    *,
    workspace: AgentWorkspace,
    inputs: RunInputs,
    control: ExecutionControl,
    calls: list[RetrievalCall],
    maximum_top_k: int,
    maximum_result_bytes: int,
    maximum_catalog_items: int,
    budget: _ToolCallBudget | None = None,
) -> AgentTool:
    """Return bounded discovery cards while retaining canonical hits in Workspace."""

    catalogs: dict[str, tuple[list[dict[str, Any]], dict[str, Any]]] = {}
    invocation_lock = Lock()

    def retrieve(
        query: str | None = None,
        top_k: int | None = None,
        channel_weights: RetrievalChannelWeights | None = None,
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
                            metadata={
                                **metadata,
                                "draft_item_count": workspace.draft_item_count(),
                            },
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
            channel_weights = RetrievalChannelWeights.model_validate(channel_weights)
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
                raise VideoRetrievalError("Video retrieval tool failed") from exc
            control.check()
            result = VideoChunkSearchResult.model_validate(result).model_copy(deep=True)
            validate_result(result, inputs)
            if result.query != query:
                raise AgentModelInvocationError(
                    "Retrieval result query does not match the actual call"
                )
            if len(result.hits) > bounded_top_k:
                raise AgentModelInvocationError("Retrieval result exceeds its top_k budget")

            workspace.record_retrieval(result)
            calls.append(RetrievalCall(
                query=query,
                top_k=bounded_top_k,
                requested_weights=channel_weights.model_copy(deep=True),
                effective_weights=effective_weights,
                result=result,
            ))
            resource_id = f"retrieval-{len(catalogs) + 1}"
            items = [_retrieval_card(hit) for hit in result.hits]
            metadata = {
                "query": query,
                "result_count": len(items),
                "draft_item_count": workspace.draft_item_count(),
                "partial": result.partial,
                "warnings": [_bounded_text(value, 500) for value in result.warnings[:8]],
                "retrieval_strategy": {
                    "version": AGENT_RETRIEVAL_STRATEGY_VERSION,
                    "requested_weights": channel_weights.model_dump(mode="json"),
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
            "Search the fixed Series Scope and return a bounded Evidence catalog. "
            "Catalog entries contain descriptions and video_chunk_id handles, never "
            "the full Evidence. Use read_trieve_item to inspect a selected handle. "
            "When next_cursor is present, call retrieve with cursor only."
        ),
        args_schema=RetrieveArguments,
        maximum_result_bytes=maximum_result_bytes,
        draft_item_count=workspace.draft_item_count,
    )


def build_read_trieve_item_tool(
    *,
    workspace: AgentWorkspace,
    control: ExecutionControl,
    maximum_result_bytes: int,
    maximum_read_chars: int = 16_384,
    budget: _ToolCallBudget | None = None,
) -> AgentTool:
    def read_trieve_item(
        video_chunk_id: str,
        section: str = "overview",
        start: int = 0,
        end: int | None = None,
    ) -> ToolResult:
        _start_tool_call(control, budget, "read_trieve_item")
        sections = _detail_sections(workspace.retrieval_snapshot(video_chunk_id))
        return TextToolResult(
            success=True,
            content=_read_detail_section(
                video_chunk_id, sections,
                section=section,
                start=start,
                end=end,
                maximum_result_bytes=maximum_result_bytes,
                maximum_read_chars=maximum_read_chars,
            ),
        )

    return build_agent_tool(
        func=read_trieve_item,
        name="read_trieve_item",
        description=(
            "Read a stable Evidence snapshot by video_chunk_id. First request "
            "section='overview' to get section names and character lengths. Then "
            "read a named section with start/end character offsets [start,end); "
            "omit end to read to this section's end, and never reuse another ID's end. "
            "If a range is too large, retry with the suggested smaller end."
        ),
        args_schema=ReadItemArguments,
        maximum_result_bytes=maximum_result_bytes,
    )
