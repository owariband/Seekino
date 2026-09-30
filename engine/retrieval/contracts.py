"""Provider- and Server-neutral contracts for hydrated video retrieval."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from engine.retrieval.index_projection import LexicalField

_MAX_UNIX_TIME_MS = 9_223_372_036_854_775_807


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class _RetrievalContractModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        from_attributes=True,
        extra="forbid",
    )


class TranscriptCueView(_RetrievalContractModel):
    speaker: str | None
    text: str = Field(min_length=1)
    start_seconds: int = Field(ge=0)
    end_seconds: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_time_range(self) -> "TranscriptCueView":
        if self.end_seconds < self.start_seconds:
            raise ValueError("end_seconds must not be less than start_seconds")
        return self


class VideoChunkKeyframeReference(_RetrievalContractModel):
    """A managed keyframe plus its absolute position in the original Video."""

    uri: str = Field(min_length=1)
    at_seconds: int | None = Field(default=None, ge=0)

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_uri(cls, value: Any) -> Any:
        # Historical rows stored URI strings without timeline positions. They
        # remain readable as media, but cannot be projected as timed evidence.
        return {"uri": value, "at_seconds": None} if isinstance(value, str) else value


class VideoChunkEvidence(_RetrievalContractModel):
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
    duration_seconds: int
    upload_status: str
    parse_status: str

    video_chunk_id: str
    sequence_no: int
    start_seconds: int
    end_seconds: int
    analysis_window_start_seconds: int
    analysis_window_end_seconds: int
    chunk_media_ref: str
    analysis_window_media_ref: str
    transcript: list[TranscriptCueView]
    baseline_visual_caption: str | None
    keyframe_uris: list[VideoChunkKeyframeReference]

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
    parsed_at: int = Field(ge=0, le=_MAX_UNIX_TIME_MS, strict=True)


class VideoChunkRetrievalIdentity(_RetrievalContractModel):
    """Immutable MySQL/index identity required by versioned retrieval."""

    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        from_attributes=True,
        extra="forbid",
        frozen=True,
    )

    workspace_id: str = Field(min_length=1)
    series_id: str = Field(min_length=1)
    video_id: str = Field(min_length=1)
    parse_task_id: str = Field(min_length=1)
    content_version: int = Field(gt=0, strict=True)
    source_revision: str = Field(min_length=1)
    projection_revision: str = Field(min_length=1)
    retrieval_contract_id: str = Field(min_length=1)


class VideoChunkCandidate(_RetrievalContractModel):
    video_chunk_id: str
    fused_rank: int = Field(gt=0)
    fused_score: float = Field(gt=0, allow_inf_nan=False)
    lexical_rank: int | None = Field(default=None, gt=0)
    lexical_score: float | None = Field(default=None, allow_inf_nan=False)
    transcript_rank: int | None = Field(default=None, gt=0)
    transcript_score: float | None = Field(default=None, allow_inf_nan=False)
    text_rank: int | None = Field(default=None, gt=0)
    text_score: float | None = Field(default=None, allow_inf_nan=False)
    vision_rank: int | None = Field(default=None, gt=0)
    vision_score: float | None = Field(default=None, allow_inf_nan=False)


class MatchedEvidence(_RetrievalContractModel):
    kind: Literal["TRANSCRIPT", "VISUAL_EVENT", "CONTEXT"]
    text: str = Field(min_length=1)
    start_seconds: int | None
    end_seconds: int | None

    @model_validator(mode="after")
    def validate_time_range(self) -> "MatchedEvidence":
        if (self.start_seconds is None) != (self.end_seconds is None):
            raise ValueError("start_seconds and end_seconds must both be null or both be set")
        if (
            self.start_seconds is not None
            and self.end_seconds is not None
            and self.end_seconds < self.start_seconds
        ):
            raise ValueError("end_seconds must not be less than start_seconds")
        return self


class LexicalMatch(LexicalField):
    """Matched index text and its source; only matched_evidence asserts facts."""

    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        from_attributes=True,
        extra="forbid",
        revalidate_instances="always",
    )


class SelectionReason(_RetrievalContractModel):
    summary: str = Field(min_length=1)
    matched_evidence: list[MatchedEvidence]
    limitations: list[str]
    lexical_matches: list[LexicalMatch] = Field(default_factory=list)


class VideoChunkSearchHit(_RetrievalContractModel):
    candidate: VideoChunkCandidate
    selection_reason: SelectionReason
    evidence: VideoChunkEvidence

    @model_validator(mode="after")
    def validate_lexical_source(self) -> "VideoChunkSearchHit":
        if self.selection_reason.lexical_matches and self.candidate.lexical_rank is None:
            raise ValueError("lexical_matches require lexical_rank")
        return self


class AgentReply(_RetrievalContractModel):
    message: str = Field(min_length=1)
    result_count: int = Field(ge=0)


class VideoChunkSearchEvidence(_RetrievalContractModel):
    series_ids: list[str] = Field(min_length=1)
    agent_reply: AgentReply
    hits: list[VideoChunkSearchHit]
    searched_video_ids: list[str] | None = None
    partial: bool = False
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_result_contract(self) -> "VideoChunkSearchEvidence":
        if self.agent_reply.result_count != len(self.hits):
            raise ValueError("agentReply.resultCount must equal hits length")
        for hit in self.hits:
            if hit.candidate.video_chunk_id != hit.evidence.video_chunk_id:
                raise ValueError(
                    "candidate.videoChunkId must equal evidence.videoChunkId"
                )
        return self


class VideoChunkSearchResult(VideoChunkSearchEvidence):
    query: str = Field(min_length=1)


__all__ = [
    "AgentReply",
    "LexicalMatch",
    "MatchedEvidence",
    "SelectionReason",
    "TranscriptCueView",
    "VideoChunkCandidate",
    "VideoChunkEvidence",
    "VideoChunkKeyframeReference",
    "VideoChunkRetrievalIdentity",
    "VideoChunkSearchHit",
    "VideoChunkSearchEvidence",
    "VideoChunkSearchResult",
]
