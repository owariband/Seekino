from __future__ import annotations

import json
from typing import Annotated

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from ...common.errors import (
    AgentToolFeedbackError, AgentToolInvocationError, AgentToolOutputError,
)
from ...common.state import ExecutionControl
from ...common.tool_output import bounded_json, page_items
from ...common.tooling import build_agent_tool
from ..errors import InvalidDraftRangeError
from ..workspace import AgentWorkspace
from .retrieval import ReadItemArguments
from .shared import (
    _OptionalCursor, _ToolCallBudget, DraftReadAudit, RemoveItemArguments,
    _detail_sections, _draft_card, _read_detail_section, _start_tool_call,
)


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

def build_update_draft_item_tool(
    *,
    workspace: AgentWorkspace,
    control: ExecutionControl,
    maximum_result_bytes: int,
    budget: _ToolCallBudget | None = None,
) -> StructuredTool:
    def update_draft_item(
        video_chunk_id: str,
        start_ms: int,
        end_ms: int,
        claim: str,
    ) -> str:
        _start_tool_call(control, budget, "update_draft_item")
        try:
            operation, draft_item_count = workspace.upsert_draft_item(
                video_chunk_id=video_chunk_id,
                start_ms=start_ms,
                end_ms=end_ms,
                claim=claim,
            )
        except InvalidDraftRangeError as exc:
            raise AgentToolFeedbackError(
                "INVALID_DRAFT_RANGE",
                "时间区间不合法："
                f"当前提交 start_ms={exc.start_ms}, end_ms={exc.end_ms}；"
                "必须满足 "
                f"{exc.evidence_start_ms} <= start_ms < end_ms "
                f"<= {exc.evidence_end_ms}。",
                "请按 canonical Evidence 的时间范围修正后重新调用 update_draft_item。",
            ) from exc
        return bounded_json({
            "accepted": True,
            "operation": operation,
            "video_chunk_id": video_chunk_id,
            "draft_item_count": draft_item_count,
            **(
                {"message": "该片段已在草稿中，提交内容未变。"}
                if operation == "already_present" else {}
            ),
        }, maximum_result_bytes)

    return build_agent_tool(
        func=update_draft_item,
        name="update_draft_item",
        description=(
            "Insert or update one Draft Item by video_chunk_id. The Evidence must have "
            "been retrieved, and the range must satisfy canonical_start_ms <= "
            "start_ms < end_ms <= canonical_end_ms."
        ),
        args_schema=UpdateDraftTemArguments,
        maximum_result_bytes=maximum_result_bytes,
        draft_item_count=workspace.draft_item_count,
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
        removed, draft_item_count = workspace.remove_draft_item(video_chunk_id)
        return bounded_json({
            "removed": removed,
            "video_chunk_id": video_chunk_id,
            "draft_item_count": draft_item_count,
        }, maximum_result_bytes)

    return build_agent_tool(
        func=remove_draft_item,
        name="remove_draft_item",
        description="Remove one Draft Item by video_chunk_id.",
        args_schema=RemoveItemArguments,
        maximum_result_bytes=maximum_result_bytes,
        draft_item_count=workspace.draft_item_count,
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
        try:
            result = page_items(
                items,
                namespace="get_draft_catelog",
                resource_id=resource_id,
                cursor=cursor,
                maximum_items=maximum_catalog_items,
                maximum_bytes=maximum_result_bytes,
            )
        except AgentToolOutputError:
            raise
        except AgentToolInvocationError as exc:
            raise AgentToolFeedbackError(
                "INVALID_CATALOG_CURSOR", str(exc),
                "Call get_draft_catelog without cursor to read the current catalog.",
            ) from exc
        if audit is not None and json.loads(result)["next_cursor"] is None:
            audit.mark_catalog_complete()
        return result

    return build_agent_tool(
        func=get_draft_catelog,
        name="get_draft_catelog",
        description=(
            "List bounded Draft descriptions and video_chunk_id handles. Use "
            "read_draft_item for the full Draft Item and its canonical Evidence."
        ),
        args_schema=DraftCatalogArguments,
        maximum_result_bytes=maximum_result_bytes,
    )


def build_read_draft_item_tool(
    *,
    workspace: AgentWorkspace,
    control: ExecutionControl,
    maximum_result_bytes: int,
    maximum_read_chars: int = 16_384,
    approved: bool = False,
    budget: _ToolCallBudget | None = None,
    audit: DraftReadAudit | None = None,
) -> StructuredTool:
    def read_draft_item(
        video_chunk_id: str,
        section: str = "overview",
        start: int = 0,
        end: int | None = None,
    ) -> str:
        _start_tool_call(control, budget, "read_draft_item")
        item = workspace.draft_item(video_chunk_id, approved=approved)
        sections = _detail_sections(
            workspace.retrieval_snapshot(video_chunk_id), item,
        )
        result = _read_detail_section(
            video_chunk_id, sections,
            section=section,
            start=start,
            end=end,
            maximum_result_bytes=maximum_result_bytes,
            maximum_read_chars=maximum_read_chars,
        )
        if audit is not None:
            if section == "overview":
                audit.mark_overview(video_chunk_id, sections)
            else:
                response = json.loads(result)
                audit.mark_range(
                    video_chunk_id, section, response["start"], response["end"],
                )
        return result

    return build_agent_tool(
        func=read_draft_item,
        name="read_draft_item",
        description=(
            "Read a Draft Item and its stable Evidence snapshot by video_chunk_id. "
            "First request section='overview'; then read every required section "
            "with start/end character offsets [start,end). Omit end to read to this "
            "section's end; never reuse another ID's end. Retry oversized ranges "
            "with the suggested smaller end."
        ),
        args_schema=ReadItemArguments,
        maximum_result_bytes=maximum_result_bytes,
    )
