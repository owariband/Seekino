"""Result metadata and content snapshots for publication, indexing and export."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from engine.parsev2.schemas import VideoParseResult


@dataclass(kw_only=True)
class VideoParseContentData:
    id: str
    video_task_id: str
    version: int = 1
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ChunkParseContentState:
    chunk_parse_task_id: str
    video_chunk_id: str
    sequence_no: int
    content: dict[str, Any]
    version: int
    detail: dict[str, Any]


@dataclass(frozen=True, slots=True)
class IntervalMemberSnapshot:
    interval_parse_task_id: str
    video_parse_task_id: str
    start_sequence_no: int
    chunk_count: int
    members: tuple[ChunkParseContentState, ...]


@dataclass(frozen=True, slots=True)
class IndexableChunkState:
    tenant_id: str
    series_id: str
    video_id: str
    chunk_parse_task_id: str
    video_chunk_id: str
    sequence_no: int
    start_seconds: int
    end_seconds: int
    content: dict[str, Any]
    content_version: int
    content_detail: dict[str, Any]
    source_media_ref: str
    chunk_media_ref: str
    analysis_window_media_ref: str


@dataclass(frozen=True, slots=True)
class ChunkIndexingSnapshot:
    video_parse_task_id: str
    chunk: IndexableChunkState
    target_index_generation: int


@dataclass(kw_only=True)
class ChunkParseContentData:
    id: str
    chunk_task_id: str
    content: Any | None = None
    version: int = 1
    detail: dict[str, Any] | None = None
    create_time: int | None = None
    create_date: datetime | None = None
    update_time: int | None = None
    update_date: datetime | None = None


@dataclass(kw_only=True)
class IntervalParseContentData:
    interval_task_id: str
    content: Any
    detail: dict[str, Any] | None = None
    create_time: int | None = None
    create_date: datetime | None = None
    update_time: int | None = None
    update_date: datetime | None = None


@dataclass(frozen=True, slots=True)
class ParseExportChunkSnapshot:
    video_id: str
    parse_task_id: str
    chunk_id: str
    chunk_sequence_no: int
    content_version: int
    content: VideoParseResult
