from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

from ...common.state import ExecutionControl
from ...common.tool import AgentTool, build_agent_tool
from ...common.tool_result import JsonToolResult, ToolResult
from ..state import ChatVideoBlock
from ..workspace import AgentWorkspace
from .shared import _ToolCallBudget, RemoveItemArguments, _start_tool_call

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

def build_update_video_item_tool(
    *,
    workspace: AgentWorkspace,
    control: ExecutionControl,
    maximum_result_bytes: int,
    budget: _ToolCallBudget | None = None,
) -> AgentTool:
    def update_video_item(video_chunk_id: str, claim: str) -> ToolResult:
        _start_tool_call(control, budget, "update_video_item")
        inserted = workspace.upsert_video_item(ChatVideoBlock(
            video_chunk_id=video_chunk_id,
            claim=claim,
        ))
        return JsonToolResult(
            success=True,
            payload={
                "accepted": True,
                "operation": "inserted" if inserted else "updated",
                "video_chunk_id": video_chunk_id,
                "video_item_count": workspace.video_item_count(),
            },
        )

    return build_agent_tool(
        func=update_video_item,
        name="update_video_item",
        description=(
            "Insert or update one final Video Item from the approved Draft. Do not "
            "provide playback times; final output binds the approved Draft range."
        ),
        args_schema=UpdateVideoItemArguments,
        maximum_result_bytes=maximum_result_bytes,
    )


def build_remove_video_item_tool(
    *,
    workspace: AgentWorkspace,
    control: ExecutionControl,
    maximum_result_bytes: int,
    budget: _ToolCallBudget | None = None,
) -> AgentTool:
    def remove_video_item(video_chunk_id: str) -> ToolResult:
        _start_tool_call(control, budget, "remove_video_item")
        removed = workspace.remove_video_item(video_chunk_id)
        return JsonToolResult(
            success=True,
            payload={
                "removed": removed,
                "video_chunk_id": video_chunk_id,
                "video_item_count": workspace.video_item_count(),
            },
        )

    return build_agent_tool(
        func=remove_video_item,
        name="remove_video_item",
        description="Remove one final Video Item by video_chunk_id.",
        args_schema=RemoveItemArguments,
        maximum_result_bytes=maximum_result_bytes,
    )
