from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import NotRequired, Protocol, TypedDict

from pydantic import BaseModel, Field

from engine.parsev2.schemas import VideoParseResult


@dataclass(frozen=True, slots=True)
class RealtimeRequest:
    tenant_id: str
    video_id: str
    anchor_ms: int
    question: str
    deadline: float
    check_control: Callable[[], None] = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if (not isinstance(self.tenant_id, str) or not self.tenant_id.strip()
                or not isinstance(self.video_id, str) or not self.video_id.strip()):
            raise ValueError("realtime understanding requires workspace and video")
        if type(self.anchor_ms) is not int or self.anchor_ms < 0:
            raise ValueError("playback position must be nonnegative integer milliseconds")
        if not isinstance(self.question, str) or not self.question.strip():
            raise ValueError("realtime understanding requires the user question")


@dataclass(frozen=True, slots=True)
class ChunkTarget:
    chunk_id: str
    chunk_parse_task_id: str
    sequence_no: int
    start_ms: int
    end_ms: int


@dataclass(frozen=True, slots=True)
class RealtimeContext:
    request: RealtimeRequest
    series_id: str
    video_parse_task_id: str
    task_version: int
    duration_ms: int
    targets: tuple[ChunkTarget, ...]
    source_uri: str


@dataclass(frozen=True, slots=True)
class ChunkEvidence:
    target: ChunkTarget
    result: VideoParseResult | None = None
    content_version: int | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class FrameEvidence:
    timestamp_ms: int
    chunk_id: str
    artifact_uri: str
    image_path: Path
    caption: str = ""


class FrameCaption(BaseModel):
    frame_index: int = Field(ge=0)
    caption: str = Field(max_length=2000)


class VisualObservation(BaseModel):
    summary: str = Field(max_length=6000)
    captions: list[FrameCaption] = Field(default_factory=list, max_length=8)


class ContextSummary(BaseModel):
    summary: str = Field(min_length=1, max_length=8000)


@dataclass(frozen=True, slots=True)
class VisualEvidence:
    frames: tuple[FrameEvidence, ...] = ()
    summary: str = ""
    error: str | None = None


@dataclass(frozen=True, slots=True)
class RealtimeResult:
    context: RealtimeContext
    chunks: tuple[ChunkEvidence, ...]
    visual: VisualEvidence
    summary: str

    @property
    def frames(self) -> tuple[FrameEvidence, ...]:
        return self.visual.frames

    def as_dict(self, *, events_per_chunk: int = 4) -> dict:
        anchor = self.context.request.anchor_ms
        chunks = []
        for item in self.chunks:
            target = item.target
            events = sorted(
                item.result.timeline if item.result is not None else (),
                key=lambda event: (
                    0 if event.start_seconds <= anchor < event.end_seconds else
                    min(abs(event.start_seconds - anchor), abs(event.end_seconds - anchor))
                ),
            )[:events_per_chunk]
            events.sort(key=lambda event: event.start_seconds)
            chunks.append({
                "chunk_id": target.chunk_id,
                "start_ms": target.start_ms,
                "end_ms": target.end_ms,
                "content_version": item.content_version,
                "available": item.result is not None,
                "error": item.error,
                "after_anchor": target.start_ms > anchor,
                "evidence": [{
                    **event.model_dump(mode="json"),
                    "after_anchor": event.start_seconds > anchor,
                } for event in events],
            })
        return {
            "video_id": self.context.request.video_id,
            "series_id": self.context.series_id,
            "anchor_ms": anchor,
            "summary": self.summary,
            "chunks": chunks,
            "frames": [{
                "timestamp_ms": frame.timestamp_ms,
                "chunk_id": frame.chunk_id,
                "frame_ref": frame.artifact_uri,
                "caption": frame.caption,
                "after_anchor": frame.timestamp_ms > anchor,
            } for frame in self.visual.frames],
            "visual_summary": self.visual.summary,
            "visual_error": self.visual.error,
            "partial": any(item.result is None for item in self.chunks)
                or self.visual.error is not None,
        }


class RealtimeHost(Protocol):
    async def prepare_context(self, request: RealtimeRequest) -> RealtimeContext: ...

    async def ensure_chunk(
        self, context: RealtimeContext, target: ChunkTarget,
    ) -> ChunkEvidence: ...

    async def observe_frames(
        self, context: RealtimeContext, *, prompt: str,
    ) -> VisualEvidence: ...

    async def compose_context(
        self, context: RealtimeContext, *, prompt: str,
    ) -> ContextSummary: ...


class RealtimeState(TypedDict):
    request: RealtimeRequest
    context: NotRequired[RealtimeContext]
    chunks: NotRequired[tuple[ChunkEvidence, ...]]
    visual: NotRequired[VisualEvidence]
    result: NotRequired[RealtimeResult]
