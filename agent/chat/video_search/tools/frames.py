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
            "Returns 1–4 ordered images and durable source references. Sampling positions are requested times, "
            "not measured frame PTS; sparse still images contain no audio and do not prove continuous motion."
        ),
        args_schema=InspectVideoFramesArguments, maximum_result_bytes=maximum_result_bytes,
    )
