from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest
from gateway.schemas import EmbeddingResult
from qdrant_client import QdrantClient

from server.database import DatabaseEnvironment, create_session_factory
from server.repository.index_sync import IndexSyncRepository
from server.repository.video import SeriesScopeNotFoundError
from server.service.index_sync import IndexSyncService
from server.service.parse_import import PreparedEmbedding
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


def _ready_retrieval(repository, prepared_import):
    engine, video_repository = repository
    vectors = (
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
    ).sync_due()
    assert sync_report.synced_count == 2
    embedder = FakeQueryEmbedder([1.0, 0.0])
    service = VideoRetrievalService(
        video_repository,
        store,
        embedder,
        channel_top_k=2,
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
