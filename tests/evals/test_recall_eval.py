from __future__ import annotations

import json
from pathlib import Path

import pytest
from gateway.schemas import EmbeddingResult
from qdrant_client import QdrantClient

from evals.recall.qdrant_index import (
    QdrantDatasetConflictError,
    QdrantVideoSegmentIndex,
)
from evals.recall.recall_eval import (
    RecallEvalFixture,
    RecallEvaluator,
    RecallQueryFixture,
    RelevantTimeRange,
    build_segment_chunks,
    materialize_eval_only_canonical_media,
    reciprocal_rank_fusion,
)
from engine.parse.schemas import (
    AnalysisWindow,
    ContextSnapshot,
    OwnedTimelineEvent,
    SegmentParseResult,
    VideoParseResult,
)


class FakeEmbedder:
    def __init__(self, *, model_name: str = "fake-shared-embedding", fps=1.0):
        self.model_name = model_name
        self.output_dimensionality = 2
        self.fps = fps
        self.text_calls: list[str] = []
        self.video_calls: list[Path] = []

    def embed_text(self, text: str) -> EmbeddingResult:
        self.text_calls.append(text)
        normalized = text.lower()
        if "beta" in normalized:
            vector = [0.0, 1.0]
        elif "gamma" in normalized:
            vector = [-1.0, 0.0]
        else:
            vector = [1.0, 0.0]
        return EmbeddingResult(
            model=self.model_name,
            modality="text",
            vector=vector,
        )

    def embed_video(self, video_path: str | Path) -> EmbeddingResult:
        path = Path(video_path)
        self.video_calls.append(path)
        if "0001" in path.name:
            vector = [0.0, 1.0]
        elif "0002" in path.name:
            vector = [-1.0, 0.0]
        else:
            vector = [1.0, 0.0]
        return EmbeddingResult(
            model=self.model_name,
            modality="video",
            vector=vector,
        )


def _parse_result(source_video: Path) -> VideoParseResult:
    segments = [
        _segment(
            source_video,
            sequence_no=0,
            start_ms=0,
            end_ms=60_000,
            analysis_start_ms=0,
            analysis_end_ms=63_000,
            compact="alpha compact",
        ),
        _segment(
            source_video,
            sequence_no=1,
            start_ms=60_000,
            end_ms=120_000,
            analysis_start_ms=57_000,
            analysis_end_ms=123_000,
            compact="beta compact",
        ),
        _segment(
            source_video,
            sequence_no=2,
            start_ms=120_000,
            end_ms=180_000,
            analysis_start_ms=117_000,
            analysis_end_ms=180_000,
            compact="gamma compact",
        ),
    ]
    return VideoParseResult(
        source_video=source_video,
        duration_ms=180_000,
        segment_duration_ms=60_000,
        context_overlap_ms=3_000,
        segments=segments,
        events=[event for segment in segments for event in segment.events],
    )


def _segment(
    source_video: Path,
    *,
    sequence_no: int,
    start_ms: int,
    end_ms: int,
    analysis_start_ms: int,
    analysis_end_ms: int,
    compact: str,
) -> SegmentParseResult:
    event = OwnedTimelineEvent(
        start_ms=start_ms + 1000,
        end_ms=start_ms + 2000,
        event_type="DIALOGUE",
        description=f"{compact} owned event",
        text=f"{compact} owned dialogue",
        source_segment_sequence_no=sequence_no,
    )
    before = ContextSnapshot(
        snapshot_id=f"before-{sequence_no}",
        after_segment_sequence_no=sequence_no - 1,
        compact_text=f"LEAK_CONTEXT_BEFORE_{sequence_no}",
        recent_events=[],
    )
    after = ContextSnapshot(
        snapshot_id=f"after-{sequence_no}",
        after_segment_sequence_no=sequence_no,
        compact_text=f"LEAK_CONTEXT_AFTER_{sequence_no}",
        recent_events=[],
    )
    return SegmentParseResult(
        window=AnalysisWindow(
            sequence_no=sequence_no,
            source_video=source_video,
            media_path=source_video.parent / f"analysis-{sequence_no}.mp4",
            segment_start_ms=start_ms,
            segment_end_ms=end_ms,
            analysis_start_ms=analysis_start_ms,
            analysis_end_ms=analysis_end_ms,
        ),
        context_before=before,
        candidate_transcript=[],
        window_summary=f"LEAK_WINDOW_SUMMARY_{sequence_no}",
        observed_events=[],
        events=[event],
        self_compact=compact,
        context_after=after,
        context_before_compact=before.compact_text,
        context_after_compact=after.compact_text,
    )


