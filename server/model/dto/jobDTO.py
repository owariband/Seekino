from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ...config.elasticsearch import DEFAULT_INDEX_GENERATION


@dataclass(frozen=True, slots=True)
class ParseJobState:
    job_id: str
    tenant_id: str
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
    available_at: int
    lease_owner: str | None
    lease_version: int
    lease_expires_at: int | None
    heartbeat_at: int | None
    started_at: int | None
    completed_at: int | None
    processing_duration_ms: int
    processing_timing_complete: bool
    last_error: str | None
    created_at: int
    updated_at: int
    duration_seconds: int
    upload_session_id: str | None = None
    is_primary_upload_job: bool = False
    parse_config: dict | None = None
    created_by_user_id: str | None = None


@dataclass(frozen=True, slots=True)
class VideoParseTaskState:
    video_parse_task_id: str
    video_id: str
    pipeline_version: str
    status: str
    parse_config: dict
    chunk_count: int
    version: int
    next_execute_time: datetime
    attempt_count: int
    max_attempts: int
    lease_owner: str | None
    lease_version: int
    lease_expire_time: datetime | None
    heartbeat_time: datetime | None
    start_time: datetime | None
    finish_time: datetime | None
    processing_duration_ms: int
    processing_timing_complete: bool
    last_error: dict | None
    is_test: bool
    detail: dict
    create_time: datetime
    update_time: datetime
    created_by_user_id: str | None = None
    target_index_generation: int = DEFAULT_INDEX_GENERATION
    scheduler_slot: int | None = None
    prepare_retry_count: int = 0


@dataclass(frozen=True, slots=True)
class VideoParseTaskDetail:
    task: VideoParseTaskState
    tenant_id: str
    series_id: str
    series_name: str
    episode_order: int
    original_filename: str
    duration_seconds: int
    upload_session_id: str | None
    is_latest: bool
    has_active_prefix_lease: bool = False
    has_active_prefix_suffix_lease: bool = False
    has_active_execution_lease: bool = False
    has_active_deletion_lease: bool = False
    pending_takeover_enabled: bool = False


@dataclass(frozen=True, slots=True)
class VideoParseTaskInputState:
    task: VideoParseTaskState
    series_id: str
    episode_order: int
    original_filename: str
    original_source_uri: str
    inference_proxy_uri: str | None
    content_hash: str
    total_bytes: int
    duration_seconds: int
    tenant_id: str = ""
    background: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ParseJobLease:
    job_id: str
    series_id: str
    video_id: str
    pipeline_version: str
    worker_id: str
    lease_version: int
    lease_expires_at: int
    attempt_count: int
    max_attempts: int
    processing_duration_base_ms: int
    episode_order: int
    upload_sequence: int
    original_filename: str
    original_source_uri: str
    content_hash: str
    total_bytes: int
    duration_seconds: int
    tenant_id: str
    series_name: str
    series_description: str | None
    important_extra_data: dict | None
    character_bible: list[dict]
    preserve_existing_result: bool = False
    parse_config: dict | None = None


@dataclass(frozen=True, slots=True)
class VideoChunkCheckpointState:
    job_id: str
    sequence_no: int
    video_chunk_id: str
    stage: str
    chunk_start_seconds: int | None = None
    chunk_end_seconds: int | None = None
    chunk_media_uri: str | None = None
    analysis_window_media_uri: str | None = None
    parse_artifact_uri: str | None = None
    text_embedding_artifact_uri: str | None = None
    vision_embedding_artifact_uri: str | None = None
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class ParseJobSummaryState:
    tenant_id: str
    series_id: str | None
    counts: dict[str, int]
    checkpoint_count: int
    version: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class ParseJobDetail:
    job: ParseJobState
    checkpoints: tuple[VideoChunkCheckpointState, ...]
    processing_duration_ms: int = 0
    processing_timing_complete: bool = False


@dataclass(frozen=True, slots=True)
class VideoParseLease:
    video_parse_task_id: str
    video_id: str
    worker_id: str
    lease_version: int
    lease_expire_time: datetime
    attempt_count: int
    max_attempts: int
    parse_config: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ChunkParseLease:
    chunk_parse_task_id: str
    video_parse_task_id: str
    video_chunk_id: str
    worker_id: str
    lease_version: int
    lease_expire_time: datetime
    retry_count: int
    max_retry_count: int
    retry_initial_delay_seconds: float
    retry_max_delay_seconds: float

    execution_deadline: datetime | None = None
    phase: int = 1


@dataclass(frozen=True, slots=True)
class IntervalParseLease:
    interval_parse_task_id: str
    video_parse_task_id: str
    chunk_parse_task_ids: tuple[str, ...]
    start_sequence_no: int
    chunk_count: int
    phase: str
    worker_id: str
    lease_version: int
    lease_expire_time: datetime
    retry_count: int
    max_retry_count: int
    retry_initial_delay_seconds: float
    retry_max_delay_seconds: float

    execution_deadline: datetime | None = None


@dataclass(frozen=True, slots=True)
class PreparedVideoParseState:
    video_parse_task_id: str
    chunk_parse_task_ids: tuple[str, ...]
    interval_parse_task_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VideoChunkState:
    video_chunk_id: str
    sequence_no: int
    start_seconds: int
    end_seconds: int
    chunk_media_ref: str
    analysis_window_media_ref: str


@dataclass(frozen=True, slots=True)
class ChunkParseContentState:
    chunk_parse_task_id: str
    video_chunk_id: str
    sequence_no: int
    content: dict[str, Any]
    version: int
    detail: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ChunkExecutionState:
    chunk_parse_task_id: str
    video_parse_task_id: str
    video_chunk_id: str
    sequence_no: int
    start_seconds: int
    end_seconds: int
    analysis_window_start_seconds: int
    analysis_window_end_seconds: int
    source_media_ref: str
    chunk_media_ref: str
    analysis_window_media_ref: str
    task_input: VideoParseTaskInputState


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


@dataclass(frozen=True, slots=True)
class ModelCallReservation:
    video_parse_task_id: str
    call_id: str
    worker_id: str
    first_reservation: bool
    used_calls: int
    max_calls: int
    active_calls: int
    max_concurrent_calls: int
    lease_expire_time: datetime


@dataclass(frozen=True, slots=True)
class PrefixParseLease:
    video_parse_task_id: str
    chunk_parse_task_id: str
    video_chunk_id: str
    sequence_no: int
    worker_id: str
    lease_version: int
    lease_expire_time: datetime
    retry_count: int
    content_version: int
    content: dict[str, Any]
    content_detail: dict[str, Any]
    series_prefix_compact: str
    video_prefix_compact: str


@dataclass(frozen=True, slots=True)
class ChunkScheduleEntryState:
    chunk_parse_task_id: str
    sequence_no: int
    status: str
    phase: int = 1
    understanding_committed: bool = False


@dataclass(frozen=True, slots=True)
class ChunkScheduleState:
    tasks: tuple[ChunkScheduleEntryState, ...]
    wait_timeout_seconds: float
    terminal_error: str | None = None
