from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass, field
import json
from threading import Lock
from typing import Annotated, Any

from langchain_core.tools import StructuredTool, ToolException
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from engine.retrieval.video_retrieval import normalize_hybrid_retrieval_weights

from ..constants.retrieval import AGENT_RETRIEVAL_STRATEGY_VERSION
from ..schema import VideoChunkSearchHit, VideoChunkSearchResult
from .errors import (
    AgentModelInvocationError, AgentToolInvocationError, InvalidDraftRangeError,
)
from .output import validate_result
from .state import (
    ChatVideoBlock, DraftItem, ExecutionControl, RetrievalCall,
    RetrievalChannelWeights, RunInputs,
)
from .tool_output import bounded_json, cursor_position, page_items, page_text
from .workspace import AgentWorkspace


_OptionalCursor = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000),
] | None


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
    cursor: _OptionalCursor = None


class DraftCatalogArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cursor: _OptionalCursor = None


class UpdateDraftTemArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_chunk_id: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=36),
    ]
    start_ms: int = Field(strict=True)
    end_ms: int = Field(strict=True)
    claim: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000),
    ]


class RemoveItemArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_chunk_id: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=36),
    ]


class UpdateVideoItemArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_chunk_id: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=36),
    ]
    claim: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000),
    ]


@dataclass(slots=True)
class _ToolCallBudget:
    maximum: int
    role: str
    used: int = 0
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if (
            isinstance(self.maximum, bool)
            or not isinstance(self.maximum, int)
            or self.maximum <= 0
        ):
            raise ValueError("maximum Tool calls must be a positive integer")

    def consume(self, tool_name: str) -> None:
        with self._lock:
            self.used += 1
            if self.used > self.maximum:
                raise AgentModelInvocationError(
                    f"{self.role} Agent Tool-call budget exceeded before {tool_name}"
                )


@dataclass(slots=True)
class DraftReadAudit:
    catalog_complete: bool = False
    item_ids: set[str] = field(default_factory=set)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def mark_catalog_complete(self) -> None:
        with self._lock:
            self.catalog_complete = True

    def mark_item_complete(self, video_chunk_id: str) -> None:
        with self._lock:
            self.item_ids.add(video_chunk_id)

    def covers(self, video_chunk_ids: Collection[str]) -> bool:
        with self._lock:
            return self.catalog_complete and set(video_chunk_ids) <= self.item_ids


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
) -> StructuredTool:
    """Return bounded discovery cards while retaining canonical hits in Workspace."""

    catalogs: dict[str, tuple[list[dict[str, Any]], dict[str, Any]]] = {}
    invocation_lock = Lock()

    def retrieve(
        query: str | None = None,
        top_k: int | None = None,
        channel_weights: RetrievalChannelWeights | None = None,
        cursor: str | None = None,
    ) -> str:
        with invocation_lock:
            _start_tool_call(control, budget, "retrieve")
            if cursor is not None:
                position = cursor_position(cursor, namespace="retrieve")
                catalog = catalogs.get(position.resource_id)
                if catalog is None:
                    raise AgentToolInvocationError("Unknown or expired retrieve cursor")
                items, metadata = catalog
                return page_items(
                    items,
                    namespace="retrieve",
                    resource_id=position.resource_id,
                    cursor=cursor,
                    maximum_items=maximum_catalog_items,
                    maximum_bytes=maximum_result_bytes,
                    metadata=metadata,
                )
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
                raise AgentToolInvocationError("Video retrieval tool failed") from exc
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
                "partial": result.partial,
                "warnings": [_bounded_text(value, 500) for value in result.warnings[:8]],
                "retrieval_strategy": {
                    "version": AGENT_RETRIEVAL_STRATEGY_VERSION,
                    "requested_weights": channel_weights.model_dump(mode="json"),
                    "effective_weights": effective_weights.model_dump(mode="json"),
                },
            }
            catalogs[resource_id] = (items, metadata)
            return page_items(
                items,
                namespace="retrieve",
                resource_id=resource_id,
                cursor=None,
                maximum_items=maximum_catalog_items,
                maximum_bytes=maximum_result_bytes,
                metadata=metadata,
            )

    return StructuredTool.from_function(
        func=retrieve,
        name="retrieve",
        description=(
            "Search the fixed Series Scope and return a bounded Evidence catalog. "
            "Catalog entries contain descriptions and video_chunk_id handles, never "
            "the full Evidence. Use read_trieve_item to inspect a selected handle. "
            "When next_cursor is present, call retrieve with cursor only."
        ),
        args_schema=RetrieveArguments,
    )


