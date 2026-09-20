from __future__ import annotations

import logging
from dataclasses import replace
from uuid import uuid4

import pytest
from engine.parse.schemas import TranscriptCue
from gateway.schemas import EmbeddingResult
from qdrant_client import QdrantClient

from server.database import DatabaseEnvironment, create_session_factory
from server.repository.index_sync import IndexSyncRepository
from server.repository.video import SeriesScopeNotFoundError
from server.service.index_sync import IndexSyncService
from server.service.qdrant_mutation_lock import QdrantMutationLock
from server.model.dto.parseDTO import PreparedEmbedding
from server.service.video_retrieval import (
    RetrievalIndexConsistencyError,
    VideoRetrievalService,
)
from server.storage.qdrant import QdrantVideoChunkStore, VideoChunkIndexPoint


class FakeQueryEmbedder:
    def __init__(self, vector: list[float]) -> None:
        self._vector = vector
        self.calls: list[str] = []

    def embed_query(self, query: str) -> EmbeddingResult:
        self.calls.append(query)
        return EmbeddingResult(
            model="fake-shared-embedding",
            modality="text",
            vector=self._vector,
        )


def _ready_retrieval(
    repository,
    prepared_import,
    *,
    vectors=None,
    channel_top_k: int = 2,
):
    engine, video_repository = repository
    vectors = vectors or (
        ([0.8, 0.6], [0.8, 0.6]),
        ([0.0, 1.0], [0.0, 1.0]),
    )
    prepared = replace(
        prepared_import,
        chunks=tuple(
            replace(
                chunk,
                text_embedding=PreparedEmbedding(
                    vector=vectors[index][0],
                    metadata={"model": "fake-shared-embedding"},
                ),
                vision_embedding=PreparedEmbedding(
                    vector=vectors[index][1],
                    metadata={"model": "fake-shared-embedding"},
                ),
            )
            for index, chunk in enumerate(prepared_import.chunks)
        ),
    )
    report = video_repository.import_video_parse(prepared)
    client = QdrantClient(":memory:")
    store = QdrantVideoChunkStore(
        client,
        collection_name="video_retrieval_test",
        environment=DatabaseEnvironment.TEST,
        text_dimensions=2,
        vision_dimensions=2,
    )
    sync_report = IndexSyncService(
        IndexSyncRepository(create_session_factory(engine)),
        store,
        mutation_lock=QdrantMutationLock(
            engine,
            environment=DatabaseEnvironment.TEST,
            collection_name=store.collection_name,
        ),
    ).sync_due()
    assert sync_report.synced_count == 2
    embedder = FakeQueryEmbedder([1.0, 0.0])
    service = VideoRetrievalService(
        video_repository,
        store,
        embedder,
        channel_top_k=channel_top_k,
        exact=True,
    )
    return service, store, client, embedder, prepared, report


def test_formal_retrieval_closes_qdrant_rrf_and_joined_hydrate(
    repository,
    prepared_import,
) -> None:
    service, _store, client, embedder, prepared, report = _ready_retrieval(
        repository,
        prepared_import,
    )

    result = service.search(
        "  find the first action  ",
        workspace_id=prepared.workspace_id,
        series_ids=[prepared.series_id],
        top_k=2,
    )

    assert embedder.calls == ["find the first action"]
    assert result.query == "find the first action"
    assert result.series_ids == [prepared.series_id]
    assert result.searched_video_ids == [prepared.video_id]
    assert result.agent_reply.result_count == len(result.hits)
    assert "2 段备选" in result.agent_reply.message
    assert [hit.candidate.video_chunk_id for hit in result.hits] == (
        report.video_chunk_ids
    )
    assert [hit.evidence.video_chunk_id for hit in result.hits] == (
        report.video_chunk_ids
    )
    assert all(hit.evidence.series_id == prepared.series_id for hit in result.hits)
    first = result.hits[0]
    assert first.candidate.fused_rank == 1
    assert first.candidate.text_rank == 1
    assert first.candidate.vision_rank == 1
    assert first.candidate.text_score == pytest.approx(0.8)
    assert first.candidate.vision_score == pytest.approx(0.8)
    assert first.selection_reason.summary
    assert first.selection_reason.matched_evidence
    assert first.selection_reason.matched_evidence[0].text in {
        first.evidence.video_compact,
        first.evidence.all_time_events[0]["description"],
    }
    assert first.evidence.series_name == "Test Series"
    assert first.evidence.episode_order == 7
    assert first.evidence.sequence_no == 0
    assert first.evidence.start_ms == 0
    assert first.evidence.end_ms == 10_000
    assert first.evidence.retrieval_text == (
        prepared.chunks[0].retrieval_document.text
    )

    payload = result.model_dump(mode="json", by_alias=True)
    assert all(hit["evidence"]["seriesId"] == prepared.series_id for hit in payload["hits"])
    assert payload["hits"][0]["candidate"]["videoChunkId"] == (
        report.video_chunk_ids[0]
    )
    assert payload["hits"][0]["candidate"]["fusedRank"] == 1
    assert payload["agentReply"]["resultCount"] == len(payload["hits"])
    assert payload["hits"][0]["selectionReason"]["matchedEvidence"]
    assert payload["hits"][0]["evidence"]["videoCompact"]
    assert payload["hits"][0]["evidence"]["retrievalText"] == (
        prepared.chunks[0].retrieval_document.text
    )
    assert "textVector" not in payload["hits"][0]["evidence"]
    client.close()


