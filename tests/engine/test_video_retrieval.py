from pathlib import Path

import pytest

from engine.parse.schemas import (
    AnalysisWindow,
    ContextSnapshot,
    OwnedTimelineEvent,
    VideoChunkParseResult,
    VideoParseResult,
)
from engine.retrieval.video_retrieval import (
    RankedVideoChunk,
    RankedVideoChunkMatch,
    build_video_chunk_retrieval_documents,
    fuse_video_chunk_rankings,
    weighted_reciprocal_rank_fusion,
)


def _parse_result() -> VideoParseResult:
    source_video = Path("video.mp4")
    before = ContextSnapshot(
        snapshot_id="before-0",
        after_chunk_sequence_no=-1,
        compact_text="LEAK_CONTEXT_BEFORE",
        recent_events=[],
    )
    after = ContextSnapshot(
        snapshot_id="after-0",
        after_chunk_sequence_no=0,
        compact_text="LEAK_CONTEXT_AFTER",
        recent_events=[],
    )
    event = OwnedTimelineEvent(
        start_ms=1000,
        end_ms=2000,
        event_type="DIALOGUE",
        description="当前片段发生的事件",
        text="当前片段的台词",
        source_chunk_sequence_no=0,
    )
    chunk = VideoChunkParseResult(
        window=AnalysisWindow(
            sequence_no=0,
            source_video=source_video,
            media_path=Path("chunk-0000.mp4"),
            chunk_start_ms=0,
            chunk_end_ms=60_000,
            analysis_start_ms=0,
            analysis_end_ms=60_000,
        ),
        context_before=before,
        candidate_transcript=[],
        window_summary="LEAK_WINDOW_SUMMARY",
        observed_events=[],
        events=[event],
        self_compact="当前片段摘要",
        is_fallback=True,
        context_after=after,
        context_before_compact=before.compact_text,
        context_after_compact=after.compact_text,
    )
    return VideoParseResult(
        source_video=source_video,
        duration_ms=60_000,
        chunk_duration_ms=60_000,
        context_overlap_ms=0,
        chunks=[chunk],
        events=[event],
    )


def _ranked(*sequence_nos: int) -> list[RankedVideoChunk]:
    return [
        RankedVideoChunk(
            rank=rank,
            chunk_sequence_no=sequence_no,
            score=1.0 / rank,
        )
        for rank, sequence_no in enumerate(sequence_nos, start=1)
    ]


def test_retrieval_documents_expose_video_compact_without_context() -> None:
    documents = build_video_chunk_retrieval_documents(_parse_result())

    assert documents[0].video_compact == "当前片段摘要"
    assert documents[0].text == (
        "当前片段摘要\n当前片段发生的事件\n当前片段的台词"
    )
    assert "LEAK_WINDOW_SUMMARY" not in documents[0].text
    assert "LEAK_CONTEXT_BEFORE" not in documents[0].text
    assert "LEAK_CONTEXT_AFTER" not in documents[0].text
    assert "self_compact" not in documents[0].model_dump()
    assert documents[0].is_fallback is True


def test_weighted_rrf_changes_channel_priority() -> None:
    fused = weighted_reciprocal_rank_fusion(
        _ranked(0, 1),
        _ranked(1, 0),
        text_weight=2.0,
        vision_weight=1.0,
        channel_top_k=2,
        rrf_k=60,
    )

    assert [ranked.chunk_sequence_no for ranked in fused] == [0, 1]


def test_weighted_rrf_uses_only_channel_top_k() -> None:
    fused = weighted_reciprocal_rank_fusion(
        _ranked(0, 1, 2),
        _ranked(1, 0, 2),
        text_weight=1.0,
        vision_weight=1.0,
        channel_top_k=2,
        rrf_k=60,
    )

    assert [ranked.chunk_sequence_no for ranked in fused] == [0, 1]


def test_weighted_rrf_breaks_score_ties_by_sequence_number() -> None:
    fused = weighted_reciprocal_rank_fusion(
        _ranked(4),
        _ranked(2),
        text_weight=1.0,
        vision_weight=1.0,
        channel_top_k=1,
        rrf_k=60,
    )

    assert [ranked.chunk_sequence_no for ranked in fused] == [2, 4]
    assert [ranked.rank for ranked in fused] == [1, 2]


def test_zero_weight_excludes_that_channel() -> None:
    fused = weighted_reciprocal_rank_fusion(
        _ranked(0),
        _ranked(1),
        text_weight=1.0,
        vision_weight=0.0,
        channel_top_k=1,
        rrf_k=60,
    )

    assert [ranked.chunk_sequence_no for ranked in fused] == [0]


@pytest.mark.parametrize("weight", [-1.0, float("inf"), float("nan")])
def test_weighted_rrf_rejects_invalid_weights(weight: float) -> None:
    with pytest.raises(ValueError, match="finite nonnegative"):
        weighted_reciprocal_rank_fusion(
            _ranked(0),
            _ranked(1),
            text_weight=weight,
            vision_weight=1.0,
            channel_top_k=1,
            rrf_k=60,
        )


def test_weighted_rrf_requires_one_enabled_channel() -> None:
    with pytest.raises(ValueError, match="at least one channel weight"):
        weighted_reciprocal_rank_fusion(
            _ranked(0),
            _ranked(1),
            text_weight=0.0,
            vision_weight=0.0,
            channel_top_k=1,
            rrf_k=60,
        )


def test_formal_rrf_preserves_each_channel_rank_and_score() -> None:
    fused = fuse_video_chunk_rankings(
        [
            RankedVideoChunkMatch(rank=1, video_chunk_id="chunk-a", score=0.9),
            RankedVideoChunkMatch(rank=2, video_chunk_id="chunk-b", score=0.8),
        ],
        [
            RankedVideoChunkMatch(rank=1, video_chunk_id="chunk-b", score=0.95),
            RankedVideoChunkMatch(rank=2, video_chunk_id="chunk-a", score=0.7),
        ],
        text_weight=2.0,
        vision_weight=1.0,
        channel_top_k=2,
        rrf_k=60,
    )

    assert [item.video_chunk_id for item in fused] == ["chunk-a", "chunk-b"]
    assert fused[0].model_dump() == {
        "video_chunk_id": "chunk-a",
        "fused_rank": 1,
        "fused_score": (2 / 61) + (1 / 62),
        "text_rank": 1,
        "text_score": 0.9,
        "vision_rank": 2,
        "vision_score": 0.7,
    }


def test_formal_rrf_breaks_cross_channel_ties_by_video_chunk_id() -> None:
    fused = fuse_video_chunk_rankings(
        [RankedVideoChunkMatch(rank=1, video_chunk_id="chunk-c", score=0.9)],
        [RankedVideoChunkMatch(rank=1, video_chunk_id="chunk-b", score=0.9)],
        text_weight=1.0,
        vision_weight=1.0,
        channel_top_k=1,
        rrf_k=60,
    )

    assert [item.video_chunk_id for item in fused] == ["chunk-b", "chunk-c"]


def test_formal_rrf_rejects_duplicate_ids_within_one_channel() -> None:
    duplicate = RankedVideoChunkMatch(
        rank=1,
        video_chunk_id="chunk-a",
        score=0.9,
    )

    with pytest.raises(ValueError, match="duplicate video_chunk_id"):
        fuse_video_chunk_rankings(
            [duplicate, duplicate.model_copy(update={"rank": 2})],
            [],
            text_weight=1.0,
            vision_weight=1.0,
            channel_top_k=2,
            rrf_k=60,
        )
