from unittest.mock import Mock

import pytest

from engine.parsev2.schemas import VideoParseResult
from engine.rag.indexing.events import EventIndexingContract, plan_event_index
from engine.rag.retrieval.contracts import (
    ChunkRetrievalFacts, EventIndexNotReadyError, RawEventHit, RetrievalPolicy,
)
from engine.rag.vision.event_frames import EventFrameContract
from server.repository.video_parse import HydratedRetrievalCandidate, VideoParseConflictError
from server.service.video_event_retrieval import VideoEventRetrievalService
from tests.server.test_video_retrieval_service import (
    FakeQueryEmbedder, _v2_retrieval_fixture, _v2_semantic_encoder,
)


class _Repository:
    def __init__(self, scope, evidence, result):
        self.scope, self.evidence, self.result = scope, evidence, result
        self.hydrated = []
        self.coverage_error = None

    def resolve_retrieval_scope(self, series_ids, *, tenant_id):
        assert tuple(series_ids) == self.scope.series_ids and tenant_id == self.evidence.tenant_id
        return self.scope

    def validate_event_coverage(self, scope, *, index_name, contract_id, video_chunk_ids=None):
        if self.coverage_error:
            raise VideoParseConflictError(self.coverage_error)
        return ((next(iter(scope.parse_tasks_by_video_id.values())), self.evidence.video_chunk_id, 1),)

    def hydrate_retrieval_candidates(self, identities):
        self.hydrated.append(tuple(identities))
        return tuple(HydratedRetrievalCandidate(
            facts=ChunkRetrievalFacts(
                video_chunk_id=chunk, retrieval_identity=identity, sequence_no=self.evidence.sequence_no,
                start_seconds=self.evidence.start_seconds, end_seconds=self.evidence.end_seconds,
                result=self.result,
            ), evidence=self.evidence,
        ) for chunk, identity in identities)


class _Store:
    index_name = "seekino_event_test"
    semantic_dimensions = 2
    vision_dimensions = 2

    def __init__(self, plans):
        self.plans, self.calls = plans, []
        self.ready = True

    def bind_contract(self, contract):
        if not self.ready:
            raise EventIndexNotReadyError("Event index not initialized")

    def _hits(self, channel, kwargs):
        self.calls.append((channel, kwargs))
        filters = kwargs["filters"]
        eligible = [plan for plan in self.plans
                    if (not filters.event_types or plan.event_type in filters.event_types)
                    and (filters.speaker is None or plan.speaker == filters.speaker)]
        return tuple(RawEventHit(
            channel=channel, event_id=plan.event_id, parse_task_id=plan.parse_task_id,
            video_chunk_id=plan.video_chunk_id, version=plan.version, timeline_index=plan.timeline_index,
            event_type=plan.event_type, speaker=plan.speaker, lexical_fields=plan.lexical_fields,
            matched_fields=plan.lexical_fields[:1] if channel == "lexical" else (),
            rank=rank, score=1.0 / rank,
        ) for rank, plan in enumerate(eligible, 1))

    def search_lexical(self, query, **kwargs):
        return self._hits("lexical", kwargs)

    def search_vector(self, vector, *, channel, **kwargs):
        return self._hits(channel, kwargs)


def _fixture(*, events=None, budget=20):
    scope, _, evidence = _v2_retrieval_fixture()
    result = VideoParseResult(persons=[], timeline=events or evidence.all_time_events)
    encoder = _v2_semantic_encoder()
    contract = EventIndexingContract(
        semantic_encoder=encoder, vision_encoder=encoder, capture=EventFrameContract(max_height=512),
    )
    plans = plan_event_index(
        next(iter(scope.parse_tasks_by_video_id.values())), evidence.video_chunk_id, 1,
        result, evidence.start_seconds, evidence.end_seconds, contract,
    )
    repository, store = _Repository(scope, evidence, result), _Store(plans)
    embedder = FakeQueryEmbedder([1.0, 0.0])
    service = VideoEventRetrievalService(repository, store, embedder, contract=contract, policy=RetrievalPolicy(
        lexical_weight=1.0, semantic_weight=1.0, vision_weight=1.0,
        channel_top_k=20, candidate_budget=budget, top_k=1, rrf_k=60,
    ))
    inputs = dict(tenant_id=evidence.tenant_id, series_ids=scope.series_ids, top_k=1)
    return service, repository, store, embedder, inputs


@pytest.mark.parametrize("failure", ["index", "coverage", "scope"])
def test_event_not_ready_is_recoverable_before_model_calls(failure):
    service, repository, store, embedder, inputs = _fixture()
    if failure == "index":
        store.ready = False
    elif failure == "coverage":
        repository.coverage_error = "Event receipt is missing"
    else:
        repository.resolve_retrieval_scope = Mock(side_effect=VideoParseConflictError("scope is rebuilding"))
    with pytest.raises(EventIndexNotReadyError):
        service.search("从台阶跳下", **inputs)
    assert embedder.calls == [] and store.calls == []


