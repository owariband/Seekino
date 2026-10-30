from __future__ import annotations

from pydantic import Field

from agent.video_parse_v2.input import ParseConfig as VideoParseV2Config

from ...schema import ContractModel
from ..job import VideoParseTaskStatus
from .commonVO import Failure, UnixTimeMs


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
    tenant_id: str | None = None
    series_id: str | None = None
    series_name: str | None = None
    episode_order: int | None = Field(default=None, gt=0)
    original_filename: str | None = None
    upload_session_id: str | None = None
    duration_seconds: int | None = Field(default=None, gt=0)
    is_paused: bool = False
    can_pause: bool = False
    can_resume: bool = False
    active_stage: str | None = None
    can_retry: bool = False
    can_reparse: bool = False
    can_delete: bool = False
