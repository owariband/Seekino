from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class ParseJobState:
    job_id: str
    workspace_id: str
    series_id: str
    video_id: str
    series_name: str
    episode_order: int
    original_filename: str
    pipeline_version: str
    status: str
    stage: str
    progress: int
    attempt_count: int
    max_attempts: int
    version: int
    available_at: datetime
    lease_owner: str | None
    lease_version: int
    lease_expires_at: datetime | None
    heartbeat_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    duration_ms: int
    upload_session_id: str | None = None
    is_primary_upload_job: bool = False


@dataclass(frozen=True, slots=True)
class ParseJobLease:
    job_id: str
    series_id: str
    video_id: str
    pipeline_version: str
    worker_id: str
    lease_version: int
    lease_expires_at: datetime
    attempt_count: int
    max_attempts: int
    episode_order: int
    upload_sequence: int
    original_filename: str
    original_source_uri: str
    content_hash: str
    total_bytes: int
    duration_ms: int
    workspace_id: str
    series_name: str
    series_description: str | None
    important_extra_data: dict | None
    character_bible: list[dict]


@dataclass(frozen=True, slots=True)
class VideoChunkCheckpointState:
    job_id: str
    sequence_no: int
    video_chunk_id: str
    stage: str
    chunk_start_ms: int | None = None
    chunk_end_ms: int | None = None
    chunk_media_uri: str | None = None
    analysis_window_media_uri: str | None = None
    parse_artifact_uri: str | None = None
    text_embedding_artifact_uri: str | None = None
    vision_embedding_artifact_uri: str | None = None
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class ParseJobSummaryState:
    workspace_id: str
    series_id: str | None
    counts: dict[str, int]
    checkpoint_count: int
    version: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ParseJobDetail:
    job: ParseJobState
    checkpoints: tuple[VideoChunkCheckpointState, ...]
