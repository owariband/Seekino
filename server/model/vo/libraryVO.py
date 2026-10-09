from __future__ import annotations

from typing import Literal

from pydantic import Field

from ..dto.libraryDTO import Character
from ..video import VideoIndexStatus
from ..job import ParseJobStatus, VideoParseTaskStatus
from ...schema import ContractModel
from .commonVO import Failure, ImageAsset, UnixTimeMs
from .uploadVO import UploadSessionView


class LastSearchView(ContractModel):
    conversation_id: str
    query: str
    searched_at: UnixTimeMs


class VideoSummary(ContractModel):
    video_id: str
    tenant_id: str
    series_id: str
    episode_order: int = Field(gt=0)
    upload_sequence: int = Field(gt=0)
    original_filename: str
    total_bytes: int = Field(ge=0)
    duration_seconds: int = Field(gt=0)
    upload_status: Literal["COMPLETED"]
    upload_progress: Literal[100]
    parse_status: ParseJobStatus | VideoParseTaskStatus
    parse_progress: int | None = Field(ge=0, le=100)
    index_status: VideoIndexStatus
    playback_status: Literal["PREPARING", "READY", "FAILED", "MISSING"]
    playback_url: str | None
    playback_url_expires_at: UnixTimeMs | None
    thumbnail: ImageAsset | None
    latest_job_id: str | None
    last_error: Failure | None


class SeriesSummary(ContractModel):
    is_published: bool = False
    series_id: str
    tenant_id: str
    name: str
    description: str | None
    status: Literal["EMPTY", "INDEXING", "PARTIAL", "READY", "FAILED"]
    video_count: int = Field(ge=0)
    ready_video_count: int = Field(ge=0)
    failed_video_count: int = Field(ge=0)
    pending_upload_count: int = Field(ge=0)
    cover: ImageAsset | None
    last_search: LastSearchView | None
    created_at: UnixTimeMs
    updated_at: UnixTimeMs


class SeriesDetail(SeriesSummary):
    transcript_language_hint: str | None
    important_extra_data: dict | None
    character_bible: list[Character]
    videos: list[VideoSummary]
    pending_uploads: list[UploadSessionView]


SeriesView = SeriesDetail
