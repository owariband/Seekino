from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from engine.parsev2.schemas import TimelineEvent, VideoParseResult
from engine.rag.indexing.events import EventType

from ...schema import ContractModel


def parse_export_event_types() -> tuple[str, ...]:
    return tuple(VideoParseResult.model_json_schema()["properties"]["timeline"]["items"]["discriminator"]["mapping"])


class ParseExportRequest(ContractModel):
    tenant_id: str = Field(min_length=1, max_length=64)
    video_ids: list[str] | None = Field(default=None, min_length=1)
    video_parse_task_ids: list[str] | None = Field(default=None, min_length=1)
    event_types: list[str] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_source(self) -> "ParseExportRequest":
        if (self.video_ids is None) == (self.video_parse_task_ids is None):
            raise ValueError("provide exactly one of videoIds or videoParseTaskIds")
        return self


class ParseExportOptions(ContractModel):
    event_types: list[str]


class VideoTextCue(ContractModel):
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    source: Literal["dialogue", "screen_text"]
    texts: list[str]


class VideoTextTimeline(ContractModel):
    ready: bool
    cues: list[VideoTextCue]


class VideoVisualCue(ContractModel):
    start_ms: int
    end_ms: int
    kind: EventType
    description: str
    visible_persons: list[str]

    @classmethod
    def from_event(cls, event: TimelineEvent) -> "VideoVisualCue":
        return cls(start_ms=event.start_seconds, end_ms=event.end_seconds, kind=event.event_type,
                   description=event.description, visible_persons=event.visible_persons)


class VideoVisualTimeline(ContractModel):
    ready: bool
    cues: list[VideoVisualCue]
    known_persons: list[str] = Field(default_factory=list, exclude_if=lambda names: not names)


class VideoVisualQuery(ContractModel):
    tenant_id: str = Field(min_length=1, max_length=64)
    video_id: str = Field(min_length=1, max_length=64)
    query: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_query(self):
        if not self.query.strip():
            raise ValueError("query must contain text")
        return self


FindQueryTerm = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class VideoFindTerms(ContractModel):
    query: FindQueryTerm
    synonyms: list[FindQueryTerm] = Field(max_length=4)


class ParseExportSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    video_id: str
    parse_task_id: str
    chunk_id: str
    content_version: int = Field(gt=0)
    timeline_indices: list[int]


class ParseExportTime(BaseModel):
    model_config = ConfigDict(extra="forbid")
    unit: Literal["ms"] = "ms"
    origin: Literal["source_video"] = "source_video"


class ParseExportRecord(VideoParseResult):
    schema_version: Literal["seekino.parse-export.v1"] = "seekino.parse-export.v1"
    source: ParseExportSource
    time: ParseExportTime = Field(default_factory=ParseExportTime)


@dataclass(frozen=True, slots=True)
class ParseExport:
    content: bytes
    record_count: int
    event_count: int
    skipped_empty_chunks: int


@dataclass(frozen=True, slots=True)
class VideoMedia:
    path: Path
    media_type: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class PlaybackMedia:
    status: Literal["PREPARING", "READY", "FAILED", "MISSING"]
    url: str | None
    expires_at: int | None = None


@dataclass(frozen=True, slots=True)
class ImageReference:
    image_id: str
    url: str
    expires_at: int | None = None


@dataclass(frozen=True, slots=True)
class KeyframeImageReference:
    image_id: str
    url: str
    at_seconds: int
    expires_at: int | None = None