def test_event_query_preserves_all_channels_trace_and_groups_parent_once():
    from engine.rag.retrieval.events import EVENT_RETRIEVAL_STRATEGY_VERSION

    service, repository, store, embedder, inputs = _fixture(events=[{
        "event_type": "ACTION", "start_seconds": 1, "end_seconds": 4,
        "description": "角色拿起蓝色杯子", "participants": [], "visible_persons": [],
    }, {
        "event_type": "EMOTION", "start_seconds": 4, "end_seconds": 7,
        "description": "角色看着杯子笑了", "participants": [], "visible_persons": [],
    }])
    result = service.search("蓝色杯子", **inputs)
    assert embedder.calls == ["蓝色杯子"]
    assert [channel for channel, _ in store.calls] == ["lexical", "semantic", "vision"]
    assert len(repository.hydrated) == 1 and len(repository.hydrated[0]) == 1
    assert len(result.chunk_result.hits) == 1 and len(result.matches) == 2
    assert {match.event_type for match in result.matches} == {"ACTION", "EMOTION"}
    assert all(match.start_ms is not None for match in result.matches)
    assert len(result.trace["raw_candidates"]) == 6
    assert len(result.trace["fused_candidates"]) == 2
    assert result.trace["strategy_version"] == EVENT_RETRIEVAL_STRATEGY_VERSION
    assert result.trace["selected_event_ids"] == [match.event_id for match in result.matches]
    assert result.chunk_result.hits[0].selection_reason.lexical_matches


def test_speaker_scope_is_engine_normalized_and_conflicts_do_not_reach_store():
    service, _, store, _, inputs = _fixture()
    service.search("居高临下", speaker="祥子", lexical_weight=1.0, text_weight=0.0, vision_weight=0.0, **inputs)
    assert store.calls[0][1]["filters"].event_types == ("DIALOGUE",)
    assert store.calls[0][1]["filters"].speaker == "祥子"
    with pytest.raises(ValueError, match="speaker filtering"):
        service.search("居高临下", speaker="祥子", event_types=["ACTION"], **inputs)


def test_budgeted_candidates_remain_in_full_trace():
    service, _, _, _, inputs = _fixture(events=[{
        "event_type": "ACTION", "start_seconds": 1, "end_seconds": 4,
        "description": "角色拿杯子", "participants": [], "visible_persons": [],
    }, {
        "event_type": "ACTION", "start_seconds": 4, "end_seconds": 7,
        "description": "角色放下杯子", "participants": [], "visible_persons": [],
    }], budget=1)
    result = service.search("杯子", **inputs)
    assert len(result.matches) == 1 and len(result.trace["raw_candidates"]) == 6
    assert len(result.trace["budget_excluded_event_ids"]) == 1
    assert len(result.trace["fused_candidates"]) == 2


def test_hydration_rejects_changed_timeline_and_records_reason():
    service, repository, _, _, inputs = _fixture()
    repository.result = VideoParseResult(persons=[], timeline=[{
        "event_type": "ACTION", "start_seconds": 1, "end_seconds": 4,
        "description": "被修改的内容", "participants": [], "visible_persons": [],
    }])
    result = service.search("杯子", **inputs)
    assert not result.matches and not result.chunk_result.hits
    assert result.chunk_result.partial
    assert "Timeline" in result.trace["rejected"][0]["reason"]


def test_lexical_only_event_query_never_embeds():
    service, _, _, embedder, inputs = _fixture()
    result = service.search("杯子", lexical_weight=1.0, text_weight=0.0, vision_weight=0.0, **inputs)
    assert not embedder.calls
    assert len(result.matches) == 1


def test_event_factory_uses_frozen_shared_encoder_and_closes_query_model():
    from types import SimpleNamespace
    from gateway.embedding import EmbeddingResult
    from server.common.retrieval import build_event_retrieval_service
    from server.config.retrieval import RetrievalConfig

    service, repository, store, _, inputs = _fixture()
    encoder = service._contract.semantic_encoder
    model = SimpleNamespace(
        embed_query=Mock(return_value=EmbeddingResult(model=encoder.model, modality="text", vector=[1.0, 0.0])),
        close=Mock(),
    )
    gateway = SimpleNamespace(
        describe_embedding=lambda binding: SimpleNamespace(
            provider_name=encoder.provider, encoder_model=encoder.model,
            encoder_revision=encoder.encoder_revision, dimensions=encoder.dimensions,
        ), create_embedding_model=Mock(return_value=model),
    )
    built = build_event_retrieval_service(
        repository, store, gateway_config=object(), gateway_client=gateway,
        embedding_config=repository.scope.parse_config.models.embedding,
        retrieval_config=RetrievalConfig(), contract=service._contract, request_timeout_seconds=30,
    )
    result = built.search("杯子", **inputs)
    assert result.matches
    model.embed_query.assert_called_once_with("杯子")
    model.close.assert_called_once()
    with pytest.raises(ValueError, match="shared multimodal query binding"):
        build_event_retrieval_service(
            repository, store, gateway_config=object(), gateway_client=gateway,
            embedding_config=repository.scope.parse_config.models.embedding,
            retrieval_config=RetrievalConfig(), contract=service._contract.model_copy(update={
                "vision_encoder": type(encoder)(provider=encoder.provider, model="other", encoder_revision="v1", dimensions=2),
            }), request_timeout_seconds=30,
        )
