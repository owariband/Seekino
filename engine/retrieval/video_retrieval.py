"""Pure retrieval algorithms for parsed video chunks."""

from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict, Field

from ..parse.schemas import VideoParseResult


class VideoChunkRetrievalDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence_no: int = Field(ge=0)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    video_compact: str = Field(min_length=1)
    text: str = Field(min_length=1)
    is_fallback: bool


class RankedVideoChunk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rank: int = Field(gt=0)
    chunk_sequence_no: int = Field(ge=0)
    score: float


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
                start_ms=chunk.window.chunk_start_ms,
                end_ms=chunk.window.chunk_end_ms,
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

    if channel_top_k <= 0:
        raise ValueError("channel_top_k must be greater than zero")
    if rrf_k <= 0:
        raise ValueError("rrf_k must be greater than zero")
    weights = (("text_weight", text_weight), ("vision_weight", vision_weight))
    for name, weight in weights:
        if not math.isfinite(weight) or weight < 0:
            raise ValueError(f"{name} must be a finite nonnegative number")
    if text_weight == 0 and vision_weight == 0:
        raise ValueError("at least one channel weight must be greater than zero")

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


def _validate_canonical_chunks(parse_result: VideoParseResult) -> None:
    if not parse_result.chunks:
        raise ValueError("parse result contains no chunks")
    expected_start_ms = 0
    for expected_sequence_no, chunk in enumerate(parse_result.chunks):
        window = chunk.window
        if window.sequence_no != expected_sequence_no:
            raise ValueError(
                "parse result chunks must use contiguous sequence numbers"
            )
        if window.chunk_start_ms != expected_start_ms:
            raise ValueError("parse result canonical chunks must be contiguous")
        expected_start_ms = window.chunk_end_ms
    if expected_start_ms != parse_result.duration_ms:
        raise ValueError("parse result does not cover the complete video duration")
