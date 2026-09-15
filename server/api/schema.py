from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from ..constants.workspace import DEFAULT_LOCAL_WORKSPACE_ID
from ..schema import ContractModel, VideoChunkSearchResult


class SeriesCreateRequest(ContractModel):
    workspace_id: str = Field(default=DEFAULT_LOCAL_WORKSPACE_ID, min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    important_extra_data: dict[str, Any] | None = None
    character_bible: list[dict[str, Any]] = Field(default_factory=list)


class VideoSummary(ContractModel):
    video_id: str
    episode_order: int = Field(gt=0)
    upload_sequence: int = Field(gt=0)
    original_filename: str
    original_source_uri: str
    inference_proxy_uri: str | None
    content_hash: str
    total_bytes: int = Field(ge=0)
    duration_ms: int = Field(gt=0)
    upload_status: str
    parse_status: str
    upload_progress: int = Field(ge=0, le=100)
    parse_progress: int = Field(ge=0, le=100)


class SeriesView(ContractModel):
    series_id: str
    workspace_id: str
    name: str
    description: str | None
    status: str
    important_extra_data: dict[str, Any] | None
    character_bible: list[dict[str, Any]]
    video_count: int = Field(ge=0)
    videos: list[VideoSummary] = Field(default_factory=list)


class UploadSessionCreateRequest(ContractModel):
    workspace_id: str = Field(default=DEFAULT_LOCAL_WORKSPACE_ID, min_length=1, max_length=64)
    episode_order: int = Field(gt=0)
    upload_sequence: int | None = Field(default=None, gt=0)
    original_filename: str = Field(min_length=1, max_length=1024)
    media_type: str = Field(min_length=1, max_length=255)
    total_size_bytes: int = Field(gt=0)


class UploadSessionView(ContractModel):
    upload_session_id: str
    workspace_id: str
    series_id: str
    episode_order: int = Field(gt=0)
    upload_sequence: int = Field(gt=0)
    original_filename: str
    media_type: str
    total_size_bytes: int = Field(gt=0)
    preferred_segment_size_bytes: int = Field(gt=0)
    total_segments: int = Field(gt=0)
    status: str
    uploaded_segment_count: int = Field(ge=0)
    uploaded_size_bytes: int = Field(ge=0)
    missing_segment_numbers: list[int]
    expires_at: datetime
    video_id: str | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


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
    job_id: str


class VideoChunkCheckpointView(ContractModel):
    sequence_no: int = Field(ge=0)
    video_chunk_id: str
    stage: str
    chunk_media_uri: str | None
    analysis_window_media_uri: str | None
    last_error: str | None


class ParseJobView(ContractModel):
    job_id: str
    series_id: str
    video_id: str
    series_name: str
    episode_order: int = Field(gt=0)
    original_filename: str
    pipeline_version: str
    status: str
    stage: str
    progress: int = Field(ge=0, le=100)
    attempt_count: int = Field(ge=0)
    max_attempts: int = Field(gt=0)
    available_at: datetime
    lease_expires_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    last_error: str | None
    checkpoints: list[VideoChunkCheckpointView] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class VideoSearchRequest(ContractModel):
    workspace_id: str = Field(default=DEFAULT_LOCAL_WORKSPACE_ID, min_length=1, max_length=64)
    query: str = Field(min_length=1)
    series_ids: list[str] = Field(min_length=1)
    top_k: int = Field(default=10, gt=0, le=100)


class AgentSessionCreateRequest(ContractModel):
    workspace_id: str = Field(default=DEFAULT_LOCAL_WORKSPACE_ID, min_length=1, max_length=64)
    name: str = Field(default="画面札记", min_length=1, max_length=255)


class AgentConversationCreateRequest(ContractModel):
    workspace_id: str = Field(default=DEFAULT_LOCAL_WORKSPACE_ID, min_length=1, max_length=64)
    title: str = Field(default="新对话", min_length=1, max_length=255)
    series_ids: list[str] = Field(min_length=1)


class AgentMessageSendRequest(ContractModel):
    workspace_id: str = Field(default=DEFAULT_LOCAL_WORKSPACE_ID, min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=20_000)
    top_k: int | None = Field(default=None, gt=0, le=100)


class ApiError(ContractModel):
    code: str
    message: str


class AgentSessionView(ContractModel):
    session_id: str
    workspace_id: str
    name: str
    conversation_count: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime


class AgentConversationView(ContractModel):
    conversation_id: str
    session_id: str
    workspace_id: str
    title: str
    series_ids: list[str] = Field(min_length=1)
    message_count: int = Field(ge=0)
    last_message_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AgentMessageView(ContractModel):
    message_id: str
    conversation_id: str
    sequence_no: int = Field(ge=0)
    role: Literal["USER", "ASSISTANT"]
    status: Literal["PROCESSING", "COMPLETED", "FAILED"]
    content: str | None
    model_name: str | None
    reply_to_message_id: str | None
    retrieval_query: str | None
    search_result: VideoChunkSearchResult | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class AgentMessageSendView(ContractModel):
    user_message: AgentMessageView
    assistant_message: AgentMessageView


class AgentMessageEventView(ContractModel):
    conversation_id: str
    messages: list[AgentMessageView]
