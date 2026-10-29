from dataclasses import replace
from unittest.mock import Mock

import pytest

from agent.video_parse_v2.input import ParseConfig
from engine.rag.indexing.events import EventIndexingContract
from engine.rag.retrieval.contracts import RetrievalPolicy
from gateway.config import EmbeddingModelBinding, OllamaEmbeddingSourceConfig, load_config
from server import bootstrap, main as server_main
from server.common.retrieval import build_retrieval_service
from server.config import ServerSettings
from server.config.parse_profile import (
    ParseConfig as ServerParseConfig, default_video_parse_v2_config, index_contract_fingerprint,
    resolve_embedding_config,
)
from server.config.retrieval import RetrievalConfig
from server.service.video_event_retrieval import VideoEventRetrievalService
from server.service.video_retrieval import VideoParseRetrievalService, RetrievalIndexConsistencyError
from tests.server.test_video_event_index_coordinator import _event_setup
from tests.server.test_video_event_retrieval import _fixture as event_query_fixture
from tests.server.test_video_retrieval_service import (
    _v2_retrieval_fixture, _v2_semantic_encoder, _V2Repository, _V2Store, FakeQueryEmbedder,
)


def _local_text_gateway():
    gateway = load_config()
    return gateway.model_copy(update={
        "model_sources": {**gateway.model_sources, "local_embedding": OllamaEmbeddingSourceConfig(base_url="http://127.0.0.1:11434")},
        "normalized_models": {**gateway.normalized_models, "embedding": EmbeddingModelBinding(
            source="local_embedding", model="qwen3-embedding:0.6b", input_mode="text",
            dimensions=1024, video_fps=1.0, request_timeout_seconds=60,
        )},
    })


@pytest.mark.parametrize("local", [False, True])
@pytest.mark.parametrize("sparse_weight", [0, 0.5])
def test_sparse_support_is_checked_before_startup_allocates_clients(monkeypatch, tmp_path, local, sparse_weight):
    gateway = _local_text_gateway() if local else load_config()
    monkeypatch.setattr(bootstrap, "load_config", lambda _path: gateway)
    monkeypatch.setenv("SEEKINO_ENVIRONMENT", "test")
    monkeypatch.setenv("SEEKINO_DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'unused.sqlite'}")
    monkeypatch.setenv("SEEKINO_ELASTICSEARCH_URL", "http://127.0.0.1:9200")
    monkeypatch.setenv("SEEKINO_ELASTICSEARCH_INDEX", "sparse_startup_test")
    monkeypatch.setenv("SEEKINO_RETRIEVAL_SPARSE_WEIGHT", str(sparse_weight))
    settings = ServerSettings.from_environment()
    monkeypatch.setattr(server_main.caller, "init", lambda *_a, **_k: pytest.fail("allocated persistence clients"))
    monkeypatch.setattr(bootstrap, "GatewayClient", lambda *_a, **_k: pytest.fail("allocated model client"))
    if local and sparse_weight:
        with pytest.raises(ValueError, match="ollama_embedding.*does not support sparse embeddings"):
            server_main.init_application(settings)
    else:
        _, profile = bootstrap.prepare_service_configuration(settings)
        frozen = profile.to_video_parse_v2()
        assert frozen.indexing.sparse_enabled is (sparse_weight > 0)
        assert frozen.models.embedding.input_mode == ("text" if local else "multimodal")


def test_local_sparse_query_configuration_fails_before_constructing_model(monkeypatch):
    gateway = _local_text_gateway()
    monkeypatch.setattr("gateway.model_factory.GatewayClient", lambda *_a, **_k: pytest.fail("allocated model client"))
    with pytest.raises(ValueError, match="does not support sparse embeddings"):
        build_retrieval_service(
            object(), object(), gateway_config=gateway,
            embedding_config=resolve_embedding_config(gateway, dimensions=1024),
            retrieval_config=RetrievalConfig(sparse_weight=0.5), index_generation=1,
            request_timeout_seconds=60,
        )


@pytest.mark.parametrize("config_kind", ["profile", "v2"])
def test_local_upload_freezes_dense_config_and_rejects_sparse_override(repository, tmp_path, config_kind):
    from engine.common.config import VideoParseRuntimeConfig
    from server.config.media import ImageUploadConfig
    from server.config.upload import UploadServiceConfig
    from tests.server.test_upload_service import _upload_service, _VideoParseIndexStore

    defaults = ServerParseConfig.from_defaults(
        runtime=VideoParseRuntimeConfig(), gateway=_local_text_gateway(),
        upload=UploadServiceConfig(), images=ImageUploadConfig(),
        embedding_model=None, embedding_dimensions=1024, embedding_fps=None,
    )
    index_store = _VideoParseIndexStore(1024, 1024)
    index_store.vision_enabled = False
    *_, service = _upload_service(
        repository, tmp_path, parse_defaults=defaults, video_parse_index_store=index_store,
        video_parse_query_encoder_supports_sparse=False,
    )
    config = defaults if config_kind == "profile" else defaults.to_video_parse_v2()
    request = dict(
        tenant_id="local-default", series_id="77285f16-3e5b-592e-ba15-8b224b619806",
        episode_order=7, original_filename="course.mp4", media_type="video/mp4",
        total_size_bytes=10, idempotency_key="local-dense", parse_config=config,
    )
    session = service.create_session(**request)
    assert session.parse_config["models"]["embedding"]["input_mode"] == "text"
    assert session.parse_config["indexing"]["sparse_enabled"] is False
    sparse = config.model_copy(update={"indexing": config.indexing.model_copy(update={"sparse_enabled": True})})
    with pytest.raises(ValueError, match="sparse_enabled is not supported"):
        service.create_session(**{**request, "episode_order": 8, "idempotency_key": "local-sparse", "parse_config": sparse})
    assert len(service.list_sessions(tenant_id="local-default")) == 1


