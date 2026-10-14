from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from agent.video_parse_v2.input import ParseConfig as VideoParseV2Config, ParseInput
from engine.common.config import VideoParseRuntimeConfig
from engine.rag.nlp.query import accept_query_vector
from engine.rag.retrieval.contracts import QueryEncodingRequest
from gateway.config import (
    ArkModelSourceConfig, EmbeddingModelBinding, ModelBinding,
    OpenAIAudioSourceConfig, OpenAIModelSourceConfig, load_config,
)
from gateway.rerank import RerankScore

from server import bootstrap, main as main_module
from server.caller import CallerResources
from server.caller import elasticsearch as elasticsearch_caller, mysql as mysql_caller
from server.common import retrieval as retrieval_builder
from server.api.app import create_app
from server.config import ServerSettings
from server.config.elasticsearch import ElasticsearchConfig
from server.config.retrieval import RetrievalConfig
from server.config.agent import AgentServiceConfig
from server.config.auth import AuthConfig
from server.config.media import ImageUploadConfig
from server.config.parse_profile import ParseConfig
from server.config.sse import SSEConfig
from server.config.upload import UploadServiceConfig
from server.database import DatabaseEnvironment, create_schema
from tests.manual import server_mq_once as mq_main
from server.mq.video_parse_consumer import VideoParseConsumer
from server.service.video_parse import VideoParseRuntimeService
from server.service.retrieval_rerank import StructuredTextReranker
from server.service.video_retrieval import VideoParseRetrievalService
from server.storage.elasticsearch import ElasticsearchVideoChunkStore


