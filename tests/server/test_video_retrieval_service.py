from __future__ import annotations

import logging
from dataclasses import replace
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from engine.parsev1.schemas import TranscriptCue
from engine.rag import EncoderContract, LexicalField, SourceEnrichment
from gateway.ark.embedding import DOUBAO_MULTIMODAL_EMBEDDING_REVISION
from gateway.embedding import EmbeddingResult
from gateway.usage import capture_model_usage, model_call_context, track_model_call
from qdrant_client import QdrantClient

from server.database import DatabaseEnvironment, create_session_factory
from server.repository.index_sync import IndexSyncRepository
from server.repository.video import SeriesScopeNotFoundError
from server.repository.video_parse import VideoParseRetrievalScope, HydratedRetrievalCandidate
from server.config.parse_profile import default_video_parse_v2_config
from engine.parsev2.schemas import VideoParseResult
from engine.rag.contracts import VideoChunkRetrievalIdentity
from engine.rag.retrieval.contracts import ChunkRetrievalFacts, RawChunkHit
from server.schema import VideoChunkEvidence
from server.service.parsev1.index_sync import IndexSyncService
from server.service.qdrant_mutation_lock import QdrantMutationLock
from server.model.dto.parseDTO import PreparedEmbedding
from server.service.video_retrieval import (
    RetrievalIndexConsistencyError,
    VideoParseRetrievalService,
    VideoRetrievalService,
)
from server.storage.elasticsearch import VideoChunkIndexMatch
from server.storage.qdrant import QdrantVideoChunkStore, VideoChunkIndexPoint


class FakeQueryEmbedder:
    def __init__(
        self,
        vector: list[float],
        *,
        model: str = "fake-shared-embedding",
    ) -> None:
        self._vector = vector
        self._model = model
        self.calls: list[str] = []

    def embed_query(self, query: str) -> EmbeddingResult:
        self.calls.append(query)
        return EmbeddingResult(
            model=self._model,
            modality="text",
            vector=self._vector,
        )


class _V2Repository:
    def __init__(
        self,
        scope: VideoParseRetrievalScope,
        evidence: VideoChunkEvidence,
    ) -> None:
        self.scope = scope
        self.evidence = evidence
        self.scope_calls: list[tuple[tuple[str, ...], str]] = []
        self.hydrate_calls = []

    def resolve_retrieval_scope(self, series_ids, *, workspace_id, require_sparse=False):
        self.scope_calls.append((tuple(series_ids), workspace_id))
        return self.scope

    def hydrate_retrieval_candidates(self, identities):
        self.hydrate_calls.append(tuple(identities))
        return [HydratedRetrievalCandidate(
            facts=ChunkRetrievalFacts(
                video_chunk_id=chunk_id, retrieval_identity=identity,
                sequence_no=self.evidence.sequence_no,
                start_seconds=self.evidence.start_seconds, end_seconds=self.evidence.end_seconds,
                result=VideoParseResult(persons=[], timeline=self.evidence.all_time_events),
                enrichments=(SourceEnrichment(language="zh-Hans", questions=["谁拿起了蓝色杯子？"]),),
            ), evidence=self.evidence,
        ) for chunk_id, identity in identities]


class _V2Store:
    semantic_dimensions = 2
    vision_dimensions = 2

    def __init__(
        self,
        lexical_matches,
        semantic_matches,
        vision_matches,
    ) -> None:
        self.lexical_matches = lexical_matches
        self.semantic_matches = semantic_matches
        self.vision_matches = vision_matches
        self.calls = []

    def recall(self, request, *, exact=False):
        self.calls.append((request.channel, request, {"exact": exact}))
        matches = getattr(self, f"{request.channel}_matches", [])
        return tuple(RawChunkHit(
            channel=request.channel, video_chunk_id=match.video_chunk_id,
            retrieval_identity=VideoChunkRetrievalIdentity(
                **{name: getattr(match, name) for name in VideoChunkRetrievalIdentity.model_fields}
            ), rank=match.rank, score=match.score,
            start_seconds=match.chunk_start_seconds, end_seconds=match.chunk_end_seconds,
            lexical_matches=match.matched_fields if request.channel == "lexical" else (),
        ) for match in matches)


def _v2_semantic_encoder() -> EncoderContract:
    return EncoderContract(
        provider="ark",
        model="fake-shared-embedding",
        encoder_revision=DOUBAO_MULTIMODAL_EMBEDDING_REVISION,
        dimensions=2,
    )