def build_read_trieve_item_tool(
    *,
    workspace: AgentWorkspace,
    control: ExecutionControl,
    maximum_result_bytes: int,
    budget: _ToolCallBudget | None = None,
) -> StructuredTool:
    def read_trieve_item(video_chunk_id: str, cursor: str | None = None) -> str:
        _start_tool_call(control, budget, "read_trieve_item")
        hit = workspace.retrieval_item(video_chunk_id)
        content = json.dumps(
            hit.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return page_text(
            content,
            namespace="read_trieve_item",
            resource_id=video_chunk_id,
            cursor=cursor,
            maximum_bytes=maximum_result_bytes,
        )

    return StructuredTool.from_function(
        func=read_trieve_item,
        name="read_trieve_item",
        description=(
            "Read one canonical Evidence item by video_chunk_id. The response contains "
            "a page of the full serialized item and may include next_cursor. Continue "
            "with the same video_chunk_id and cursor until next_cursor is null."
        ),
        args_schema=ReadItemArguments,
    )


def build_update_draft_tem_tool(
    *,
    workspace: AgentWorkspace,
    control: ExecutionControl,
    maximum_result_bytes: int,
    budget: _ToolCallBudget | None = None,
) -> StructuredTool:
    def update_draft_tem(
        video_chunk_id: str,
        start_ms: int,
        end_ms: int,
        claim: str,
    ) -> str:
        _start_tool_call(control, budget, "update_draft_tem")
        try:
            inserted = workspace.upsert_draft_item(
                video_chunk_id=video_chunk_id,
                start_ms=start_ms,
                end_ms=end_ms,
                claim=claim,
            )
        except InvalidDraftRangeError as exc:
            raise ToolException(
                "时间区间不合法："
                f"当前提交 start_ms={exc.start_ms}, end_ms={exc.end_ms}；"
                "必须满足 "
                f"{exc.evidence_start_ms} <= start_ms < end_ms "
                f"<= {exc.evidence_end_ms}。"
                f"合法范围来自 video_chunk_id={exc.video_chunk_id!r} 的 "
                "canonical Evidence，"
                "请修正后重新调用 update_draft_tem。"
            ) from exc
        return bounded_json({
            "accepted": True,
            "operation": "inserted" if inserted else "updated",
            "video_chunk_id": video_chunk_id,
            "draft_item_count": len(workspace.draft_items()),
        }, maximum_result_bytes)

    return StructuredTool.from_function(
        func=update_draft_tem,
        name="update_draft_tem",
        description=(
            "Insert or update one Draft Item by video_chunk_id. The Evidence must have "
            "been retrieved, and the range must satisfy canonical_start_ms <= "
            "start_ms < end_ms <= canonical_end_ms."
        ),
        args_schema=UpdateDraftTemArguments,
        handle_tool_error=True,
    )


def build_remove_draft_item_tool(
    *,
    workspace: AgentWorkspace,
    control: ExecutionControl,
    maximum_result_bytes: int,
    budget: _ToolCallBudget | None = None,
) -> StructuredTool:
    def remove_draft_item(video_chunk_id: str) -> str:
        _start_tool_call(control, budget, "remove_draft_item")
        removed = workspace.remove_draft_item(video_chunk_id)
        return bounded_json({
            "removed": removed,
            "video_chunk_id": video_chunk_id,
            "draft_item_count": len(workspace.draft_items()),
        }, maximum_result_bytes)

    return StructuredTool.from_function(
        func=remove_draft_item,
        name="remove_draft_item",
        description="Remove one Draft Item by video_chunk_id.",
        args_schema=RemoveItemArguments,
    )


def build_get_draft_catelog_tool(
    *,
    workspace: AgentWorkspace,
    control: ExecutionControl,
    maximum_result_bytes: int,
    maximum_catalog_items: int,
    approved: bool = False,
    budget: _ToolCallBudget | None = None,
    audit: DraftReadAudit | None = None,
) -> StructuredTool:
    resource_id = "approved-draft" if approved else "draft"

    def get_draft_catelog(cursor: str | None = None) -> str:
        _start_tool_call(control, budget, "get_draft_catelog")
        items = [_draft_card(workspace, item) for item in workspace.draft_items(
            approved=approved,
        )]
        result = page_items(
            items,
            namespace="get_draft_catelog",
            resource_id=resource_id,
            cursor=cursor,
            maximum_items=maximum_catalog_items,
            maximum_bytes=maximum_result_bytes,
        )
        if audit is not None and json.loads(result)["next_cursor"] is None:
            audit.mark_catalog_complete()
        return result

    return StructuredTool.from_function(
        func=get_draft_catelog,
        name="get_draft_catelog",
        description=(
            "List bounded Draft descriptions and video_chunk_id handles. Use "
            "read_draft_item for the full Draft Item and its canonical Evidence."
        ),
        args_schema=DraftCatalogArguments,
    )


def build_read_draft_item_tool(
    *,
    workspace: AgentWorkspace,
    control: ExecutionControl,
    maximum_result_bytes: int,
    approved: bool = False,
    budget: _ToolCallBudget | None = None,
    audit: DraftReadAudit | None = None,
) -> StructuredTool:
    namespace = "read_approved_draft_item" if approved else "read_draft_item"

    def read_draft_item(video_chunk_id: str, cursor: str | None = None) -> str:
        _start_tool_call(control, budget, "read_draft_item")
        item = workspace.draft_item(video_chunk_id, approved=approved)
        hit = workspace.retrieval_item(video_chunk_id)
        content = json.dumps({
            "draft": item.model_dump(mode="json"),
            "retrieval_item": hit.model_dump(mode="json", by_alias=True),
        }, ensure_ascii=False, separators=(",", ":"))
        result = page_text(
            content,
            namespace=namespace,
            resource_id=video_chunk_id,
            cursor=cursor,
            maximum_bytes=maximum_result_bytes,
        )
        if audit is not None and json.loads(result)["next_cursor"] is None:
            audit.mark_item_complete(video_chunk_id)
        return result

    return StructuredTool.from_function(
        func=read_draft_item,
        name="read_draft_item",
        description=(
            "Read one Draft Item and its canonical Evidence by video_chunk_id. Continue "
            "with next_cursor until the complete serialized detail has been read."
        ),
        args_schema=ReadItemArguments,
    )


def build_update_video_item_tool(
    *,
    workspace: AgentWorkspace,
    control: ExecutionControl,
    maximum_result_bytes: int,
    budget: _ToolCallBudget | None = None,
) -> StructuredTool:
    def update_video_item(video_chunk_id: str, claim: str) -> str:
        _start_tool_call(control, budget, "update_video_item")
        inserted = workspace.upsert_video_item(ChatVideoBlock(
            video_chunk_id=video_chunk_id,
            claim=claim,
        ))
        return bounded_json({
            "accepted": True,
            "operation": "inserted" if inserted else "updated",
            "video_chunk_id": video_chunk_id,
            "video_item_count": workspace.video_item_count(),
        }, maximum_result_bytes)

    return StructuredTool.from_function(
        func=update_video_item,
        name="update_video_item",
        description=(
            "Insert or update one final Video Item from the approved Draft. Do not "
            "provide playback times; the Server binds the approved Draft range."
        ),
        args_schema=UpdateVideoItemArguments,
    )


def build_remove_video_item_tool(
    *,
    workspace: AgentWorkspace,
    control: ExecutionControl,
    maximum_result_bytes: int,
    budget: _ToolCallBudget | None = None,
) -> StructuredTool:
    def remove_video_item(video_chunk_id: str) -> str:
        _start_tool_call(control, budget, "remove_video_item")
        removed = workspace.remove_video_item(video_chunk_id)
        return bounded_json({
            "removed": removed,
            "video_chunk_id": video_chunk_id,
            "video_item_count": workspace.video_item_count(),
        }, maximum_result_bytes)

    return StructuredTool.from_function(
        func=remove_video_item,
        name="remove_video_item",
        description="Remove one final Video Item by video_chunk_id.",
        args_schema=RemoveItemArguments,
    )


def build_query_tools(
    search: Callable[..., VideoChunkSearchResult],
    *,
    workspace: AgentWorkspace,
    inputs: RunInputs,
    control: ExecutionControl,
    calls: list[RetrievalCall],
    maximum_top_k: int,
    maximum_tool_calls: int | None,
    maximum_result_bytes: int,
    maximum_catalog_items: int,
) -> list[StructuredTool]:
    budget = (
        _ToolCallBudget(maximum_tool_calls, "Query")
        if maximum_tool_calls is not None else None
    )
    return [
        build_retrieve_tool(
            search,
            workspace=workspace,
            inputs=inputs,
            control=control,
            calls=calls,
            maximum_top_k=maximum_top_k,
            maximum_result_bytes=maximum_result_bytes,
            maximum_catalog_items=maximum_catalog_items,
            budget=budget,
        ),
        build_read_trieve_item_tool(
            workspace=workspace,
            control=control,
            maximum_result_bytes=maximum_result_bytes,
            budget=budget,
        ),
        build_update_draft_tem_tool(
            workspace=workspace,
            control=control,
            maximum_result_bytes=maximum_result_bytes,
            budget=budget,
        ),
        build_remove_draft_item_tool(
            workspace=workspace,
            control=control,
            maximum_result_bytes=maximum_result_bytes,
            budget=budget,
        ),
        build_get_draft_catelog_tool(
            workspace=workspace,
            control=control,
            maximum_result_bytes=maximum_result_bytes,
            maximum_catalog_items=maximum_catalog_items,
            budget=budget,
        ),
        build_read_draft_item_tool(
            workspace=workspace,
            control=control,
            maximum_result_bytes=maximum_result_bytes,
            budget=budget,
        ),
    ]


def build_evaluator_tools(
    search: Callable[..., VideoChunkSearchResult],
    *,
    workspace: AgentWorkspace,
    inputs: RunInputs,
    control: ExecutionControl,
    calls: list[RetrievalCall],
    maximum_top_k: int,
    maximum_tool_calls: int | None,
    maximum_result_bytes: int,
    maximum_catalog_items: int,
    audit: DraftReadAudit | None = None,
) -> list[StructuredTool]:
    budget = (
        _ToolCallBudget(maximum_tool_calls, "Evaluator")
        if maximum_tool_calls is not None else None
    )
    return [
        build_retrieve_tool(
            search,
            workspace=workspace,
            inputs=inputs,
            control=control,
            calls=calls,
            maximum_top_k=maximum_top_k,
            maximum_result_bytes=maximum_result_bytes,
            maximum_catalog_items=maximum_catalog_items,
            budget=budget,
        ),
        build_read_trieve_item_tool(
            workspace=workspace,
            control=control,
            maximum_result_bytes=maximum_result_bytes,
            budget=budget,
        ),
        build_get_draft_catelog_tool(
            workspace=workspace,
            control=control,
            maximum_result_bytes=maximum_result_bytes,
            maximum_catalog_items=maximum_catalog_items,
            budget=budget,
            audit=audit,
        ),
        build_read_draft_item_tool(
            workspace=workspace,
            control=control,
            maximum_result_bytes=maximum_result_bytes,
            budget=budget,
            audit=audit,
        ),
    ]


def build_chat_tools(
    *,
    workspace: AgentWorkspace,
    control: ExecutionControl,
    maximum_tool_calls: int | None,
    maximum_result_bytes: int,
    maximum_catalog_items: int,
) -> list[StructuredTool]:
    budget = (
        _ToolCallBudget(maximum_tool_calls, "Chat")
        if maximum_tool_calls is not None else None
    )
    return [
        build_remove_video_item_tool(
            workspace=workspace,
            control=control,
            maximum_result_bytes=maximum_result_bytes,
            budget=budget,
        ),
        build_update_video_item_tool(
            workspace=workspace,
            control=control,
            maximum_result_bytes=maximum_result_bytes,
            budget=budget,
        ),
        build_get_draft_catelog_tool(
            workspace=workspace,
            control=control,
            maximum_result_bytes=maximum_result_bytes,
            maximum_catalog_items=maximum_catalog_items,
            approved=True,
            budget=budget,
        ),
        build_read_draft_item_tool(
            workspace=workspace,
            control=control,
            maximum_result_bytes=maximum_result_bytes,
            approved=True,
            budget=budget,
        ),
    ]


def _start_tool_call(
    control: ExecutionControl,
    budget: _ToolCallBudget | None,
    tool_name: str,
) -> None:
    control.check()
    if budget is not None:
        budget.consume(tool_name)


def _retrieval_card(hit: VideoChunkSearchHit) -> dict[str, Any]:
    return {
        "video_chunk_id": hit.candidate.video_chunk_id,
        "episode_order": hit.evidence.episode_order,
        "start_ms": hit.evidence.start_ms,
        "end_ms": hit.evidence.end_ms,
        "description": _bounded_text(hit.selection_reason.summary, 500),
        "fused_rank": hit.candidate.fused_rank,
        "fused_score": hit.candidate.fused_score,
        "transcript_rank": hit.candidate.transcript_rank,
        "semantic_rank": hit.candidate.text_rank,
        "visual_rank": hit.candidate.vision_rank,
    }


def _draft_card(workspace: AgentWorkspace, item: DraftItem) -> dict[str, Any]:
    evidence = workspace.retrieval_item(item.video_chunk_id).evidence
    return {
        "video_chunk_id": item.video_chunk_id,
        "episode_order": evidence.episode_order,
        "start_ms": item.start_ms,
        "end_ms": item.end_ms,
        "description": _bounded_text(item.claim, 500),
    }


def _bounded_text(value: str, maximum_chars: int) -> str:
    if len(value) <= maximum_chars:
        return value
    return value[: maximum_chars - 1].rstrip() + "…"
