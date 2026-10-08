from pathlib import Path

import pytest

from engine.parsev1.schemas import (
    AnalysisWindow,
    ContextSnapshot,
    OwnedTimelineEvent,
    VideoChunkParseResult,
    VideoParseResult,
)
from engine.rag.contracts import (
    LexicalMatch,
    SelectionReason,
    VideoChunkCandidate,
    VideoChunkEvidence,
    VideoChunkRetrievalIdentity,
    VideoChunkSearchHit,
)
from engine.rag.indexing.projection import LexicalField
from engine.rag.retrieval.fusion import (
    RankedVideoChunk,
    RankedVideoChunkMatch,
    VersionedRankedVideoChunkMatch,
    fuse_video_chunk_rankings,
    fuse_versioned_video_chunk_rankings,
    normalize_hybrid_retrieval_weights,
    normalize_video_retrieval_weights,
    weighted_reciprocal_rank_fusion,
)
from engine.rag.nlp.transcript import (
    TranscriptChunkDocument,
    TranscriptCueDocument,
    rank_transcript_chunks,
)
from engine.parsev1.retrieval import build_video_chunk_retrieval_documents


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
        start_seconds=1000,
        end_seconds=2000,
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
            chunk_start_seconds=0,
            chunk_end_seconds=60000,
            analysis_start_seconds=0,
            analysis_end_seconds=60000,
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
        duration_seconds=60000,
        chunk_duration_seconds=60,
        context_overlap_seconds=0,
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


def _retrieval_identity(**updates: object) -> VideoChunkRetrievalIdentity:
    values: dict[str, object] = {
        "workspace_id": "workspace-1",
        "series_id": "series-1",
        "video_id": "video-1",
        "parse_task_id": "task-1",
        "content_version": 3,
        "index_generation": 1,
    }
    values.update(updates)
    return VideoChunkRetrievalIdentity.model_validate(values)


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


def test_retrieval_weights_are_normalized_without_changing_their_ratio() -> None:
    assert normalize_video_retrieval_weights(7, 3) == pytest.approx((0.7, 0.3))
    assert normalize_video_retrieval_weights(1e308, 1e308) == pytest.approx(
        (0.5, 0.5)
    )


@pytest.mark.parametrize(
    "text_weight, vision_weight",
    [
        (True, 1.0),
        (1.0, False),
        (-1.0, 1.0),
        (float("nan"), 1.0),
        (1.0, float("inf")),
        (0.0, 0.0),
    ],
)
def test_retrieval_weight_normalization_rejects_invalid_values(
    text_weight: float,
    vision_weight: float,
) -> None:
    with pytest.raises(ValueError):
        normalize_video_retrieval_weights(text_weight, vision_weight)


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
        "lexical_rank": None,
        "lexical_score": None,
        "transcript_rank": None,
        "transcript_score": None,
        "text_rank": 1,
        "text_score": 0.9,
        "vision_rank": 2,
        "vision_score": 0.7,
    }


def test_versioned_rrf_preserves_identity_across_three_channels() -> None:
    identity = _retrieval_identity()

    fused = fuse_versioned_video_chunk_rankings(
        [VersionedRankedVideoChunkMatch(
            rank=1, video_chunk_id="chunk-a", score=0.9,
            retrieval_identity=identity,
        )],
        [VersionedRankedVideoChunkMatch(
            rank=1, video_chunk_id="chunk-a", score=0.8,
            retrieval_identity=identity,
        )],
        lexical_rankings=[VersionedRankedVideoChunkMatch(
            rank=1, video_chunk_id="chunk-a", score=12.0,
            retrieval_identity=identity,
        )],
        lexical_weight=1.0,
        text_weight=1.0,
        vision_weight=1.0,
        channel_top_k=10,
        rrf_k=60,
    )

    assert len(fused) == 1
    assert fused[0].retrieval_identity == identity
    assert fused[0].model_dump(mode="json")["retrieval_identity"] == {
        "workspace_id": "workspace-1",
        "series_id": "series-1",
        "video_id": "video-1",
        "parse_task_id": "task-1",
        "content_version": 3,
        "index_generation": 1,
    }


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("workspace_id", "workspace-2"),
        ("series_id", "series-2"),
        ("video_id", "video-2"),
        ("parse_task_id", "task-2"),
        ("content_version", 4),
        ("index_generation", 2),
    ],
)
def test_versioned_rrf_rejects_cross_channel_identity_changes(
    field: str,
    changed: object,
) -> None:
    first = _retrieval_identity()
    second = _retrieval_identity(**{field: changed})

    with pytest.raises(
        ValueError,
        match=(
            "multiple workspace/index generations"
            if field in {"workspace_id", "index_generation"}
            else "retrieval identity changed between channels"
        ),
    ):
        fuse_versioned_video_chunk_rankings(
            [VersionedRankedVideoChunkMatch(
                rank=1, video_chunk_id="chunk-a", score=0.9,
                retrieval_identity=first,
            )],
            [VersionedRankedVideoChunkMatch(
                rank=1, video_chunk_id="chunk-a", score=0.8,
                retrieval_identity=second,
            )],
            text_weight=1.0,
            vision_weight=1.0,
            channel_top_k=10,
            rrf_k=60,
        )