@pytest.mark.parametrize("module_name", [
    "server.main", "server.bootstrap", "server.api.app", "server.cli", "scripts.local_services",
    "server.common.storage", "server.common.retrieval",
    "server.caller", "server.caller.base", "server.caller.mysql",
    "server.caller.elasticsearch", "server.caller.redis",
])
def test_formal_entrypoints_import_without_qdrant_or_v1_runtime(module_name):
    runtime_blocks = (
        "agent.chat.video_search.runtime", "agent.chat.supervisor",
        "agent.video_parse_v2.runtime", "langgraph",
    ) if module_name in {
        "server.cli", "server.common.storage", "server.common.retrieval",
        "server.caller", "server.caller.base", "server.caller.mysql",
        "server.caller.elasticsearch", "server.caller.redis",
    } else ()
    business_blocks = ("server.bootstrap",) if module_name.startswith("server.caller") else ()
    script = f"""
import importlib
import importlib.abc
import sys

blocked = (
    'qdrant_client', 'server.storage.qdrant', 'engine.parsev1.runtime',
    'engine.parsev1.pipeline', 'server.service.parsev1.video_processing',
    'server.service.parsev1.index_sync', 'server.mq.consumer',
) + {runtime_blocks!r} + {business_blocks!r}

class RejectLegacyImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + '.') for name in blocked):
            raise AssertionError('formal entrypoint imported ' + fullname)
        return None

sys.meta_path.insert(0, RejectLegacyImports())
importlib.import_module({module_name!r})
assert not any(
    name == prefix or name.startswith(prefix + '.')
    for name in sys.modules for prefix in blocked
)
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script], capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


class _ElasticsearchClient:
    def __init__(self, url, *, api_key):
        self.url = url
        self.api_key = api_key
        self.closed = False

    def close(self):
        self.closed = True


def test_database_construction_disposes_engine_when_session_factory_fails(monkeypatch):
    closed = []
    engine = SimpleNamespace(dispose=lambda: closed.append("database"))
    monkeypatch.setattr(mysql_caller, "create_database_engine", lambda *_a, **_k: engine)

    def fail(_engine):
        raise RuntimeError("session setup failed")

    monkeypatch.setattr(mysql_caller, "create_session_factory", fail)
    with pytest.raises(RuntimeError, match="session setup failed"):
        mysql_caller.build_database_storage(
            "unused", environment=DatabaseEnvironment.TEST, pool_recycle_seconds=1200,
        )
    assert closed == ["database"]


@pytest.mark.parametrize(("mode", "failure"), [
    ("local", "elasticsearch"), ("cloud", "elasticsearch"),
    ("cloud", "redis"), ("cloud", "identity"),
])
def test_application_construction_closes_allocated_clients_when_setup_fails(
    monkeypatch, tmp_path, mode, failure,
):
    from redis.exceptions import ConnectionError as RedisConnectionError
    from server.caller import redis as redis_caller
    for name in tuple(os.environ):
        if name.startswith("ANIVAG_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANIVAG_ENVIRONMENT", "test")
    monkeypatch.setenv("ANIVAG_DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("ANIVAG_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("ANIVAG_ELASTICSEARCH_URL", "http://127.0.0.1:9200")
    monkeypatch.setenv("ANIVAG_ELASTICSEARCH_INDEX", "failure_test")
    monkeypatch.setenv("ARK_API_KEY", "test-ark-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    if mode == "cloud":
        monkeypatch.setenv("ANIVAG_AUTH_MODE", "cloud")
        monkeypatch.setenv("ANIVAG_AUTH_SECRET_KEY", "x" * 32)
        monkeypatch.setenv("ANIVAG_AUTH_REDIS_URL", "redis://127.0.0.1:16379/15")
        monkeypatch.setenv("ANIVAG_SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("ANIVAG_SMTP_SENDER", "noreply@example.com")
    settings = ServerSettings.from_environment()
    settings = replace(settings, agent=replace(
        settings.agent, total_timeout_enabled=True, execution_timeout_seconds=480,
    ))
    closed = []
    engine = SimpleNamespace(dispose=lambda: closed.append("database"))
    monkeypatch.setattr(mysql_caller, "create_database_engine", lambda *_a, **_k: engine)
    monkeypatch.setattr(mysql_caller, "create_session_factory", lambda _engine: object())
    def ping():
        if failure == "redis":
            raise RedisConnectionError("connection failed")
        return True

    redis_client = SimpleNamespace(ping=ping, close=lambda: closed.append("redis"))
    monkeypatch.setattr(redis_caller.Redis, "from_url", lambda *_a, **_k: redis_client)

    def bootstrap_identity(_self):
        if failure == "identity":
            raise RuntimeError("identity setup failed")

    monkeypatch.setattr(bootstrap.AuthService, "bootstrap", bootstrap_identity)
    client = SimpleNamespace(close=lambda: closed.append("elasticsearch"))
    monkeypatch.setattr(elasticsearch_caller, "Elasticsearch", lambda *_a, **_k: client)

    real_store = elasticsearch_caller.ElasticsearchVideoChunkStore
    def build_store(*args, **kwargs):
        if failure == "elasticsearch":
            raise RuntimeError("store setup failed")
        return real_store(*args, **kwargs)

    monkeypatch.setattr(elasticsearch_caller, "ElasticsearchVideoChunkStore", build_store)
    message = {"elasticsearch": "store setup failed", "redis": "Redis is unavailable", "identity": "identity setup failed"}[failure]
    with pytest.raises(RuntimeError, match=message):
        main_module.init_application(settings)
    assert closed == (["elasticsearch"] if failure != "redis" else []) + (["redis"] if mode == "cloud" else []) + ["database"]


@pytest.mark.parametrize(("agent_config", "message"), [
    (AgentServiceConfig(total_timeout_enabled=False), "requires total_timeout_enabled"),
    (AgentServiceConfig(execution_timeout_seconds=600), "shorter than the query lease"),
])
def test_cloud_timeout_validation_runs_before_creating_external_clients(agent_config, message):
    settings = SimpleNamespace(
        parse_execution_mode="v2", elasticsearch=object(), agent=agent_config,
        auth=SimpleNamespace(mode="cloud", query_lease_seconds=600),
    )
    with pytest.raises(ValueError, match=message):
        bootstrap.prepare_service_configuration(settings)


@pytest.mark.parametrize("kind", ["database", "elasticsearch"])
@pytest.mark.parametrize("fails", [False, True])
def test_storage_context_closes_its_resource_after_command(monkeypatch, kind, fails):
    closed = []
    selected = object()
    if kind == "database":
        engine = SimpleNamespace(dispose=lambda: closed.append(kind))
        monkeypatch.setattr(mysql_caller, "create_database_engine", lambda *_a, **_k: engine)
        monkeypatch.setattr(mysql_caller, "create_session_factory", lambda _engine: selected)
        context = mysql_caller.open_database_storage(
            "unused", environment=DatabaseEnvironment.TEST, pool_recycle_seconds=1200,
        )
    else:
        client = SimpleNamespace(close=lambda: closed.append(kind))
        monkeypatch.setattr(elasticsearch_caller, "Elasticsearch", lambda *_a, **_k: client)
        monkeypatch.setattr(elasticsearch_caller, "ElasticsearchVideoChunkStore", lambda *_a, **_k: selected)
        context = elasticsearch_caller.open_elasticsearch_storage(
            ElasticsearchConfig(url="http://127.0.0.1:9200", index_name="context_test"),
            environment=DatabaseEnvironment.TEST, semantic_dimensions=2, vision_dimensions=2,
        )

    def execute():
        with context as (_resource, value):
            assert value is selected
            assert closed == []
            if fails:
                raise RuntimeError("command failed")

    if fails:
        with pytest.raises(RuntimeError, match="command failed"):
            execute()
    else:
        execute()
    assert closed == [kind]


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


def test_parse_execution_settings_reach_the_frozen_task(monkeypatch, tmp_path):
    monkeypatch.setenv("ANIVAG_ENVIRONMENT", "test")
    monkeypatch.setenv("ANIVAG_DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'config.sqlite'}")
    monkeypatch.setenv("ANIVAG_ELASTICSEARCH_URL", "http://127.0.0.1:9200")
    monkeypatch.setenv("ANIVAG_ELASTICSEARCH_INDEX", "config_test")
    monkeypatch.setenv("ANIVAG_PARSE_BATCH_MAX_SIZE", "7")
    monkeypatch.setenv("ANIVAG_PARSE_DISPATCH_INTERVAL_SECONDS", "0.25")
    monkeypatch.setenv("ANIVAG_MQ_RETRY_BASE_SECONDS", "9")
    monkeypatch.setenv("ANIVAG_MQ_RETRY_MAX_SECONDS", "81")

    settings = ServerSettings.from_environment()
    _gateway, profile = bootstrap.prepare_service_configuration(settings)
    frozen = profile.to_video_parse_v2()

    assert frozen.batch_max_size == 7
    assert frozen.dispatch_interval_seconds == 0.25
    assert frozen.execution.retry_initial_delay_seconds == 9
    assert frozen.execution.retry_max_delay_seconds == 81


@pytest.mark.parametrize("supervisor_backend", ["legacy", "deepagents", "codex"])
@pytest.mark.parametrize(
    ("total_timeout_enabled", "global_timeout", "supervisor_timeout", "execution_timeout", "expected_agent_timeout", "expected_independent_timeout", "expected_retrieval_timeout"),
    [
        pytest.param(True, 120, 75, 90, 90, 75, 90, id="existing-runtime-controls"),
        pytest.param(True, None, 600, 480, 180, 480, 480, id="config-budget-without-global-override"),
        pytest.param(True, None, 75, 480, 180, 75, 480, id="smaller-config-model-budget"),
        pytest.param(True, 60, 600, 480, 60, 60, 60, id="explicit-smaller-global-override"),
        pytest.param(False, None, 600, 480, 180, 600, 480, id="no-total-deadline-keeps-model-budget"),
        pytest.param(False, 60, 600, 480, 60, 60, 60, id="no-total-deadline-keeps-request-override"),
    ],
)
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
def test_process_assembly_receives_runtime_configuration(
    monkeypatch,
    tmp_path,
    configured_models,
    expected_models,
    supervisor_backend,
    total_timeout_enabled,
    global_timeout,
    supervisor_timeout,
    execution_timeout,
    expected_agent_timeout,
    expected_independent_timeout,
    expected_retrieval_timeout,
):
    for name in tuple(os.environ):
        if name.startswith("ANIVAG_"):
            monkeypatch.delenv(name, raising=False)
    if global_timeout is not None:
        monkeypatch.setenv("ANIVAG_AGENT_REQUEST_TIMEOUT_SECONDS", str(global_timeout))
    monkeypatch.setenv("ANIVAG_ENVIRONMENT", "test")
    monkeypatch.setenv("ANIVAG_PARSE_EXECUTION_MODE", "v2")
    monkeypatch.setenv(
        "ANIVAG_DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'bootstrap.sqlite'}"
    )
    monkeypatch.setenv("ANIVAG_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("ANIVAG_ELASTICSEARCH_URL", "http://127.0.0.1:9200")
    monkeypatch.setenv("ANIVAG_ELASTICSEARCH_INDEX", "bootstrap_test")
    monkeypatch.setenv("ANIVAG_ELASTICSEARCH_API_KEY", "test-key")
    monkeypatch.setenv("ARK_API_KEY", "test-ark-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    configuration = yaml.safe_load((Path(__file__).resolve().parents[2] / "config.yaml").read_text())
    configuration["models"]["agent"] = {
        "source": "deepseek_api", "model": "deepseek-flash",
        "max_output_tokens": 8192, "request_timeout_seconds": 180,
        "fallback": {"source": "ark_default", "model": "doubao-seed-2-0-lite-260428"},
    }
    if supervisor_backend == "legacy":
        configuration.pop("supervisor_harness", None)
        configuration["models"].pop("supervisor", None)
    else:
        configuration["supervisor_harness"] = supervisor_backend
        configuration["models"]["supervisor"] = {
            "source": "codex_local" if supervisor_backend == "codex" else "deepseek_api",
            "model": "independent-supervisor-model",
            "supports_vision_input": supervisor_backend == "codex",
            "reasoning_effort": "low",
            "request_timeout_seconds": supervisor_timeout,
            "max_output_tokens": 1024,
            "max_input_chars": 80_000,
            "context_window_tokens": 32_000,
            "context_safety_margin_tokens": 2048,
            "auto_compact_tokens": 22_000,
            "compact_target_tokens": 12_000,
        }
    config_path = tmp_path / "models.yaml"
    config_path.write_text(yaml.safe_dump(configuration, allow_unicode=True))
    monkeypatch.setenv("ANIVAG_MODEL_CONFIG", str(config_path))
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
        images=ImageUploadConfig(maximum_upload_size_bytes=2048, maximum_pixels=1_000_000, model_max_edge=512),
        agent=AgentServiceConfig(
            max_loop_turns=2, max_model_calls_per_turn=4,
            max_tool_calls_per_turn=2, limits_enabled=True,
            total_timeout_enabled=total_timeout_enabled,
            query_reasoning_effort="low",
            evaluator_reasoning_effort="high",
            chat_reasoning_effort="max",
            supervisor_reasoning_effort="high",
            max_output_tokens=2048,
            max_prompt_chars=120_000,
            stage_timeout_seconds=45,
            evaluator_stage_timeout_seconds=210,
            execution_timeout_seconds=execution_timeout, maximum_concurrent_kicks=2,
        ),
    )
    assert settings.agent_request_timeout_seconds == global_timeout
    # Change the environment after loading Settings to prove assembly uses it.
    monkeypatch.setenv("ANIVAG_VIDEO_EMBEDDING_MODEL", "unexpected-env-model")
    monkeypatch.setenv("ANIVAG_VIDEO_EMBEDDING_FPS", "0.5")
    monkeypatch.setenv("ANIVAG_BACKGROUND_IMAGE_MAX_EDGE", "2048")
    monkeypatch.setattr(elasticsearch_caller, "Elasticsearch", _ElasticsearchClient)
    received = {}
    real_create_engine = mysql_caller.create_database_engine

    def create_engine(*args, **kwargs):
        received["pool_recycle_seconds"] = kwargs["pool_recycle_seconds"]
        engine = real_create_engine(*args, **kwargs)
        create_schema(engine)
        return engine

    monkeypatch.setattr(mysql_caller, "create_database_engine", create_engine)
    real_retrieval = retrieval_builder.VideoParseRetrievalService

    def create_retrieval(*args, **kwargs):
        received["retrieval"] = kwargs
        received["query_embedder"] = args[2]
        return real_retrieval(*args, **kwargs)

    monkeypatch.setattr(retrieval_builder, "VideoParseRetrievalService", create_retrieval)
    supervisor_builds = []
    real_build_supervisor = bootstrap.AgentModelFactory.build_supervisor

    def build_supervisor(factory, **kwargs):
        supervisor_builds.append({**kwargs, "binding": factory._binding})
        return real_build_supervisor(factory, **kwargs)

    monkeypatch.setattr(bootstrap.AgentModelFactory, "build_supervisor", build_supervisor)
    application = main_module.init_application(settings)
    services = application.services
    try:
        assert services.media.image_upload_config == settings.images
        assert received["retrieval"]["channel_top_k"] == 35
        assert received["retrieval"]["rrf_k"] == 42
        assert received["retrieval"]["text_weight"] == 0.25
        assert received["retrieval"]["vision_weight"] == 1.5
        assert received["retrieval"]["tracing"] is services.tracing
        assert services.tracing is services.agent._tracing
        assert services.tracing is services.parse_usage._tracing
        assert services.tracing is services.retrieval._tracing
        assert services.tracing is None
        assert services.agent._parse_usage is services.parse_usage
        embedder = received["query_embedder"]
        assert embedder._binding.model == settings.video_embedding_model
        assert embedder._binding.video_fps == settings.video_embedding_fps
        assert embedder._binding.request_timeout_seconds == expected_retrieval_timeout
        defaults = services.uploads.parse_defaults
        assert defaults is not None
        assert defaults.embedding.model == settings.video_embedding_model
        assert isinstance(services.parse_consumer, VideoParseConsumer)
        worker = services.parse_consumer._handlers.prepare.__self__
        assert worker._parse_usage is services.parse_usage
        assert services.parse_consumer._worker_id == settings.mq.consumer_id
        assert services.parse_consumer._poll_interval.total_seconds() == settings.mq.poll_interval_seconds
        assert services.parse_consumer._lease_duration == settings.mq.lease_duration
        runtime = services.agent._runtime
        assert runtime._retrieval_channel == "lexical"
        assert runtime._query_model.model_name == expected_models[0]
        assert runtime._evaluator_model.model_name == expected_models[1]
        assert runtime._chat_model.model_name == expected_models[2]
        assert runtime._query_model.request_timeout_seconds == expected_agent_timeout
        assert runtime._evaluator_model.request_timeout_seconds == expected_agent_timeout
        assert runtime._chat_model._request_timeout_seconds == expected_agent_timeout
        assert callable(runtime._evaluator_model.run_structured)
        assert runtime._query_model.provider_name == "deepseek"
        assert runtime._evaluator_model.provider_name == "deepseek"
        assert runtime._chat_model.provider_name == "deepseek"
        assert runtime._query_model.reasoning_effort == "low"
        assert runtime._evaluator_model.reasoning_effort == "high"
        assert runtime._chat_model.reasoning_effort == "max"
        supervisor_model = services.agent._supervisor_runtime._model
        assert callable(supervisor_model.run_agent)
        assert callable(supervisor_model.confirm_turn)
        assert callable(supervisor_model.discard_turn)
        assert supervisor_model.model_name == (
            expected_models[2] if supervisor_backend == "legacy" else "independent-supervisor-model"
        )
        assert supervisor_model.provider_name == ("codex" if supervisor_backend == "codex" else "deepseek")
        assert supervisor_builds[0]["harness"] == ("deepagents" if supervisor_backend == "legacy" else supervisor_backend)
        assert supervisor_builds[0]["reasoning_effort"] == ("high" if supervisor_backend == "legacy" else "low")
        expected_supervisor_timeout = expected_agent_timeout if supervisor_backend == "legacy" else expected_independent_timeout
        assert supervisor_builds[0]["request_timeout_seconds"] == expected_supervisor_timeout
        assert supervisor_builds[0]["total_timeout_enabled"] is total_timeout_enabled
        assert services.agent._supervisor_runtime._total_timeout_enabled is total_timeout_enabled
        actual_supervisor_timeout = (
            supervisor_model._agent_model.request_timeout_seconds
            if supervisor_backend == "codex" else supervisor_model._request_timeout_seconds
        )
        assert actual_supervisor_timeout == expected_supervisor_timeout
        assert supervisor_builds[0]["max_output_tokens"] == (2048 if supervisor_backend == "legacy" else 1024)
        assert supervisor_builds[0]["max_input_chars"] == 120_000
        if supervisor_backend != "legacy":
            assert supervisor_builds[0]["binding"].max_input_chars == 80_000
            assert supervisor_builds[0]["binding"].context_window_tokens == 32_000
            assert supervisor_builds[0]["binding"].fallback is None
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
        assert runtime._evaluator_model.context_window_policy == (
            runtime._query_model.context_window_policy
        )
        assert runtime._chat_model.context_window_policy == (
            runtime._query_model.context_window_policy
        )
        assert runtime._chat_model.max_output_tokens == 2048
        assert runtime._evaluator_model.max_output_tokens == 2048
        assert runtime._max_loop_turns == 2
        assert runtime._max_model_calls == 4
        assert runtime._max_tool_calls == 2
        assert runtime._limits_enabled is True
        assert runtime._total_timeout_enabled is total_timeout_enabled
        assert runtime._max_prompt_chars == 120_000
        assert runtime._max_tool_result_bytes == 16_384
        assert runtime._max_read_chars_per_call == 16_384
        assert runtime._max_catalog_items == 20
        assert runtime._stage_timeout == 45
        assert runtime._evaluator_stage_timeout == 210
        assert runtime._execution_timeout == execution_timeout
        assert services.agent._executor._max_workers == 2
        updated = replace(
            settings.agent, evaluator_stage_timeout_seconds=240,
            total_timeout_enabled=not total_timeout_enabled,
            max_tool_result_bytes=8_192, max_read_chars_per_call=4_096,
            query_reasoning_effort="max", evaluator_reasoning_effort="none",
            chat_reasoning_effort="low",
            supervisor_reasoning_effort="none",
            maximum_concurrent_kicks=5,
        )
        replacement = services.agent._runtime_factory(updated)
        replacement_supervisor = None
        try:
            assert replacement._evaluator_stage_timeout == 240
            assert replacement._total_timeout_enabled is updated.total_timeout_enabled
            assert replacement._max_tool_result_bytes == 8_192
            assert replacement._max_read_chars_per_call == 4_096
            assert replacement._query_model.reasoning_effort == "max"
            assert replacement._evaluator_model.reasoning_effort == "none"
            assert replacement._chat_model.reasoning_effort == "low"
            replacement_supervisor = services.agent._supervisor_factory(replacement, updated)
            assert callable(replacement_supervisor._model.run_agent)
            if supervisor_backend != "codex":
                assert replacement_supervisor._model.session_store is supervisor_model.session_store
            assert supervisor_builds[1]["reasoning_effort"] == ("none" if supervisor_backend == "legacy" else "low")
            replacement_request_timeout = min(
                timeout for timeout in (
                    global_timeout,
                    execution_timeout if updated.total_timeout_enabled else None,
                    180 if supervisor_backend == "legacy" else supervisor_timeout,
                ) if timeout is not None
            )
            assert supervisor_builds[1]["request_timeout_seconds"] == replacement_request_timeout
            assert supervisor_builds[1]["total_timeout_enabled"] is updated.total_timeout_enabled
            assert replacement_supervisor._total_timeout_enabled is updated.total_timeout_enabled
            assert services.agent.active_maximum_concurrent_kicks == 2
        finally:
            if replacement_supervisor is not None:
                replacement_supervisor.close()
            else:
                replacement.close()
        app = create_app(services, settings=settings)
        assert app.state.sse_config == settings.sse
    finally:
        application.close()
    assert isinstance(application.resources.elasticsearch_client, _ElasticsearchClient)
    assert application.resources.elasticsearch_client.url == "http://127.0.0.1:9200"
    assert application.resources.elasticsearch_client.api_key == "test-key"
    assert application.resources.elasticsearch_client.closed
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
    real_retrieval = retrieval_builder.VideoParseRetrievalService
    def create_retrieval(*args, **kwargs):
        received.update(kwargs)
        return real_retrieval(*args, **kwargs)
    monkeypatch.setattr(retrieval_builder, "VideoParseRetrievalService", create_retrieval)

    monkeypatch.setattr(elasticsearch_caller, "Elasticsearch", _ElasticsearchClient)
    real_create_engine = mysql_caller.create_database_engine

    def create_engine(*args, **kwargs):
        engine = real_create_engine(*args, **kwargs)
        create_schema(engine)
        return engine

    monkeypatch.setattr(mysql_caller, "create_database_engine", create_engine)

    def forbidden_v1_runtime(*_args, **_kwargs):
        raise AssertionError("V2 mode must not construct the V1 parse runtime")

    def forbidden_v1_consumer(*_args, **_kwargs):
        raise AssertionError("V2 mode must not construct the V1 parse consumer")

    monkeypatch.setattr("engine.parsev1.runtime.EngineVideoParseRuntime", forbidden_v1_runtime)
    monkeypatch.setattr("server.mq.consumer.ParseJobConsumer", forbidden_v1_consumer)

    application = main_module.init_application(settings)
    services = application.services
    try:
        consumer = services.parse_consumer
        assert isinstance(consumer, VideoParseConsumer)
        runtime = consumer._handlers.prepare.__self__
        assert isinstance(runtime, VideoParseRuntimeService)
        assert services.media._video_parse_repository is runtime._repository
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
        assert runtime._elasticsearch_store._client is application.resources.elasticsearch_client
        assert isinstance(services.retrieval, VideoParseRetrievalService)
        assert services.retrieval._embedder._binding.model == "selected-task-embedding"
        assert services.retrieval._embedder._binding.dimensions == 512
        assert services.retrieval._embedder._binding.video_fps == 2
        assert services.retrieval._embedder._binding.source == services.uploads.parse_defaults.embedding.source
        assert received["candidate_budget"] == settings.retrieval.candidate_budget
        assert received["sparse_weight"] == settings.retrieval.sparse_weight
        assert received["expected_embedding_source"] == services.uploads.parse_defaults.embedding.source
        assert runtime._sparse_enabled is advanced_retrieval
        if advanced_retrieval:
            assert isinstance(received["reranker"], StructuredTextReranker)
            assert received["reranker"].model_name == load_config().resolve_model_binding("rerank").model
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
        initial_defaults = services.uploads.parse_defaults
        initial_config = runtime._gateway_config
        updated_config = initial_config.model_copy(update={"normalized_models": {
            **initial_config.normalized_models,
            "vision": initial_config.normalized_models["vision"].model_copy(
                update={"model": "updated-vision-model"},
            ),
        }})
        reload_paths = []
        def reload_config(path):
            reload_paths.append(path)
            return updated_config
        monkeypatch.setattr(bootstrap, "load_config", reload_config)
        assert callable(services.uploads._parse_defaults_loader)
        refreshed_defaults = services.uploads._parse_defaults_loader()
        assert reload_paths == [settings.model_config_path]
        assert refreshed_defaults.vision.model == "updated-vision-model"
        assert refreshed_defaults.vision.source == initial_defaults.vision.source
        assert refreshed_defaults.embedding == initial_defaults.embedding
        assert services.uploads.parse_defaults is initial_defaults
        assert initial_defaults.vision.model != refreshed_defaults.vision.model
        assert runtime._gateway_config is initial_config
    finally:
        application.close()


def test_v2_endpoint_override_preserves_route_and_shares_query_index_identity(
    monkeypatch,
    tmp_path,
):
    for name in tuple(os.environ):
        if name.startswith("ANIVAG_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANIVAG_ENVIRONMENT", "test")
    monkeypatch.setenv("ANIVAG_PARSE_EXECUTION_MODE", "v2")
    monkeypatch.setenv("ANIVAG_DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'endpoint-bootstrap.sqlite'}")
    monkeypatch.setenv("ANIVAG_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("ANIVAG_ELASTICSEARCH_URL", "http://127.0.0.1:9200")
    monkeypatch.setenv("ANIVAG_ELASTICSEARCH_INDEX", "endpoint_bootstrap_test")
    monkeypatch.setenv("ANIVAG_VIDEO_EMBEDDING_MODEL", "ep-A")
    monkeypatch.setenv("ARK_API_KEY", "test-ark-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    settings = ServerSettings.from_environment()
    monkeypatch.setattr(elasticsearch_caller, "Elasticsearch", _ElasticsearchClient)
    real_create_engine = mysql_caller.create_database_engine

    def create_engine(*args, **kwargs):
        engine = real_create_engine(*args, **kwargs)
        create_schema(engine)
        return engine

    monkeypatch.setattr(mysql_caller, "create_database_engine", create_engine)
    clients = []
    requests = []
    response_model = "doubao-embedding-vision-250615"

    class Client:
        def __init__(self):
            self.closed = False
            self.multimodal_embeddings = SimpleNamespace(create=self.create)

        @staticmethod
        def create(**kwargs):
            requests.append(kwargs)
            return SimpleNamespace(
                model=response_model,
                data=SimpleNamespace(embedding=[1.0] * settings.text_vector_dimensions),
            )

        def close(self):
            self.closed = True

    def create_client(**_kwargs):
        client = Client()
        clients.append(client)
        return client

    monkeypatch.setattr("arkruntime.Ark.volc", create_client)
    application = main_module.init_application(settings)
    services = application.services
    try:
        defaults = services.uploads.parse_defaults
        assert defaults.embedding.model == "ep-A"
        assert defaults.embedding.encoder_model == response_model
        restored = ParseConfig.model_validate_json(defaults.model_dump_json())
        snapshot = VideoParseV2Config.model_validate_json(restored.to_video_parse_v2().model_dump_json())
        assert snapshot.models.embedding.model == "ep-A"
        assert snapshot.models.embedding.encoder_model == response_model
        inputs = ParseInput(series_id="series", video_id="video", parse_config=snapshot)
        runtime = services.parse_consumer._handlers.index.__self__
        coordinator, client = runtime._build_index_coordinator(
            inputs, check_active=lambda: None,
            index_generation=settings.elasticsearch.index_generation,
        )
        try:
            query_encoder = services.retrieval._semantic_encoder
            assert query_encoder.model == response_model
            assert services.uploads._video_parse_query_encoder is query_encoder
            assert coordinator._embedder.model_name == "ep-A"
            assert coordinator.contract.semantic_encoder == query_encoder
            assert coordinator.contract.vision_encoder == query_encoder
            request = QueryEncodingRequest(channels=("semantic", "vision"), text="query", encoder=query_encoder)
            assert request.encoder_id == coordinator.contract.semantic_encoder_id
            result = services.retrieval._embedder.embed_query(request.text)
            assert result.model == response_model
            assert accept_query_vector(request, model=result.model, vector=result.vector).encoder_id == request.encoder_id
            response_model = "wrong-encoder"
            wrong = services.retrieval._embedder.embed_query(request.text)
            assert wrong.model == "wrong-encoder"
            with pytest.raises(ValueError, match="response model differs"):
                accept_query_vector(request, model=wrong.model, vector=wrong.vector)
            assert [item["model"] for item in requests] == ["ep-A", "ep-A"]
        finally:
            client.close()
        assert clients and all(item.closed for item in clients)
    finally:
        application.close()


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
    monkeypatch.setenv("ANIVAG_PARSE_EXECUTION_MODE", "v2")
    settings = ServerSettings.from_environment()

    with pytest.raises(ValueError, match="Video Parse V2 requires"):
        main_module.init_application(settings)


def test_mq_once_reuses_process_application_assembly(monkeypatch):
    from types import SimpleNamespace

    events = []
    settings = SimpleNamespace(environment="test", logging=object())
    services = SimpleNamespace(
        parse_consumer=SimpleNamespace(
            run_once=lambda: events.append("run_once"),
        ),
        close=lambda: events.append("close"),
    )
    runtime = SimpleNamespace(services=services, close=services.close)
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
        "init_application",
        lambda received: runtime if received is settings else None,
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
    runtime = SimpleNamespace(services=services, close=services.close)
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
        "init_application",
        lambda received: runtime if received is settings else None,
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
        agent=SimpleNamespace(close=lambda: order.append("agent")),
        realtime_understanding=SimpleNamespace(close=lambda: order.append("realtime")),
        parse_consumer=SimpleNamespace(close=lambda: order.append("consumer")),
        agent_model_factory=SimpleNamespace(close=lambda: order.append("model_factory")),
        tracing=SimpleNamespace(close=lambda: order.append("tracing")),
        parse_usage=SimpleNamespace(close=lambda: order.append("usage_reader")),
    )
    resources = CallerResources(
        elasticsearch_client=SimpleNamespace(close=lambda: order.append("es")),
        redis_client=SimpleNamespace(close=lambda: order.append("redis")),
        engine=SimpleNamespace(dispose=lambda: order.append("database")),
    )
    runtime = main_module.ApplicationRuntime(services=services, resources=resources)
    runtime.close()
    runtime.close()
    assert order == ["consumer", "agent", "realtime", "model_factory", "tracing", "usage_reader", "es", "redis", "database"]


@pytest.mark.parametrize("failure", ["consumer", "agent", "realtime"])
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

    def close_realtime():
        order.append("realtime")
        if failure == "realtime":
            raise RuntimeError("realtime close failed")

    services = bootstrap.ApplicationServices(
        library=None, uploads=None, jobs=None, media=None, retrieval=None,
        agent=SimpleNamespace(close=close_agent),
        realtime_understanding=SimpleNamespace(close=close_realtime),
        parse_consumer=SimpleNamespace(close=close_consumer),
        agent_model_factory=SimpleNamespace(close=lambda: order.append("model_factory")),
        tracing=SimpleNamespace(close=lambda: order.append("tracing")),
        parse_usage=SimpleNamespace(close=lambda: order.append("usage_reader")),
    )
    resources = CallerResources(
        elasticsearch_client=SimpleNamespace(close=lambda: order.append("es")),
        engine=SimpleNamespace(dispose=lambda: order.append("database")),
    )
    runtime = main_module.ApplicationRuntime(services=services, resources=resources)
    with pytest.raises(RuntimeError, match=f"{failure} close failed"):
        runtime.close()
    runtime.close()
    assert order == ["consumer", "agent", "realtime", "model_factory", "tracing", "usage_reader", "es", "database"]


@pytest.mark.parametrize("method", ["embed_query", "embed_image", "embed_sparse"])
def test_query_embedder_uses_a_client_per_call_and_always_closes(monkeypatch, method):
    models = []
    bindings = []
    binding = EmbeddingModelBinding(
        source="selected-embedding", model="embedding", dimensions=1024,
        video_fps=1, request_timeout_seconds=30, kind="embedding",
    )
    class Embedder:
        def __init__(self):
            self.closed = False
        def execute(self, query):
            if query == "fail":
                raise RuntimeError("embedding failed")
            return self
        embed_query = execute
        embed_image = execute
        embed_sparse = execute
        def close(self):
            self.closed = True
    def create_model(selected):
        bindings.append(selected)
        model = Embedder()
        models.append(model)
        return model
    client = SimpleNamespace(create_embedding_model=create_model)
    embedder = retrieval_builder._LazyQueryEmbedder(gateway_client=client, binding=binding)
    assert models == []
    invoke = getattr(embedder, method)
    assert invoke("first") is not invoke("second")
    with pytest.raises(RuntimeError, match="embedding failed"):
        invoke("fail")
    assert len(models) == 3 and all(model.closed for model in models)
    assert bindings == [binding] * 3


def test_retrieval_uses_gateway_encoder_metadata_and_independent_rerank_binding():
    from gateway.embedding import EmbeddingModelInfo
    from server.config.parse_profile import ParseEmbeddingConfig

    config = load_config()
    config = config.model_copy(update={
        "model_sources": {**config.model_sources, "third-party": OpenAIModelSourceConfig(
            provider="third-party", base_url="https://scorer.example/v1", api_key_env="SCORER_KEY",
        )},
        "normalized_models": {**config.normalized_models, "rerank": ModelBinding(
            source="third-party", model="independent-scorer", kind="rerank",
            request_timeout_seconds=30, max_output_tokens=1024,
        )},
    })
    descriptions = []

    def describe(binding):
        descriptions.append(binding)
        return EmbeddingModelInfo(
            provider_name="external-encoder", model_name=binding.model,
            encoder_model="actual-encoder", encoder_revision="external-revision", dimensions=512,
        )

    def forbidden_create(*args, **kwargs):
        raise AssertionError("retrieval construction must not allocate model clients")

    client = SimpleNamespace(
        config=config, describe_embedding=describe,
        create_embedding_model=forbidden_create, create_rerank_model=forbidden_create,
    )
    service, encoder = retrieval_builder.build_retrieval_service(
        SimpleNamespace(), SimpleNamespace(semantic_dimensions=512, vision_dimensions=512),
        gateway_config=config, gateway_client=client,
        embedding_config=ParseEmbeddingConfig(
            source="explicit-encoder", model="encoder-endpoint", dimensions=512, video_fps=2,
            request_timeout_seconds=11,
        ),
        retrieval_config=RetrievalConfig(rerank_enabled=True), index_generation=7,
        request_timeout_seconds=17, max_output_tokens=512,
    )
    assert encoder.provider == "external-encoder"
    assert encoder.model == "actual-encoder" and encoder.encoder_revision == "external-revision"
    assert encoder.dimensions == 512
    assert descriptions == [service._embedder._binding]
    assert descriptions[0].source == "explicit-encoder" and descriptions[0].model == "encoder-endpoint"
    assert descriptions[0].request_timeout_seconds == 11
    assert service._reranker.provider_name == "third-party"
    assert service._reranker.model_name == "independent-scorer"
    binding = service._reranker._delegate._binding
    assert binding.request_timeout_seconds == 17 and binding.max_output_tokens == 512


def test_query_embedder_preserves_frozen_binding_without_business_environment_overrides(monkeypatch):
    received = {}
    monkeypatch.setenv("ANIVAG_VIDEO_EMBEDDING_MODEL", "new-default")
    binding = EmbeddingModelBinding(
        source="selected-connection", model="frozen-embedding", dimensions=512,
        video_fps=2, request_timeout_seconds=30, kind="embedding",
    )
    class Model:
        def close(self):
            received["closed"] = True
        def embed_sparse(self, text):
            return text
    def create_model(selected):
        received["binding"] = selected
        return Model()
    embedder = retrieval_builder._LazyQueryEmbedder(
        gateway_client=SimpleNamespace(create_embedding_model=create_model), binding=binding,
    )
    assert embedder.embed_sparse("query") == "query"
    assert received["binding"] is binding
    assert received["closed"] is True


@pytest.mark.parametrize("fails", [False, True])
def test_text_reranker_uses_one_selected_model_and_closes_each_call(monkeypatch, fails):
    events = []
    binding = ModelBinding(
        source="selected-connection", model="selected-scorer", kind="rerank",
        max_output_tokens=512, request_timeout_seconds=30,
    )
    class Model:
        def rerank(self, query, documents):
            assert query == "query" and documents == ["candidate"]
            if fails:
                raise RuntimeError("scorer failed")
            return (RerankScore(0, 1),)
        def close(self):
            events.append(("close",))
    def create_model(selected):
        events.append(("open", selected))
        return Model()
    client = SimpleNamespace(
        config=SimpleNamespace(source=lambda source: SimpleNamespace(provider="selected-provider")),
        create_rerank_model=create_model,
    )
    delegate = retrieval_builder._LazyTextRerankModel(
        gateway_client=client, binding=binding,
    )
    assert delegate.provider_name == "selected-provider"
    if fails:
        with pytest.raises(RuntimeError, match="scorer failed"):
            delegate.rerank("query", ["candidate"])
    else:
        assert delegate.rerank("query", ["candidate"]) == (RerankScore(0, 1),)
    assert events == [("open", binding), ("close",)]


@pytest.mark.parametrize("option", ["sparse", "rerank", "unsupported_source"])
def test_unsupported_retrieval_options_fail_before_allocating_services(monkeypatch, option):
    from types import SimpleNamespace
    config = load_config()
    if option == "unsupported_source":
        config = config.model_copy(update={
            "model_sources": {**config.model_sources, "audio": OpenAIAudioSourceConfig(
                provider="speech", base_url="https://speech.example/v1", api_key_env="KEY",
            )},
            "normalized_models": {**config.normalized_models, "rerank": ModelBinding(
                source="audio", model="speech", kind="rerank",
            )},
        })
    monkeypatch.setattr(bootstrap, "load_config", lambda _path: config)
    def forbidden_database(*args, **kwargs):
        raise AssertionError("invalid retrieval must fail before allocating services")
    monkeypatch.setattr(mysql_caller, "create_database_engine", forbidden_database)
    settings = SimpleNamespace(
        model_config_path=None,
        parse_execution_mode="v2" if option == "unsupported_source" else "v1",
        elasticsearch=object() if option == "unsupported_source" else None,
        agent=AgentServiceConfig(), auth=SimpleNamespace(mode="local"),
        retrieval=RetrievalConfig(
            sparse_weight=0.5 if option == "sparse" else 0,
            rerank_enabled=option != "sparse",
        ),
    )
    with pytest.raises(ValueError, match="does not support adapter" if option == "unsupported_source" else "supports only Video Parse V2"):
        main_module.init_application(settings)


@pytest.mark.parametrize("injected", [False, True])
def test_app_lifespan_closes_only_services_it_owns(monkeypatch, injected):
    from fastapi.testclient import TestClient
    from types import SimpleNamespace
    lifecycle = []
    services = SimpleNamespace(
        start=lambda: lifecycle.append("start"),
        close=lambda: lifecycle.append("close"),
    )
    runtime = SimpleNamespace(services=services, start=services.start, close=services.close)
    settings = SimpleNamespace(sse=SSEConfig(), allowed_origins=(), auth=AuthConfig())
    monkeypatch.setattr(main_module, "init_application", lambda _settings: runtime)
    app = create_app(services if injected else None, settings=settings)
    with TestClient(app):
        pass
    assert lifecycle == ([] if injected else ["start", "close"])


@pytest.mark.parametrize("failure", ["elasticsearch", "redis"])
def test_application_keeps_closing_clients_after_dependency_close_fails(failure):
    order = []

    def close(name):
        order.append(name)
        if name == failure:
            raise RuntimeError(f"{name} close failed")

    services = bootstrap.ApplicationServices(
        library=None, uploads=None, jobs=None, media=None, retrieval=None,
        agent=SimpleNamespace(close=lambda: close("agent")),
    )
    resources = CallerResources(
        elasticsearch_client=SimpleNamespace(close=lambda: close("elasticsearch")),
        redis_client=SimpleNamespace(close=lambda: close("redis")),
        engine=SimpleNamespace(dispose=lambda: close("database")),
    )
    runtime = main_module.ApplicationRuntime(services=services, resources=resources)
    with pytest.raises(RuntimeError, match=f"{failure} close failed"):
        runtime.close()
    runtime.close()
    assert order == ["agent", "elasticsearch", "redis", "database"]