def test_per_call_weights_change_fused_order_without_mutating_service_defaults(
    repository,
    prepared_import,
) -> None:
    service, _store, client, _embedder, prepared, report = _ready_retrieval(
        repository,
        prepared_import,
        vectors=(
            ([1.0, 0.0], [0.0, 1.0]),
            ([0.0, 1.0], [1.0, 0.0]),
        ),
    )
    try:
        defaults_before = service.search(
            "find the strongest channel",
            workspace_id=prepared.workspace_id,
            series_ids=[prepared.series_id],
            top_k=2,
        )
        semantic = service.search(
            "find the strongest channel",
            workspace_id=prepared.workspace_id,
            series_ids=[prepared.series_id],
            top_k=2,
            text_weight=0.8,
            vision_weight=0.2,
        )
        visual = service.search(
            "find the strongest channel",
            workspace_id=prepared.workspace_id,
            series_ids=[prepared.series_id],
            top_k=2,
            text_weight=0.2,
            vision_weight=0.8,
        )
        defaults_after = service.search(
            "find the strongest channel",
            workspace_id=prepared.workspace_id,
            series_ids=[prepared.series_id],
            top_k=2,
        )

        assert semantic.hits[0].candidate.video_chunk_id == report.video_chunk_ids[0]
        assert visual.hits[0].candidate.video_chunk_id == report.video_chunk_ids[1]
        assert [hit.candidate.video_chunk_id for hit in defaults_after.hits] == [
            hit.candidate.video_chunk_id for hit in defaults_before.hits
        ]
    finally:
        client.close()


def test_zero_weight_skips_the_disabled_channel(
    repository,
    prepared_import,
    monkeypatch,
) -> None:
    service, store, client, _embedder, prepared, _report = _ready_retrieval(
        repository,
        prepared_import,
    )
    vision_calls = 0
    original_search_vision = store.search_vision

    def count_vision_calls(*args, **kwargs):
        nonlocal vision_calls
        vision_calls += 1
        return original_search_vision(*args, **kwargs)

    monkeypatch.setattr(store, "search_vision", count_vision_calls)
    try:
        result = service.search(
            "semantic only",
            workspace_id=prepared.workspace_id,
            series_ids=[prepared.series_id],
            top_k=2,
            text_weight=1.0,
            vision_weight=0.0,
        )

        assert vision_calls == 0
        assert all(hit.candidate.vision_rank is None for hit in result.hits)
        assert all(
            any(
                limitation == "本次检索策略未启用视觉通道。"
                for limitation in hit.selection_reason.limitations
            )
            for hit in result.hits
        )
        assert all(
            not any(
                "视觉通道已启用" in limitation
                for limitation in hit.selection_reason.limitations
            )
            for hit in result.hits
        )
    finally:
        client.close()


def test_enabled_channel_miss_is_distinct_from_disabled_channel(
    repository,
    prepared_import,
) -> None:
    service, _store, client, _embedder, prepared, _report = _ready_retrieval(
        repository,
        prepared_import,
        vectors=(
            ([1.0, 0.0], [0.0, 1.0]),
            ([0.0, 1.0], [1.0, 0.0]),
        ),
        channel_top_k=1,
    )
    try:
        result = service.search(
            "find both modalities",
            workspace_id=prepared.workspace_id,
            series_ids=[prepared.series_id],
            top_k=2,
            text_weight=0.5,
            vision_weight=0.5,
        )

        assert len(result.hits) == 2
        assert any(
            "视觉通道已启用，但该片段未进入该通道候选"
            in limitation
            for hit in result.hits
            for limitation in hit.selection_reason.limitations
        )
        assert any(
            "文本语义通道已启用，但该片段未进入该通道候选"
            in limitation
            for hit in result.hits
            for limitation in hit.selection_reason.limitations
        )
        assert all(
            "未启用文本语义通道" not in limitation
            and "未启用视觉通道" not in limitation
            for hit in result.hits
            for limitation in hit.selection_reason.limitations
        )
    finally:
        client.close()