def test_versioned_rrf_allows_different_tasks_for_different_videos() -> None:
    fused = fuse_versioned_video_chunk_rankings(
        [VersionedRankedVideoChunkMatch(
            rank=1, video_chunk_id="chunk-a", score=0.9,
            retrieval_identity=_retrieval_identity(),
        )],
        [VersionedRankedVideoChunkMatch(
            rank=1, video_chunk_id="chunk-b", score=0.8,
            retrieval_identity=_retrieval_identity(
                series_id="series-2",
                video_id="video-2",
                parse_task_id="task-2",
                content_version=7,
            ),
        )],
        text_weight=1.0,
        vision_weight=1.0,
        channel_top_k=10,
        rrf_k=60,
    )

    assert [item.video_chunk_id for item in fused] == ["chunk-a", "chunk-b"]
    assert [
        item.retrieval_identity.parse_task_id for item in fused
    ] == ["task-1", "task-2"]


def test_versioned_rrf_rejects_unversioned_channel_input() -> None:
    with pytest.raises(
        TypeError,
        match="text rankings require VersionedRankedVideoChunkMatch",
    ):
        fuse_versioned_video_chunk_rankings(  # type: ignore[arg-type]
            [RankedVideoChunkMatch(
                rank=1, video_chunk_id="chunk-a", score=0.9,
            )],
            [],
            text_weight=1.0,
            vision_weight=0.0,
            channel_top_k=10,
            rrf_k=60,
        )


def test_versioned_rrf_fuses_four_channels_and_preserves_sparse_evidence() -> None:
    identity = _retrieval_identity()
    first = VersionedRankedVideoChunkMatch(
        rank=1, video_chunk_id="chunk-a", score=0.9,
        retrieval_identity=identity,
    )
    second = first.model_copy(update={"video_chunk_id": "chunk-b", "rank": 2})
    reversed_rankings = [
        second.model_copy(update={"rank": 1}),
        first.model_copy(update={"rank": 2}),
    ]
    fused = fuse_versioned_video_chunk_rankings(
        [first, second], reversed_rankings,
        lexical_rankings=[first, second], lexical_weight=0.25,
        sparse_rankings=reversed_rankings, sparse_weight=0.25,
        text_weight=0.25, vision_weight=0.25, channel_top_k=2, rrf_k=60,
    )

    assert [item.video_chunk_id for item in fused] == ["chunk-a", "chunk-b"]
    assert fused[0].fused_score == pytest.approx(0.5 / 61 + 0.5 / 62)
    assert (fused[0].sparse_rank, fused[0].sparse_score) == (2, 0.9)
    assert fused[0].model_dump()["sparse_rank"] == 2
    assert fused[0].retrieval_identity == identity


def test_versioned_rrf_default_wire_and_disabled_sparse_are_compatible() -> None:
    from pydantic import TypeAdapter

    first = VersionedRankedVideoChunkMatch(
        rank=1, video_chunk_id="chunk-a", score=0.9,
        retrieval_identity=_retrieval_identity(),
    )
    kwargs = dict(text_weight=1.0, vision_weight=1.0, channel_top_k=2, rrf_k=60)
    expected = fuse_video_chunk_rankings([first], [first], **kwargs)[0].model_dump()
    fused = fuse_versioned_video_chunk_rankings(
        [first], [first],
        # Disabled Sparse neither accepts a vote nor consumes an identity.
        sparse_rankings=[RankedVideoChunkMatch(
            rank=1, video_chunk_id="other-chunk", score=9.0,
        )],  # type: ignore[list-item]
        **kwargs,
    )
    expected["retrieval_identity"] = first.retrieval_identity.model_dump()
    assert fused[0].model_dump() == expected
    assert TypeAdapter(list[type(fused[0])]).dump_python(fused) == [expected]
    assert "sparse_rank" not in fused[0].model_dump_json()


