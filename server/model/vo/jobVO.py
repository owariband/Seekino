from __future__ import annotations

from pydantic import Field

from ...schema import ContractModel
from ..job import ParseJobStatus, ParseJobStage, VideoChunkCheckpointStage
from .commonVO import Failure, UnixTimeMs


class VideoChunkCheckpointView(ContractModel):
    sequence_no: int = Field(ge=0)
    video_chunk_id: str
    stage: VideoChunkCheckpointStage
    chunk_start_ms: int | None = Field(ge=0)
    chunk_end_ms: int | None = Field(gt=0)
    last_error: Failure | None


class CurrentVideoChunkView(ContractModel):
    sequence_no: int = Field(ge=0)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)


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
    completed_duration_ms: int | None = Field(ge=0)
    completed_through_ms: int | None = Field(ge=0)
    total_duration_ms: int = Field(gt=0)
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
    can_cancel: bool
    version: int = Field(ge=1)
    created_at: UnixTimeMs
    updated_at: UnixTimeMs


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
