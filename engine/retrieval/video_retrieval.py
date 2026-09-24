"""Pure retrieval algorithms for parsed video chunks."""

from __future__ import annotations

import math
import unicodedata
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..parsev1.schemas import VideoParseResult

AGENT_RETRIEVAL_STRATEGY_VERSION = "query-adaptive-three-channel-rrf-v2"

_TRANSCRIPT_PHRASE_BASE_SCORE = 0.9
_TRANSCRIPT_PHRASE_COVERAGE_BONUS = 0.09


class VideoChunkRetrievalDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence_no: int = Field(ge=0)
    start_seconds: int = Field(ge=0)
    end_seconds: int = Field(gt=0)
    video_compact: str = Field(min_length=1)
    text: str = Field(min_length=1)
    is_fallback: bool


class RankedVideoChunk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rank: int = Field(gt=0)
    chunk_sequence_no: int = Field(ge=0)
    score: float


class RankedVideoChunkMatch(BaseModel):
    """One formal VideoChunk result from a single retrieval channel."""

    model_config = ConfigDict(extra="forbid")

    rank: int = Field(gt=0)
    video_chunk_id: str = Field(min_length=1)
    score: float = Field(allow_inf_nan=False)


class TranscriptCueDocument(BaseModel):
    """One absolute-time ASR cue available to lexical retrieval."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    start_seconds: int = Field(ge=0)
    end_seconds: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_time_range(self) -> "TranscriptCueDocument":
        if self.end_seconds < self.start_seconds:
            raise ValueError("end_seconds must not be less than start_seconds")
        return self


class TranscriptChunkDocument(BaseModel):
    """The ordered ASR cues owned by one searchable VideoChunk."""

    model_config = ConfigDict(extra="forbid")

    video_chunk_id: str = Field(min_length=1)
    cues: list[TranscriptCueDocument] = Field(min_length=1)


class RankedTranscriptMatch(RankedVideoChunkMatch):
    """One lexical ASR match with the exact cue range kept for explanation."""

    matched_text: str = Field(min_length=1)
    start_seconds: int = Field(ge=0)
    end_seconds: int = Field(ge=0)


class FusedVideoChunkRanking(BaseModel):
    """RRF output with the original channel evidence preserved."""

    model_config = ConfigDict(extra="forbid")

    video_chunk_id: str = Field(min_length=1)
    fused_rank: int = Field(gt=0)
    fused_score: float = Field(gt=0, allow_inf_nan=False)
    transcript_rank: int | None = Field(default=None, gt=0)
    transcript_score: float | None = Field(default=None, allow_inf_nan=False)
    text_rank: int | None = Field(default=None, gt=0)
    text_score: float | None = Field(default=None, allow_inf_nan=False)
    vision_rank: int | None = Field(default=None, gt=0)
    vision_score: float | None = Field(default=None, allow_inf_nan=False)


def build_video_chunk_retrieval_documents(
    parse_result: VideoParseResult,
) -> list[VideoChunkRetrievalDocument]:
    """Build self-owned retrieval text for every canonical video chunk."""

    _validate_canonical_chunks(parse_result)
    documents: list[VideoChunkRetrievalDocument] = []
    for chunk in parse_result.chunks:
        video_compact = chunk.self_compact.strip()
        # The compact carries semantic continuity; owned events preserve exact
        # actions and dialogue terms. Their partial overlap is intentional.
        parts = [video_compact]
        for event in chunk.events:
            parts.append(event.description.strip())
            if event.text:
                parts.append(event.text.strip())
        text = "\n".join(part for part in parts if part).strip()
        if not video_compact or not text:
            raise ValueError(
                f"chunk {chunk.window.sequence_no} has no self-owned retrieval text"
            )
        documents.append(
            VideoChunkRetrievalDocument(
                sequence_no=chunk.window.sequence_no,
                start_seconds=chunk.window.chunk_start_seconds,
                end_seconds=chunk.window.chunk_end_seconds,
                video_compact=video_compact,
                text=text,
                is_fallback=chunk.is_fallback,
            )
        )
    return documents


def weighted_reciprocal_rank_fusion(
    text_rankings: list[RankedVideoChunk],
    vision_rankings: list[RankedVideoChunk],
    *,
    text_weight: float,
    vision_weight: float,
    channel_top_k: int,
    rrf_k: int,
) -> list[RankedVideoChunk]:
    """Fuse text and vision rankings with configurable channel weights."""

    _validate_rrf_parameters(
        text_weight=text_weight,
        vision_weight=vision_weight,
        channel_top_k=channel_top_k,
        rrf_k=rrf_k,
    )

    scores: dict[int, float] = {}
    for rankings, weight in (
        (text_rankings, text_weight),
        (vision_rankings, vision_weight),
    ):
        if weight == 0:
            continue
        for ranked in rankings[:channel_top_k]:
            scores[ranked.chunk_sequence_no] = scores.get(
                ranked.chunk_sequence_no, 0.0
            ) + weight / (rrf_k + ranked.rank)

    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return [
        RankedVideoChunk(
            rank=rank,
            chunk_sequence_no=sequence_no,
            score=score,
        )
        for rank, (sequence_no, score) in enumerate(ordered, start=1)
    ]


def fuse_video_chunk_rankings(
    text_rankings: Sequence[RankedVideoChunkMatch],
    vision_rankings: Sequence[RankedVideoChunkMatch],
    *,
    transcript_rankings: Sequence[RankedVideoChunkMatch] = (),
    transcript_weight: float = 0.0,
    text_weight: float,
    vision_weight: float,
    channel_top_k: int,
    rrf_k: int,
) -> list[FusedVideoChunkRanking]:
    """Fuse formal VideoChunk IDs while retaining every channel's rank and score."""

    _validate_rrf_parameters(
        transcript_weight=transcript_weight,
        text_weight=text_weight,
        vision_weight=vision_weight,
        channel_top_k=channel_top_k,
        rrf_k=rrf_k,
    )
    transcript_by_id = _rankings_by_video_chunk_id(
        transcript_rankings[:channel_top_k],
        channel_name="transcript",
    )
    text_by_id = _rankings_by_video_chunk_id(
        text_rankings[:channel_top_k],
        channel_name="text",
    )
    vision_by_id = _rankings_by_video_chunk_id(
        vision_rankings[:channel_top_k],
        channel_name="vision",
    )

    scores: dict[str, float] = {}
    for rankings, weight in (
        (transcript_by_id, transcript_weight),
        (text_by_id, text_weight),
        (vision_by_id, vision_weight),
    ):
        if weight == 0:
            continue
        for video_chunk_id, ranked in rankings.items():
            scores[video_chunk_id] = scores.get(video_chunk_id, 0.0) + (
                weight / (rrf_k + ranked.rank)
            )

    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    candidates: list[FusedVideoChunkRanking] = []
    for fused_rank, (video_chunk_id, fused_score) in enumerate(ordered, start=1):
        transcript = transcript_by_id.get(video_chunk_id)
        text = text_by_id.get(video_chunk_id)
        vision = vision_by_id.get(video_chunk_id)
        candidates.append(
            FusedVideoChunkRanking(
                video_chunk_id=video_chunk_id,
                fused_rank=fused_rank,
                fused_score=fused_score,
                transcript_rank=(
                    transcript.rank if transcript is not None else None
                ),
                transcript_score=(
                    transcript.score if transcript is not None else None
                ),
                text_rank=text.rank if text is not None else None,
                text_score=text.score if text is not None else None,
                vision_rank=vision.rank if vision is not None else None,
                vision_score=vision.score if vision is not None else None,
            )
        )
    return candidates