def test_versioned_rrf_accepts_sparse_as_only_enabled_channel() -> None:
    match = VersionedRankedVideoChunkMatch(
        rank=1, video_chunk_id="chunk-a", score=1.5,
        retrieval_identity=_retrieval_identity(),
    )
    fused = fuse_versioned_video_chunk_rankings(
        [], [], sparse_rankings=[match], sparse_weight=1.0,
        text_weight=0.0, vision_weight=0.0, channel_top_k=1, rrf_k=60,
    )
    assert fused[0].fused_score == 1 / 61
    assert (fused[0].sparse_rank, fused[0].sparse_score) == (1, 1.5)
    assert fused[0].text_rank is None


@pytest.mark.parametrize("channel", ["text", "sparse"])
def test_enabled_sparse_rrf_uses_the_chunk_version_identity(channel: str) -> None:
    match = VersionedRankedVideoChunkMatch(
        rank=1, video_chunk_id="chunk-a", score=1.5,
        retrieval_identity=_retrieval_identity(),
    )
    fused = fuse_versioned_video_chunk_rankings(
        [match] if channel == "text" else [], [],
        sparse_rankings=[match] if channel == "sparse" else [],
        sparse_weight=1.0, text_weight=1.0, vision_weight=0.0,
        channel_top_k=2, rrf_k=60,
    )
    assert fused[0].retrieval_identity == match.retrieval_identity
    assert fused[0].fused_score == pytest.approx(1 / 61)


def test_sparse_rrf_rejects_duplicate_votes_and_cross_channel_identity() -> None:
    match = VersionedRankedVideoChunkMatch(
        rank=1, video_chunk_id="chunk-a", score=1.5,
        retrieval_identity=_retrieval_identity(),
    )
    kwargs = dict(sparse_weight=1.0, text_weight=1.0, vision_weight=0.0,
                  channel_top_k=2, rrf_k=60)
    with pytest.raises(ValueError, match="sparse rankings contain duplicate"):
        fuse_versioned_video_chunk_rankings(
            [], [], sparse_rankings=[match, match], **kwargs,
        )
    changed = match.model_copy(update={
        "retrieval_identity": _retrieval_identity(content_version=4),
    })
    with pytest.raises(ValueError, match="identity changed between channels"):
        fuse_versioned_video_chunk_rankings(
            [match], [], sparse_rankings=[changed], **kwargs,
        )


@pytest.mark.parametrize("parameter,value", [
    ("sparse_weight", True), ("sparse_weight", -1),
    ("sparse_weight", float("inf")), ("sparse_weight", float("nan")),
    ("channel_top_k", True), ("channel_top_k", 1.5),
    ("channel_top_k", 0), ("rrf_k", True), ("rrf_k", 1.5), ("rrf_k", 0),
])
def test_versioned_rrf_rejects_invalid_sparse_and_budget_parameters(
    parameter: str, value: object,
) -> None:
    kwargs = dict(text_weight=1.0, vision_weight=0.0, channel_top_k=2, rrf_k=60)
    kwargs[parameter] = value
    with pytest.raises(ValueError, match=parameter):
        fuse_versioned_video_chunk_rankings([], [], **kwargs)


def test_candidate_keeps_lexical_and_legacy_transcript_ranks_separate() -> None:
    candidate = VideoChunkCandidate.model_validate({
        "videoChunkId": "chunk-a",
        "fusedRank": 1,
        "fusedScore": 0.032,
        "lexicalRank": 2,
        "lexicalScore": 1.5,
        "transcriptRank": 1,
        "transcriptScore": 0.9,
    })

    assert candidate.lexical_rank == 2
    assert candidate.transcript_rank == 1
    assert candidate.model_dump(by_alias=True)["lexicalRank"] == 2
    legacy = VideoChunkCandidate.model_validate({
        "videoChunkId": "chunk-a", "fusedRank": 1, "fusedScore": 0.032,
        "transcriptRank": 1, "transcriptScore": 0.9,
    })
    assert legacy.lexical_rank is None
    assert legacy.transcript_rank == 1


