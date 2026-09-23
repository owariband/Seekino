from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _rename_legacy_fields(data: Any, fields: dict[str, str]) -> Any:
    """Consume old persisted field names without exposing them in new output."""

    if not isinstance(data, dict):
        return data
    populated = dict(data)
    for legacy_name, current_name in fields.items():
        if legacy_name in populated:
            populated.setdefault(current_name, populated[legacy_name])
            populated.pop(legacy_name)
    return populated


class TranscriptCue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_seconds: int = Field(ge=0)
    end_seconds: int = Field(ge=0)
    text: str = Field(min_length=1)


class TranscriptAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transcript: list[TranscriptCue]


class TimelineEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_seconds: int = Field(ge=0)
    end_seconds: int = Field(ge=0)
    event_type: Literal[
        "SCENE",
        "ACTION",
        "DIALOGUE",
        "EMOTION",
        "RELATION_CHANGE",
        "OTHER",
    ]
    description: str = Field(min_length=1)
    text: str | None

    @model_validator(mode="after")
    def validate_time_range(self) -> "TimelineEvent":
        if self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must be greater than start_seconds")
        return self


class VideoTimeline(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1)
    events: list[TimelineEvent]


class NeutralVisualObservation(BaseModel):
    """Anonymous, frame-addressable evidence produced by the vision stage."""

    model_config = ConfigDict(extra="forbid")

    evidence_frames: list[int] = Field(min_length=1)
    subjects: list[str]
    visual_fact: str = Field(min_length=1)
    hard_subtitle: str | None
    visible_speaker: str | None


class NeutralVisualEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observations: list[NeutralVisualObservation]


class VideoParseProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Keep persisted values compatible with the existing episode checkpoints.
    provider: Literal["doubao", "deepseek", "ollama", "llama_cpp"]
    model: str = Field(min_length=1)
    input_mode: Literal["video", "ordered_frames"]
    frame_fps: float | None = Field(default=None, gt=0)
    frame_timestamps_seconds: list[float] = Field(default_factory=list)
    group_frame_counts: list[int] = Field(default_factory=list)
    group_duration_seconds: int | None = Field(default=None, gt=0)
    max_frames_per_group: int | None = Field(default=None, gt=0)
    fallback_transcript_model: str | None = None
    visual_model: str | None = None
    refiner_model: str | None = None
    fallback_reason: Literal["content_filter"] | None = None
    primary_model: str | None = None
    primary_error_code: str | None = None
    primary_filter_type: str | None = None
    primary_filter_details: str | None = None
    primary_response_id: str | None = None
    primary_partial_output_chars: int | None = Field(default=None, ge=0)


class VideoEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    duration_seconds: int = Field(gt=0)
    candidate_transcript: list[TranscriptCue]
    summary: str = Field(min_length=1)
    events: list[TimelineEvent]
    provenance: VideoParseProvenance | None = None


class AnalysisWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence_no: int = Field(ge=0)
    source_video: Path
    media_path: Path
    chunk_start_seconds: int = Field(ge=0)
    chunk_end_seconds: int = Field(gt=0)
    analysis_start_seconds: int = Field(ge=0)
    analysis_end_seconds: int = Field(gt=0)
    analysis_duration_seconds: int = Field(gt=0)
    relative_chunk_start_seconds: int = Field(ge=0)
    relative_chunk_end_seconds: int = Field(gt=0)

    @model_validator(mode="before")
    @classmethod
    def populate_derived_ranges(cls, data: Any) -> Any:
        populated = _rename_legacy_fields(
            data,
            {
                "segment_start_seconds": "chunk_start_seconds",
                "segment_end_seconds": "chunk_end_seconds",
                "relative_segment_start_seconds": "relative_chunk_start_seconds",
                "relative_segment_end_seconds": "relative_chunk_end_seconds",
            },
        )
        if not isinstance(populated, dict):
            return populated
        required = {
            "chunk_start_seconds",
            "chunk_end_seconds",
            "analysis_start_seconds",
            "analysis_end_seconds",
        }
        if not required.issubset(populated):
            return populated

        populated.setdefault(
            "analysis_duration_seconds",
            populated["analysis_end_seconds"] - populated["analysis_start_seconds"],
        )
        populated.setdefault(
            "relative_chunk_start_seconds",
            populated["chunk_start_seconds"] - populated["analysis_start_seconds"],
        )
        populated.setdefault(
            "relative_chunk_end_seconds",
            populated["chunk_end_seconds"] - populated["analysis_start_seconds"],
        )
        return populated

    @model_validator(mode="after")
    def validate_ranges(self) -> "AnalysisWindow":
        if self.chunk_end_seconds <= self.chunk_start_seconds:
            raise ValueError("chunk_end_seconds must be greater than chunk_start_seconds")
        if self.analysis_end_seconds <= self.analysis_start_seconds:
            raise ValueError("analysis_end_seconds must be greater than analysis_start_seconds")
        if self.analysis_start_seconds > self.chunk_start_seconds:
            raise ValueError("analysis window must include the canonical chunk start")
        if self.analysis_end_seconds < self.chunk_end_seconds:
            raise ValueError("analysis window must include the canonical chunk end")
        if self.analysis_duration_seconds != self.analysis_end_seconds - self.analysis_start_seconds:
            raise ValueError("analysis_duration_seconds is inconsistent with the window")
        if (
            self.relative_chunk_start_seconds
            != self.chunk_start_seconds - self.analysis_start_seconds
        ):
            raise ValueError(
                "relative_chunk_start_seconds is inconsistent with the window"
            )
        if (
            self.relative_chunk_end_seconds
            != self.chunk_end_seconds - self.analysis_start_seconds
        ):
            raise ValueError(
                "relative_chunk_end_seconds is inconsistent with the window"
            )
        return self