def rank_transcript_chunks(
    query: str,
    documents: Sequence[TranscriptChunkDocument],
    *,
    limit: int,
) -> list[RankedTranscriptMatch]:
    """Rank literal ASR phrase matches without pretending they are semantic hits."""

    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must not be empty")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    normalized_query = _normalize_transcript_text(query)
    if not normalized_query:
        return []

    matches: list[tuple[float, str, int, int, str]] = []
    seen_ids: set[str] = set()
    for document in documents:
        if document.video_chunk_id in seen_ids:
            raise ValueError(
                "transcript documents contain duplicate video_chunk_id "
                f"{document.video_chunk_id!r}"
            )
        seen_ids.add(document.video_chunk_id)
        best = _best_transcript_match(normalized_query, document.cues)
        if best is None:
            continue
        score, first_cue, last_cue = best
        matched_cues = document.cues[first_cue : last_cue + 1]
        matches.append(
            (
                score,
                document.video_chunk_id,
                matched_cues[0].start_seconds,
                matched_cues[-1].end_seconds,
                " ".join(cue.text.strip() for cue in matched_cues),
            )
        )

    matches.sort(key=lambda item: (-item[0], item[2], item[1]))
    return [
        RankedTranscriptMatch(
            rank=rank,
            video_chunk_id=video_chunk_id,
            score=score,
            matched_text=matched_text,
            start_seconds=start_seconds,
            end_seconds=end_seconds,
        )
        for rank, (score, video_chunk_id, start_seconds, end_seconds, matched_text) in enumerate(
            matches[:limit],
            start=1,
        )
    ]