def _v2_retrieval_fixture():
    workspace_id = "mine"
    series_id = str(uuid4())
    video_id = str(uuid4())
    chunk_id = str(uuid4())
    parse_task_id = str(uuid4())
    config = default_video_parse_v2_config()
    config = config.model_copy(update={"models": config.models.model_copy(update={
        "embedding": config.models.embedding.model_copy(update={"model": "fake-shared-embedding", "dimensions": 2})
    })})
    scope = VideoParseRetrievalScope(
        series_ids=(series_id,),
        searched_video_ids=(video_id,),
        parse_tasks_by_video_id={video_id: parse_task_id},
        series_by_video_id={video_id: series_id},
        parse_config=config,
        index_generation=2,
    )
    match = VideoChunkIndexMatch(
        rank=1,
        score=1.5,
        workspace_id=workspace_id,
        series_id=series_id,
        video_id=video_id,
        video_chunk_id=chunk_id,
        parse_task_id=parse_task_id,
        content_version=3,
        index_generation=2,
        chunk_start_seconds=0,
        chunk_end_seconds=10,
    )
    evidence = VideoChunkEvidence(
        series_id=series_id,
        workspace_id=workspace_id,
        series_name="V2 Series",
        series_description=None,
        series_status="READY",
        important_extra_data=None,
        character_bible=[],
        video_id=video_id,
        episode_order=1,
        original_filename="episode.mp4",
        original_source_uri="artifact://v2/episode.mp4",
        inference_proxy_uri=None,
        content_hash="a" * 64,
        duration_seconds=10,
        upload_status="COMPLETED",
        parse_status="COMPLETE",
        video_chunk_id=chunk_id,
        sequence_no=0,
        start_seconds=0,
        end_seconds=10,
        analysis_window_start_seconds=0,
        analysis_window_end_seconds=10,
        chunk_media_ref="artifact://v2/chunk.mp4",
        analysis_window_media_ref="artifact://v2/window.mp4",
        transcript=[],
        baseline_visual_caption=None,
        keyframe_uris=[],
        all_time_events=[{
            "event_type": "ACTION",
            "start_seconds": 1,
            "end_seconds": 4,
            "description": "角色拿起蓝色杯子",
            "participants": [],
            "visible_persons": [],
        }],
        all_time_event_text="角色拿起蓝色杯子",
        retrieval_text="角色拿起蓝色杯子并走向门口",
        video_compact="角色准备离开",
        context_before_compact="",
        context_after_compact="角色准备离开",
        window_summary="角色拿起杯子",
        model_provider="ark",
        model_name="understanding-v2",
        pipeline_version="video-parse-v2",
        is_fallback=False,
        parse_provenance={"parseTaskId": parse_task_id},
        parsed_at=0,
    )
    return scope, match, evidence


@pytest.mark.parametrize("mode", ["v1", "v2"])
def test_retrieval_tracing_keeps_the_chat_run_and_records_embedding_failure(mode):
    scope, _, evidence = _v2_retrieval_fixture()
    observed = []
    observer = observed.append

    class Tracing:
        @contextmanager
        def scope(self, context):
            with model_call_context(context), capture_model_usage(observer, combine=True):
                yield

    class Embedder:
        @staticmethod
        def embed_query(_query):
            with track_model_call(provider="fixture", model="embed", operation="text_embedding"):
                raise RuntimeError("embedding failed")

    if mode == "v1":
        repository = SimpleNamespace(
            require_ready_series=lambda *_args, **_kwargs: scope.series_ids,
            list_indexed_video_ids=lambda *_args, **_kwargs: list(scope.searched_video_ids),
        )
        service = VideoRetrievalService(
            repository, SimpleNamespace(text_dimensions=2, vision_dimensions=2),
            Embedder(), tracing=Tracing(),
        )
    else:
        service = VideoParseRetrievalService(
            _V2Repository(scope, evidence), _V2Store([], [], []), Embedder(),
            semantic_encoder=_v2_semantic_encoder(),
            expected_index_generation=scope.index_generation,
            tracing=Tracing(),
        )
    with model_call_context({"run_id": "chat-run", "conversation_id": "conversation"}):
        with pytest.raises(RuntimeError, match="embedding failed"):
            service.search("private query", workspace_id=evidence.workspace_id, series_ids=scope.series_ids)
    assert len(observed) == 1
    call = observed[0]
    assert call.status == "failed" and call.usage is None
    assert call.context["run_id"] == "chat-run"
    assert call.context["conversation_id"] == "conversation"
    assert call.context["workspace_id"] == evidence.workspace_id
    assert call.context["series_ids"] == scope.series_ids
    assert call.context["video_ids"] == scope.searched_video_ids
    if mode == "v2":
        assert call.context["video_parse_task_ids"] == tuple(scope.parse_tasks_by_video_id.values())
    assert "query" not in call.context


