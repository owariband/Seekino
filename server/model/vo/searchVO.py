from __future__ import annotations

from typing import Literal

from pydantic import Field

from ...schema import (
    AgentReply, ContractModel, SelectionReason, TranscriptCueView,
    VideoChunkCandidate,
)
from .commonVO import ImageAsset, UnixTimeMs


class Keyframe(ImageAsset):
    at_seconds: int = Field(ge=0)
    alt: str | None


class VisualEventView(ContractModel):
    description: str
    start_seconds: int = Field(ge=0)
    end_seconds: int = Field(gt=0)


class VideoChunkEvidenceView(ContractModel):
    workspace_id: str
    series_id: str
    series_name: str
    series_status: str
    video_id: str
    episode_order: int
    original_filename: str
    duration_seconds: int
    video_chunk_id: str
    sequence_no: int
    start_seconds: int
    end_seconds: int
    analysis_window_start_seconds: int
    analysis_window_end_seconds: int
    playback_status: Literal["PREPARING", "READY", "FAILED", "MISSING"]
    playback_url: str | None
    playback_url_expires_at: UnixTimeMs | None
    thumbnail: Keyframe | None
    keyframes: list[Keyframe]
    transcript: list[TranscriptCueView]
    baseline_visual_caption: str | None
    all_time_events: list[VisualEventView]
    all_time_event_text: str
    video_compact: str
    context_before_compact: str | None
    context_after_compact: str | None
    window_summary: str
    is_fallback: bool
    model_provider: str | None
    model_name: str | None
    pipeline_version: str
    parsed_at: UnixTimeMs


class VideoChunkCandidateView(VideoChunkCandidate):
    rank: int = Field(gt=0)


class SearchHitView(ContractModel):
    candidate: VideoChunkCandidateView
    selection_reason: SelectionReason
    evidence: VideoChunkEvidenceView


class SearchEvidenceFieldsView(ContractModel):
    series_ids: list[str]
    searched_video_ids: list[str]
    partial: bool
    warnings: list[str]
    hits: list[SearchHitView]


class SearchEvidenceView(SearchEvidenceFieldsView):
    query: str


class SearchResultView(SearchEvidenceView):
    agent_reply: AgentReply