def test_retrieval_reports_a_qdrant_orphan_instead_of_dropping_it(
    repository,
    prepared_import,
) -> None:
    service, store, client, _embedder, prepared, _report = _ready_retrieval(
        repository,
        prepared_import,
    )
    store.upsert(
        VideoChunkIndexPoint(
            series_id=prepared.series_id,
            video_id=str(uuid4()),
            video_chunk_id=str(uuid4()),
            source_revision="orphan-revision",
            is_fallback=False,
            text_vector=[1.0, 0.0],
            vision_vector=[1.0, 0.0],
        )
    )

    with pytest.raises(
        RetrievalIndexConsistencyError,
        match="missing from MySQL",
    ):
        service.search(
            "find orphan",
            workspace_id=prepared.workspace_id,
            series_ids=[prepared.series_id],
            top_k=1,
        )
    client.close()


def test_retrieval_detects_a_stale_qdrant_source_revision(
    repository,
    prepared_import,
) -> None:
    service, store, client, _embedder, prepared, report = _ready_retrieval(
        repository,
        prepared_import,
    )
    client.set_payload(
        collection_name=store.collection_name,
        payload={"sourceRevision": "stale-revision"},
        points=[report.video_chunk_ids[0]],
        wait=True,
    )

    with pytest.raises(
        RetrievalIndexConsistencyError,
        match="sourceRevision",
    ):
        service.search(
            "find stale",
            workspace_id=prepared.workspace_id,
            series_ids=[prepared.series_id],
            top_k=1,
        )
    client.close()


def test_retrieval_checks_ready_scope_before_calling_the_embedder(
    repository,
    prepared_import,
) -> None:
    _engine, video_repository = repository
    video_repository.import_video_parse(prepared_import)
    embedder = FakeQueryEmbedder([1.0, 0.0])
    service = VideoRetrievalService(
        video_repository,
        QdrantVideoChunkStore(
            QdrantClient(":memory:"),
            collection_name="not_ready_test",
            environment=DatabaseEnvironment.TEST,
            text_dimensions=2,
            vision_dimensions=2,
        ),
        embedder,
    )

    with pytest.raises(RuntimeError, match="not READY"):
        service.search(
            "must not embed",
            workspace_id=prepared_import.workspace_id,
            series_ids=[prepared_import.series_id],
        )
    assert embedder.calls == []


def test_transcript_only_retrieval_matches_real_asr_without_embedding(
    repository,
    prepared_import,
) -> None:
    transcript_chunks = []
    for index, chunk in enumerate(prepared_import.chunks):
        cues = (
            [
                TranscriptCue(start_ms=1_000, end_ms=2_000, text="别把责任"),
                TranscriptCue(start_ms=2_000, end_ms=3_000, text="推给别人。"),
            ]
            if index == 0
            else [TranscriptCue(start_ms=1_000, end_ms=2_000, text="其他对白")]
        )
        transcript_chunks.append(
            replace(
                chunk,
                parse_result=chunk.parse_result.model_copy(
                    update={"candidate_transcript": cues}
                ),
            )
        )
    prepared_import = replace(
        prepared_import,
        chunks=tuple(transcript_chunks),
    )
    service, _store, client, embedder, prepared, report = _ready_retrieval(
        repository,
        prepared_import,
    )
    try:
        result = service.search(
            "『别把责任，推给别人！』",
            workspace_id=prepared.workspace_id,
            series_ids=[prepared.series_id],
            top_k=2,
            transcript_weight=1.0,
            text_weight=0.0,
            vision_weight=0.0,
        )

        assert embedder.calls == []
        assert [hit.candidate.video_chunk_id for hit in result.hits] == [
            report.video_chunk_ids[0]
        ]
        hit = result.hits[0]
        assert hit.candidate.transcript_rank == 1
        assert hit.candidate.transcript_score == 1.0
        assert hit.candidate.text_rank is None
        assert hit.candidate.vision_rank is None
        assert hit.selection_reason.matched_evidence[0].model_dump() == {
            "kind": "TRANSCRIPT",
            "text": "别把责任 推给别人。",
            "start_ms": 1_000,
            "end_ms": 3_000,
        }
    finally:
        client.close()


