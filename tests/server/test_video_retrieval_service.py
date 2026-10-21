from __future__ import annotations

import json
import logging
import subprocess
import sys
from dataclasses import replace
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from engine.rag import EncoderContract, LexicalField, SourceEnrichment
from gateway.ark.embedding import DOUBAO_MULTIMODAL_EMBEDDING_REVISION
from gateway.embedding import EmbeddingResult
from gateway.usage import capture_model_usage, model_call_context, track_model_call

from server.model.dto.jobDTO import VideoParseRetrievalScope, HydratedRetrievalCandidate
from server.config.parse_profile import default_video_parse_v2_config
from engine.parsev2.schemas import VideoParseResult
from engine.rag.contracts import VideoChunkRetrievalIdentity
from engine.rag.retrieval.contracts import ChunkRetrievalFacts, RawChunkHit
from server.schema import VideoChunkEvidence
from server.service.video_retrieval import (
    RetrievalIndexConsistencyError,
    VideoParseRetrievalService,
)
from server.storage.elasticsearch import VideoChunkIndexMatch


def test_formal_retrieval_import_does_not_load_qdrant_or_agent_runtime():
    subprocess.run([sys.executable, "-B", "-c", """
import sys
from server.service.video_retrieval import VideoParseRetrievalService
assert 'server.storage.qdrant' not in sys.modules
assert not any(name == 'qdrant_client' or name.startswith('qdrant_client.') for name in sys.modules)
assert 'agent.video_parse_v2.runtime' not in sys.modules
assert 'agent.chat.video_search.runtime' not in sys.modules
assert not any(name == 'langgraph' or name.startswith('langgraph.') for name in sys.modules)
"""], check=True)


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

    def resolve_retrieval_scope(self, series_ids, *, tenant_id, require_sparse=False):
        self.scope_calls.append((tuple(series_ids), tenant_id))
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
    tenant_id = "mine"
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
        tenant_id=tenant_id,
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
        tenant_id=tenant_id,
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


def test_retrieval_tracing_keeps_the_chat_run_and_records_embedding_failure():
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

    service = VideoParseRetrievalService(
        _V2Repository(scope, evidence), _V2Store([], [], []), Embedder(),
        semantic_encoder=_v2_semantic_encoder(),
        expected_index_generation=scope.index_generation,
        tracing=Tracing(),
    )
    with model_call_context({"run_id": "chat-run", "conversation_id": "conversation"}):
        with pytest.raises(RuntimeError, match="embedding failed"):
            service.search("private query", tenant_id=evidence.tenant_id, series_ids=scope.series_ids)
    assert len(observed) == 1
    call = observed[0]
    assert call.status == "failed" and call.usage is None
    assert call.context["run_id"] == "chat-run"
    assert call.context["conversation_id"] == "conversation"
    assert call.context["tenant_id"] == evidence.tenant_id
    assert call.context["series_ids"] == scope.series_ids
    assert call.context["video_ids"] == scope.searched_video_ids
    assert call.context["video_parse_task_ids"] == tuple(scope.parse_tasks_by_video_id.values())
    assert "query" not in call.context


def test_retrieval_sql_capture_inherits_chat_owner_without_an_independent_run():
    from server.service.parse_usage import ParseUsageService

    scope, _, evidence = _v2_retrieval_fixture()
    recorded = []
    usage = ParseUsageService(SimpleNamespace(record_call=recorded.append))

    class Embedder:
        @staticmethod
        def embed_query(_query):
            with track_model_call(provider="fixture", model="embed", operation="text_embedding"):
                raise RuntimeError("embedding failed")

    retrieval = VideoParseRetrievalService(
        _V2Repository(scope, evidence), _V2Store([], [], []), Embedder(),
        semantic_encoder=_v2_semantic_encoder(), expected_index_generation=scope.index_generation,
    )
    context = {"tenant_id": evidence.tenant_id, "assistant_message_id": "chat-message",
               "conversation_id": "conversation"}
    with usage.capture_chat(context), pytest.raises(RuntimeError, match="embedding failed"):
        retrieval.search("private query", tenant_id=evidence.tenant_id, series_ids=scope.series_ids)
    assert len(recorded) == 1
    row = recorded[0]
    assert row.task_type == "chat_retrieval" and row.run_id == "chat-message"
    assert row.metadata["status"] == "failed" and row.usage["tokens"] is None
    assert row.metadata["context"]["series_ids"] == scope.series_ids
    assert row.metadata["context"]["video_ids"] == scope.searched_video_ids
    assert row.metadata["context"]["video_parse_task_ids"] == tuple(scope.parse_tasks_by_video_id.values())
    with pytest.raises(RuntimeError, match="embedding failed"):
        retrieval.search("private query", tenant_id=evidence.tenant_id, series_ids=scope.series_ids)
    assert len(recorded) == 1


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
        tenant_id=evidence.tenant_id,
        series_ids=[evidence.series_id],
        top_k=1,
    )

    assert repository.scope_calls == [((evidence.series_id,), evidence.tenant_id)]
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
        service.search("角色离开", tenant_id=evidence.tenant_id, series_ids=scope.series_ids)
    assert store.calls == []
    assert embedder.calls == []


