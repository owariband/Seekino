from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TranscriptCue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    text: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_time_range(self) -> "TranscriptCue":
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms must be greater than start_ms")
        return self


class TranscriptAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transcript: list[TranscriptCue]


class TimelineEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
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
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms must be greater than start_ms")
        return self


class VideoTimeline(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1)
    events: list[TimelineEvent]


class VideoParseProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Keep persisted values compatible with the existing episode checkpoints.
    provider: Literal["doubao", "ollama", "llama_cpp"]
    model: str = Field(min_length=1)
    input_mode: Literal["video", "ordered_frames"]
    frame_fps: float | None = Field(default=None, gt=0)
    frame_timestamps_ms: list[int] = Field(default_factory=list)
    fallback_reason: Literal["content_filter"] | None = None
    primary_model: str | None = None
    primary_error_code: str | None = None
    primary_filter_type: str | None = None
    primary_filter_details: str | None = None
    primary_response_id: str | None = None
    primary_partial_output_chars: int | None = Field(default=None, ge=0)


class VideoEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    duration_ms: int = Field(gt=0)
    candidate_transcript: list[TranscriptCue]
    summary: str = Field(min_length=1)
    events: list[TimelineEvent]
    provenance: VideoParseProvenance | None = None


class AnalysisWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence_no: int = Field(ge=0)
    source_video: Path
    media_path: Path
    segment_start_ms: int = Field(ge=0)
    segment_end_ms: int = Field(gt=0)
    analysis_start_ms: int = Field(ge=0)
    analysis_end_ms: int = Field(gt=0)
    analysis_duration_ms: int = Field(gt=0)
    relative_segment_start_ms: int = Field(ge=0)
    relative_segment_end_ms: int = Field(gt=0)

    @model_validator(mode="before")
    @classmethod
    def populate_derived_ranges(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        required = {
            "segment_start_ms",
            "segment_end_ms",
            "analysis_start_ms",
            "analysis_end_ms",
        }
        if not required.issubset(data):
            return data

        populated = dict(data)
        populated.setdefault(
            "analysis_duration_ms",
            data["analysis_end_ms"] - data["analysis_start_ms"],
        )
        populated.setdefault(
            "relative_segment_start_ms",
            data["segment_start_ms"] - data["analysis_start_ms"],
        )
        populated.setdefault(
            "relative_segment_end_ms",
            data["segment_end_ms"] - data["analysis_start_ms"],
        )
        return populated

    @model_validator(mode="after")
    def validate_ranges(self) -> "AnalysisWindow":
        if self.segment_end_ms <= self.segment_start_ms:
            raise ValueError("segment_end_ms must be greater than segment_start_ms")
        if self.analysis_end_ms <= self.analysis_start_ms:
            raise ValueError("analysis_end_ms must be greater than analysis_start_ms")
        if self.analysis_start_ms > self.segment_start_ms:
            raise ValueError("analysis window must include the canonical segment start")
        if self.analysis_end_ms < self.segment_end_ms:
            raise ValueError("analysis window must include the canonical segment end")
        if self.analysis_duration_ms != self.analysis_end_ms - self.analysis_start_ms:
            raise ValueError("analysis_duration_ms is inconsistent with the window")
        if (
            self.relative_segment_start_ms
            != self.segment_start_ms - self.analysis_start_ms
        ):
            raise ValueError(
                "relative_segment_start_ms is inconsistent with the window"
            )
        if (
            self.relative_segment_end_ms
            != self.segment_end_ms - self.analysis_start_ms
        ):
            raise ValueError(
                "relative_segment_end_ms is inconsistent with the window"
            )
        return self


class OwnedTimelineEvent(TimelineEvent):
    source_segment_sequence_no: int = Field(ge=0)


class ContextEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segment_sequence_no: int = Field(ge=0)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    event_type: str = Field(min_length=1, max_length=32)
    description: str = Field(min_length=1, max_length=400)
    text: str | None = Field(default=None, max_length=240)

    @model_validator(mode="after")
    def validate_time_range(self) -> "ContextEvent":
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms must be greater than start_ms")
        return self


class ContextSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: str = Field(min_length=1)
    after_segment_sequence_no: int = Field(ge=-1)
    compact_text: str
    recent_events: list[ContextEvent]


class ObservedTimelineEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relative_event: TimelineEvent
    absolute_start_ms: int = Field(ge=0)
    absolute_end_ms: int = Field(gt=0)
    owned_by_current_segment: bool
    kept_after_deduplication: bool

    @model_validator(mode="after")
    def validate_time_range(self) -> "ObservedTimelineEvent":
        if self.absolute_end_ms <= self.absolute_start_ms:
            raise ValueError("absolute_end_ms must be greater than absolute_start_ms")
        if self.kept_after_deduplication and not self.owned_by_current_segment:
            raise ValueError("an unowned event cannot be kept")
        return self


class SegmentParseResult(BaseModel):
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
    def validate_compact_text(self) -> "SegmentParseResult":
        if self.context_before_compact != self.context_before.compact_text:
            raise ValueError("context_before_compact is inconsistent with snapshot")
        if self.context_after_compact != self.context_after.compact_text:
            raise ValueError("context_after_compact is inconsistent with snapshot")
        return self


class VideoParseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_video: Path
    duration_ms: int = Field(gt=0)
    segment_duration_ms: int = Field(gt=0)
    context_overlap_ms: int = Field(ge=0)
    segments: list[SegmentParseResult]
    events: list[OwnedTimelineEvent]


class TranscriptCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transcript_model: str = Field(min_length=1)
    transcript_source: Literal["lite", "none"] = "lite"
    window: AnalysisWindow
    analysis: TranscriptAnalysis


class SegmentCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transcript_model: str = Field(min_length=1)
    transcript_source: Literal["lite", "none"] = "lite"
    video_model: str = Field(min_length=1)
    segment: SegmentParseResult