def test_text_mode_and_query_instruction_survive_task_serialization_and_change_contract():
    config = default_video_parse_v2_config()
    original = config.models.embedding
    embedding = original.model_copy(update={"input_mode": "text", "query_instruction": "Find relevant course passages."})
    updated = config.model_copy(update={"models": config.models.model_copy(update={"embedding": embedding})})
    recovered = ParseConfig.model_validate_json(updated.model_dump_json())
    assert recovered.models.embedding == embedding
    assert recovered.ocr_enabled == config.ocr_enabled
    assert recovered.models.understanding == config.models.understanding
    options = dict(provider="ark", encoder_revision="v1", max_height=720)
    fingerprints = {
        index_contract_fingerprint(item, **options) for item in (
            original, original.model_copy(update={"input_mode": "text"}), embedding,
        )
    }
    assert len(fingerprints) == 3


def test_text_index_publishes_chunks_events_and_retries_without_media_or_vision(tmp_path):
    service, repository, chunks, events, embedder, lease, _, commands, _ = _event_setup(
        tmp_path, warm_chunk=False, text_only=True, event_count=2,
    )
    service._media_resolver = Mock(side_effect=AssertionError("text index must not resolve media"))
    events.fail_write = "partial"
    with pytest.raises(TimeoutError):
        service.index_chunk(lease)
    assert not repository.completed
    assert embedder.text_calls == 5  # Chunk + two descriptions + two dialogue variants.
    assert embedder.video_calls == 0 and embedder.image_calls == [] and commands == []
    events.fail_write = None
    assert service.index_chunk(lease) == "complete"
    assert embedder.text_calls == 5  # Lost publication confirmation reuses all encodings.
    assert chunks.bulk_calls[0][0][0].vision_vector is None
    document = next(iter(events.documents.values()))
    assert document.vision_vector is None and document.detail.capture.frames == ()
    assert len(document.semantic_vectors) == 3
    assert service.index_chunk(lease) == "complete"
    assert embedder.text_calls == 5 and commands == []


def test_text_event_query_masks_requested_vision_and_preserves_source_times():
    old, repository, store, embedder, inputs = event_query_fixture()
    contract = EventIndexingContract(semantic_encoder=old._contract.semantic_encoder,
                                    vision_encoder=None, capture=None)
    service = VideoEventRetrievalService(repository, store, embedder, contract=contract,
        policy=RetrievalPolicy(lexical_weight=1, semantic_weight=1, vision_weight=1,
                               channel_top_k=20, candidate_budget=40, top_k=1, rrf_k=60))
    result = service.search("杯子", lexical_weight=1, text_weight=1, vision_weight=10, **inputs)
    assert {channel for channel, _ in store.calls} == {"lexical", "semantic"}
    assert result.trace["channel_weights"]["vision"] == 0
    assert len(embedder.calls) == 1 and result.matches
    assert result.matches[0].start_ms == repository.result.timeline[0].start_seconds


@pytest.mark.parametrize("different", [None, "mode", "instruction", "model"])
def test_text_chunk_query_checks_task_contract_before_encoding(different):
    scope, match, evidence = _v2_retrieval_fixture()
    embedding = scope.parse_config.models.embedding.model_copy(update={"input_mode": "text"})
    if different == "mode":
        embedding = embedding.model_copy(update={"input_mode": "multimodal"})
    elif different == "instruction":
        embedding = embedding.model_copy(update={"query_instruction": "Different task"})
    elif different == "model":
        embedding = embedding.model_copy(update={"model": "other"})
    config = scope.parse_config.model_copy(update={"models": scope.parse_config.models.model_copy(update={"embedding": embedding})})
    repository = _V2Repository(replace(scope, parse_config=config), evidence)
    store = _V2Store([match], [match], [])
    store.vision_enabled = False
    embedder = FakeQueryEmbedder([1., 0.])
    service = VideoParseRetrievalService(repository, store, embedder,
        semantic_encoder=_v2_semantic_encoder(), expected_index_generation=2,
        expected_embedding_input_mode="text")
    if different:
        with pytest.raises(RetrievalIndexConsistencyError):
            service.search("杯子", tenant_id=evidence.tenant_id, series_ids=scope.series_ids)
        assert not embedder.calls and not store.calls
    else:
        result = service.search("杯子", tenant_id=evidence.tenant_id, series_ids=scope.series_ids,
                                lexical_weight=1, text_weight=1, vision_weight=10)
        assert result.hits
        assert {channel for channel, *_ in store.calls} == {"lexical", "semantic"}
        assert embedder.calls == ["杯子"]