class OwnedTimelineEvent(TimelineEvent):
    source_chunk_sequence_no: int = Field(ge=0)

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_sequence_name(cls, data: Any) -> Any:
        return _rename_legacy_fields(
            data,
            {"source_segment_sequence_no": "source_chunk_sequence_no"},
        )


class ContextEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_sequence_no: int = Field(ge=0)
    start_seconds: int = Field(ge=0)
    end_seconds: int = Field(gt=0)
    event_type: str = Field(min_length=1, max_length=32)
    description: str = Field(min_length=1, max_length=400)
    text: str | None = Field(default=None, max_length=240)

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_sequence_name(cls, data: Any) -> Any:
        return _rename_legacy_fields(
            data,
            {"segment_sequence_no": "chunk_sequence_no"},
        )

    @model_validator(mode="after")
    def validate_time_range(self) -> "ContextEvent":
        if self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must be greater than start_seconds")
        return self


class ContextSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: str = Field(min_length=1)
    after_chunk_sequence_no: int = Field(ge=-1)
    compact_text: str
    recent_events: list[ContextEvent]

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_sequence_name(cls, data: Any) -> Any:
        return _rename_legacy_fields(
            data,
            {"after_segment_sequence_no": "after_chunk_sequence_no"},
        )


class ObservedTimelineEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relative_event: TimelineEvent
    video_start_seconds: int = Field(ge=0)
    video_end_seconds: int = Field(gt=0)
    owned_by_current_chunk: bool
    kept_after_deduplication: bool

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_owner_name(cls, data: Any) -> Any:
        return _rename_legacy_fields(
            data,
            {"owned_by_current_segment": "owned_by_current_chunk"},
        )

    @model_validator(mode="after")
    def validate_time_range(self) -> "ObservedTimelineEvent":
        if self.video_end_seconds <= self.video_start_seconds:
            raise ValueError(
                "video_end_seconds must be greater than video_start_seconds"
            )
        if self.kept_after_deduplication and not self.owned_by_current_chunk:
            raise ValueError("an unowned event cannot be kept")
        return self


class VideoChunkParseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window: AnalysisWindow
    context_before: ContextSnapshot
    candidate_transcript: list[TranscriptCue]
    video_parse: VideoParseProvenance | None = None
    is_fallback: bool = False
    window_summary: str = Field(min_length=1)
    observed_events: list[ObservedTimelineEvent]
    events: list[OwnedTimelineEvent]
    self_compact: str
    context_after: ContextSnapshot
    context_before_compact: str
    context_after_compact: str

    @model_validator(mode="before")
    @classmethod
    def populate_compatibility_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        populated = dict(data)
        if "needs_regeneration" in populated:
            populated.setdefault("is_fallback", populated["needs_regeneration"])
            populated.pop("needs_regeneration")
        before = data.get("context_before")
        after = data.get("context_after")
        if before is not None:
            compact = (
                before.compact_text
                if isinstance(before, ContextSnapshot)
                else before.get("compact_text")
            )
            populated.setdefault("context_before_compact", compact)
        if after is not None:
            compact = (
                after.compact_text
                if isinstance(after, ContextSnapshot)
                else after.get("compact_text")
            )
            populated.setdefault("context_after_compact", compact)
        return populated

    @model_validator(mode="after")
    def validate_compact_text(self) -> "VideoChunkParseResult":
        if self.context_before_compact != self.context_before.compact_text:
            raise ValueError("context_before_compact is inconsistent with snapshot")
        if self.context_after_compact != self.context_after.compact_text:
            raise ValueError("context_after_compact is inconsistent with snapshot")
        return self


class VideoFallbackModels(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    transcript_model: str = Field(min_length=1)
    provider: Literal["deepseek"]
    visual_model: str = Field(min_length=1)
    refiner_model: str = Field(min_length=1)
    input_mode: Literal["ordered_frames"]
    frame_fps: float = Field(gt=0)
    group_duration_seconds: int = Field(gt=0)
    max_frames_per_group: int = Field(gt=0)
    max_height: int = Field(gt=0)


class VideoParseModels(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transcript: str | None
    transcript_mode: Literal["lite", "none"]
    video_parser: str = Field(min_length=1)
    fallback: VideoFallbackModels


class VideoParseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    models: VideoParseModels | None = None
    source_video: Path
    duration_seconds: int = Field(gt=0)
    chunk_duration_seconds: int = Field(gt=0)
    context_overlap_seconds: int = Field(ge=0)
    chunks: list[VideoChunkParseResult]
    events: list[OwnedTimelineEvent]

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_chunk_names(cls, data: Any) -> Any:
        return _rename_legacy_fields(
            data,
            {
                "segment_duration_seconds": "chunk_duration_seconds",
                "segments": "chunks",
            },
        )


class TranscriptCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transcript_model: str = Field(min_length=1)
    transcript_source: Literal["lite", "none"] = "lite"
    window: AnalysisWindow
    analysis: TranscriptAnalysis


class VideoChunkCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transcript_model: str = Field(min_length=1)
    transcript_source: Literal["lite", "none"] = "lite"
    video_model: str = Field(min_length=1)
    chunk: VideoChunkParseResult

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_chunk_name(cls, data: Any) -> Any:
        return _rename_legacy_fields(data, {"segment": "chunk"})