def test_transcript_retrieval_soft_validates_persisted_time_ranges(
    repository,
    prepared_import,
    caplog,
) -> None:
    first, second = prepared_import.chunks
    prepared_import = replace(
        prepared_import,
        chunks=(
            replace(
                first,
                parse_result=first.parse_result.model_copy(
                    update={
                        "candidate_transcript": [
                            TranscriptCue(
                                start_ms=1_000,
                                end_ms=1_000,
                                text="瞬间回应",
                            ),
                            TranscriptCue(
                                start_ms=3_000,
                                end_ms=2_000,
                                text="反向坏数据",
                            ),
                        ]
                    }
                ),
            ),
            second,
        ),
    )
    service, _store, client, embedder, prepared, report = _ready_retrieval(
        repository,
        prepared_import,
    )
    try:
        with caplog.at_level(logging.WARNING, logger="server.repository.video"):
            result = service.search(
                "瞬间回应",
                workspace_id=prepared.workspace_id,
                series_ids=[prepared.series_id],
                top_k=2,
                transcript_weight=1.0,
                text_weight=0.0,
                vision_weight=0.0,
            )

        assert embedder.calls == []
        assert [hit.candidate.video_chunk_id for hit in result.hits] == [
            report.video_chunk_ids[0]
        ]
        assert result.hits[0].selection_reason.matched_evidence[0].model_dump() == {
            "kind": "TRANSCRIPT",
            "text": "瞬间回应",
            "start_ms": 1_000,
            "end_ms": 1_000,
        }
        assert "retained_point_cue_count=1" in caplog.text
        assert "skipped_invalid_cue_count=1" in caplog.text
    finally:
        client.close()


def test_transcript_only_miss_is_a_normal_empty_result(
    repository,
    prepared_import,
) -> None:
    service, _store, client, embedder, prepared, _report = _ready_retrieval(
        repository,
        prepared_import,
    )
    try:
        result = service.search(
            "不存在的原话",
            workspace_id=prepared.workspace_id,
            series_ids=[prepared.series_id],
            top_k=2,
            transcript_weight=1.0,
            text_weight=0.0,
            vision_weight=0.0,
        )

        assert embedder.calls == []
        assert result.hits == []
        assert result.agent_reply.result_count == 0
    finally:
        client.close()


def test_transcript_retrieval_assigns_overlap_cue_to_canonical_owner(
    repository,
    prepared_import,
) -> None:
    first, second = prepared_import.chunks
    first_window = first.parse_result.window.model_copy(
        update={"analysis_end_ms": 13_000, "analysis_duration_ms": 13_000}
    )
    second_window = second.parse_result.window.model_copy(
        update={
            "analysis_start_ms": 7_000,
            "analysis_duration_ms": 13_000,
            "relative_chunk_start_ms": 3_000,
        }
    )
    phrase = "边界处的同一句台词"
    prepared_import = replace(
        prepared_import,
        chunks=(
            replace(
                first,
                parse_result=first.parse_result.model_copy(
                    update={
                        "window": first_window,
                        "candidate_transcript": [
                            TranscriptCue(
                                start_ms=10_500,
                                end_ms=11_500,
                                text=phrase,
                            )
                        ],
                    }
                ),
            ),
            replace(
                second,
                parse_result=second.parse_result.model_copy(
                    update={
                        "window": second_window,
                        "candidate_transcript": [
                            TranscriptCue(
                                start_ms=3_500,
                                end_ms=4_500,
                                text=phrase,
                            )
                        ],
                    }
                ),
            ),
        ),
    )
    service, _store, client, _embedder, prepared, report = _ready_retrieval(
        repository,
        prepared_import,
    )
    try:
        result = service.search(
            phrase,
            workspace_id=prepared.workspace_id,
            series_ids=[prepared.series_id],
            top_k=2,
            transcript_weight=1.0,
            text_weight=0.0,
            vision_weight=0.0,
        )

        assert [hit.candidate.video_chunk_id for hit in result.hits] == [
            report.video_chunk_ids[1]
        ]
        assert result.hits[0].selection_reason.matched_evidence[0].start_ms == 10_500
    finally:
        client.close()


def test_retrieval_checks_workspace_before_calling_the_embedder(
    repository,
    prepared_import,
) -> None:
    service, _store, client, embedder, prepared, _report = _ready_retrieval(
        repository,
        prepared_import,
    )
    try:
        with pytest.raises(SeriesScopeNotFoundError, match="workspace"):
            service.search(
                "must not embed",
                workspace_id="another-workspace",
                series_ids=[prepared.series_id],
            )
        assert embedder.calls == []
    finally:
        client.close()
