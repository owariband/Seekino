from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from ...schema import ContractModel
from ..video import SeriesVisibility, VideoIndexStatus
from .uploadDTO import UploadSessionState


class Character(ContractModel):
    name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)


class SeriesCreateRequest(ContractModel):
    tenant_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    transcript_language_hint: str | None = Field(default=None, max_length=64)
    important_extra_data: dict | None = None
    character_bible: list[Character] = Field(default_factory=list)


class SeriesBackground(ContractModel):
    format: Literal["markdown", "json"] = "markdown"
    content: str


class SeriesUpdateRequest(ContractModel):
    tenant_id: str = Field(min_length=1, max_length=64)
    series_id: str = Field(min_length=1)
    display_name: str | None = Field(default=None, max_length=255)
    description: str | None = None
    transcript_language_hint: str | None = Field(default=None, max_length=64)
    important_extra_data: SeriesBackground | None = None
    visibility: SeriesVisibility | None = None


class VideoEpisodeUpdateRequest(ContractModel):
    tenant_id: str = Field(min_length=1, max_length=64)
    video_id: str = Field(min_length=1)
    episode_order: int = Field(gt=0, le=2_147_483_647, strict=True)


@dataclass(frozen=True, slots=True)
class VideoSummaryState:
    video_id: str
    tenant_id: str
    series_id: str
    episode_order: int
    upload_sequence: int
    original_filename: str
    original_source_uri: str
    inference_proxy_uri: str | None
    content_hash: str
    total_bytes: int
    duration_seconds: int
    upload_status: str
    parse_status: str
    upload_progress: int
    parse_progress: int | None
    index_status: VideoIndexStatus
    last_error: str | None
    keyframe_uris: tuple[str, ...] = ()
    display_name: str | None = None
    description: str | None = None
    cover_uri: str | None = None
    has_outline: bool = False


@dataclass(frozen=True, slots=True)
class SeriesLastSearchState:
    conversation_id: str
    query: str
    searched_at: int


@dataclass(frozen=True, slots=True)
class SeriesPublisherState:
    kind: Literal["user", "platform"]
    display_name: str


@dataclass(frozen=True, slots=True)
class SeriesState:
    series_id: str
    tenant_id: str
    name: str
    description: str | None
    transcript_language_hint: str | None
    status: str
    important_extra_data: dict | None
    character_bible: list[dict]
    video_count: int
    created_at: int
    updated_at: int
    visibility: SeriesVisibility = SeriesVisibility.PRIVATE
    publisher: SeriesPublisherState | None = None
    publisher_active: bool = False
    display_name: str | None = None
    ready_video_count: int = 0
    failed_video_count: int = 0
    pending_upload_count: int = 0
    last_search: SeriesLastSearchState | None = None
    videos: tuple[VideoSummaryState, ...] = ()
    pending_uploads: tuple[UploadSessionState, ...] = ()
    representative_video: VideoSummaryState | None = None


@dataclass(frozen=True, slots=True)
class VideoChunkState:
    video_chunk_id: str
    sequence_no: int
    start_seconds: int
    end_seconds: int
    chunk_media_ref: str
    analysis_window_media_ref: str


@dataclass(kw_only=True)
class VideoData:
    id: str
    series_id: str
    episode_order: int
    upload_sequence: int
    filename: str
    source_uri: str
    proxy_uri: str | None = None
    hash: str
    size_bytes: int
    duration_ms: int
    upload_status: str
    parse_status: str
    upload_progress: int
    parse_progress: int
    create_time: int | None = None
    create_date: datetime | None = None
    update_time: int | None = None
    update_date: datetime | None = None


@dataclass(kw_only=True)
class VideoChunkData:
    id: str
    video_id: str
    sequence_no: int
    start_ms: int
    end_ms: int
    window_start_ms: int
    window_end_ms: int
    chunk_media_ref: str
    analysis_window_media_ref: str
    transcript: list[dict[str, Any]] = field(default_factory=list)
    baseline_visual_caption: str | None = None
    keyframe_uris: list[str | dict[str, Any]] = field(default_factory=list)
    create_time: int | None = None
    create_date: datetime | None = None
    update_time: int | None = None
    update_date: datetime | None = None


@dataclass(kw_only=True)
class SeriesData:
    id: str
    tenant_id: str
    name: str
    display_name: str | None = None
    description: str | None = None
    transcript_language_hint: str | None = None
    status: str
    visibility: str = "private"
    important_extra_data: dict[str, Any] | None = None
    character_bible: list[dict[str, Any]] = field(default_factory=list)
    create_time: int | None = None
    create_date: datetime | None = None
    update_time: int | None = None
    update_date: datetime | None = None
