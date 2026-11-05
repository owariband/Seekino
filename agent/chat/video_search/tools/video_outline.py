from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from ...common.errors import (
    AgentModelInvocationError, AgentToolFeedbackError, AgentToolOutputError,
)
from ...common.state import ExecutionControl, RunInputs
from ...common.tool import AgentTool, build_agent_tool
from ...common.tool_output import bounded_json
from ...common.tool_result import JsonToolResult, ToolResult
from .shared import _ToolCallBudget, _start_tool_call


_TOOL_NAME = "get_video_outline"


class VideoOutlineItemArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    series: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
    ] | None = None
    episode: int = Field(gt=0, le=2_147_483_647, strict=True)


class GetVideoOutlineArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    videos: list[VideoOutlineItemArguments] = Field(min_length=1, max_length=5)


def build_get_video_outline_tool(
    video_outline: Callable[..., dict | None],
    *,
    inputs: RunInputs,
    control: ExecutionControl,
    maximum_result_bytes: int,
    outline_chunk_ids: set[str] | None = None,
    budget: _ToolCallBudget | None = None,
) -> AgentTool:
    def get_video_outline(videos: list[dict]) -> ToolResult:
        _start_tool_call(control, budget, _TOOL_NAME)
        resolved_videos = [
            item.model_dump() if hasattr(item, "model_dump") else item
            for item in videos
        ]
        try:
            found = video_outline(
                resolved_videos, tenant_id=inputs["tenant_id"],
                series_ids=list(inputs["series_ids"]),
            )
        except AgentModelInvocationError:
            raise
        except Exception as exc:
            control.check()
            raise AgentToolFeedbackError(
                "VIDEO_OUTLINE_FAILED", "The video outline could not be read.",
                "Fall back to search_video_events or retrieve for these videos.",
            ) from exc
        control.check()
        if not isinstance(found, dict):
            raise AgentModelInvocationError("Video outline returned an invalid result")
        error = found.get("error")
        if isinstance(error, dict):
            raise AgentToolFeedbackError(
                str(error.get("code") or "VIDEO_OUTLINE_FAILED"),
                str(error.get("message") or "The requested video could not be resolved."),
                str(error.get("hint") or "Address videos by series name and episode."),
            )
        payload = found.get("videos")
        if not isinstance(payload, list):
            raise AgentModelInvocationError("Video outline returned an invalid result")
        if outline_chunk_ids is not None:
            for item in payload:
                if not isinstance(item, dict):
                    continue
                for section in item.get("sections") or ():
                    if isinstance(section, dict):
                        outline_chunk_ids.update(section.get("video_chunk_ids") or ())
        try:
            bounded_json(payload, maximum_result_bytes)
        except AgentToolOutputError as exc:
            raise AgentToolFeedbackError(
                "OUTLINE_TOO_LARGE", "The outline batch exceeds the result size limit.",
                "Request fewer videos, or read one video at a time.",
            ) from exc
        return JsonToolResult(success=True, payload=payload)

    return build_agent_tool(
        func=get_video_outline, name=_TOOL_NAME,
        description=(
            "Read persisted chapter outlines for videos addressed by (series name, episode): "
            "title, description and time-ordered sections with start_ms/end_ms and overlapping "
            "video_chunk_ids. Use it first for whole-video overview, structure, chapter or summary "
            "questions; read specific facts afterward with search_video_events(video_chunk_ids=...) "
            "or read_retrieval_evidence. Returns available=false for a video without an outline yet."
        ),
        args_schema=GetVideoOutlineArguments, maximum_result_bytes=maximum_result_bytes,
    )
