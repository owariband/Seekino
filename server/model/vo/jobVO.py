from __future__ import annotations

from pydantic import Field

from agent.video_parse_v2.input import ParseConfig as VideoParseV2Config

from ...config.parse_profile import ParseConfig
from ...schema import ContractModel
from ..job import (
    ParseJobStatus,
    ParseJobStage,
    VideoChunkCheckpointStage,
    VideoParseTaskStatus,
)
from .commonVO import Failure, UnixTimeMs


class VideoChunkCheckpointView(ContractModel):
    sequence_no: int = Field(ge=0)
    video_chunk_id: str
    stage: VideoChunkCheckpointStage
    chunk_start_seconds: int | None = Field(ge=0)
    chunk_end_seconds: int | None = Field(gt=0)
    last_error: Failure | None


class CurrentVideoChunkView(ContractModel):
    sequence_no: int = Field(ge=0)
    start_seconds: int = Field(ge=0)
    end_seconds: int = Field(gt=0)


class ParseJobView(ContractModel):
    job_id: str
    upload_session_id: str | None
    workspace_id: str
    series_id: str
    video_id: str
    series_name: str
    episode_order: int = Field(gt=0)
    original_filename: str
    pipeline_version: str
    retry_of_job_id: str | None
    status: ParseJobStatus
    stage: ParseJobStage
    progress: int = Field(ge=0, le=100)
    overall_progress: int | None = Field(ge=0, le=100)
    stage_progress: int | None = Field(ge=0, le=100)
    completed_duration_seconds: int | None = Field(ge=0)
    completed_through_seconds: int | None = Field(ge=0)
    video_duration_seconds: int = Field(gt=0)
    total_duration_seconds: int = Field(gt=0)
    processing_duration_ms: int = Field(ge=0)
    processing_timing_complete: bool
    completed_chunk_count: int | None = Field(ge=0)
    total_chunk_count: int = Field(ge=0)
    current_chunk: CurrentVideoChunkView | None
    attempt_count: int = Field(ge=0)
    max_attempts: int = Field(gt=0)
    available_at: UnixTimeMs | None
    estimated_remaining_ms: int | None = Field(ge=0)
    started_at: UnixTimeMs | None
    completed_at: UnixTimeMs | None
    last_error: Failure | None
    checkpoints: list[VideoChunkCheckpointView]
    can_retry: bool
    can_reparse: bool
    can_cancel: bool
    version: int = Field(ge=1)
    created_at: UnixTimeMs
    updated_at: UnixTimeMs
    parse_config: ParseConfig | None = None


class VideoParseTaskView(ContractModel):
    video_parse_task_id: str
    video_id: str
    pipeline_version: str
    status: VideoParseTaskStatus
    parse_config: VideoParseV2Config
    chunk_count: int = Field(gt=0)
    version: int = Field(gt=0)
    next_execute_time: UnixTimeMs
    attempt_count: int = Field(ge=0)
    max_attempts: int = Field(gt=0)
    lease_owner: str | None
    lease_version: int = Field(ge=0)
    lease_expire_time: UnixTimeMs | None
    heartbeat_time: UnixTimeMs | None
    start_time: UnixTimeMs | None
    finish_time: UnixTimeMs | None
    processing_duration_ms: int = Field(ge=0)
    processing_timing_complete: bool
    last_error: Failure | None
    is_test: bool
    detail: dict
    create_time: UnixTimeMs
    update_time: UnixTimeMs


class JobCounts(ContractModel):
    queued: int = Field(ge=0)
    running: int = Field(ge=0)
    retry_wait: int = Field(ge=0)
    cancelling: int = Field(ge=0)
    completed: int = Field(ge=0)
    failed: int = Field(ge=0)
    cancelled: int = Field(ge=0)


class JobSummary(ContractModel):
    workspace_id: str
    series_id: str | None
    counts: JobCounts
    checkpoint_count: int = Field(ge=0)
    version: int = Field(ge=0)
    updated_at: UnixTimeMs