@pytest.mark.parametrize("encoder_model", [None, "fake-shared-embedding"])
def test_v2_endpoint_retrieval_uses_the_same_encoder_identity_as_indexing(encoder_model):
    scope, match, evidence = _v2_retrieval_fixture()
    config = scope.parse_config.model_copy(update={"models": scope.parse_config.models.model_copy(update={
        "embedding": scope.parse_config.models.embedding.model_copy(update={
            "model": "ep-custom", "encoder_model": encoder_model,
        }),
    })})
    scope = replace(scope, parse_config=config)
    store = _V2Store([match], [match], [match])
    embedder = FakeQueryEmbedder([1.0, 0.0])
    service = VideoParseRetrievalService(
        _V2Repository(scope, evidence), store, embedder,
        semantic_encoder=_v2_semantic_encoder(), expected_index_generation=scope.index_generation,
    )
    result = service.search("蓝色杯子", tenant_id=evidence.tenant_id, series_ids=scope.series_ids)
    assert len(result.hits) == 1
    assert embedder.calls == ["蓝色杯子"]
    assert [call[0] for call in store.calls] == ["lexical", "semantic", "vision"]


@pytest.mark.parametrize("returned_model", ["another-model", "ep-custom"])
def test_v2_endpoint_retrieval_still_rejects_a_wrong_actual_model(returned_model):
    scope, match, evidence = _v2_retrieval_fixture()
    config = scope.parse_config.model_copy(update={"models": scope.parse_config.models.model_copy(update={
        "embedding": scope.parse_config.models.embedding.model_copy(update={
            "model": "ep-custom", "encoder_model": "fake-shared-embedding",
        }),
    })})
    scope = replace(scope, parse_config=config)
    store = _V2Store([match], [match], [match])
    embedder = FakeQueryEmbedder([1.0, 0.0], model=returned_model)
    service = VideoParseRetrievalService(
        _V2Repository(scope, evidence), store, embedder,
        semantic_encoder=_v2_semantic_encoder(), expected_index_generation=scope.index_generation,
    )
    with pytest.raises(RetrievalIndexConsistencyError, match="query encoding response model"):
        service.search("蓝色杯子", tenant_id=evidence.tenant_id, series_ids=scope.series_ids)
    assert store.calls == []
    assert embedder.calls == ["蓝色杯子"]


def test_v2_endpoint_retrieval_rejects_encoder_drift_before_model_calls():
    scope, match, evidence = _v2_retrieval_fixture()
    config = scope.parse_config.model_copy(update={"models": scope.parse_config.models.model_copy(update={
        "embedding": scope.parse_config.models.embedding.model_copy(update={
            "model": "ep-custom", "encoder_model": "different-encoder",
        }),
    })})
    scope = replace(scope, parse_config=config)
    store = _V2Store([match], [match], [match])
    embedder = FakeQueryEmbedder([1.0, 0.0])
    service = VideoParseRetrievalService(
        _V2Repository(scope, evidence), store, embedder,
        semantic_encoder=_v2_semantic_encoder(), expected_index_generation=scope.index_generation,
    )
    with pytest.raises(RetrievalIndexConsistencyError, match="model or dimensions"):
        service.search("蓝色杯子", tenant_id=evidence.tenant_id, series_ids=scope.series_ids)
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
            tenant_id=evidence.tenant_id,
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
            tenant_id=evidence.tenant_id,
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
        service.search("蓝色杯子", tenant_id=evidence.tenant_id, series_ids=scope.series_ids)
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
            tenant_id=evidence.tenant_id,
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
            tenant_id=evidence.tenant_id,
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
        tenant_id=evidence.tenant_id,
        series_ids=[evidence.series_id],
    )

    assert result.partial is True
    assert result.warnings == [warning]
    assert result.searched_video_ids == list(scope.searched_video_ids)
    assert result.hits == []


@pytest.mark.parametrize("top_k", [61, 80, 100])
@pytest.mark.parametrize("candidate_budget", [None, 1])
def test_v2_legal_top_k_is_not_rejected_by_a_smaller_candidate_budget(top_k, candidate_budget):
    scope, match, evidence = _v2_retrieval_fixture()
    repository = _V2Repository(scope, evidence)
    service = VideoParseRetrievalService(
        repository, _V2Store([], [match], []), FakeQueryEmbedder([1.0, 0.0]),
        semantic_encoder=_v2_semantic_encoder(), expected_index_generation=scope.index_generation,
        candidate_budget=candidate_budget,
    )

    result = service.search("蓝色杯子", tenant_id=evidence.tenant_id,
                            series_ids=scope.series_ids, top_k=top_k)

    assert [hit.candidate.video_chunk_id for hit in result.hits] == [match.video_chunk_id]
    assert len(repository.hydrate_calls) == 1