def test_v2_retrieval_fuses_es_channels_and_hydrates_exact_identity() -> None:
    scope, match, evidence = _v2_retrieval_fixture()
    lexical_match = replace(
        match,
        matched_fields=(
            LexicalField(
                field="questions",
                raw_text="谁拿起了蓝色杯子？",
                language="zh-Hans",
                start_seconds=0,
                end_seconds=10,
                enrichment_index=0,
            ),
            LexicalField(
                field="event_description",
                raw_text="角色拿起蓝色杯子",
                start_seconds=1,
                end_seconds=4,
                timeline_index=0,
            ),
        ),
    )
    repository = _V2Repository(scope, evidence)
    store = _V2Store([lexical_match], [match], [match])
    embedder = FakeQueryEmbedder([1.0, 0.0])
    service = VideoParseRetrievalService(
        repository,
        store,
        embedder,
        semantic_encoder=_v2_semantic_encoder(),
        expected_index_generation=scope.index_generation,
        exact=True,
    )

    result = service.search(
        "  蓝色杯子  ",
        workspace_id=evidence.workspace_id,
        series_ids=[evidence.series_id],
        top_k=1,
    )

    assert repository.scope_calls == [((evidence.series_id,), evidence.workspace_id)]
    assert [call[0] for call in store.calls] == ["lexical", "semantic", "vision"]
    assert embedder.calls == ["蓝色杯子"]
    for _channel, request, kwargs in store.calls:
        assert request.scope.tasks[0].series_id == evidence.series_id
        assert request.scope.tasks[0].parse_task_id == match.parse_task_id
        assert request.index_generation == scope.index_generation
        assert kwargs["exact"] is True
    assert len(repository.hydrate_calls) == 1
    hydrated_chunk_id, hydrated_identity = repository.hydrate_calls[0][0]
    assert hydrated_chunk_id == match.video_chunk_id
    assert hydrated_identity.parse_task_id == match.parse_task_id
    assert hydrated_identity.content_version == match.content_version
    assert hydrated_identity.index_generation == match.index_generation
    hit = result.hits[0]
    assert hit.candidate.lexical_rank == 1
    assert hit.candidate.text_rank == 1
    assert hit.candidate.vision_rank == 1
    assert [item.field for item in hit.selection_reason.lexical_matches] == [
        "questions",
        "event_description",
    ]
    assert all(
        item.text != "谁拿起了蓝色杯子？"
        for item in hit.selection_reason.matched_evidence
    )
    assert any(
        "只用于召回线索" in item
        for item in hit.selection_reason.limitations
    )
    assert hit.evidence is evidence


@pytest.mark.parametrize(("field", "value"), [("model", "another-model"), ("dimensions", 3)])
def test_v2_retrieval_rejects_task_embedding_config_drift_before_es(field, value):
    scope, match, evidence = _v2_retrieval_fixture()
    config = scope.parse_config.model_copy(update={"models": scope.parse_config.models.model_copy(update={
        "embedding": scope.parse_config.models.embedding.model_copy(update={field: value})
    })})
    scope = replace(scope, parse_config=config)
    store = _V2Store([match], [match], [match])
    embedder = FakeQueryEmbedder([1.0, 0.0])
    service = VideoParseRetrievalService(
        _V2Repository(scope, evidence), store, embedder,
        semantic_encoder=_v2_semantic_encoder(), expected_index_generation=scope.index_generation,
    )
    with pytest.raises(RetrievalIndexConsistencyError, match="model or dimensions"):
        service.search("角色离开", workspace_id=evidence.workspace_id, series_ids=scope.series_ids)
    assert store.calls == []
    assert embedder.calls == []


