from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class ApiModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        from_attributes=True,
        extra="forbid",
    )


class ParseImportReport(ApiModel):
    series_id: str
    video_id: str
    video_chunk_ids: list[str]
    chunk_count: int = Field(gt=0)
    vectors_imported: bool


class IndexSyncRunReport(ApiModel):
    materialized_count: int = Field(ge=0)
    selected_count: int = Field(ge=0)
    synced_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    stale_result_count: int = Field(ge=0)


class IndexReconcileReport(ApiModel):
    materialized_count: int = Field(ge=0)
    inspected_count: int = Field(ge=0)
    consistent_count: int = Field(ge=0)
    repair_attempted_count: int = Field(ge=0)
    synced_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    stale_result_count: int = Field(ge=0)


class SeriesCreateRequest(ApiModel):
    workspace_id: str = Field(default="local-default", min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    important_extra_data: dict[str, Any] | None = None
    character_bible: list[dict[str, Any]] = Field(default_factory=list)


class VideoSummary(ApiModel):
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


class SeriesView(ApiModel):
    series_id: str
    workspace_id: str
    name: str
    description: str | None
    status: str
    important_extra_data: dict[str, Any] | None
    character_bible: list[dict[str, Any]]
    video_count: int = Field(ge=0)
    videos: list[VideoSummary] = Field(default_factory=list)


class UploadSessionCreateRequest(ApiModel):
    workspace_id: str = Field(default="local-default", min_length=1, max_length=64)
    episode_order: int = Field(gt=0)
    upload_sequence: int | None = Field(default=None, gt=0)
    original_filename: str = Field(min_length=1, max_length=1024)
    media_type: str = Field(min_length=1, max_length=255)
    total_size_bytes: int = Field(gt=0)


class UploadSessionView(ApiModel):
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


class UploadSegmentView(ApiModel):
    upload_session_id: str
    segment_number: int = Field(ge=0)
    start_byte: int = Field(ge=0)
    end_byte_exclusive: int = Field(gt=0)
    actual_size_bytes: int = Field(gt=0)
    checksum_sha256: str


class UploadCompleteView(ApiModel):
    upload_session: UploadSessionView
    video_id: str
    job_id: str


class VideoChunkCheckpointView(ApiModel):
    sequence_no: int = Field(ge=0)
    video_chunk_id: str
    stage: str
    chunk_media_uri: str | None
    analysis_window_media_uri: str | None
    last_error: str | None


class ParseJobView(ApiModel):
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


class VideoSearchRequest(ApiModel):
    workspace_id: str = Field(default="local-default", min_length=1, max_length=64)
    query: str = Field(min_length=1)
    series_ids: list[str] = Field(min_length=1)
    top_k: int = Field(default=10, gt=0, le=100)


class AgentSessionCreateRequest(ApiModel):
    workspace_id: str = Field(default="local-default", min_length=1, max_length=64)
    name: str = Field(default="画面札记", min_length=1, max_length=255)


class AgentConversationCreateRequest(ApiModel):
    workspace_id: str = Field(default="local-default", min_length=1, max_length=64)
    title: str = Field(default="新对话", min_length=1, max_length=255)
    series_ids: list[str] = Field(min_length=1)


class AgentMessageSendRequest(ApiModel):
    workspace_id: str = Field(default="local-default", min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=20_000)
    top_k: int | None = Field(default=None, gt=0, le=100)


class ApiError(ApiModel):
    code: str
    message: str


class TranscriptCueView(ApiModel):
    speaker: str | None
    text: str = Field(min_length=1)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_time_range(self) -> "TranscriptCueView":
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms must be greater than start_ms")
        return self


class VideoChunkEvidence(ApiModel):
    series_id: str
    workspace_id: str
    series_name: str
    series_description: str | None
    series_status: str
    important_extra_data: dict[str, Any] | None
    character_bible: list[dict[str, Any]]

    video_id: str
    episode_order: int
    original_filename: str
    original_source_uri: str
    inference_proxy_uri: str | None
    content_hash: str
    duration_ms: int
    upload_status: str
    parse_status: str

    video_chunk_id: str
    sequence_no: int
    start_ms: int
    end_ms: int
    analysis_window_start_ms: int
    analysis_window_end_ms: int
    chunk_media_ref: str
    analysis_window_media_ref: str
    transcript: list[TranscriptCueView]
    baseline_visual_caption: str | None
    keyframe_uris: list[str]

    all_time_events: list[dict[str, Any]]
    all_time_event_text: str
    retrieval_text: str
    video_compact: str
    context_before_compact: str
    context_after_compact: str
    window_summary: str
    model_provider: str | None
    model_name: str | None
    pipeline_version: str
    is_fallback: bool
    parse_provenance: dict[str, Any] | None
    parsed_at: datetime


class VideoChunkCandidate(ApiModel):
    video_chunk_id: str
    fused_rank: int = Field(gt=0)
    fused_score: float = Field(gt=0, allow_inf_nan=False)
    text_rank: int | None = Field(default=None, gt=0)
    text_score: float | None = Field(default=None, allow_inf_nan=False)
    vision_rank: int | None = Field(default=None, gt=0)
    vision_score: float | None = Field(default=None, allow_inf_nan=False)


class MatchedEvidence(ApiModel):
    kind: Literal["TRANSCRIPT", "VISUAL_EVENT", "CONTEXT"]
    text: str = Field(min_length=1)
    start_ms: int | None
    end_ms: int | None

    @model_validator(mode="after")
    def validate_time_range(self) -> "MatchedEvidence":
        if (self.start_ms is None) != (self.end_ms is None):
            raise ValueError("start_ms and end_ms must both be null or both be set")
        if (
            self.start_ms is not None
            and self.end_ms is not None
            and self.end_ms <= self.start_ms
        ):
            raise ValueError("end_ms must be greater than start_ms")
        return self


class SelectionReason(ApiModel):
    summary: str = Field(min_length=1)
    matched_evidence: list[MatchedEvidence]
    limitations: list[str]


class VideoChunkSearchHit(ApiModel):
    candidate: VideoChunkCandidate
    selection_reason: SelectionReason
    evidence: VideoChunkEvidence


class AgentReply(ApiModel):
    message: str = Field(min_length=1)
    result_count: int = Field(ge=0)


class VideoChunkSearchResult(ApiModel):
    query: str = Field(min_length=1)
    series_ids: list[str] = Field(min_length=1)
    agent_reply: AgentReply
    hits: list[VideoChunkSearchHit]

    @model_validator(mode="after")
    def validate_result_contract(self) -> "VideoChunkSearchResult":
        if self.agent_reply.result_count != len(self.hits):
            raise ValueError("agentReply.resultCount must equal hits length")
        for hit in self.hits:
            if hit.candidate.video_chunk_id != hit.evidence.video_chunk_id:
                raise ValueError(
                    "candidate.videoChunkId must equal evidence.videoChunkId"
                )
        return self


class AgentSessionView(ApiModel):
    session_id: str
    workspace_id: str
    name: str
    conversation_count: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime


class AgentConversationView(ApiModel):
    conversation_id: str
    session_id: str
    workspace_id: str
    title: str
    series_ids: list[str] = Field(min_length=1)
    message_count: int = Field(ge=0)
    last_message_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AgentMessageView(ApiModel):
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


class AgentMessageSendView(ApiModel):
    user_message: AgentMessageView
    assistant_message: AgentMessageView


class AgentMessageEventView(ApiModel):
    conversation_id: str
    messages: list[AgentMessageView]
