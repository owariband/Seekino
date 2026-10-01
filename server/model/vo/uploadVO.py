from __future__ import annotations

from pydantic import Field

from agent.video_parse_v2.input import ParseConfig as VideoParseV2Config

from ...config.parse_profile import ParseConfig
from ..upload import UploadSessionStatus, UploadCompletionStage
from ...schema import ContractModel
from .commonVO import Failure, UnixTimeMs


class UploadSessionView(ContractModel):
    upload_session_id: str
    workspace_id: str
    series_id: str
    episode_order: int = Field(gt=0)
    upload_sequence: int = Field(gt=0)
    original_filename: str
    media_type: str
    total_size_bytes: int = Field(gt=0)
    content_sha256: str | None
    preferred_segment_size_bytes: int = Field(gt=0)
    total_segments: int = Field(gt=0)
    status: UploadSessionStatus
    uploaded_segment_count: int = Field(ge=0)
    uploaded_size_bytes: int = Field(ge=0)
    missing_segment_numbers: list[int]
    completion_stage: UploadCompletionStage | None
    completion_progress: int | None = Field(ge=0, le=100)
    estimated_remaining_ms: int | None = Field(ge=0)
    expires_at: UnixTimeMs
    video_id: str | None
    job_id: str | None
    initial_video_parse_task_id: str | None
    last_error: Failure | None
    version: int = Field(ge=1)
    created_at: UnixTimeMs
    updated_at: UnixTimeMs
    parse_config: ParseConfig | None = None
    video_parse_config: VideoParseV2Config | None = None


class UploadSegmentView(ContractModel):
    upload_session_id: str
    segment_number: int = Field(ge=0)
    start_byte: int = Field(ge=0)
    end_byte_exclusive: int = Field(gt=0)
    actual_size_bytes: int = Field(gt=0)
    checksum_sha256: str


class UploadCompleteView(ContractModel):
    upload_session: UploadSessionView
    video_id: str
    job_id: str | None
    video_parse_task_id: str