def _media(tmp_path: Path) -> dict[int, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    result: dict[int, Path] = {}
    for sequence_no in range(3):
        path = tmp_path / f"segment-{sequence_no:04d}.mp4"
        path.write_bytes(f"video-{sequence_no}".encode())
        result[sequence_no] = path
    return result


def test_text_chunks_hard_exclude_window_and_context_leakage(tmp_path) -> None:
    parse_result = _parse_result(tmp_path / "episode.mp4")

    chunks = build_segment_chunks(parse_result)

    assert chunks[0].text == (
        "alpha compact\nalpha compact owned event\nalpha compact owned dialogue"
    )
    rendered = "\n".join(chunk.text for chunk in chunks)
    assert "LEAK_WINDOW_SUMMARY" not in rendered
    assert "LEAK_CONTEXT_BEFORE" not in rendered
    assert "LEAK_CONTEXT_AFTER" not in rendered


def test_evaluator_caches_both_channels_and_scores_time_range_fixture(
    tmp_path,
) -> None:
    source_video = tmp_path / "episode.mp4"
    source_video.write_bytes(b"source-video")
    parse_result = _parse_result(source_video)
    fixture = RecallEvalFixture(
        queries=[
            RecallQueryFixture(
                query_id="q-beta",
                query="find beta",
                relevant_time_ranges_ms=[
                    RelevantTimeRange(start_ms=60_000, end_ms=120_000)
                ],
            )
        ]
    )
    media_by_sequence = _media(tmp_path / "media")
    embedder = FakeEmbedder()
    evaluator = RecallEvaluator(
        embedder,
        cache_dir=tmp_path / "cache",
        channel_top_k=3,
        rrf_k=60,
    )

    result = evaluator.evaluate(
        parse_result,
        fixture,
        source_video=source_video,
        media_by_sequence=media_by_sequence,
    )

    assert len(embedder.text_calls) == 4  # three chunks plus one query
    assert len(embedder.video_calls) == 3
    assert result.queries[0].relevant_segment_sequence_nos == [1]
    assert result.text_metrics.hit_at == {1: 1.0, 3: 1.0, 5: 1.0, 10: 1.0}
    assert result.vision_metrics.hit_at[1] == 1.0
    assert result.fused_metrics.mrr == 1.0

    cache_payload = json.loads(
        (tmp_path / "cache/segments/segment-0001.video.json").read_text()
    )
    assert cache_payload["model"] == "fake-shared-embedding"
    assert cache_payload["dimensions"] == 2
    assert cache_payload["fps"] == 1.0
    assert cache_payload["source"]["size_bytes"] == len(b"source-video")
    assert cache_payload["segment"] == {
        "sequence_no": 1,
        "start_ms": 60_000,
        "end_ms": 120_000,
        "max_height": 720,
    }
    assert not list((tmp_path / "cache").rglob("*.tmp"))

    cached_embedder = FakeEmbedder()
    cached_evaluator = RecallEvaluator(
        cached_embedder,
        cache_dir=tmp_path / "cache",
        channel_top_k=3,
        rrf_k=60,
    )
    cached_evaluator.evaluate(
        parse_result,
        fixture,
        source_video=source_video,
        media_by_sequence=media_by_sequence,
    )
    assert cached_embedder.text_calls == []
    assert cached_embedder.video_calls == []


def test_evaluator_can_persist_and_query_both_channels_through_qdrant(
    tmp_path,
) -> None:
    source_video = tmp_path / "episode.mp4"
    source_video.write_bytes(b"source-video")
    parse_result = _parse_result(source_video)
    fixture = RecallEvalFixture(
        queries=[
            RecallQueryFixture(
                query_id="q-beta",
                query="find beta",
                relevant_segment_sequence_nos=[1],
            )
        ]
    )
    client = QdrantClient(":memory:")
    index = QdrantVideoSegmentIndex(
        client,
        collection_name="video_segments_test",
        dataset_id="test:mygo:ep07",
    )
    evaluator = RecallEvaluator(
        FakeEmbedder(),
        cache_dir=tmp_path / "cache",
        channel_top_k=3,
        rrf_k=60,
        text_weight=3.0,
        vision_weight=1.0,
        qdrant_index=index,
        qdrant_exact=True,
    )

    result = evaluator.evaluate(
        parse_result,
        fixture,
        source_video=source_video,
        media_by_sequence=_media(tmp_path / "media"),
    )

    assert result.retrieval_backend == "qdrant"
    assert result.qdrant_exact is True
    assert result.qdrant_collection == "video_segments_test"
    assert result.dataset_id == "test:mygo:ep07"
    assert result.text_weight == 3.0
    assert result.vision_weight == 1.0
    assert index.count_segments() == 3
    assert result.queries[0].text_rankings[0].segment_sequence_no == 1
    assert result.queries[0].vision_rankings[0].segment_sequence_no == 1
    assert result.queries[0].fused_rankings[0].segment_sequence_no == 1


def test_qdrant_dataset_conflict_fails_before_embedding(tmp_path) -> None:
    source_video = tmp_path / "episode.mp4"
    source_video.write_bytes(b"source-video")
    parse_result = _parse_result(source_video)
    client = QdrantClient(":memory:")
    index = QdrantVideoSegmentIndex(
        client,
        collection_name="video_segments_test",
        dataset_id="test:mygo:ep07:wrong-snapshot",
    )
    index.ensure_collection(text_dimensions=2, vision_dimensions=2)
    first_chunk = build_segment_chunks(parse_result)[0]
    index.upsert_segments(
        [first_chunk],
        text_vectors={0: [1.0, 0.0]},
        vision_vectors={0: [1.0, 0.0]},
    )
    embedder = FakeEmbedder()
    evaluator = RecallEvaluator(
        embedder,
        cache_dir=tmp_path / "cache",
        qdrant_index=index,
    )
    fixture = RecallEvalFixture(
        queries=[
            RecallQueryFixture(
                query_id="q-beta",
                query="find beta",
                relevant_segment_sequence_nos=[1],
            )
        ]
    )

    with pytest.raises(QdrantDatasetConflictError, match="Use a new dataset ID"):
        evaluator.evaluate(
            parse_result,
            fixture,
            source_video=source_video,
            media_by_sequence=_media(tmp_path / "media"),
        )

    assert embedder.text_calls == []
    assert embedder.video_calls == []


def test_changed_embedding_metadata_invalidates_cache(tmp_path) -> None:
    source_video = tmp_path / "episode.mp4"
    source_video.write_bytes(b"source-video")
    parse_result = _parse_result(source_video)
    fixture = RecallEvalFixture(
        queries=[
            RecallQueryFixture(
                query_id="q-beta",
                query="find beta",
                relevant_segment_sequence_nos=[1],
            )
        ]
    )
    media_by_sequence = _media(tmp_path / "media")
    first = RecallEvaluator(FakeEmbedder(), cache_dir=tmp_path / "cache")
    first.evaluate(
        parse_result,
        fixture,
        source_video=source_video,
        media_by_sequence=media_by_sequence,
    )

    changed = FakeEmbedder(fps=2.0)
    second = RecallEvaluator(changed, cache_dir=tmp_path / "cache")
    second.evaluate(
        parse_result,
        fixture,
        source_video=source_video,
        media_by_sequence=media_by_sequence,
    )

    assert len(changed.text_calls) == 4
    assert len(changed.video_calls) == 3


def test_eval_media_uses_canonical_zero_overlap_windows(tmp_path, monkeypatch) -> None:
    source_video = tmp_path / "episode.mp4"
    source_video.write_bytes(b"source-video")
    parse_result = _parse_result(source_video)
    captured_windows = []

    def fake_materialize(windows, *, max_height):
        captured_windows.extend(windows)
        for window in windows:
            window.media_path.parent.mkdir(parents=True, exist_ok=True)
            window.media_path.write_bytes(b"eval")

    monkeypatch.setattr(
        "evals.recall.recall_eval.materialize_analysis_windows",
        fake_materialize,
    )

    media = materialize_eval_only_canonical_media(
        parse_result,
        source_video=source_video,
        output_dir=tmp_path / "output",
        max_height=480,
    )

    assert set(media) == {0, 1, 2}
    assert all("eval-only-canonical-media" in str(path) for path in media.values())
    assert all(
        window.analysis_start_ms == window.segment_start_ms
        and window.analysis_end_ms == window.segment_end_ms
        for window in captured_windows
    )


def test_rrf_uses_only_configured_channel_top_k() -> None:
    from evals.recall.recall_eval import RankedSegment

    text = [
        RankedSegment(rank=1, segment_sequence_no=0, score=0.9),
        RankedSegment(rank=2, segment_sequence_no=1, score=0.8),
        RankedSegment(rank=3, segment_sequence_no=2, score=0.7),
    ]
    vision = [
        RankedSegment(rank=1, segment_sequence_no=1, score=0.95),
        RankedSegment(rank=2, segment_sequence_no=0, score=0.85),
        RankedSegment(rank=3, segment_sequence_no=2, score=0.75),
    ]

    fused = reciprocal_rank_fusion(text, vision, channel_top_k=2, rrf_k=60)

    assert [ranked.segment_sequence_no for ranked in fused] == [0, 1]
    assert 2 not in {ranked.segment_sequence_no for ranked in fused}


def test_parse_result_must_cover_complete_episode(tmp_path) -> None:
    parse_result = _parse_result(tmp_path / "episode.mp4")
    parse_result.segments.pop()

    with pytest.raises(ValueError, match="complete video duration"):
        build_segment_chunks(parse_result)
