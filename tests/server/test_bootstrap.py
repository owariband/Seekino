from __future__ import annotations

import os
import sys
from dataclasses import replace

import pytest
from engine.parsev1.pipeline import ParsePipeline
from engine.parsev1.runtime import EngineVideoParseRuntime, VideoParseRuntimeConfig
from gateway.config import load_config
from gateway.openai.structured import OpenAIStructuredToolAgentModel

from server import bootstrap
from server.api.app import create_app
from server.config import ServerSettings
from server.config.retrieval import RetrievalConfig
from server.config.agent import AgentServiceConfig
from server.config.media import ImageUploadConfig
from server.config.parse_profile import ParseConfig
from server.config.sse import SSEConfig
from server.config.upload import UploadServiceConfig
from server.database import create_schema
from tests.manual import server_mq_once as mq_main
from server.mq.video_parse_consumer import VideoParseConsumer
from server.service.video_parse import VideoParseRuntimeService
from server.service.video_retrieval import VideoParseRetrievalService
from server.storage.elasticsearch import ElasticsearchVideoChunkStore


class _QdrantClient:
    def __init__(self, **_kwargs):
        self.closed = False

    def close(self):
        self.closed = True


class _ElasticsearchClient:
    def __init__(self, url, *, api_key):
        self.url = url
        self.api_key = api_key
        self.closed = False

    def close(self):
        self.closed = True


def test_parse_defaults_accept_existing_runtime_values_beyond_ui_recommendations() -> None:
    config = ParseConfig.from_defaults(
        runtime=VideoParseRuntimeConfig(
            chunk_duration_seconds=150, context_overlap_seconds=40,
            max_height=3000, asr_concurrency=20,
            max_context_events=150, max_context_chars=150_000,
            request_timeout_seconds=4000,
        ),
        gateway=load_config(),
        upload=UploadServiceConfig(max_job_attempts=12),
        images=ImageUploadConfig(model_max_edge=5000),
        embedding_model="test-embedding", embedding_dimensions=1024,
        embedding_fps=1,
    )
    assert config.version == 2
    assert config.window.analysis_max_height == 3000
    assert config.context.background_image_max_edge == 5000
    assert config.execution.max_attempts == 12


