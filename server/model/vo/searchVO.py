from __future__ import annotations

from typing import Literal

from pydantic import Field

from ...schema import (
    AgentReply, ContractModel, SelectionReason, TranscriptCueView,
    VideoChunkCandidate,
)
from .commonVO import ImageAsset, UnixTimeMs


class Keyframe(ImageAsset):
    at_ms: int = Field(ge=0)
    alt: str | None


class VisualEventView(ContractModel):
    description: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)


class VideoChunkEvidenceView(ContractModel):
    workspace_id: str
    series_id: str
    series_name: str
    series_status: str
    video_id: str
    episode_order: int
    original_filename: str
    duration_ms: int
    video_chunk_id: str
    sequence_no: int
    start_ms: int
    end_ms: int
    analysis_window_start_ms: int
    analysis_window_end_ms: int
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


class SearchResultView(ContractModel):
    query: str
    series_ids: list[str]
    searched_video_ids: list[str]
    partial: bool
    warnings: list[str]
    agent_reply: AgentReply
    hits: list[SearchHitView]