@pytest.mark.parametrize("top_k", [0, -1, True, 1.5, "2"])
def test_v2_invalid_top_k_is_rejected_before_scope_encoding_or_recall(top_k):
    scope, match, evidence = _v2_retrieval_fixture()
    repository = _V2Repository(scope, evidence)
    store = _V2Store([], [match], [])
    embedder = FakeQueryEmbedder([1.0, 0.0])
    service = VideoParseRetrievalService(
        repository, store, embedder, semantic_encoder=_v2_semantic_encoder(),
        expected_index_generation=scope.index_generation,
    )

    with pytest.raises(ValueError, match="top_k must be a positive integer"):
        service.search("蓝色杯子", tenant_id=evidence.tenant_id,
                       series_ids=scope.series_ids, top_k=top_k)

    assert repository.scope_calls == store.calls == embedder.calls == []


@pytest.mark.parametrize("candidate_budget,top_k,pool_size", [(None, 1, 4), (2, 1, 2), (2, 3, 3)])
def test_v2_candidate_pool_follows_recall_config_and_covers_requested_top_k(
    candidate_budget, top_k, pool_size,
):
    scope, first, evidence = _v2_retrieval_fixture()
    matches = [replace(first, video_chunk_id=str(uuid4()), rank=rank)
               for rank in (1, 2, 1, 2)]

    class Repository(_V2Repository):
        def hydrate_retrieval_candidates(self, identities):
            self.hydrate_calls.append(tuple(identities))
            return tuple(_V2Repository(scope, evidence.model_copy(update={"video_chunk_id": chunk_id}))
                         .hydrate_retrieval_candidates([(chunk_id, identity)])[0]
                         for chunk_id, identity in identities)

    repository = Repository(scope, evidence)
    store = _V2Store([], matches[:2], matches[2:])
    service = VideoParseRetrievalService(
        repository, store, FakeQueryEmbedder([1.0, 0.0]),
        semantic_encoder=_v2_semantic_encoder(), expected_index_generation=scope.index_generation,
        channel_top_k=2, lexical_weight=0.0, candidate_budget=candidate_budget,
    )

    result = service.search("蓝色杯子", tenant_id=evidence.tenant_id,
                            series_ids=scope.series_ids, top_k=top_k)

    assert len(repository.hydrate_calls[0]) == pool_size
    assert len(result.hits) == top_k
    assert all(request.limit == 2 for _, request, _ in store.calls)


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
    result = service.search("蓝色杯子", tenant_id=evidence.tenant_id,
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
            service.search("蓝色杯子", tenant_id=evidence.tenant_id, series_ids=scope.series_ids)
    else:
        result = service.search("蓝色杯子", tenant_id=evidence.tenant_id, series_ids=scope.series_ids)
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
    result = service.search("蓝色杯子", tenant_id=evidence.tenant_id, series_ids=scope.series_ids)
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
        service.search("不存在的事件", tenant_id=evidence.tenant_id, series_ids=scope.series_ids)


def test_v2_retrieval_logs_match_sources_with_bounded_text(caplog):
    caplog.set_level(logging.INFO, logger="server.service.video_retrieval")
    scope, match, evidence = _v2_retrieval_fixture()
    description = "角色拿起杯子并走向门口。" * 40
    evidence = evidence.model_copy(update={"all_time_events": [
        {**evidence.all_time_events[0], "description": description},
    ]})
    match = replace(match, matched_fields=(LexicalField(
        field="event_description", raw_text=description,
        start_seconds=1, end_seconds=4, timeline_index=0,
    ),))
    service = VideoParseRetrievalService(
        _V2Repository(scope, evidence), _V2Store([match], [], []),
        FakeQueryEmbedder([1.0, 0.0]), semantic_encoder=_v2_semantic_encoder(),
        expected_index_generation=scope.index_generation, lexical_weight=1.0,
        text_weight=0.0, vision_weight=0.0,
    )
    query = "private-query-only-for-diagnostics-test"
    result = service.search(query, tenant_id=evidence.tenant_id, series_ids=scope.series_ids)

    logged = next(record.getMessage() for record in caplog.records
                  if record.getMessage().startswith("event=video_retrieval_lexical_matches "))
    assert f"video_chunk_id={match.video_chunk_id}" in logged
    assert "lexical_rank=1" in logged and "matched_field_count=1" in logged
    identity = json.loads(logged.split("retrieval_identity=", 1)[1].split(" matched_field_count=", 1)[0])
    assert identity["parse_task_id"] == match.parse_task_id
    assert identity["content_version"] == match.content_version
    source = json.loads(logged.split(" matched_fields=", 1)[1])[0]
    assert (source["field"], source["timeline_index"], source["start_seconds"], source["end_seconds"]) == (
        "event_description", 0, 1, 4,
    )
    assert source["text_preview"] == description[:256]
    assert source["text_chars"] == len(description)
    assert query not in logged and description not in logged
    assert result.hits[0].selection_reason.matched_evidence[0].text == description