@pytest.mark.parametrize(
    "embedder",
    [
        FakeQueryEmbedder([1.0, 0.0], model="another-model"),
        FakeQueryEmbedder([1.0, 0.0, 0.0]),
    ],
)
def test_v2_retrieval_rejects_gateway_embedding_metadata_before_vector_search(
    embedder,
) -> None:
    scope, match, evidence = _v2_retrieval_fixture()
    store = _V2Store([match], [match], [match])
    service = VideoParseRetrievalService(
        _V2Repository(scope, evidence),
        store,
        embedder,
        semantic_encoder=_v2_semantic_encoder(),
        expected_index_generation=scope.index_generation,
    )

    with pytest.raises(RetrievalIndexConsistencyError, match="query encoding|embedding vector"):
        service.search(
            "角色离开",
            workspace_id=evidence.workspace_id,
            series_ids=[evidence.series_id],
        )

    assert store.calls == []
    assert embedder.calls == ["角色离开"]


def test_v2_retrieval_rejects_index_generation_before_any_es_query() -> None:
    scope, match, evidence = _v2_retrieval_fixture()
    store = _V2Store([match], [match], [match])
    embedder = FakeQueryEmbedder([1.0, 0.0])
    service = VideoParseRetrievalService(
        _V2Repository(scope, evidence),
        store,
        embedder,
        semantic_encoder=_v2_semantic_encoder(),
        expected_index_generation=scope.index_generation + 1,
    )

    with pytest.raises(RetrievalIndexConsistencyError, match="index generation"):
        service.search(
            "角色离开",
            workspace_id=evidence.workspace_id,
            series_ids=[evidence.series_id],
        )

    assert store.calls == []
    assert embedder.calls == []


def test_v2_retrieval_rejects_changed_query_source_before_model_or_es_calls():
    scope, match, evidence = _v2_retrieval_fixture()
    store = _V2Store([], [match], [])
    embedder = FakeQueryEmbedder([1.0, 0.0])
    service = VideoParseRetrievalService(_V2Repository(scope, evidence), store, embedder,
        semantic_encoder=_v2_semantic_encoder(), expected_index_generation=scope.index_generation,
        expected_embedding_source="another-ark-source")
    with pytest.raises(RetrievalIndexConsistencyError, match="embedding source"):
        service.search("蓝色杯子", workspace_id=evidence.workspace_id, series_ids=scope.series_ids)
    assert store.calls == []
    assert embedder.calls == []


def test_v2_retrieval_rejects_cross_channel_identity_drift() -> None:
    scope, match, evidence = _v2_retrieval_fixture()
    repository = _V2Repository(scope, evidence)
    store = _V2Store(
        [replace(match, matched_fields=(LexicalField(
            field="summary",
            raw_text="角色准备离开",
            language="zh-Hans",
            start_seconds=0,
            end_seconds=10,
            enrichment_index=0,
        ),))],
        [match],
        [replace(match, content_version=match.content_version + 1)],
    )
    service = VideoParseRetrievalService(
        repository,
        store,
        FakeQueryEmbedder([1.0, 0.0]),
        semantic_encoder=_v2_semantic_encoder(),
        expected_index_generation=scope.index_generation,
    )

    with pytest.raises(RetrievalIndexConsistencyError, match="identity"):
        service.search(
            "角色离开",
            workspace_id=evidence.workspace_id,
            series_ids=[evidence.series_id],
        )

    assert repository.hydrate_calls == []


def test_v2_retrieval_rejects_legacy_transcript_weight_before_search() -> None:
    scope, match, evidence = _v2_retrieval_fixture()
    repository = _V2Repository(scope, evidence)
    store = _V2Store([match], [match], [match])
    service = VideoParseRetrievalService(
        repository,
        store,
        FakeQueryEmbedder([1.0, 0.0]),
        semantic_encoder=_v2_semantic_encoder(),
        expected_index_generation=scope.index_generation,
    )

    with pytest.raises(ValueError, match="lexical_weight"):
        service.search(
            "角色离开",
            workspace_id=evidence.workspace_id,
            series_ids=[evidence.series_id],
            transcript_weight=1.0,
            text_weight=0.0,
            vision_weight=0.0,
        )

    assert repository.scope_calls == []
    assert store.calls == []