def test_lexical_match_preserves_dialogue_and_question_sources_on_wire() -> None:
    dialogue = LexicalMatch.model_validate({
        "field": "dialogue", "rawText": "门口等你。", "language": "zh",
        "role": "ORIGINAL", "speaker": "小明", "startSeconds": 12,
        "endSeconds": 14, "timelineIndex": 3,
    })
    question = LexicalMatch.model_validate({
        "field": "questions", "rawText": "谁在门口等待？", "language": "zh",
        "startSeconds": 10, "endSeconds": 20, "enrichmentIndex": 0,
    })
    reason = SelectionReason(
        summary="检索线索", matched_evidence=[], limitations=[],
        lexical_matches=[dialogue, question],
    )

    wire = reason.model_dump(mode="json", by_alias=True)
    assert wire["lexicalMatches"][0]["timelineIndex"] == 3
    assert wire["lexicalMatches"][1]["enrichmentIndex"] == 0
    assert wire["lexicalMatches"][1]["field"] == "questions"
    assert wire["matchedEvidence"] == []
    assert SelectionReason.model_validate({
        "summary": "旧 ASR 结果", "matchedEvidence": [], "limitations": [],
    }).lexical_matches == []
    assert LexicalMatch.model_validate(LexicalField(
        field="questions", raw_text="谁在门口等待？", language="zh",
        start_seconds=10000, end_seconds=20000, enrichment_index=0,
    )).model_dump(by_alias=True)["rawText"] == "谁在门口等待？"


def test_lexical_match_rejects_fabricated_provenance() -> None:
    with pytest.raises(ValueError, match="event fields require only timeline_index"):
        LexicalMatch.model_validate({
            "field": "dialogue", "rawText": "门口等你。", "language": "zh",
            "role": "ORIGINAL", "startSeconds": 12, "endSeconds": 14,
            "enrichmentIndex": 0,
        })
    with pytest.raises(ValueError, match="enrichment fields require only enrichment_index"):
        LexicalMatch.model_validate({
            "field": "questions", "rawText": "谁在门口等待？", "language": "zh",
            "startSeconds": 10, "endSeconds": 20, "timelineIndex": 0,
        })


def test_lexical_match_requires_lexical_rank_on_hit() -> None:
    with pytest.raises(ValueError, match="lexical_matches require lexical_rank"):
        VideoChunkSearchHit(
            candidate=VideoChunkCandidate(
                video_chunk_id="chunk-a", fused_rank=1, fused_score=0.1,
            ),
            selection_reason=SelectionReason(
                summary="只有问题命中", matched_evidence=[], limitations=[],
                lexical_matches=[LexicalMatch(
                    field="questions", raw_text="谁在门口等待？", language="zh",
                    start_seconds=10000, end_seconds=20000, enrichment_index=0,
                )],
            ),
            evidence=VideoChunkEvidence.model_construct(),
        )


def test_formal_rrf_fuses_lexical_weight_and_preserves_its_evidence() -> None:
    fused = fuse_video_chunk_rankings(
        [
            RankedVideoChunkMatch(rank=1, video_chunk_id="chunk-b", score=0.9),
            RankedVideoChunkMatch(rank=2, video_chunk_id="chunk-a", score=0.8),
        ],
        [],
        lexical_rankings=[
            RankedVideoChunkMatch(rank=1, video_chunk_id="chunk-a", score=12.0),
            RankedVideoChunkMatch(rank=2, video_chunk_id="chunk-b", score=9.0),
        ],
        lexical_weight=2.0,
        text_weight=1.0,
        vision_weight=0.0,
        channel_top_k=2,
        rrf_k=60,
    )

    assert [item.video_chunk_id for item in fused] == ["chunk-a", "chunk-b"]
    assert fused[0].fused_score == (2 / 61) + (1 / 62)
    assert (fused[0].lexical_rank, fused[0].lexical_score) == (1, 12.0)
    assert (fused[0].text_rank, fused[0].text_score) == (2, 0.8)
    assert (fused[0].transcript_rank, fused[0].transcript_score) == (None, None)


def test_formal_rrf_accepts_lexical_as_the_only_enabled_channel() -> None:
    fused = fuse_video_chunk_rankings(
        [],
        [],
        lexical_rankings=[
            RankedVideoChunkMatch(rank=1, video_chunk_id="chunk-a", score=3.0)
        ],
        lexical_weight=1.0,
        text_weight=0.0,
        vision_weight=0.0,
        channel_top_k=1,
        rrf_k=60,
    )

    assert len(fused) == 1
    assert fused[0].fused_score == 1 / 61
    assert fused[0].lexical_rank == 1


