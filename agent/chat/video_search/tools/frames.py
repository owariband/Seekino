from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from ...common.errors import AgentModelInvocationError, AgentToolFeedbackError
from ...common.state import ExecutionControl, RunInputs
from ...common.tool import AgentTool, build_agent_tool
from ...common.tool_output import bounded_json
from ...common.tool_result import JsonToolResult, ToolError
from ..frame_evidence import (
    FrameInspectionSession, FrameReader, FrameReadError, FrameReadRequest,
    PointFrameReader, PointFrameReadRequest,
)
from ..output import retrieval_hit
from ..state import RetrievalCall
from .shared import _ToolCallBudget, _start_tool_call

_DEFAULT_FRAME_COUNT = 3


class InspectVideoFramesArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_chunk_id: str = Field(min_length=1, max_length=64)
    start_ms: int | None = Field(default=None, strict=True, ge=0)
    end_ms: int | None = Field(default=None, strict=True, ge=0)
    frame_count: int = Field(default=_DEFAULT_FRAME_COUNT, strict=True, ge=1, le=4)


def build_inspect_video_frames_tool(
    *, reader: FrameReader, session: FrameInspectionSession,
    calls: list[RetrievalCall], inputs: RunInputs, control: ExecutionControl,
    role: str, max_height: int, maximum_result_bytes: int,
    budget: _ToolCallBudget | None = None,
) -> AgentTool:
    if type(max_height) is not int or max_height <= 0:
        raise ValueError("Frame tools require a positive max_height")

    def inspect_video_frames(
        video_chunk_id: str, start_ms: int | None = None,
        end_ms: int | None = None, frame_count: int = _DEFAULT_FRAME_COUNT,
    ) -> JsonToolResult:
        _start_tool_call(control, budget, "inspect_video_frames")
        hit = retrieval_hit(calls, video_chunk_id)
        evidence = hit.evidence
        if (evidence.tenant_id != inputs["tenant_id"]
                or evidence.series_id not in inputs["series_ids"]):
            raise AgentToolFeedbackError(
                "FRAME_SCOPE_MISMATCH", "The Chunk is outside the current retrieval scope.",
                "Use a Chunk returned by retrieve in this conversation.",
            )
        try:
            request = FrameReadRequest(
                evidence=evidence, inspection_id=uuid4().hex, role=role,
                conversation_id=inputs["conversation_id"],
                message_id=inputs["assistant_message_id"],
                start_ms=evidence.start_seconds if start_ms is None else start_ms,
                end_ms=evidence.end_seconds if end_ms is None else end_ms,
                frame_count=frame_count, max_height=max_height,
                timeout_seconds=control.remaining_seconds(), check_control=control.check,
            )
        except ValueError as exc:
            raise AgentToolFeedbackError(
                "INVALID_FRAME_RANGE", str(exc),
                "Use absolute integer milliseconds inside this Chunk and 1–4 sampling positions.",
            ) from exc
        with session.lock:
            control.check()
            request = replace(request, timeout_seconds=control.remaining_seconds())
            if (session.maximum_inspections is not None
                    and session.used_inspections >= session.maximum_inspections):
                raise AgentToolFeedbackError(
                    "FRAME_INSPECTION_LIMIT", "The frame inspection budget for this Kick is exhausted.",
                    "Use the evidence already read and state remaining visual uncertainty.",
                )
            session.used_inspections += 1
            key = request.cache_key()
            try:
                inspection = reader(request, session.cache.get(key))
            except FrameReadError as exc:
                session.cache.pop(key, None)
                return JsonToolResult(success=False, payload={
                    "inspection_id": request.inspection_id, "video_chunk_id": video_chunk_id,
                    "start_ms": request.start_ms, "end_ms": request.end_ms,
                    "frame_count": frame_count,
                    "error": ToolError(code=exc.code, message=str(exc), retryable=exc.retryable).as_dict(),
                })
            control.check()
            payload = inspection.payload
            if (payload.get("inspection_id") != request.inspection_id
                    or payload.get("video_chunk_id") != video_chunk_id
                    or payload.get("frame_count") != frame_count
                    or len(inspection.artifact.images) != frame_count
                    or payload.get("images_supplied") is not True):
                raise AgentModelInvocationError("Frame reader returned inconsistent image evidence")
            bounded_json(payload, maximum_result_bytes)
            receipt = {
                name: payload[name] for name in (
                    "inspection_id", "video_chunk_id", "start_ms", "end_ms", "frame_count",
                    "frames", "cache_hit", "images_supplied", "manifest_uri", "manifest_sha256",
                ) if name in payload
            } | {"role": role}
            session.cache[key] = inspection
            session.receipts.append(receipt)
            return JsonToolResult(success=True, payload=payload, artifact=inspection.artifact)

    return build_agent_tool(
        func=inspect_video_frames, name="inspect_video_frames",
        description=(
            "View actual source-video frames for a retrieved Chunk. Times are absolute integer milliseconds. "
            "Omit start_ms/end_ms for a whole-Chunk overview, or narrow the interval to inspect a visual claim. "
            "Returns 1–4 ordered images and durable source references. Uniform midpoints do not guarantee a "
            "specific moment; use single-point inspection when available. Sampling positions are requested times, "
            "not measured frame PTS; sparse still images contain no audio and do not prove continuous motion."
        ),
        args_schema=InspectVideoFramesArguments, maximum_result_bytes=maximum_result_bytes,
    )


class GetVideoFrameArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_id: str | None = Field(default=None, min_length=1, max_length=64)
    timestamp_ms: int = Field(
        strict=True, ge=0,
        description="Absolute source-video milliseconds, for example 01:00 is 60000. User-specified time takes priority over playback.",
    )


def build_get_video_frame_tool(
    *, reader: PointFrameReader, session: FrameInspectionSession,
    inputs: RunInputs, control: ExecutionControl,
    role: str, max_height: int, maximum_result_bytes: int,
    budget: _ToolCallBudget | None = None,
) -> AgentTool:
    if type(max_height) is not int or max_height <= 0:
        raise ValueError("Frame tools require a positive max_height")

    def resolve_target(video_id: str | None) -> tuple[str, tuple[str, ...]]:
        current_video = inputs.get("current_video")
        current_video = current_video if isinstance(current_video, dict) else {}
        extra_context = inputs.get("extra_context")
        playback = extra_context.get("playback") if isinstance(extra_context, dict) else None
        playback = playback if isinstance(playback, dict) else {}

        requested = video_id.strip() if isinstance(video_id, str) else ""
        if not requested:
            for source in (current_video, playback):
                candidate = source.get("videoId")
                if isinstance(candidate, str) and candidate.strip():
                    requested = candidate.strip()
                    break
        if not requested:
            raise AgentToolFeedbackError(
                "VIDEO_NOT_SPECIFIED",
                "No video is open and no video_id was supplied.",
                "Open the video first, or pass a video_id that belongs to the current scope.",
            )

        allowed = {
            item.strip() for item in inputs["series_ids"]
            if isinstance(item, str) and item.strip()
        }
        # The validated playback video may be playable without being searchable/READY.
        for source in (current_video, playback):
            if source.get("videoId") == requested:
                series_id = source.get("seriesId")
                if isinstance(series_id, str) and series_id.strip():
                    allowed.add(series_id.strip())
        if not allowed:
            raise AgentToolFeedbackError(
                "FRAME_SCOPE_MISMATCH",
                "The video is outside the current workspace and Series scope.",
                "Use a video that belongs to the current retrieval scope.",
            )
        return requested, tuple(sorted(allowed))

    def get_video_frame(timestamp_ms: int, video_id: str | None = None) -> JsonToolResult:
        _start_tool_call(control, budget, "get_video_frame")
        resolved_video_id, allowed_series_ids = resolve_target(video_id)
        try:
            request = PointFrameReadRequest(
                tenant_id=inputs["tenant_id"], allowed_series_ids=allowed_series_ids,
                video_id=resolved_video_id, timestamp_ms=timestamp_ms,
                inspection_id=uuid4().hex, role=role,
                conversation_id=inputs["conversation_id"],
                message_id=inputs["assistant_message_id"],
                max_height=max_height,
                timeout_seconds=control.remaining_seconds(), check_control=control.check,
            )
        except ValueError as exc:
            raise AgentToolFeedbackError(
                "INVALID_FRAME_TIMESTAMP", str(exc),
                "Use one nonnegative integer millisecond timestamp inside the source video.",
            ) from exc
        with session.lock:
            control.check()
            request = replace(request, timeout_seconds=control.remaining_seconds())
            if (session.maximum_inspections is not None
                    and session.used_inspections >= session.maximum_inspections):
                raise AgentToolFeedbackError(
                    "FRAME_INSPECTION_LIMIT", "The frame inspection budget for this Kick is exhausted.",
                    "Use the evidence already read and state remaining visual uncertainty.",
                )
            session.used_inspections += 1
            key = request.cache_key()
            try:
                inspection = reader.read_point(request, session.cache.get(key))
            except FrameReadError as exc:
                session.cache.pop(key, None)
                return JsonToolResult(success=False, payload={
                    "inspection_id": request.inspection_id, "video_id": request.video_id,
                    "requested_timestamp_ms": request.timestamp_ms,
                    "error": ToolError(code=exc.code, message=str(exc), retryable=exc.retryable).as_dict(),
                })
            control.check()
            payload = inspection.payload
            if (payload.get("inspection_id") != request.inspection_id
                    or payload.get("source_kind") != "source_video_frame"
                    or payload.get("video_id") != request.video_id
                    or payload.get("requested_timestamp_ms") != request.timestamp_ms
                    or payload.get("frame_count") != 1
                    or len(inspection.artifact.images) != 1
                    or payload.get("images_supplied") is not True):
                raise AgentModelInvocationError("Frame reader returned inconsistent image evidence")
            bounded_json(payload, maximum_result_bytes)
            receipt = {
                name: payload[name] for name in (
                    "inspection_id", "video_id", "series_id", "requested_timestamp_ms",
                    "frame_count", "frames", "cache_hit", "images_supplied",
                    "manifest_uri", "manifest_sha256",
                ) if name in payload
            } | {"role": role}
            session.cache[key] = inspection
            session.receipts.append(receipt)
            return JsonToolResult(success=True, payload=payload, artifact=inspection.artifact)

    return build_agent_tool(
        func=get_video_frame, name="get_video_frame",
        description=(
            "View the actual source-video frame at one absolute integer millisecond timestamp. "
            "Omit video_id for the currently open or playback video; otherwise pass a video_id from the "
            "current scope. This needs no retrieved Chunk. Returns one image, "
            "the requested timestamp and durable source references. The source frame PTS is not measured, "
            "so do not treat the requested timestamp as an exact frame boundary."
        ),
        args_schema=GetVideoFrameArguments, maximum_result_bytes=maximum_result_bytes,
    )
