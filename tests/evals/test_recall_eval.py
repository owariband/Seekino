from __future__ import annotations

import json
from pathlib import Path

import pytest
from gateway.embedding import EmbeddingResult

from evals.recall.elasticsearch_index import ElasticsearchDatasetConflictError
from evals.recall.recall_eval import (
    RecallEvalFixture,
    RecallEvalResult,
    RecallEvaluator,
    RecallQueryFixture,
    RelevantTimeRange,
    build_video_chunk_retrieval_documents,
    cosine_rankings,
    materialize_eval_only_canonical_media,
    reciprocal_rank_fusion,
    write_recall_result,
)
from engine.parsev1.schemas import (
    AnalysisWindow,
    ContextSnapshot,
    OwnedTimelineEvent,
    VideoChunkParseResult,
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


class FakeElasticsearchIndex:
    index_name = "anivag_recall_eval_test"
    dataset_id = "test:mygo:ep07"

    def __init__(self, *, conflict: bool = False) -> None:
        self.conflict = conflict
        self.text_vectors: dict[int, list[float]] = {}
        self.vision_vectors: dict[int, list[float]] = {}
        self.vision_input_sha256: dict[int, str] | None = None
        self.search_modes: list[str] = []

    def ensure_index(self, *, text_dimensions: int, vision_dimensions: int) -> None:
        assert (text_dimensions, vision_dimensions) == (2, 2)

    def assert_dataset_snapshot(
        self,
        documents,
        *,
        vision_input_sha256,
        embedding_model,
        embedding_fps,
        allow_incomplete,
    ):
        if self.conflict or (
            self.vision_input_sha256 is not None
            and self.vision_input_sha256 != vision_input_sha256
        ):
            raise ElasticsearchDatasetConflictError("Use a new dataset ID")
        assert embedding_model == "fake-shared-embedding"
        assert embedding_fps == 1.0
        assert len(documents) == 3

    def upsert_chunks(
        self,
        documents,
        *,
        text_vectors,
        vision_vectors,
        vision_input_sha256,
        embedding_model,
        embedding_fps,
    ) -> None:
        assert len(documents) == 3
        self.text_vectors = dict(text_vectors)
        self.vision_vectors = dict(vision_vectors)
        self.vision_input_sha256 = dict(vision_input_sha256)

    def search_text(self, query_vector, *, limit, mode):
        self.search_modes.append(mode)
        return cosine_rankings(query_vector, self.text_vectors)[:limit]

    def search_vision(self, query_vector, *, limit, mode):
        self.search_modes.append(mode)
        return cosine_rankings(query_vector, self.vision_vectors)[:limit]


def _parse_result(source_video: Path) -> VideoParseResult:
    chunks = [
        _chunk(
            source_video,
            sequence_no=0,
            start_seconds=0,
            end_seconds=60,
            analysis_start_seconds=0,
            analysis_end_seconds=63,
            compact="alpha compact",
        ),
        _chunk(
            source_video,
            sequence_no=1,
            start_seconds=60,
            end_seconds=120,
            analysis_start_seconds=57,
            analysis_end_seconds=123,
            compact="beta compact",
        ),
        _chunk(
            source_video,
            sequence_no=2,
            start_seconds=120,
            end_seconds=180,
            analysis_start_seconds=117,
            analysis_end_seconds=180,
            compact="gamma compact",
        ),
    ]
    return VideoParseResult(
        source_video=source_video,
        duration_seconds=180,
        chunk_duration_seconds=60,
        context_overlap_seconds=3,
        chunks=chunks,
        events=[event for chunk in chunks for event in chunk.events],
    )


def _chunk(
    source_video: Path,
    *,
    sequence_no: int,
    start_seconds: int,
    end_seconds: int,
    analysis_start_seconds: int,
    analysis_end_seconds: int,
    compact: str,
) -> VideoChunkParseResult:
    event = OwnedTimelineEvent(
        start_seconds=start_seconds + 1,
        end_seconds=start_seconds + 2,
        event_type="DIALOGUE",
        description=f"{compact} owned event",
        text=f"{compact} owned dialogue",
        source_chunk_sequence_no=sequence_no,
    )
    before = ContextSnapshot(
        snapshot_id=f"before-{sequence_no}",
        after_chunk_sequence_no=sequence_no - 1,
        compact_text=f"LEAK_CONTEXT_BEFORE_{sequence_no}",
        recent_events=[],
    )
    after = ContextSnapshot(
        snapshot_id=f"after-{sequence_no}",
        after_chunk_sequence_no=sequence_no,
        compact_text=f"LEAK_CONTEXT_AFTER_{sequence_no}",
        recent_events=[],
    )
    return VideoChunkParseResult(
        window=AnalysisWindow(
            sequence_no=sequence_no,
            source_video=source_video,
            media_path=source_video.parent / f"analysis-{sequence_no}.mp4",
            chunk_start_seconds=start_seconds,
            chunk_end_seconds=end_seconds,
            analysis_start_seconds=analysis_start_seconds,
            analysis_end_seconds=analysis_end_seconds,
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
        path = tmp_path / f"chunk-{sequence_no:04d}.mp4"
        path.write_bytes(f"video-{sequence_no}".encode())
        result[sequence_no] = path
    return result


def test_retrieval_documents_exclude_window_and_context_leakage(tmp_path) -> None:
    parse_result = _parse_result(tmp_path / "episode.mp4")

    documents = build_video_chunk_retrieval_documents(parse_result)

    assert documents[0].text == (
        "alpha compact\nalpha compact owned event\nalpha compact owned dialogue"
    )
    rendered = "\n".join(document.text for document in documents)
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
                relevant_time_ranges_seconds=[
                    RelevantTimeRange(start_seconds=60, end_seconds=120)
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

    assert len(embedder.text_calls) == 4  # three VideoChunks plus one query
    assert len(embedder.video_calls) == 3
    assert result.queries[0].relevant_chunk_sequence_nos == [1]
    assert result.text_metrics.hit_at == {1: 1.0, 3: 1.0, 5: 1.0, 10: 1.0}
    assert result.vision_metrics.hit_at[1] == 1.0
    assert result.fused_metrics.mrr == 1.0

    cache_payload = json.loads(
        (tmp_path / "cache/chunks/chunk-0001.video.json").read_text()
    )
    assert cache_payload["model"] == "fake-shared-embedding"
    assert cache_payload["dimensions"] == 2
    assert cache_payload["fps"] == 1.0
    assert cache_payload["source"]["size_bytes"] == len(b"source-video")
    assert cache_payload["chunk"] == {
        "sequence_no": 1,
        "start_seconds": 60,
        "end_seconds": 120,
        "max_height": 720,
    }
    assert not list((tmp_path / "cache").rglob("*.tmp"))

    legacy_cache_dir = tmp_path / "cache/segments"
    legacy_cache_dir.mkdir(parents=True)
    for cache_path in (tmp_path / "cache/chunks").glob("*.json"):
        legacy_payload = json.loads(cache_path.read_text())
        legacy_payload["segment"] = legacy_payload.pop("chunk")
        legacy_path = legacy_cache_dir / cache_path.name.replace(
            "chunk-", "segment-", 1
        )
        legacy_path.write_text(json.dumps(legacy_payload), encoding="utf-8")
        cache_path.unlink()

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


def test_evaluator_can_query_both_channels_through_elasticsearch(
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
                relevant_chunk_sequence_nos=[1],
            )
        ]
    )
    index = FakeElasticsearchIndex()
    evaluator = RecallEvaluator(
        FakeEmbedder(),
        cache_dir=tmp_path / "cache",
        channel_top_k=3,
        rrf_k=60,
        text_weight=3.0,
        vision_weight=1.0,
        elasticsearch_index=index,
        elasticsearch_search_mode="exact",
    )

    result = evaluator.evaluate(
        parse_result,
        fixture,
        source_video=source_video,
        media_by_sequence=_media(tmp_path / "media"),
    )

    assert result.retrieval_backend == "elasticsearch"
    assert result.elasticsearch_search_mode == "exact"
    assert result.elasticsearch_index == "anivag_recall_eval_test"
    assert result.dataset_id == "test:mygo:ep07"
    assert result.text_weight == 3.0
    assert result.vision_weight == 1.0
    assert index.search_modes == ["exact", "exact"]
    assert result.queries[0].text_rankings[0].chunk_sequence_no == 1
    assert result.queries[0].vision_rankings[0].chunk_sequence_no == 1
    assert result.queries[0].fused_rankings[0].chunk_sequence_no == 1
    assert index.vision_input_sha256 == {
        sequence_no: json.loads(
            (tmp_path / f"cache/chunks/chunk-{sequence_no:04d}.video.json").read_text()
        )["input_sha256"]
        for sequence_no in range(3)
    }
    report_path = tmp_path / "recall-eval.json"
    write_recall_result(result, report_path)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["retrieval_backend"] == "elasticsearch"
    assert "qdrant_exact" not in report and "qdrant_collection" not in report


def test_elasticsearch_dataset_conflict_fails_before_embedding(tmp_path) -> None:
    source_video = tmp_path / "episode.mp4"
    source_video.write_bytes(b"source-video")
    parse_result = _parse_result(source_video)
    index = FakeElasticsearchIndex(conflict=True)
    embedder = FakeEmbedder()
    evaluator = RecallEvaluator(
        embedder,
        cache_dir=tmp_path / "cache",
        elasticsearch_index=index,
    )
    fixture = RecallEvalFixture(
        queries=[
            RecallQueryFixture(
                query_id="q-beta",
                query="find beta",
                relevant_chunk_sequence_nos=[1],
            )
        ]
    )

    with pytest.raises(ElasticsearchDatasetConflictError, match="Use a new dataset ID"):
        evaluator.evaluate(
            parse_result,
            fixture,
            source_video=source_video,
            media_by_sequence=_media(tmp_path / "media"),
        )

    assert embedder.text_calls == []
    assert embedder.video_calls == []


@pytest.mark.parametrize("changed_input", ["source", "media", "max_height"])
def test_elasticsearch_dataset_identity_covers_visual_inputs_before_embedding(
    tmp_path,
    changed_input: str,
) -> None:
    source_video = tmp_path / "episode.mp4"
    source_video.write_bytes(b"source-video")
    parse_result = _parse_result(source_video)
    fixture = RecallEvalFixture(
        queries=[
            RecallQueryFixture(
                query_id="q-beta",
                query="find beta",
                relevant_chunk_sequence_nos=[1],
            )
        ]
    )
    index = FakeElasticsearchIndex()
    first_media = _media(tmp_path / "media-first")
    RecallEvaluator(
        FakeEmbedder(),
        cache_dir=tmp_path / "cache-first",
        elasticsearch_index=index,
    ).evaluate(
        parse_result,
        fixture,
        source_video=source_video,
        media_by_sequence=first_media,
    )

    next_source = source_video
    next_media = first_media
    next_max_height = 720
    if changed_input == "source":
        next_source = tmp_path / "episode-copy.mp4"
        next_source.write_bytes(b"source-video")
    elif changed_input == "media":
        next_media = _media(tmp_path / "media-second")
    else:
        next_max_height = 480

    embedder = FakeEmbedder()
    evaluator = RecallEvaluator(
        embedder,
        cache_dir=tmp_path / "cache-second",
        max_height=next_max_height,
        elasticsearch_index=index,
    )
    with pytest.raises(ElasticsearchDatasetConflictError, match="new dataset ID"):
        evaluator.evaluate(
            parse_result,
            fixture,
            source_video=next_source,
            media_by_sequence=next_media,
        )

    assert embedder.text_calls == []
    assert embedder.video_calls == []


def test_old_qdrant_report_remains_readable(tmp_path) -> None:
    source_video = tmp_path / "episode.mp4"
    source_video.write_bytes(b"source-video")
    result = RecallEvaluator(FakeEmbedder(), cache_dir=tmp_path / "cache").evaluate(
        _parse_result(source_video),
        RecallEvalFixture(queries=[RecallQueryFixture(
            query_id="q-beta", query="find beta", relevant_chunk_sequence_nos=[1],
        )]),
        source_video=source_video,
        media_by_sequence=_media(tmp_path / "media"),
    )
    old_report = result.model_dump(mode="json", exclude_none=True)
    old_report.update(
        retrieval_backend="qdrant",
        qdrant_exact=True,
        qdrant_collection="historical_collection",
        dataset_id="historical_dataset",
    )
    restored = RecallEvalResult.model_validate(old_report)
    assert restored.retrieval_backend == "qdrant"
    assert restored.qdrant_exact is True
    assert restored.qdrant_collection == "historical_collection"
    assert restored.dataset_id == "historical_dataset"


def test_changed_embedding_metadata_invalidates_cache(tmp_path) -> None:
    source_video = tmp_path / "episode.mp4"
    source_video.write_bytes(b"source-video")
    parse_result = _parse_result(source_video)
    fixture = RecallEvalFixture(
        queries=[
            RecallQueryFixture(
                query_id="q-beta",
                query="find beta",
                relevant_chunk_sequence_nos=[1],
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
        window.analysis_start_seconds == window.chunk_start_seconds
        and window.analysis_end_seconds == window.chunk_end_seconds
        for window in captured_windows
    )


def test_rrf_uses_only_configured_channel_top_k() -> None:
    from evals.recall.recall_eval import RankedVideoChunk

    text = [
        RankedVideoChunk(rank=1, chunk_sequence_no=0, score=0.9),
        RankedVideoChunk(rank=2, chunk_sequence_no=1, score=0.8),
        RankedVideoChunk(rank=3, chunk_sequence_no=2, score=0.7),
    ]
    vision = [
        RankedVideoChunk(rank=1, chunk_sequence_no=1, score=0.95),
        RankedVideoChunk(rank=2, chunk_sequence_no=0, score=0.85),
        RankedVideoChunk(rank=3, chunk_sequence_no=2, score=0.75),
    ]

    fused = reciprocal_rank_fusion(text, vision, channel_top_k=2, rrf_k=60)

    assert [ranked.chunk_sequence_no for ranked in fused] == [0, 1]
    assert 2 not in {ranked.chunk_sequence_no for ranked in fused}


def test_parse_result_must_cover_complete_episode(tmp_path) -> None:
    parse_result = _parse_result(tmp_path / "episode.mp4")
    parse_result.chunks.pop()

    with pytest.raises(ValueError, match="complete video duration"):
        build_video_chunk_retrieval_documents(parse_result)