@pytest.mark.parametrize(
    ("configured_models", "expected_models"),
    [
        (
            ("configured-query-model", "configured-review-model", "configured-chat-model"),
            ("configured-query-model", "configured-review-model", "configured-chat-model"),
        ),
        (
            (None, None, None),
            (
                "deepseek-flash",
                "deepseek-flash",
                "deepseek-flash",
            ),
        ),
    ],
)
@pytest.mark.parametrize("with_es", [False, True])
def test_process_assembly_receives_runtime_configuration(
    monkeypatch,
    tmp_path,
    configured_models,
    expected_models,
    with_es,
):
    for name in tuple(os.environ):
        if name.startswith("ANIVAG_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANIVAG_ENVIRONMENT", "test")
    monkeypatch.setenv("ANIVAG_PARSE_EXECUTION_MODE", "v1")
    monkeypatch.setenv(
        "ANIVAG_DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'bootstrap.sqlite'}"
    )
    monkeypatch.setenv("ANIVAG_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("ANIVAG_QDRANT_URL", "http://127.0.0.1:6333")
    monkeypatch.setenv("ANIVAG_QDRANT_COLLECTION", "bootstrap_test")
    if with_es:
        monkeypatch.setenv("ANIVAG_ELASTICSEARCH_URL", "http://127.0.0.1:9200")
        monkeypatch.setenv("ANIVAG_ELASTICSEARCH_INDEX", "bootstrap_test")
        monkeypatch.setenv("ANIVAG_ELASTICSEARCH_API_KEY", "test-key")
    monkeypatch.setenv("ARK_API_KEY", "test-ark-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    settings = replace(
        ServerSettings.from_environment(),
        sse=SSEConfig(poll_interval_seconds=0.5, keepalive_interval_seconds=9),
        retrieval=RetrievalConfig(
            channel_top_k=35, rrf_k=42, text_weight=0.25, vision_weight=1.5
        ),
        database_pool_recycle_seconds=1200,
        video_embedding_model="configured-embedding-model",
        video_embedding_fps=2.0,
        agent_query_model=configured_models[0],
        agent_evaluator_model=configured_models[1],
        agent_chat_model=configured_models[2],
        agent_fallback_model="configured-fallback-model",
        agent_request_timeout_seconds=120,
        images=ImageUploadConfig(maximum_upload_size_bytes=2048, maximum_pixels=1_000_000, model_max_edge=512),
        agent=AgentServiceConfig(
            max_loop_turns=2, max_model_calls_per_turn=4,
            max_tool_calls_per_turn=2, limits_enabled=True,
            query_reasoning_effort="low",
            evaluator_reasoning_effort="high",
            chat_reasoning_effort="max",
            max_output_tokens=2048,
            max_prompt_chars=120_000,
            stage_timeout_seconds=45,
            evaluator_stage_timeout_seconds=210,
            execution_timeout_seconds=90, maximum_concurrent_kicks=2,
        ),
    )
    # Change the environment after loading Settings to prove assembly uses it.
    monkeypatch.setenv("ANIVAG_VIDEO_EMBEDDING_MODEL", "unexpected-env-model")
    monkeypatch.setenv("ANIVAG_VIDEO_EMBEDDING_FPS", "0.5")
    monkeypatch.setenv("ANIVAG_BACKGROUND_IMAGE_MAX_EDGE", "2048")
    monkeypatch.setattr(bootstrap, "QdrantClient", _QdrantClient)
    monkeypatch.setattr(bootstrap, "Elasticsearch", _ElasticsearchClient)
    received = {}
    real_create_engine = bootstrap.create_database_engine

    def create_engine(*args, **kwargs):
        received["pool_recycle_seconds"] = kwargs["pool_recycle_seconds"]
        engine = real_create_engine(*args, **kwargs)
        create_schema(engine)
        return engine

    monkeypatch.setattr(bootstrap, "create_database_engine", create_engine)
    real_retrieval = bootstrap.VideoRetrievalService

    def create_retrieval(*args, **kwargs):
        received["retrieval"] = kwargs
        received["query_embedder"] = args[2]
        return real_retrieval(*args, **kwargs)

    class Embedder:
        def __init__(self, model, **kwargs):
            received["processing_embedding_agent"] = self
            received["processing_embedder"] = {
                "model": model,
                "fps": kwargs["fps"],
                "dimensions": kwargs["output_dimensionality"],
            }

    class Consumer:
        running = False
        failure = None

        def __init__(self, queue, processor, **kwargs):
            received["queue"] = queue
            received["processor"] = processor
            received["consumer"] = kwargs

        def start(self):
            self.running = True

        def close(self):
            self.running = False

    monkeypatch.setattr(bootstrap, "VideoRetrievalService", create_retrieval)
    monkeypatch.setattr(bootstrap, "DoubaoMultimodalEmbedder", Embedder)
    monkeypatch.setattr(bootstrap, "ParseJobConsumer", Consumer)
    services = bootstrap.build_application_services(settings)
    try:
        assert services.media.image_upload_config == settings.images
        assert received["retrieval"] == {
            "channel_top_k": 35,
            "rrf_k": 42,
            "text_weight": 0.25,
            "vision_weight": 1.5,
            "tracing": services.tracing,
        }
        assert services.tracing is services.agent._tracing
        assert services.tracing is services.parse_usage._tracing
        assert services.tracing is services.retrieval._tracing
        assert services.tracing is None
        assert services.agent._parse_usage is services.parse_usage
        assert received["processor"]._parse_usage is None
        embedder = received["query_embedder"]
        assert embedder._model == settings.video_embedding_model
        assert embedder._fps == settings.video_embedding_fps
        assert embedder._request_timeout_seconds == 90
        assert received["processing_embedder"] == {
            "model": settings.video_embedding_model,
            "fps": settings.video_embedding_fps,
            "dimensions": settings.text_vector_dimensions,
        }
        processor = received["processor"]
        assert type(processor).__module__ == "server.service.parsev1.video_processing"
        pipeline = processor._pipeline
        assert isinstance(pipeline, ParsePipeline)
        parse_runtime = pipeline._understand._understanding_agent._runtime
        assert isinstance(parse_runtime, EngineVideoParseRuntime)
        assert parse_runtime._config == settings.parse
        expected_asr_agent = (
            parse_runtime if settings.parse.asr_mode == "lite" else None
        )
        assert pipeline._transcribe._agent is expected_asr_agent
        assert pipeline._embed._agent is received["processing_embedding_agent"]
        assert pipeline._embed._dimensions == settings.text_vector_dimensions
        assert pipeline.models == parse_runtime.describe_models(
            asr_enabled=settings.parse.asr_mode == "lite"
        )
        processing_config = processor._config
        assert processing_config.chunk_duration_seconds == settings.parse.chunk_duration_seconds
        assert processing_config.context_overlap_seconds == settings.parse.context_overlap_seconds
        assert processing_config.analysis_max_height == settings.parse.max_height
        assert processing_config.canonical_max_height == settings.parse.max_height
        assert processing_config.asr_concurrency == settings.parse.asr_concurrency
        assert processing_config.asr_mode == settings.parse.asr_mode
        defaults = services.uploads.parse_defaults
        assert defaults is not None
        assert defaults.embedding.model == settings.video_embedding_model
        selected = defaults.model_copy(update={
            "window": defaults.window.model_copy(update={"analysis_max_height": 480}),
            "vision": defaults.vision.model_copy(update={"fps": 0.5}),
        })
        configured = processor._processor_factory(selected)
        assert configured._config.analysis_max_height == 480
        configured_pipeline = configured._pipeline
        assert isinstance(configured_pipeline, ParsePipeline)
        configured_runtime = (
            configured_pipeline._understand._understanding_agent._runtime
        )
        assert isinstance(configured_runtime, EngineVideoParseRuntime)
        expected_configured_asr_agent = (
            configured_runtime if selected.asr.mode == "lite" else None
        )
        assert (
            configured_pipeline._transcribe._agent
            is expected_configured_asr_agent
        )
        assert configured_runtime._config.video_fps == 0.5
        assert (
            configured_pipeline._embed._agent
            is received["processing_embedding_agent"]
        )
        assert configured_pipeline._embed._dimensions == selected.embedding.dimensions
        assert configured_pipeline.models == configured_runtime.describe_models(
            asr_enabled=selected.asr.mode == "lite"
        )
        assert configured._parse_config == selected
        assert received["consumer"]["worker_id"] == settings.mq.consumer_id
        assert received["consumer"]["poll_interval_seconds"] == settings.mq.poll_interval_seconds
        assert received["consumer"]["lease_duration"] == settings.mq.lease_duration
        runtime = services.agent._runtime
        assert runtime._retrieval_channel == "transcript"
        assert runtime._query_model.model_name == expected_models[0]
        assert runtime._evaluator_model.model_name == expected_models[1]
        assert runtime._chat_model.model_name == expected_models[2]
        assert runtime._query_model.request_timeout_seconds == 90
        assert runtime._evaluator_model.request_timeout_seconds == 90
        assert runtime._chat_model._request_timeout_seconds == 90
        assert isinstance(
            runtime._evaluator_model,
            OpenAIStructuredToolAgentModel,
        )
        assert runtime._query_model.provider_name == "deepseek"
        assert runtime._evaluator_model.provider_name == "deepseek"
        assert runtime._chat_model.provider_name == "deepseek"
        assert runtime._query_model.reasoning_effort == "low"
        assert runtime._evaluator_model.reasoning_effort == "high"
        assert runtime._chat_model.reasoning_effort == "max"
        assert runtime._query_model.fallback_provider_name == "ark"
        assert runtime._evaluator_model.fallback_provider_name == "ark"
        assert runtime._chat_model.fallback_provider_name == "ark"
        assert runtime._query_model.fallback_model_name == "configured-fallback-model"
        assert runtime._evaluator_model.fallback_model_name == "configured-fallback-model"
        assert runtime._chat_model.fallback_model_name == "configured-fallback-model"
        assert runtime._query_model.max_output_tokens == 2048
        assert runtime._query_model.max_input_chars == 120_000
        assert runtime._query_model.context_window_policy.maximum_input_tokens == 57_856
        assert runtime._query_model.context_window_policy.auto_compact_tokens == 48_000
        assert runtime._evaluator_model.context_window_policy is (
            runtime._query_model.context_window_policy
        )
        assert runtime._chat_model.context_window_policy is (
            runtime._query_model.context_window_policy
        )
        assert runtime._chat_model.max_output_tokens == 2048
        assert runtime._evaluator_model.max_output_tokens == 2048
        assert runtime._max_loop_turns == 2
        assert runtime._max_model_calls == 4
        assert runtime._max_tool_calls == 2
        assert runtime._limits_enabled is True
        assert runtime._max_prompt_chars == 120_000
        assert runtime._max_tool_result_bytes == 16_384
        assert runtime._max_read_chars_per_call == 16_384
        assert runtime._max_catalog_items == 20
        assert runtime._stage_timeout == 45
        assert runtime._evaluator_stage_timeout == 210
        assert runtime._execution_timeout == 90
        assert services.agent._executor._max_workers == 2
        updated = replace(
            settings.agent, evaluator_stage_timeout_seconds=240,
            max_tool_result_bytes=8_192, max_read_chars_per_call=4_096,
            query_reasoning_effort="max", evaluator_reasoning_effort="none",
            chat_reasoning_effort="low",
            maximum_concurrent_kicks=5,
        )
        replacement = services.agent._runtime_factory(updated)
        try:
            assert replacement._evaluator_stage_timeout == 240
            assert replacement._max_tool_result_bytes == 8_192
            assert replacement._max_read_chars_per_call == 4_096
            assert replacement._query_model.reasoning_effort == "max"
            assert replacement._evaluator_model.reasoning_effort == "none"
            assert replacement._chat_model.reasoning_effort == "low"
            assert services.agent.active_maximum_concurrent_kicks == 2
        finally:
            replacement.close()
        app = create_app(services, settings=settings)
        assert app.state.sse_config == settings.sse
    finally:
        services.close()
    assert services.qdrant_client.closed
    if with_es:
        assert isinstance(services.elasticsearch_client, _ElasticsearchClient)
        assert services.elasticsearch_client.url == "http://127.0.0.1:9200"
        assert services.elasticsearch_client.api_key == "test-key"
        assert services.elasticsearch_client.closed
    else:
        assert services.elasticsearch_client is None
    assert received["pool_recycle_seconds"] == settings.database_pool_recycle_seconds


@pytest.mark.parametrize("advanced_retrieval", [False, True])
def test_v2_mode_wires_durable_worker_and_elasticsearch(
    monkeypatch,
    tmp_path,
    advanced_retrieval,
):
    for name in tuple(os.environ):
        if name.startswith("ANIVAG_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANIVAG_ENVIRONMENT", "test")
    monkeypatch.setenv(
        "ANIVAG_DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'v2-bootstrap.sqlite'}"
    )
    monkeypatch.setenv("ANIVAG_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("ANIVAG_QDRANT_URL", "http://127.0.0.1:6333")
    monkeypatch.setenv("ANIVAG_QDRANT_COLLECTION", "bootstrap_test")
    monkeypatch.setenv("ANIVAG_ELASTICSEARCH_URL", "http://127.0.0.1:9200")
    monkeypatch.setenv("ANIVAG_ELASTICSEARCH_INDEX", "bootstrap_test")
    monkeypatch.setenv("ANIVAG_ELASTICSEARCH_INDEX_GENERATION", "7")
    monkeypatch.setenv("ANIVAG_PARSE_EXECUTION_MODE", "v2")
    monkeypatch.setenv("ARK_API_KEY", "test-ark-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    settings = ServerSettings.from_environment()
    if advanced_retrieval:
        settings = replace(settings, retrieval=RetrievalConfig(
            candidate_budget=80, sparse_weight=0.25, rerank_enabled=True,
        ))

    real_defaults = bootstrap.ParseConfig.from_defaults
    def selected_defaults(**kwargs):
        defaults = real_defaults(**kwargs)
        return defaults.model_copy(update={"embedding": defaults.embedding.model_copy(update={
            "model": "selected-task-embedding", "dimensions": 512, "video_fps": 2,
        })})
    monkeypatch.setattr(bootstrap.ParseConfig, "from_defaults", selected_defaults)
    received = {}
    real_retrieval = bootstrap.VideoParseRetrievalService
    def create_retrieval(*args, **kwargs):
        received.update(kwargs)
        return real_retrieval(*args, **kwargs)
    monkeypatch.setattr(bootstrap, "VideoParseRetrievalService", create_retrieval)

    monkeypatch.setattr(bootstrap, "QdrantClient", _QdrantClient)
    monkeypatch.setattr(bootstrap, "Elasticsearch", _ElasticsearchClient)
    real_create_engine = bootstrap.create_database_engine

    def create_engine(*args, **kwargs):
        engine = real_create_engine(*args, **kwargs)
        create_schema(engine)
        return engine

    monkeypatch.setattr(bootstrap, "create_database_engine", create_engine)

    def forbidden_v1_runtime(*_args, **_kwargs):
        raise AssertionError("V2 mode must not construct the V1 parse runtime")

    def forbidden_v1_consumer(*_args, **_kwargs):
        raise AssertionError("V2 mode must not construct the V1 parse consumer")

    monkeypatch.setattr(bootstrap, "EngineVideoParseRuntime", forbidden_v1_runtime)
    monkeypatch.setattr(bootstrap, "ParseJobConsumer", forbidden_v1_consumer)

    services = bootstrap.build_application_services(settings)
    try:
        consumer = services.parse_consumer
        assert isinstance(consumer, VideoParseConsumer)
        runtime = consumer._handlers.prepare.__self__
        assert isinstance(runtime, VideoParseRuntimeService)
        assert services.tracing is None
        assert runtime._parse_usage is services.parse_usage
        assert services.agent._parse_usage is services.parse_usage
        assert runtime._tracing is services.tracing
        assert services.parse_usage._tracing is services.tracing
        assert services.agent._tracing is services.tracing
        assert services.retrieval._tracing is services.tracing
        assert consumer._queue is runtime._queue
        assert consumer._handlers.understand.__self__ is runtime
        assert consumer._handlers.refine.__self__ is runtime
        assert consumer._handlers.index.__self__ is runtime
        assert consumer._handlers.prefix.__self__ is runtime
        assert consumer._handlers.maintenance.__self__ is runtime
        assert consumer._maintenance_interval == settings.mq.lease_duration
        assert isinstance(runtime._elasticsearch_store, ElasticsearchVideoChunkStore)
        assert runtime._elasticsearch_store._client is services.elasticsearch_client
        assert isinstance(services.retrieval, VideoParseRetrievalService)
        assert services.retrieval._embedder._model == "selected-task-embedding"
        assert services.retrieval._embedder._dimensions == 512
        assert services.retrieval._embedder._fps == 2
        assert isinstance(services.retrieval._embedder._source, bootstrap.ArkModelSourceConfig)
        assert received["candidate_budget"] == settings.retrieval.candidate_budget
        assert received["sparse_weight"] == settings.retrieval.sparse_weight
        assert received["expected_embedding_source"] == services.uploads.parse_defaults.embedding.source
        assert runtime._sparse_enabled is advanced_retrieval
        if advanced_retrieval:
            assert isinstance(received["reranker"], bootstrap.StructuredTextReranker)
            assert received["reranker"].model_name == load_config().agent_model.model
        else:
            assert received["reranker"] is None
        assert services.retrieval._store is runtime._elasticsearch_store
        assert services.retrieval._repository is runtime._repository
        assert services.retrieval._expected_index_generation == 7
        assert services.retrieval._semantic_encoder.claims() == {
            "provider": "ark",
            "model": services.uploads.parse_defaults.embedding.model,
            "encoder_revision": "gateway-ark-embedding-v1",
            "dimensions": 512,
            "normalization": "none",
            "output_contract": "float-vector-v1",
        }
        assert (
            services.uploads._video_parse_query_encoder
            is services.retrieval._semantic_encoder
        )
        assert services.uploads._video_parse_query_encoder_source == (
            services.uploads.parse_defaults.embedding.source
        )
        assert runtime._index_generation == 7
        assert services.agent._runtime._retrieval_channel == "lexical"
        assert services.uploads._video_parse_index_store is runtime._elasticsearch_store
        assert runtime._media_service.base_directory == (
            tmp_path / "artifacts" / "test" / "work" / "video-parse-v2"
        )
    finally:
        services.close()


def test_v2_mode_requires_elasticsearch_before_allocating_services(
    monkeypatch,
    tmp_path,
):
    for name in tuple(os.environ):
        if name.startswith("ANIVAG_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANIVAG_ENVIRONMENT", "test")
    monkeypatch.setenv(
        "ANIVAG_DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'missing-es.sqlite'}"
    )
    monkeypatch.setenv("ANIVAG_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("ANIVAG_QDRANT_URL", "http://127.0.0.1:6333")
    monkeypatch.setenv("ANIVAG_QDRANT_COLLECTION", "bootstrap_test")
    monkeypatch.setenv("ANIVAG_PARSE_EXECUTION_MODE", "v2")
    settings = ServerSettings.from_environment()

    with pytest.raises(ValueError, match="Video Parse V2 requires"):
        bootstrap.build_application_services(settings)


def test_mq_once_reuses_application_bootstrap(monkeypatch):
    from types import SimpleNamespace

    events = []
    settings = SimpleNamespace(environment="test", logging=object())
    services = SimpleNamespace(
        parse_consumer=SimpleNamespace(
            run_once=lambda: events.append("run_once"),
        ),
        close=lambda: events.append("close"),
    )
    monkeypatch.setattr(
        mq_main.ServerSettings,
        "from_environment",
        classmethod(lambda _cls: settings),
    )
    monkeypatch.setattr(
        mq_main,
        "configure_logging",
        lambda process, **kwargs: events.append(
            (process, kwargs["environment"], kwargs["config"])
        ),
    )
    monkeypatch.setattr(
        mq_main,
        "build_application_services",
        lambda received: services if received is settings else None,
    )
    monkeypatch.setattr(sys, "argv", ["python -m tests.manual.server_mq_once", "--once"])

    mq_main.main()

    assert events == [("mq", "test", settings.logging), "run_once", "close"]


def test_mq_once_uses_v2_synchronous_diagnostic_entrypoint(monkeypatch):
    from types import SimpleNamespace

    events = []
    settings = SimpleNamespace(environment="test", logging=object())
    services = SimpleNamespace(
        parse_consumer=SimpleNamespace(
            run_once_sync=lambda: events.append("run_once_sync"),
            run_once=lambda: events.append("unexpected_async_tick"),
        ),
        close=lambda: events.append("close"),
    )
    monkeypatch.setattr(
        mq_main.ServerSettings,
        "from_environment",
        classmethod(lambda _cls: settings),
    )
    monkeypatch.setattr(
        mq_main,
        "configure_logging",
        lambda process, **kwargs: events.append(
            (process, kwargs["environment"], kwargs["config"])
        ),
    )
    monkeypatch.setattr(
        mq_main,
        "build_application_services",
        lambda received: services if received is settings else None,
    )
    monkeypatch.setattr(sys, "argv", ["python -m tests.manual.server_mq_once", "--once"])

    mq_main.main()

    assert events == [
        ("mq", "test", settings.logging),
        "run_once_sync",
        "close",
    ]


def test_mq_entrypoint_rejects_persistent_process_mode(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["python -m tests.manual.server_mq_once"])

    with pytest.raises(SystemExit) as error:
        mq_main.main()

    assert error.value.code == 2


def test_application_stops_consumer_before_agent_database_and_vector_store():
    from server.bootstrap import ApplicationServices
    from types import SimpleNamespace
    order = []
    services = ApplicationServices(
        library=None, uploads=None, jobs=None, media=None, retrieval=None,
        agent=SimpleNamespace(close=lambda: order.append("agent")), index_sync=None,
        parse_consumer=SimpleNamespace(close=lambda: order.append("consumer")),
        agent_model_factory=SimpleNamespace(close=lambda: order.append("model_factory")),
        tracing=SimpleNamespace(close=lambda: order.append("tracing")),
        parse_usage=SimpleNamespace(close=lambda: order.append("usage_reader")),
        qdrant_client=SimpleNamespace(close=lambda: order.append("vectors")),
        elasticsearch_client=SimpleNamespace(close=lambda: order.append("es")),
        engine=SimpleNamespace(dispose=lambda: order.append("database")),
    )
    services.close()
    assert order == ["consumer", "agent", "model_factory", "tracing", "usage_reader", "es", "vectors", "database"]


@pytest.mark.parametrize("failure", ["consumer", "agent"])
def test_application_closes_tracing_after_model_teardown_even_if_host_close_fails(failure):
    from types import SimpleNamespace

    order = []

    def close_agent():
        order.append("agent")
        if failure == "agent":
            raise RuntimeError("agent close failed")

    def close_consumer():
        order.append("consumer")
        if failure == "consumer":
            raise RuntimeError("consumer close failed")

    services = bootstrap.ApplicationServices(
        library=None, uploads=None, jobs=None, media=None, retrieval=None,
        agent=SimpleNamespace(close=close_agent), index_sync=None,
        parse_consumer=SimpleNamespace(close=close_consumer),
        agent_model_factory=SimpleNamespace(close=lambda: order.append("model_factory")),
        tracing=SimpleNamespace(close=lambda: order.append("tracing")),
        parse_usage=SimpleNamespace(close=lambda: order.append("usage_reader")),
        qdrant_client=SimpleNamespace(close=lambda: order.append("vectors")),
        elasticsearch_client=SimpleNamespace(close=lambda: order.append("es")),
        engine=SimpleNamespace(dispose=lambda: order.append("database")),
    )
    with pytest.raises(RuntimeError, match=f"{failure} close failed"):
        services.close()
    assert order == ["consumer", "agent", "model_factory", "tracing", "usage_reader", "es", "vectors", "database"]


@pytest.mark.parametrize("method", ["embed_query", "embed_image", "embed_sparse"])
def test_query_embedder_uses_a_client_per_call_and_always_closes(monkeypatch, method):
    from types import SimpleNamespace
    clients = []
    def create_client(**kwargs):
        client = SimpleNamespace(options=kwargs, closed=False)
        def close():
            client.closed = True
        client.close = close
        clients.append(client)
        return client
    class Embedder:
        def __init__(self, _model, **kwargs):
            self.client = kwargs["client"]
        def execute(self, query):
            if query == "fail":
                raise RuntimeError("embedding failed")
            return self.client
        embed_query = execute
        embed_image = execute
        embed_sparse = execute
    monkeypatch.setattr(bootstrap, "Ark", SimpleNamespace(volc=create_client))
    monkeypatch.setattr(bootstrap, "DoubaoMultimodalEmbedder", Embedder)
    embedder = bootstrap._LazyQueryEmbedder(model="embedding", dimensions=1024, fps=1, request_timeout_seconds=30)
    assert clients == []
    invoke = getattr(embedder, method)
    assert invoke("first") is not invoke("second")
    with pytest.raises(RuntimeError, match="embedding failed"):
        invoke("fail")
    assert len(clients) == 3 and all(client.closed for client in clients)
    assert all(client.options == {"timeout": 30, "max_retries": 0} for client in clients)


def test_query_embedder_uses_the_selected_ark_source_key(monkeypatch):
    from types import SimpleNamespace
    received = {}
    monkeypatch.setenv("SELECTED_EMBEDDING_KEY", "selected-key")
    class Client:
        def close(self):
            received["closed"] = True
    def create_client(**kwargs):
        received["options"] = kwargs
        return Client()
    class Embedder:
        def __init__(self, _model, **kwargs):
            received["delegate"] = kwargs
        def embed_sparse(self, text):
            return text
    monkeypatch.setattr(bootstrap, "Ark", SimpleNamespace(volc=create_client))
    monkeypatch.setattr(bootstrap, "DoubaoMultimodalEmbedder", Embedder)
    embedder = bootstrap._LazyQueryEmbedder(
        model="embedding", dimensions=512, fps=2, request_timeout_seconds=30,
        source=bootstrap.ArkModelSourceConfig(api_key_env="SELECTED_EMBEDDING_KEY"),
    )
    assert embedder.embed_sparse("query") == "query"
    assert received["options"] == {"timeout": 30, "max_retries": 0, "api_key": "selected-key"}
    assert received["closed"] is True


@pytest.mark.parametrize("fails", [False, True])
def test_text_reranker_uses_one_selected_model_and_closes_each_call(monkeypatch, fails):
    from pydantic import BaseModel
    from gateway.multimodal import TextInput
    events = []
    monkeypatch.setenv("SELECTED_RERANK_KEY", "selected-key")
    class Output(BaseModel):
        score: int
    class Model:
        def __init__(self, model, **kwargs):
            events.append(("open", model, kwargs))
        def generate(self, inputs, *, schema_name, result_type):
            assert inputs == [TextInput("candidate")]
            assert schema_name == "rerank" and result_type is Output
            if fails:
                raise RuntimeError("scorer failed")
            return Output(score=1)
        def close(self):
            events.append(("close",))
    monkeypatch.setattr(bootstrap, "DeepSeekMultimodalModel", Model)
    delegate = bootstrap._LazyTextRerankModel(
        source=bootstrap.OpenAIModelSourceConfig(
            provider="deepseek", profile="deepseek", base_url="https://api.deepseek.com",
            api_key_env="SELECTED_RERANK_KEY",
        ),
        model="selected-scorer", max_output_tokens=512, request_timeout_seconds=30,
    )
    if fails:
        with pytest.raises(RuntimeError, match="scorer failed"):
            delegate.generate([TextInput("candidate")], schema_name="rerank", result_type=Output)
    else:
        assert delegate.generate(
            [TextInput("candidate")], schema_name="rerank", result_type=Output,
        ).score == 1
    assert events == [("open", "selected-scorer", {
        "base_url": "https://api.deepseek.com", "api_key": "selected-key",
        "max_output_tokens": 512, "request_timeout_seconds": 30,
    }), ("close",)]


@pytest.mark.parametrize("option", ["sparse", "rerank", "unsupported_source"])
def test_unsupported_retrieval_options_fail_before_allocating_services(monkeypatch, option):
    from types import SimpleNamespace
    config = load_config()
    if option == "unsupported_source":
        config = config.model_copy(update={"agent_model": config.agent_model.model_copy(update={
            "source": "ark_default",
        })})
    monkeypatch.setattr(bootstrap, "load_config", lambda _path: config)
    def forbidden_database(*args, **kwargs):
        raise AssertionError("invalid retrieval must fail before allocating services")
    monkeypatch.setattr(bootstrap, "create_database_engine", forbidden_database)
    settings = SimpleNamespace(
        model_config_path=None,
        parse_execution_mode="v2" if option == "unsupported_source" else "v1",
        elasticsearch=object() if option == "unsupported_source" else None,
        retrieval=RetrievalConfig(
            sparse_weight=0.5 if option == "sparse" else 0,
            rerank_enabled=option != "sparse",
        ),
    )
    with pytest.raises(ValueError, match="requires a DeepSeek" if option == "unsupported_source" else "require Video Parse V2"):
        bootstrap.build_application_services(settings)


@pytest.mark.parametrize("injected", [False, True])
def test_app_lifespan_closes_only_services_it_owns(monkeypatch, injected):
    from fastapi.testclient import TestClient
    from types import SimpleNamespace
    import server.api.app as app_module
    lifecycle = []
    services = SimpleNamespace(
        start=lambda: lifecycle.append("start"),
        close=lambda: lifecycle.append("close"),
    )
    settings = SimpleNamespace(sse=SSEConfig(), allowed_origins=())
    monkeypatch.setattr(app_module, "build_application_services", lambda _settings: services)
    app = create_app(services if injected else None, settings=settings)
    with TestClient(app):
        pass
    assert lifecycle == ([] if injected else ["start", "close"])