def test_v2_retrieval_marks_a_complete_subset_as_partial() -> None:
    scope, _match, evidence = _v2_retrieval_fixture()
    warning = "部分视频的最新任务尚未完成。"
    scope = replace(scope, partial=True, warnings=(warning,))
    service = VideoParseRetrievalService(
        _V2Repository(scope, evidence),
        _V2Store([], [], []),
        FakeQueryEmbedder([1.0, 0.0]),
        semantic_encoder=_v2_semantic_encoder(),
        expected_index_generation=scope.index_generation,
    )

    result = service.search(
        "角色离开",
        workspace_id=evidence.workspace_id,
        series_ids=[evidence.series_id],
    )

    assert result.partial is True
    assert result.warnings == [warning]
    assert result.searched_video_ids == list(scope.searched_video_ids)
    assert result.hits == []


def test_v2_rerank_can_select_a_candidate_outside_the_original_top_k():
    from engine.rag.retrieval.rerank import RerankScorerContract, accept_rerank_result

    scope, first, evidence = _v2_retrieval_fixture()
    second = replace(first, video_chunk_id=str(uuid4()), rank=2, score=0.5)
    second_evidence = evidence.model_copy(update={"video_chunk_id": second.video_chunk_id})

    class Repository(_V2Repository):
        def hydrate_retrieval_candidates(self, identities):
            self.hydrate_calls.append(tuple(identities))
            return tuple(_V2Repository(scope, evidence if chunk_id == first.video_chunk_id
                else second_evidence).hydrate_retrieval_candidates([(chunk_id, identity)])[0]
                for chunk_id, identity in identities)

    class Scorer:
        contract = RerankScorerContract(model="test-score", protocol_provenance="test structured scores")

        def score(self, request):
            assert len(request.items) == 2
            assert all("谁拿起了蓝色杯子？" not in item.text for item in request.items)
            return accept_rerank_result(request, model=self.contract.model,
                scores=[(first.video_chunk_id, 0.1), (second.video_chunk_id, 0.9)])

    repository = Repository(scope, evidence)
    service = VideoParseRetrievalService(repository, _V2Store([], [first, second], []),
        FakeQueryEmbedder([1.0, 0.0]), semantic_encoder=_v2_semantic_encoder(),
        expected_index_generation=scope.index_generation, lexical_weight=0.0,
        text_weight=1.0, vision_weight=0.0, reranker=Scorer())
    result = service.search("蓝色杯子", workspace_id=evidence.workspace_id,
                            series_ids=scope.series_ids, top_k=1)
    assert len(repository.hydrate_calls[0]) == 2
    assert result.hits[0].candidate.video_chunk_id == second.video_chunk_id
    assert result.hits[0].candidate.fused_rank == 2
    proof = result.hits[0].selection_reason.matched_evidence[0]
    assert (proof.start_seconds, proof.end_seconds) == (0, 10)
    assert proof.kind == "CONTEXT"
    assert "蓝色杯子" not in proof.text


@pytest.mark.parametrize("failure", ["error", "incomplete", "control"])
def test_v2_rerank_fallback_preserves_rrf_and_propagates_host_control(failure, caplog):
    from engine.rag.retrieval.rerank import RerankScorerContract, RerankResult
    from gateway.usage import ModelCallControlError

    scope, match, evidence = _v2_retrieval_fixture()

    class Scorer:
        contract = RerankScorerContract(model="test-score", protocol_provenance="test structured scores")

        def score(self, request):
            if failure == "control":
                raise ModelCallControlError("host limit")
            if failure == "error":
                raise TimeoutError("scorer unavailable")
            return RerankResult(request=request, model=self.contract.model, scores=())

    service = VideoParseRetrievalService(_V2Repository(scope, evidence), _V2Store([], [match], []),
        FakeQueryEmbedder([1.0, 0.0]), semantic_encoder=_v2_semantic_encoder(),
        expected_index_generation=scope.index_generation, lexical_weight=0.0,
        text_weight=1.0, vision_weight=0.0, reranker=Scorer())
    if failure == "control":
        with pytest.raises(ModelCallControlError, match="host limit"):
            service.search("蓝色杯子", workspace_id=evidence.workspace_id, series_ids=scope.series_ids)
    else:
        result = service.search("蓝色杯子", workspace_id=evidence.workspace_id, series_ids=scope.series_ids)
        assert result.hits[0].candidate.video_chunk_id == match.video_chunk_id
        assert "using RRF" in caplog.text


