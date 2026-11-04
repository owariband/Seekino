from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

from ...common.errors import AgentModelInvocationError, AgentToolFeedbackError
from ...common.state import ExecutionControl, RunInputs
from ...common.tool import AgentTool, build_agent_tool
from ...common.tool_output import bounded_json
from ...common.tool_result import JsonToolResult, ToolResult
from .shared import _ToolCallBudget, _start_tool_call


_TOOL_NAME = "get_video_outline"


class GetVideoOutlineArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_id: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=36),
    ]


def build_get_video_outline_tool(
    video_outline: Callable[..., dict | None],
    *,
    inputs: RunInputs,
    control: ExecutionControl,
    maximum_result_bytes: int,
    budget: _ToolCallBudget | None = None,
) -> AgentTool:
    def get_video_outline(video_id: str) -> ToolResult:
        _start_tool_call(control, budget, _TOOL_NAME)
        try:
            found = video_outline(
                video_id, tenant_id=inputs["tenant_id"], series_ids=list(inputs["series_ids"]),
            )
        except AgentModelInvocationError:
            raise
        except Exception as exc:
            control.check()
            raise AgentToolFeedbackError(
                "VIDEO_OUTLINE_FAILED", "The video outline could not be read.",
                "Fall back to search_video_events or retrieve for this video.",
            ) from exc
        control.check()
        if found is None:
            raise AgentToolFeedbackError(
                "OUT_OF_SCOPE_VIDEO", "The video is outside the conversation's authorized scope.",
                "Read the outline only for a video inside the conversation's series.",
            )
        if not isinstance(found, dict):
            raise AgentModelInvocationError("Video outline returned an invalid result")
        bounded_json(found, maximum_result_bytes)
        return JsonToolResult(success=True, payload=found)

    return build_agent_tool(
        func=get_video_outline, name=_TOOL_NAME,
        description=(
            "Read one video's persisted chapter outline: title, description and time-ordered "
            "sections with start_ms/end_ms and overlapping video_chunk_ids. Use it first for "
            "whole-video overview, structure, chapter or summary questions; read specific facts "
            "afterward with search_video_events(video_chunk_ids=...) or read_retrieval_evidence. "
            "Returns available=false when the video has no outline yet."
        ),
        args_schema=GetVideoOutlineArguments, maximum_result_bytes=maximum_result_bytes,
    )