def normalize_hybrid_retrieval_weights(
    transcript_weight: float,
    text_weight: float,
    vision_weight: float,
) -> tuple[float, float, float]:
    """Validate and normalize one call's Transcript/Semantic/Visual ratio."""

    weights = (
        ("transcript_weight", transcript_weight),
        ("text_weight", text_weight),
        ("vision_weight", vision_weight),
    )
    for name, weight in weights:
        if (
            isinstance(weight, bool)
            or not isinstance(weight, (int, float))
            or not math.isfinite(weight)
            or weight < 0
        ):
            raise ValueError(f"{name} must be a finite nonnegative number")
    scale = max(transcript_weight, text_weight, vision_weight)
    if scale <= 0:
        raise ValueError("at least one channel weight must be greater than zero")
    # Scale before summing so individually finite large values cannot overflow.
    scaled = (
        transcript_weight / scale,
        text_weight / scale,
        vision_weight / scale,
    )
    scaled_total = sum(scaled)
    return (
        scaled[0] / scaled_total,
        scaled[1] / scaled_total,
        scaled[2] / scaled_total,
    )


def normalize_video_retrieval_weights(
    text_weight: float,
    vision_weight: float,
) -> tuple[float, float]:
    """Validate and normalize one call's Text/Vision weight ratio."""

    _, normalized_text, normalized_vision = normalize_hybrid_retrieval_weights(
        0.0,
        text_weight,
        vision_weight,
    )
    return normalized_text, normalized_vision


def _rankings_by_video_chunk_id(
    rankings: Sequence[RankedVideoChunkMatch],
    *,
    channel_name: str,
) -> dict[str, RankedVideoChunkMatch]:
    by_id: dict[str, RankedVideoChunkMatch] = {}
    for ranked in rankings:
        if ranked.video_chunk_id in by_id:
            raise ValueError(
                f"{channel_name} rankings contain duplicate video_chunk_id "
                f"{ranked.video_chunk_id!r}"
            )
        by_id[ranked.video_chunk_id] = ranked
    return by_id


def _validate_rrf_parameters(
    *,
    transcript_weight: float = 0.0,
    text_weight: float,
    vision_weight: float,
    channel_top_k: int,
    rrf_k: int,
) -> None:
    if channel_top_k <= 0:
        raise ValueError("channel_top_k must be greater than zero")
    if rrf_k <= 0:
        raise ValueError("rrf_k must be greater than zero")
    weights = (
        ("transcript_weight", transcript_weight),
        ("text_weight", text_weight),
        ("vision_weight", vision_weight),
    )
    for name, weight in weights:
        if (
            isinstance(weight, bool)
            or not isinstance(weight, (int, float))
            or not math.isfinite(weight)
            or weight < 0
        ):
            raise ValueError(f"{name} must be a finite nonnegative number")
    if transcript_weight == 0 and text_weight == 0 and vision_weight == 0:
        raise ValueError("at least one channel weight must be greater than zero")


def _normalize_transcript_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _best_transcript_match(
    query: str,
    cues: Sequence[TranscriptCueDocument],
) -> tuple[float, int, int] | None:
    normalized_cues = [_normalize_transcript_text(cue.text) for cue in cues]
    joined = "".join(normalized_cues)
    best: tuple[float, int, int] | None = None

    offset = joined.find(query)
    while offset >= 0:
        first_cue = _cue_index_at_offset(normalized_cues, offset)
        last_cue = _cue_index_at_offset(
            normalized_cues,
            offset + len(query) - 1,
        )
        span_length = sum(
            len(value) for value in normalized_cues[first_cue : last_cue + 1]
        )
        score = (
            1.0
            if span_length == len(query)
            else _TRANSCRIPT_PHRASE_BASE_SCORE
            + _TRANSCRIPT_PHRASE_COVERAGE_BONUS * (len(query) / span_length)
        )
        candidate = (score, first_cue, last_cue)
        if best is None or score > best[0] or (
            score == best[0] and (first_cue, last_cue) < (best[1], best[2])
        ):
            best = candidate
        offset = joined.find(query, offset + 1)
    return best


def _cue_index_at_offset(cues: Sequence[str], offset: int) -> int:
    consumed = 0
    for index, cue in enumerate(cues):
        consumed += len(cue)
        if offset < consumed:
            return index
    return len(cues) - 1


def _validate_canonical_chunks(parse_result: VideoParseResult) -> None:
    if not parse_result.chunks:
        raise ValueError("parse result contains no chunks")
    expected_start_seconds = 0
    for expected_sequence_no, chunk in enumerate(parse_result.chunks):
        window = chunk.window
        if window.sequence_no != expected_sequence_no:
            raise ValueError(
                "parse result chunks must use contiguous sequence numbers"
            )
        if window.chunk_start_seconds != expected_start_seconds:
            raise ValueError("parse result canonical chunks must be contiguous")
        expected_start_seconds = window.chunk_end_seconds
    if expected_start_seconds != parse_result.duration_seconds:
        raise ValueError("parse result does not cover the complete video duration")
