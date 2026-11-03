"""Task state, execution leases and scheduling snapshots."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from agent.video_parse_v2.state import ChunkTaskPhase
from engine.parsev2.schemas import VideoParseResult

from ...utils.time import now_utc


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

    @property
    def target_index_generation(self) -> int:
        return self.parse_config["indexing"]["index_generation"]


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
class VideoParseRetryResult:
    next_execute_time: datetime | None
    prepare_failure_count: int | None


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
    phase: int = ChunkTaskPhase.PARSE


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
class ModelCallReservation:
    video_parse_task_id: str
    call_id: str
    worker_id: str
    first_reservation: bool
    used_calls: int
    active_calls: int
    lease_expire_time: datetime


@dataclass(frozen=True, slots=True)
class VideoSummarizeLease:
    video_parse_task_id: str
    video_id: str
    worker_id: str
    lease_version: int
    lease_expire_time: datetime
    retry_count: int
    source_fingerprint: str
    task_input: VideoParseTaskInputState
    config_snapshot: dict[str, Any]


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
    phase: int = ChunkTaskPhase.PARSE
    understanding_committed: bool = False


@dataclass(frozen=True, slots=True)
class ChunkScheduleState:
    tasks: tuple[ChunkScheduleEntryState, ...]
    wait_timeout_seconds: float
    terminal_error: str | None = None


@dataclass(kw_only=True)
class VideoParseTaskData:
    id: str
    creator_id: str | None = None
    video_id: str
    pipeline_version: str
    status: str
    parse_config: dict[str, Any]
    chunk_count: int
    version: int
    next_execute_time: datetime = field(default_factory=now_utc)
    attempt_count: int = 0
    max_attempts: int
    lease_owner: str | None = None
    lease_version: int = 0
    lease_expire_time: datetime | None = None
    heartbeat_time: datetime | None = None
    start_time: datetime | None = None
    finish_time: datetime | None = None
    processing_duration_ms: int = 0
    processing_timing_complete: bool = True
    last_error: dict[str, Any] | None = None
    is_test: bool = False
    is_deleted: bool = False
    detail: dict[str, Any] = field(default_factory=dict)
    create_time: int | None = None
    create_date: datetime | None = None
    update_time: int | None = None
    update_date: datetime | None = None


@dataclass(kw_only=True)
class ChunkParseTaskData:
    id: str
    video_task_id: str
    chunk_id: str
    phase: int = ChunkTaskPhase.PARSE
    indexed_content_version: int | None = None
    status: str
    next_execute_time: datetime = field(default_factory=now_utc)
    retry_count: int = 0
    lease_owner: str | None = None
    lease_version: int = 0
    lease_expire_time: datetime | None = None
    start_time: datetime | None = None
    finish_time: datetime | None = None
    last_error: dict[str, Any] | None = None
    detail: dict[str, Any] | None = None
    create_time: int | None = None
    create_date: datetime | None = None
    update_time: int | None = None
    update_date: datetime | None = None


@dataclass(kw_only=True)
class IntervalParseTaskData:
    id: str
    chunk_task_id: str
    video_task_id: str
    chunk_task_ids: list[str]
    start_sequence_no: int
    chunk_count: int
    ready_mask: int = 0
    status: str
    next_execute_time: datetime = field(default_factory=now_utc)
    retry_count: int = 0
    lease_owner: str | None = None
    lease_version: int = 0
    lease_expire_time: datetime | None = None
    start_time: datetime | None = None
    finish_time: datetime | None = None
    last_error: dict[str, Any] | None = None
    create_time: int | None = None
    create_date: datetime | None = None
    update_time: int | None = None
    update_date: datetime | None = None


@dataclass(frozen=True, slots=True)
class RealtimeChunkState:
    chunk_parse_task_id: str
    video_chunk_id: str
    sequence_no: int
    start_ms: int
    end_ms: int
    status: str
    content: VideoParseResult | None
    content_version: int | None
    last_error: dict | None
    parent_error: str | None = None
    phase: int = ChunkTaskPhase.PARSE
