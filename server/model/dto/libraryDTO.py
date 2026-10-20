from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

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
    latest_job_id: str | None
    last_error: str | None
    keyframe_uris: tuple[str, ...] = ()


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