def test_formal_rrf_rejects_lexical_and_transcript_double_counting() -> None:
    with pytest.raises(ValueError, match="lexical_weight and transcript_weight"):
        fuse_video_chunk_rankings(
            [],
            [],
            lexical_weight=0.5,
            transcript_weight=0.5,
            text_weight=0.0,
            vision_weight=0.0,
            channel_top_k=1,
            rrf_k=60,
        )


@pytest.mark.parametrize("weight", [True, -1.0, float("inf"), float("nan")])
def test_formal_rrf_rejects_invalid_lexical_weight(weight: float) -> None:
    with pytest.raises(ValueError, match="lexical_weight must be a finite nonnegative"):
        fuse_video_chunk_rankings(
            [],
            [],
            lexical_weight=weight,
            text_weight=1.0,
            vision_weight=0.0,
            channel_top_k=1,
            rrf_k=60,
        )


def test_formal_rrf_rejects_duplicate_lexical_chunk_votes() -> None:
    duplicate = RankedVideoChunkMatch(rank=1, video_chunk_id="chunk-a", score=3.0)
    with pytest.raises(ValueError, match="lexical rankings contain duplicate"):
        fuse_video_chunk_rankings(
            [],
            [],
            lexical_rankings=[duplicate, duplicate.model_copy(update={"rank": 2})],
            lexical_weight=1.0,
            text_weight=0.0,
            vision_weight=0.0,
            channel_top_k=2,
            rrf_k=60,
        )


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


def test_transcript_ranking_matches_normalized_phrase_across_adjacent_cues() -> None:
    documents = [
        TranscriptChunkDocument(
            video_chunk_id="chunk-a",
            cues=[
                TranscriptCueDocument(text="别把责任", start_seconds=1000, end_seconds=2000),
                TranscriptCueDocument(text="推给别人。", start_seconds=2000, end_seconds=3000),
            ],
        ),
        TranscriptChunkDocument(
            video_chunk_id="chunk-b",
            cues=[
                TranscriptCueDocument(
                    text="她说：别把责任推给别人。",
                    start_seconds=4000,
                    end_seconds=5000,
                )
            ],
        ),
    ]

    ranked = rank_transcript_chunks("『别把责任，推给别人！』", documents, limit=10)

    assert [item.video_chunk_id for item in ranked] == ["chunk-a", "chunk-b"]
    assert ranked[0].score == 1.0
    assert ranked[0].matched_text == "别把责任 推给别人。"
    assert (ranked[0].start_seconds, ranked[0].end_seconds) == (1000, 3000)
    assert ranked[1].score < ranked[0].score


def test_transcript_ranking_does_not_turn_semantic_similarity_into_a_match() -> None:
    documents = [
        TranscriptChunkDocument(
            video_chunk_id="chunk-a",
            cues=[
                TranscriptCueDocument(
                    text="别把责任推给别人。",
                    start_seconds=1000,
                    end_seconds=2000,
                )
            ],
        )
    ]

    assert rank_transcript_chunks("不要甩锅", documents, limit=10) == []


def test_three_channel_rrf_preserves_transcript_rank_and_changes_order() -> None:
    fused = fuse_video_chunk_rankings(
        [RankedVideoChunkMatch(rank=1, video_chunk_id="semantic", score=0.9)],
        [RankedVideoChunkMatch(rank=1, video_chunk_id="visual", score=0.9)],
        transcript_rankings=[
            RankedVideoChunkMatch(rank=1, video_chunk_id="transcript", score=1.0)
        ],
        transcript_weight=0.8,
        text_weight=0.1,
        vision_weight=0.1,
        channel_top_k=10,
        rrf_k=60,
    )

    assert [item.video_chunk_id for item in fused] == [
        "transcript",
        "semantic",
        "visual",
    ]
    assert fused[0].transcript_rank == 1
    assert fused[0].transcript_score == 1.0


def test_three_channel_weights_normalize_without_overflow() -> None:
    assert normalize_hybrid_retrieval_weights(6, 3, 1) == pytest.approx(
        (0.6, 0.3, 0.1)
    )
    assert normalize_hybrid_retrieval_weights(1e308, 1e308, 1e308) == pytest.approx(
        (1 / 3, 1 / 3, 1 / 3)
    )