def test_v2_sparse_channel_uses_native_weights_and_keeps_chunk_evidence():
    from gateway.embedding import SparseEmbeddingResult

    scope, match, evidence = _v2_retrieval_fixture()
    store = _V2Store([], [], [])
    store.sparse_matches = [match]

    class Embedder:
        def embed_query(self, _query):
            raise AssertionError("dense query was not requested")

        def embed_sparse(self, query):
            assert query == "蓝色杯子"
            return SparseEmbeddingResult(model=scope.embedding_model, weights={"17": 0.5})

    service = VideoParseRetrievalService(_V2Repository(scope, evidence), store, Embedder(),
        semantic_encoder=_v2_semantic_encoder(), expected_index_generation=scope.index_generation,
        lexical_weight=0.0, text_weight=0.0, vision_weight=0.0, sparse_weight=1.0)
    result = service.search("蓝色杯子", workspace_id=evidence.workspace_id, series_ids=scope.series_ids)
    assert store.calls[0][0] == "sparse"
    assert store.calls[0][1].token_weights == (("17", 0.5),)
    assert result.hits[0].candidate.video_chunk_id == match.video_chunk_id
    assert "尚未定位到具体事件" in result.hits[0].selection_reason.limitations[0]


def test_v2_retrieval_rejects_index_event_text_not_in_current_formal_content():
    scope, match, evidence = _v2_retrieval_fixture()
    match = replace(match, matched_fields=(LexicalField(field="event_description",
        raw_text="不存在的事件", start_seconds=1, end_seconds=4, timeline_index=0),))
    service = VideoParseRetrievalService(_V2Repository(scope, evidence), _V2Store([match], [], []),
        FakeQueryEmbedder([1.0, 0.0]), semantic_encoder=_v2_semantic_encoder(),
        expected_index_generation=scope.index_generation, lexical_weight=1.0,
        text_weight=0.0, vision_weight=0.0)
    with pytest.raises(RetrievalIndexConsistencyError, match="formal projection source"):
        service.search("不存在的事件", workspace_id=evidence.workspace_id, series_ids=scope.series_ids)


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
    assert first.evidence.start_seconds == 0
    assert first.evidence.end_seconds == 10
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
            video_id=prepared.video_id,
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


def test_retrieval_excludes_unavailable_video_points_before_channel_top_k(
    repository,
    prepared_import,
) -> None:
    service, store, client, _embedder, prepared, report = _ready_retrieval(
        repository,
        prepared_import,
        channel_top_k=1,
    )
    unavailable_video_id = str(uuid4())
    store.upsert(
        VideoChunkIndexPoint(
            series_id=prepared.series_id,
            video_id=unavailable_video_id,
            video_chunk_id=str(uuid4()),
            source_revision="stale-revision",
            is_fallback=False,
            text_vector=[1.0, 0.0],
            vision_vector=[1.0, 0.0],
        )
    )

    try:
        result = service.search(
            "find available video",
            workspace_id=prepared.workspace_id,
            series_ids=[prepared.series_id],
            top_k=1,
        )

        assert result.searched_video_ids == [prepared.video_id]
        assert [hit.candidate.video_chunk_id for hit in result.hits] == [
            report.video_chunk_ids[0]
        ]
    finally:
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
                TranscriptCue(start_seconds=1, end_seconds=2, text="别把责任"),
                TranscriptCue(start_seconds=2, end_seconds=3, text="推给别人。"),
            ]
            if index == 0
            else [TranscriptCue(start_seconds=1, end_seconds=2, text="其他对白")]
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
            "start_seconds": 1,
            "end_seconds": 3,
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
                                start_seconds=1,
                                end_seconds=1,
                                text="瞬间回应",
                            ),
                            TranscriptCue(
                                start_seconds=3,
                                end_seconds=2,
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
            "start_seconds": 1,
            "end_seconds": 1,
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
        update={"analysis_end_seconds": 13, "analysis_duration_seconds": 13}
    )
    second_window = second.parse_result.window.model_copy(
        update={
            "analysis_start_seconds": 7,
            "analysis_duration_seconds": 13,
            "relative_chunk_start_seconds": 3,
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
                                start_seconds=10,
                                end_seconds=12,
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
                                start_seconds=3,
                                end_seconds=5,
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
        assert result.hits[0].selection_reason.matched_evidence[0].start_seconds == 10
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
