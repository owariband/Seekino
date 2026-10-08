"""Shared Chunk ranking and RRF algorithms, independent of Parse versions."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_serializer

from engine.rag.contracts import VideoChunkRetrievalIdentity

AGENT_RETRIEVAL_STRATEGY_VERSION = "query-adaptive-three-channel-rrf-v2"

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


class VersionedRankedVideoChunkMatch(RankedVideoChunkMatch):
    """One channel result pinned to one formal result and index projection."""

    retrieval_identity: VideoChunkRetrievalIdentity


class FusedVideoChunkRanking(BaseModel):
    """RRF output with the original channel evidence preserved."""

    model_config = ConfigDict(extra="forbid")

    video_chunk_id: str = Field(min_length=1)
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


class VersionedFusedVideoChunkRanking(FusedVideoChunkRanking):
    """RRF output that remains pinned to its formal result and projection."""

    retrieval_identity: VideoChunkRetrievalIdentity
    sparse_rank: int | None = Field(default=None, gt=0)
    sparse_score: float | None = Field(default=None, allow_inf_nan=False)

    @model_serializer(mode="wrap")
    def serialize_ranking(self, handler: Any) -> dict[str, Any]:
        ranking = handler(self)
        for field in ("sparse_rank", "sparse_score"):
            if getattr(self, field) is None:
                ranking.pop(field, None)
        return ranking


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
    lexical_rankings: Sequence[RankedVideoChunkMatch] = (),
    lexical_weight: float = 0.0,
    transcript_rankings: Sequence[RankedVideoChunkMatch] = (),
    transcript_weight: float = 0.0,
    text_weight: float,
    vision_weight: float,
    channel_top_k: int,
    rrf_k: int,
) -> list[FusedVideoChunkRanking]:
    """Fuse formal VideoChunk IDs while retaining every channel's rank and score."""

    _validate_rrf_parameters(
        lexical_weight=lexical_weight,
        transcript_weight=transcript_weight,
        text_weight=text_weight,
        vision_weight=vision_weight,
        channel_top_k=channel_top_k,
        rrf_k=rrf_k,
    )
    if lexical_weight > 0 and transcript_weight > 0:
        raise ValueError(
            "lexical_weight and transcript_weight cannot both be greater than zero"
        )
    lexical_by_id = _rankings_by_video_chunk_id(
        lexical_rankings[:channel_top_k],
        channel_name="lexical",
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
        (lexical_by_id, lexical_weight),
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
        lexical = lexical_by_id.get(video_chunk_id)
        transcript = transcript_by_id.get(video_chunk_id)
        text = text_by_id.get(video_chunk_id)
        vision = vision_by_id.get(video_chunk_id)
        candidates.append(
            FusedVideoChunkRanking(
                video_chunk_id=video_chunk_id,
                fused_rank=fused_rank,
                fused_score=fused_score,
                lexical_rank=lexical.rank if lexical is not None else None,
                lexical_score=lexical.score if lexical is not None else None,
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


def fuse_versioned_video_chunk_rankings(
    text_rankings: Sequence[VersionedRankedVideoChunkMatch],
    vision_rankings: Sequence[VersionedRankedVideoChunkMatch],
    *,
    lexical_rankings: Sequence[VersionedRankedVideoChunkMatch] = (),
    lexical_weight: float = 0.0,
    transcript_rankings: Sequence[VersionedRankedVideoChunkMatch] = (),
    transcript_weight: float = 0.0,
    sparse_rankings: Sequence[VersionedRankedVideoChunkMatch] = (),
    sparse_weight: float = 0.0,
    text_weight: float,
    vision_weight: float,
    channel_top_k: int,
    rrf_k: int,
) -> list[VersionedFusedVideoChunkRanking]:
    """Fuse only candidates whose cross-channel result identity agrees."""

    for name, value in (("channel_top_k", channel_top_k), ("rrf_k", rrf_k)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    _validate_rrf_parameters(
        lexical_weight=lexical_weight,
        transcript_weight=transcript_weight,
        sparse_weight=sparse_weight,
        text_weight=text_weight,
        vision_weight=vision_weight,
        channel_top_k=channel_top_k,
        rrf_k=rrf_k,
    )
    if lexical_weight > 0 and transcript_weight > 0:
        raise ValueError(
            "lexical_weight and transcript_weight cannot both be greater than zero"
        )
    channels = (
        ("lexical", lexical_rankings[:channel_top_k], lexical_weight),
        ("transcript", transcript_rankings[:channel_top_k], transcript_weight),
        ("text", text_rankings[:channel_top_k], text_weight),
        ("vision", vision_rankings[:channel_top_k], vision_weight),
        ("sparse", sparse_rankings[:channel_top_k] if sparse_weight else (), sparse_weight),
    )
    identities = _validated_versioned_identities(
        tuple((name, rankings) for name, rankings, _ in channels)
    )
    scores: dict[str, float] = {}
    channel_evidence: dict[str, dict[str, object]] = {}
    for name, rankings, weight in channels:
        by_id = _rankings_by_video_chunk_id(rankings, channel_name=name)
        for chunk_id, ranked in by_id.items():
            channel_evidence.setdefault(chunk_id, {}).update({
                f"{name}_rank": ranked.rank,
                f"{name}_score": ranked.score,
            })
            if weight:
                scores[chunk_id] = scores.get(chunk_id, 0.0) + weight / (
                    rrf_k + ranked.rank
                )
    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return [
        VersionedFusedVideoChunkRanking(
            video_chunk_id=chunk_id,
            fused_rank=rank,
            fused_score=score,
            retrieval_identity=identities[chunk_id],
            **channel_evidence[chunk_id],
        )
        for rank, (chunk_id, score) in enumerate(ordered, start=1)
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


def _validated_versioned_identities(
    channels: Sequence[
        tuple[str, Sequence[VersionedRankedVideoChunkMatch]]
    ],
) -> dict[str, VideoChunkRetrievalIdentity]:
    by_id: dict[str, VideoChunkRetrievalIdentity] = {}
    trusted_scope: tuple[str, int] | None = None
    for channel_name, rankings in channels:
        for ranked in rankings:
            if not isinstance(ranked, VersionedRankedVideoChunkMatch):
                raise TypeError(
                    f"{channel_name} rankings require "
                    "VersionedRankedVideoChunkMatch"
                )
            identity = ranked.retrieval_identity
            scope = (identity.workspace_id, identity.index_generation)
            if trusted_scope is None:
                trusted_scope = scope
            elif trusted_scope != scope:
                raise ValueError(
                    "versioned rankings span multiple workspace/index generations"
                )
            current = by_id.get(ranked.video_chunk_id)
            if current is not None and current != identity:
                raise ValueError(
                    "retrieval identity changed between channels for "
                    f"video_chunk_id {ranked.video_chunk_id!r} in {channel_name}"
                )
            by_id[ranked.video_chunk_id] = identity
    return by_id


def _validate_rrf_parameters(
    *,
    lexical_weight: float = 0.0,
    transcript_weight: float = 0.0,
    sparse_weight: float = 0.0,
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
        ("lexical_weight", lexical_weight),
        ("transcript_weight", transcript_weight),
        ("sparse_weight", sparse_weight),
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
    if all(weight == 0 for _, weight in weights):
        raise ValueError("at least one channel weight must be greater than zero")


__all__ = [
    "AGENT_RETRIEVAL_STRATEGY_VERSION",
    "RankedVideoChunk",
    "RankedVideoChunkMatch",
    "VersionedRankedVideoChunkMatch",
    "FusedVideoChunkRanking",
    "VersionedFusedVideoChunkRanking",
    "weighted_reciprocal_rank_fusion",
    "fuse_video_chunk_rankings",
    "fuse_versioned_video_chunk_rankings",
    "normalize_hybrid_retrieval_weights",
    "normalize_video_retrieval_weights"
]
