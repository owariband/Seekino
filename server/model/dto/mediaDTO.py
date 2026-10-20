from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from engine.parsev2.schemas import VideoParseResult

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
    schema_version: Literal["anivag.parse-export.v1"] = "anivag.parse-export.v1"
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
