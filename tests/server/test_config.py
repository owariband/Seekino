from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from gateway.config import ArkModelSourceConfig, GatewayConfig, load_config
from server.config import ServerSettings
from server.config.elasticsearch import ElasticsearchConfig, resolve_vector_dimensions
from server.config.parse_profile import ParseConfig, ParseVisionConfig, resolve_embedding_config
from server.config.retrieval import RetrievalConfig, resolve_retrieval_model_limits
from server.config.settings import (
    resolve_agent_request_timeout_seconds,
    resolve_artifact_root,
    resolve_database_pool_recycle_seconds,
    resolve_database_url,
    resolve_environment,
    resolve_execution_mode,
    resolve_parse_max_height,
)
from server.config.global_agent_settings import (
    GlobalAgentSettingsConflictError,
    patch_global_agent_settings,
    read_global_agent_settings,
)
from server.database import DatabaseEnvironment


@pytest.fixture(autouse=True, params=["legacy", "v2"])
def _config_without_agent_runtime(monkeypatch, tmp_path: Path, request) -> None:
    """Exercise environment overrides and settings edits with both YAML formats."""
    root = Path(__file__).resolve().parents[2]
    source = root / ("tests/gateway/fixtures/legacy-config.yaml" if request.param == "legacy" else "config.yaml")
    payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    payload.pop("agent_runtime", None)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    monkeypatch.setenv("ANIVAG_MODEL_CONFIG", str(config_path))


def _clean_anivag_environment(monkeypatch) -> None:
    model_config_path = os.environ["ANIVAG_MODEL_CONFIG"]
    for name in tuple(os.environ):
        if name.startswith("ANIVAG_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANIVAG_MODEL_CONFIG", model_config_path)
    monkeypatch.setenv(
        "ANIVAG_DATABASE_URL",
        "mysql+pymysql://user:password@127.0.0.1/anivag_development",
    )


def test_server_settings_load_upload_parse_agent_and_mq_defaults(monkeypatch) -> None:
    _clean_anivag_environment(monkeypatch)

    settings = ServerSettings.from_environment()

    assert settings.environment is DatabaseEnvironment.DEVELOPMENT
    assert settings.upload.preferred_segment_size_bytes == 8 * 1024 * 1024
    assert settings.upload.pipeline_version == "m2-seconds"
    assert settings.parse.chunk_duration_seconds == 60
    assert settings.parse.context_overlap_seconds == 3
    assert settings.agent.history_message_limit == 12
    assert settings.agent.default_top_k == 3
    assert settings.agent.maximum_top_k == 20
    assert settings.agent.max_loop_turns == 3
    assert settings.agent.max_model_calls_per_turn == 6
    assert settings.agent.maximum_model_calls_per_turn == 12
    assert settings.agent.max_tool_calls_per_turn == 24
    assert settings.agent.maximum_tool_calls_per_turn == 48
    assert settings.agent.limits_enabled is False
    assert settings.agent.query_reasoning_effort == "none"
    assert settings.agent.evaluator_reasoning_effort == "none"
    assert settings.agent.chat_reasoning_effort == "none"
    assert settings.agent.max_output_tokens == 8192
    assert settings.agent.max_prompt_chars == 200_000
    assert settings.agent.context_window_tokens == 64_000
    assert settings.agent.context_safety_margin_tokens == 4_096
    assert settings.agent.auto_compact_tokens == 48_000
    assert settings.agent.compact_target_tokens == 24_000
    assert settings.agent.max_tool_result_bytes == 16_384
    assert settings.agent.max_read_chars_per_call == 16_384
    assert settings.agent.max_catalog_items_per_page == 20
    assert settings.agent.stage_timeout_seconds == 480
    assert settings.agent.evaluator_stage_timeout_seconds == 480
    assert settings.agent.chat_timeout_seconds == 480
    assert settings.agent.supervisor_timeout_seconds == 480
    assert settings.agent.execution_timeout_seconds == 480
    assert settings.agent.maximum_concurrent_kicks == 4
    assert settings.agent_query_model is None
    assert settings.agent_chat_model is None
    assert settings.agent_fallback_model is None
    assert settings.agent_evaluator_model is None
    assert settings.mq.heartbeat_interval < settings.mq.lease_duration
    assert settings.mq.poll_interval_seconds == 2.0
    assert settings.mq.retry_base == timedelta(seconds=30)
    assert settings.mq.retry_max == timedelta(seconds=900)
    assert settings.text_vector_dimensions == settings.vision_vector_dimensions
    assert settings.sse.poll_interval_seconds == 1.0
    assert settings.sse.keepalive_interval_seconds == 15.0
    assert settings.retrieval.channel_top_k == 20
    assert settings.retrieval.rrf_k == 60
    assert settings.retrieval.lexical_weight == 1.0
    assert settings.retrieval.text_weight == settings.retrieval.vision_weight == 1.0
    assert settings.retrieval.candidate_budget is None
    assert settings.retrieval.sparse_weight == 0.0
    assert settings.retrieval.rerank_enabled is False
    assert settings.database_pool_recycle_seconds == 3600
    assert settings.video_embedding_model is None
    assert settings.video_embedding_fps is None
    assert settings.images.maximum_upload_size_bytes == 10 * 1024 * 1024
    assert settings.images.maximum_pixels == 40_000_000
    assert settings.images.model_max_edge == 1024
    assert settings.logging.directory == Path("outputs/server/logs")
    assert settings.logging.level == "info"
    assert settings.logging.maximum_file_size_bytes == 10 * 1024 * 1024
    assert settings.logging.backup_count == 5
    assert settings.elasticsearch is None
    assert settings.parse_execution_mode == "v2"


def test_server_settings_select_exactly_one_parse_worker_family(monkeypatch) -> None:
    _clean_anivag_environment(monkeypatch)
    monkeypatch.setenv("ANIVAG_PARSE_EXECUTION_MODE", "V2")

    assert ServerSettings.from_environment().parse_execution_mode == "v2"

    monkeypatch.setenv("ANIVAG_PARSE_EXECUTION_MODE", "v1")
    with pytest.raises(ValueError, match="V1 execution is retired"):
        ServerSettings.from_environment()

    monkeypatch.setenv("ANIVAG_PARSE_EXECUTION_MODE", "both")
    with pytest.raises(ValueError, match="must be 'v2'"):
        ServerSettings.from_environment()


def test_elasticsearch_settings_are_explicit_and_environment_isolated(monkeypatch) -> None:
    _clean_anivag_environment(monkeypatch)
    monkeypatch.setenv("ANIVAG_ELASTICSEARCH_URL", "http://127.0.0.1:9200")
    monkeypatch.setenv("ANIVAG_ELASTICSEARCH_INDEX", "anivag_video_chunks_development")
    monkeypatch.setenv("ANIVAG_ELASTICSEARCH_API_KEY", "test-key")
    monkeypatch.setenv("ANIVAG_ELASTICSEARCH_INDEX_GENERATION", "7")

    config = ServerSettings.from_environment().elasticsearch

    assert config is not None
    assert config.url == "http://127.0.0.1:9200"
    assert config.index_name == "anivag_video_chunks_development"
    assert config.api_key == "test-key"
    assert config.index_generation == 7


def test_elasticsearch_index_generation_defaults_to_one(monkeypatch) -> None:
    _clean_anivag_environment(monkeypatch)
    monkeypatch.setenv("ANIVAG_ELASTICSEARCH_URL", "http://127.0.0.1:9200")
    monkeypatch.setenv("ANIVAG_ELASTICSEARCH_INDEX", "anivag_video_chunks_development")

    config = ServerSettings.from_environment().elasticsearch

    assert config is not None
    assert config.index_generation == 1


@pytest.mark.parametrize("value", ["0", "-1", "not-an-integer"])
def test_elasticsearch_index_generation_must_be_a_positive_integer(
    monkeypatch, value
) -> None:
    _clean_anivag_environment(monkeypatch)
    monkeypatch.setenv("ANIVAG_ELASTICSEARCH_URL", "http://127.0.0.1:9200")
    monkeypatch.setenv("ANIVAG_ELASTICSEARCH_INDEX", "anivag_video_chunks_development")
    monkeypatch.setenv("ANIVAG_ELASTICSEARCH_INDEX_GENERATION", value)

    with pytest.raises(ValueError, match="positive integer"):
        ServerSettings.from_environment()


def test_elasticsearch_index_generation_must_fit_external_version_contract(
    monkeypatch,
) -> None:
    _clean_anivag_environment(monkeypatch)
    monkeypatch.setenv("ANIVAG_ELASTICSEARCH_URL", "http://127.0.0.1:9200")
    monkeypatch.setenv(
        "ANIVAG_ELASTICSEARCH_INDEX", "anivag_video_chunks_development"
    )
    monkeypatch.setenv("ANIVAG_ELASTICSEARCH_INDEX_GENERATION", str(1 << 31))

    with pytest.raises(ValueError, match="external version contract"):
        ServerSettings.from_environment()


@pytest.mark.parametrize(
    ("url", "index_name", "message"),
    [
        ("http://127.0.0.1:9200", "", "configured together"),
        ("", "anivag_video_chunks_development", "configured together"),
        ("http://127.0.0.1:9200/path", "anivag_video_chunks_development", "origin"),
        ("http://127.0.0.1:9200", "anivag_video_chunks_test", "dev component"),
    ],
)
def test_elasticsearch_settings_reject_incomplete_or_unsafe_values(
    monkeypatch, url, index_name, message,
) -> None:
    _clean_anivag_environment(monkeypatch)
    if url:
        monkeypatch.setenv("ANIVAG_ELASTICSEARCH_URL", url)
    if index_name:
        monkeypatch.setenv("ANIVAG_ELASTICSEARCH_INDEX", index_name)

    with pytest.raises(ValueError, match=message):
        ServerSettings.from_environment()


def test_config_yaml_exposes_agent_timeouts() -> None:
    source = Path(__file__).resolve().parents[2] / "config.yaml"
    settings, _ = read_global_agent_settings(source)

    assert (
        settings.stage_timeout_seconds,
        settings.evaluator_stage_timeout_seconds,
        settings.chat_timeout_seconds,
        settings.supervisor_timeout_seconds,
        settings.execution_timeout_seconds,
    ) == (480, 480, 480, 480, 480)


def test_server_settings_reject_cross_modal_dimension_mismatch(monkeypatch) -> None:
    _clean_anivag_environment(monkeypatch)
    monkeypatch.setenv("ANIVAG_TEXT_VECTOR_DIMENSIONS", "1024")
    monkeypatch.setenv("ANIVAG_VISION_VECTOR_DIMENSIONS", "768")

    with pytest.raises(ValueError, match="must match"):
        ServerSettings.from_environment()


def test_server_settings_load_sse_retrieval_database_and_embedding_overrides(monkeypatch):
    _clean_anivag_environment(monkeypatch)
    overrides = {
        "ANIVAG_SSE_POLL_SECONDS": "0.5",
        "ANIVAG_SSE_KEEPALIVE_SECONDS": "8",
        "ANIVAG_RETRIEVAL_CHANNEL_TOP_K": "32",
        "ANIVAG_RETRIEVAL_RRF_K": "45",
        "ANIVAG_RETRIEVAL_LEXICAL_WEIGHT": "2.0",
        "ANIVAG_RETRIEVAL_TEXT_WEIGHT": "0",
        "ANIVAG_RETRIEVAL_VISION_WEIGHT": "1.5",
        "ANIVAG_RETRIEVAL_CANDIDATE_BUDGET": "80",
        "ANIVAG_RETRIEVAL_SPARSE_WEIGHT": "0.75",
        "ANIVAG_RETRIEVAL_RERANK_ENABLED": "true",
        "ANIVAG_DATABASE_POOL_RECYCLE_SECONDS": "1200",
        "ANIVAG_VIDEO_EMBEDDING_MODEL": " embedding-test-model ",
        "ANIVAG_VIDEO_EMBEDDING_FPS": "2.0",
        "ANIVAG_MAX_IMAGE_UPLOAD_SIZE_BYTES": "2048",
        "ANIVAG_MAX_IMAGE_PIXELS": "1000000",
        "ANIVAG_BACKGROUND_IMAGE_MAX_EDGE": "512",
        "ANIVAG_LOG_DIRECTORY": "outputs/test-logs",
        "ANIVAG_LOG_LEVEL": "DEBUG",
        "ANIVAG_LOG_MAX_BYTES": "2048",
        "ANIVAG_LOG_BACKUP_COUNT": "2",
        "ANIVAG_MQ_CONSUMER_ID": "consumer-a",
        "ANIVAG_MQ_LEASE_SECONDS": "120",
        "ANIVAG_MQ_HEARTBEAT_SECONDS": "30",
        "ANIVAG_MQ_POLL_SECONDS": "0.25",
        "ANIVAG_MQ_RETRY_BASE_SECONDS": "12",
        "ANIVAG_MQ_RETRY_MAX_SECONDS": "120",
        "ANIVAG_CHUNK_DURATION_SECONDS": "75",
        "ANIVAG_CONTEXT_OVERLAP_SECONDS": "5",
    }
    for name, value in overrides.items():
        monkeypatch.setenv(name, value)

    settings = ServerSettings.from_environment()

    assert settings.sse.poll_interval_seconds == 0.5
    assert settings.sse.keepalive_interval_seconds == 8
    assert settings.retrieval.channel_top_k == 32
    assert settings.retrieval.rrf_k == 45
    assert settings.retrieval.lexical_weight == 2.0
    assert settings.retrieval.text_weight == 0
    assert settings.retrieval.vision_weight == 1.5
    assert settings.retrieval.candidate_budget == 80
    assert settings.retrieval.sparse_weight == 0.75
    assert settings.retrieval.rerank_enabled is True
    assert settings.database_pool_recycle_seconds == 1200
    assert settings.video_embedding_model == "embedding-test-model"
    assert settings.video_embedding_fps == 2.0
    assert settings.images.maximum_upload_size_bytes == 2048
    assert settings.images.maximum_pixels == 1_000_000
    assert settings.images.model_max_edge == 512
    assert settings.logging.directory == Path("outputs/test-logs")
    assert settings.logging.level == "debug"
    assert settings.logging.maximum_file_size_bytes == 2048
    assert settings.logging.backup_count == 2
    assert settings.mq.consumer_id == "consumer-a"
    assert settings.mq.lease_duration == timedelta(seconds=120)
    assert settings.mq.heartbeat_interval == timedelta(seconds=30)
    assert settings.mq.poll_interval_seconds == 0.25
    assert settings.mq.retry_base == timedelta(seconds=12)
    assert settings.mq.retry_max == timedelta(seconds=120)
    assert settings.parse.chunk_duration_seconds == 75
    assert settings.parse.context_overlap_seconds == 5


def test_server_settings_do_not_read_legacy_media_millisecond_variables(
    monkeypatch,
) -> None:
    _clean_anivag_environment(monkeypatch)
    monkeypatch.setenv("ANIVAG_CHUNK_DURATION_MS", "75000")
    monkeypatch.setenv("ANIVAG_CONTEXT_OVERLAP_MS", "5000")

    settings = ServerSettings.from_environment()

    assert settings.parse.chunk_duration_seconds == 60
    assert settings.parse.context_overlap_seconds == 3


def test_server_settings_accepts_legacy_worker_environment(monkeypatch) -> None:
    _clean_anivag_environment(monkeypatch)
    monkeypatch.setenv("ANIVAG_WORKER_ID", "legacy-worker")
    monkeypatch.setenv("ANIVAG_WORKER_LEASE_SECONDS", "180")
    monkeypatch.setenv("ANIVAG_WORKER_HEARTBEAT_SECONDS", "45")
    monkeypatch.setenv("ANIVAG_WORKER_POLL_SECONDS", "0.5")
    monkeypatch.setenv("ANIVAG_WORKER_RETRY_BASE_SECONDS", "20")
    monkeypatch.setenv("ANIVAG_WORKER_RETRY_MAX_SECONDS", "200")

    settings = ServerSettings.from_environment()

    assert settings.mq.consumer_id == "legacy-worker"
    assert settings.mq.lease_duration == timedelta(seconds=180)
    assert settings.mq.heartbeat_interval == timedelta(seconds=45)
    assert settings.mq.poll_interval_seconds == 0.5
    assert settings.mq.retry_base == timedelta(seconds=20)
    assert settings.mq.retry_max == timedelta(seconds=200)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ANIVAG_SSE_POLL_SECONDS", "0"),
        ("ANIVAG_SSE_KEEPALIVE_SECONDS", "-1"),
        ("ANIVAG_SSE_KEEPALIVE_SECONDS", "nan"),
        ("ANIVAG_VIDEO_EMBEDDING_FPS", "inf"),
        ("ANIVAG_RETRIEVAL_CHANNEL_TOP_K", "0"),
        ("ANIVAG_RETRIEVAL_RRF_K", "-1"),
        ("ANIVAG_RETRIEVAL_LEXICAL_WEIGHT", "nan"),
        ("ANIVAG_RETRIEVAL_TEXT_WEIGHT", "nan"),
        ("ANIVAG_RETRIEVAL_VISION_WEIGHT", "-1"),
        ("ANIVAG_RETRIEVAL_CANDIDATE_BUDGET", "0"),
        ("ANIVAG_RETRIEVAL_SPARSE_WEIGHT", "nan"),
        ("ANIVAG_RETRIEVAL_SPARSE_WEIGHT", "-1"),
        ("ANIVAG_RETRIEVAL_RERANK_ENABLED", "invalid"),
        ("ANIVAG_DATABASE_POOL_RECYCLE_SECONDS", "0"),
        ("ANIVAG_MAX_IMAGE_UPLOAD_SIZE_BYTES", "0"),
        ("ANIVAG_MAX_IMAGE_PIXELS", "-1"),
        ("ANIVAG_BACKGROUND_IMAGE_MAX_EDGE", "0"),
        ("ANIVAG_LOG_LEVEL", "invalid"),
        ("ANIVAG_LOG_MAX_BYTES", "0"),
        ("ANIVAG_LOG_BACKUP_COUNT", "-1"),
        ("ANIVAG_MQ_POLL_SECONDS", "0"),
    ],
)
def test_server_settings_reject_invalid_runtime_tuning(monkeypatch, name, value):
    _clean_anivag_environment(monkeypatch)
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError):
        ServerSettings.from_environment()


def test_server_settings_rejects_invalid_mq_timing(monkeypatch):
    _clean_anivag_environment(monkeypatch)
    monkeypatch.setenv("ANIVAG_MQ_LEASE_SECONDS", "60")
    monkeypatch.setenv("ANIVAG_MQ_HEARTBEAT_SECONDS", "60")
    with pytest.raises(ValueError, match="heartbeat_interval"):
        ServerSettings.from_environment()



def test_server_settings_reject_disabling_all_retrieval_channels(monkeypatch):
    _clean_anivag_environment(monkeypatch)
    monkeypatch.setenv("ANIVAG_RETRIEVAL_LEXICAL_WEIGHT", "0")
    monkeypatch.setenv("ANIVAG_RETRIEVAL_TEXT_WEIGHT", "0")
    monkeypatch.setenv("ANIVAG_RETRIEVAL_VISION_WEIGHT", "0")

    with pytest.raises(ValueError, match="at least one recall channel"):
        ServerSettings.from_environment()


def test_scoped_database_settings_ignore_unrelated_invalid_configuration(monkeypatch):
    _clean_anivag_environment(monkeypatch)
    monkeypatch.setenv("ANIVAG_MODEL_CONFIG", "/missing/model-config.yaml")
    monkeypatch.setenv("ANIVAG_ELASTICSEARCH_URL", "invalid")
    monkeypatch.setenv("ANIVAG_ELASTICSEARCH_INDEX_GENERATION", "invalid")
    monkeypatch.setenv("ANIVAG_PARSE_EXECUTION_MODE", "v1")
    monkeypatch.setenv("ANIVAG_RETRIEVAL_CHANNEL_TOP_K", "invalid")
    monkeypatch.setenv("ANIVAG_TEXT_VECTOR_DIMENSIONS", "invalid")

    assert resolve_environment() is DatabaseEnvironment.DEVELOPMENT
    assert resolve_database_url() == os.environ["ANIVAG_DATABASE_URL"]
    assert resolve_database_pool_recycle_seconds() == 3600
    assert resolve_agent_request_timeout_seconds() == 180
    assert resolve_artifact_root() == Path("outputs/server/artifacts")
    assert resolve_parse_max_height() == 720


def test_explicit_settings_inputs_override_invalid_environment(monkeypatch, tmp_path):
    _clean_anivag_environment(monkeypatch)
    for name in (
        "ANIVAG_ENVIRONMENT", "ANIVAG_DATABASE_POOL_RECYCLE_SECONDS",
        "ANIVAG_AGENT_REQUEST_TIMEOUT_SECONDS", "ANIVAG_PARSE_MAX_HEIGHT",
        "ANIVAG_PARSE_EXECUTION_MODE",
    ):
        monkeypatch.setenv(name, "invalid")
    monkeypatch.setenv("ANIVAG_DATABASE_URL", " ")
    monkeypatch.setenv("ANIVAG_ARTIFACT_ROOT", "ignored-root")

    assert resolve_environment("test") is DatabaseEnvironment.TEST
    assert resolve_database_url(" sqlite+pysqlite:///:memory: ") == "sqlite+pysqlite:///:memory:"
    assert resolve_database_pool_recycle_seconds(90) == 90
    assert resolve_agent_request_timeout_seconds(15) == 15
    assert resolve_parse_max_height(1080) == 1080
    assert resolve_execution_mode("v2") == "v2"
    assert resolve_artifact_root(tmp_path) == tmp_path


@pytest.mark.parametrize("resolver,value", [
    (resolve_database_pool_recycle_seconds, 0),
    (resolve_parse_max_height, -1),
    (resolve_agent_request_timeout_seconds, float("inf")),
    (resolve_agent_request_timeout_seconds, 0),
])
def test_explicit_settings_inputs_keep_existing_validation(resolver, value):
    with pytest.raises(ValueError):
        resolver(value)


def test_elasticsearch_overrides_are_scoped_and_can_clear_api_key(monkeypatch):
    _clean_anivag_environment(monkeypatch)
    monkeypatch.delenv("ANIVAG_DATABASE_URL")
    monkeypatch.setenv("ANIVAG_MODEL_CONFIG", "/missing/model-config.yaml")
    monkeypatch.setenv("ANIVAG_ELASTICSEARCH_URL", "invalid")
    monkeypatch.setenv("ANIVAG_ELASTICSEARCH_INDEX", "invalid")
    monkeypatch.setenv("ANIVAG_ELASTICSEARCH_INDEX_GENERATION", "invalid")
    monkeypatch.setenv("ANIVAG_ELASTICSEARCH_API_KEY", "environment-key")

    config = ElasticsearchConfig.from_environment(
        DatabaseEnvironment.TEST, url="http://127.0.0.1:9200",
        index_name="chunks_test", index_generation=7, api_key="",
    )
    assert config is not None
    assert config.index_generation == 7
    assert config.api_key is None
    inherited_key = ElasticsearchConfig.from_environment(
        DatabaseEnvironment.TEST, url=config.url, index_name=config.index_name,
        index_generation=7,
    )
    assert inherited_key is not None and inherited_key.api_key == "environment-key"


def test_vector_dimensions_override_bad_environment_and_preserve_index_initialization(monkeypatch):
    monkeypatch.setenv("ANIVAG_TEXT_VECTOR_DIMENSIONS", "invalid")
    monkeypatch.setenv("ANIVAG_VISION_VECTOR_DIMENSIONS", "invalid")
    assert resolve_vector_dimensions(text_dimensions=2, vision_dimensions=3) == (2, 3)
    with pytest.raises(ValueError, match="must match"):
        resolve_vector_dimensions(text_dimensions=2, vision_dimensions=3, require_matching=True)


def test_retrieval_overrides_precede_invalid_environment_and_keep_zero_values(monkeypatch):
    for name in (
        "CHANNEL_TOP_K", "RRF_K", "LEXICAL_WEIGHT", "TEXT_WEIGHT", "VISION_WEIGHT",
        "CANDIDATE_BUDGET", "SPARSE_WEIGHT", "RERANK_ENABLED",
    ):
        monkeypatch.setenv("ANIVAG_RETRIEVAL_" + name, "invalid")

    config = RetrievalConfig.from_environment(
        channel_top_k=30, rrf_k=45, lexical_weight=1, text_weight=0,
        vision_weight=0, candidate_budget=80, sparse_weight=0, rerank_enabled=False,
    )
    assert config == RetrievalConfig(
        channel_top_k=30, rrf_k=45, lexical_weight=1, text_weight=0,
        vision_weight=0, candidate_budget=80, sparse_weight=0, rerank_enabled=False,
    )


def test_retrieval_partial_overrides_inherit_other_environment_values(monkeypatch):
    _clean_anivag_environment(monkeypatch)
    monkeypatch.setenv("ANIVAG_RETRIEVAL_CHANNEL_TOP_K", "32")
    monkeypatch.setenv("ANIVAG_RETRIEVAL_LEXICAL_WEIGHT", "2")
    monkeypatch.setenv("ANIVAG_RETRIEVAL_CANDIDATE_BUDGET", "80")
    monkeypatch.setenv("ANIVAG_RETRIEVAL_SPARSE_WEIGHT", "0.75")
    monkeypatch.setenv("ANIVAG_RETRIEVAL_RERANK_ENABLED", "true")

    config = RetrievalConfig.from_environment(rrf_k=20)
    assert (config.channel_top_k, config.rrf_k, config.lexical_weight) == (32, 20, 2)
    assert config.candidate_budget == 80 and config.sparse_weight == 0.75
    assert config.rerank_enabled is True


def test_embedding_resolution_shares_endpoint_identity_and_explicit_overrides(monkeypatch):
    gateway = load_config()
    monkeypatch.setenv("ANIVAG_VIDEO_EMBEDDING_MODEL", "ep-selected")
    monkeypatch.setenv("ANIVAG_VIDEO_EMBEDDING_FPS", "2")
    config = resolve_embedding_config(gateway, dimensions=512)
    assert config.model == "ep-selected"
    assert config.encoder_model == gateway.multimodal_embedding.model
    assert config.video_fps == 2 and config.dimensions == 512
    assert isinstance(gateway.model_sources[config.source], ArkModelSourceConfig)

    monkeypatch.setenv("ANIVAG_VIDEO_EMBEDDING_FPS", "invalid")
    explicit = resolve_embedding_config(
        gateway, dimensions=1024, model=gateway.multimodal_embedding.model, video_fps=1,
    )
    assert explicit.model == gateway.multimodal_embedding.model
    assert explicit.encoder_model is None and explicit.video_fps == 1


def test_parse_defaults_preserve_settings_snapshot_after_embedding_environment_changes(monkeypatch):
    _clean_anivag_environment(monkeypatch)
    settings = ServerSettings.from_environment()
    gateway = load_config()
    assert settings.video_embedding_model is None and settings.video_embedding_fps is None
    monkeypatch.setenv("ANIVAG_VIDEO_EMBEDDING_MODEL", "ep-later-environment")
    monkeypatch.setenv("ANIVAG_VIDEO_EMBEDDING_FPS", "invalid")

    defaults = ParseConfig.from_defaults(
        runtime=settings.parse, gateway=gateway, upload=settings.upload, images=settings.images,
        embedding_model=settings.video_embedding_model,
        embedding_dimensions=settings.text_vector_dimensions,
        embedding_fps=settings.video_embedding_fps,
    )
    assert defaults.embedding.model == gateway.multimodal_embedding.model
    assert defaults.embedding.video_fps == gateway.multimodal_embedding.fps


def test_vision_controls_reach_frozen_task_without_changing_asr(monkeypatch):
    _clean_anivag_environment(monkeypatch)
    settings = ServerSettings.from_environment()
    gateway = load_config()
    defaults = ParseConfig.from_defaults(
        runtime=settings.parse, gateway=gateway, upload=settings.upload, images=settings.images,
        embedding_model=settings.video_embedding_model,
        embedding_dimensions=settings.text_vector_dimensions,
        embedding_fps=settings.video_embedding_fps,
    )
    frozen = defaults.to_video_parse_v2()
    resolved = gateway.resolve_model_binding("vision")
    assert frozen.models.understanding.max_output_tokens == resolved.max_output_tokens
    assert frozen.models.understanding.request_timeout_seconds == resolved.request_timeout_seconds
    assert frozen.models.understanding.context_window_tokens == resolved.context_window_tokens
    assert frozen.models.understanding.auto_compact_tokens == resolved.auto_compact_tokens
    assert frozen.models.understanding.compact_target_tokens == resolved.compact_target_tokens
    assert frozen.models.understanding.max_input_chars == resolved.max_input_chars
    assert frozen.models.understanding.reasoning_effort == resolved.reasoning_effort
    assert frozen.models.asr.reasoning_effort is None
    assert frozen.models.asr.max_output_tokens == gateway.resolve_model_binding("transcription").max_output_tokens

    historical = frozen.model_dump(mode="json")
    historical["models"]["understanding"].pop("reasoning_effort")
    historical["models"]["understanding"]["max_output_tokens"] = 8192
    restored = type(frozen).model_validate(historical)
    assert restored.models.understanding.reasoning_effort is None
    assert restored.models.understanding.max_output_tokens == 8192


@pytest.mark.parametrize("source,model,budget,input_mode", [
    ("deepseek_api", "deepseek-flash", 8192, "ordered_frames"),
    ("ark_default", "doubao-seed-2-1-pro-260628", 65536, "video"),
])
def test_parse_vision_source_freezes_compatible_media_without_changing_other_bindings(
    monkeypatch, source, model, budget, input_mode,
):
    _clean_anivag_environment(monkeypatch)
    settings = ServerSettings.from_environment()
    gateway = load_config()
    payload = yaml.safe_load(Path(os.environ["ANIVAG_MODEL_CONFIG"]).read_text())
    if gateway.config_version == 2:
        payload["models"]["vision"].update(
            source=source, model=model, max_output_tokens=budget, reasoning_effort="none",
        )
        for field in ("auto_compact_tokens", "compact_target_tokens"):
            payload["models"]["vision"].pop(field, None)
    else:
        payload["video_parser"].update(
            vision_source=source, video_model=model,
            vision_max_output_tokens=budget, vision_reasoning_effort="none",
        )
    selected = GatewayConfig.model_validate(payload)

    def freeze(config):
        return ParseConfig.from_defaults(
            runtime=settings.parse, gateway=config, upload=settings.upload, images=settings.images,
            embedding_model=settings.video_embedding_model,
            embedding_dimensions=settings.text_vector_dimensions,
            embedding_fps=settings.video_embedding_fps,
        ).to_video_parse_v2()

    previous, frozen = freeze(gateway), freeze(selected)
    understanding = frozen.models.understanding
    assert (understanding.source, understanding.model) == (source, model)
    assert understanding.max_output_tokens == budget
    assert understanding.reasoning_effort == "none"
    assert frozen.models.refine is None
    assert frozen.visual_input.mode == input_mode
    if input_mode == "ordered_frames":
        assert frozen.visual_input.max_frames_per_chunk == selected.video_parser.max_frames_per_window
    assert frozen.models.asr == previous.models.asr
    assert frozen.models.embedding == previous.models.embedding
    assert selected.normalized_models["agent"] == gateway.normalized_models["agent"]


@pytest.mark.parametrize("by_alias", [False, True])
def test_unset_vision_reasoning_preserves_legacy_request_shape(by_alias):
    legacy = {
        "source": "ark_default", "model": "legacy-vision", "input_mode": "video",
        "fps": 1.0, "max_frames_per_window": None, "max_output_tokens": None,
    }
    expected = {
        (ParseVisionConfig.model_fields[key].alias if by_alias else key): value
        for key, value in legacy.items()
    }
    for payload in (legacy, {**legacy, "reasoning_effort": None}):
        config = ParseVisionConfig.model_validate(payload)
        assert config.model_dump(mode="json", by_alias=by_alias) == expected
        assert ParseVisionConfig.model_validate_json(
            config.model_dump_json(by_alias=by_alias)
        ) == config
    configured = ParseVisionConfig.model_validate({**legacy, "reasoning_effort": "none"})
    key = "reasoningEffort" if by_alias else "reasoning_effort"
    assert configured.model_dump(mode="json", by_alias=by_alias) == {
        **expected, key: "none",
    }


def test_retrieval_model_limits_reuse_yaml_runtime_precedence(monkeypatch):
    _clean_anivag_environment(monkeypatch)
    config_path = Path(os.environ["ANIVAG_MODEL_CONFIG"])
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    payload["agent_runtime"] = {"execution_timeout_seconds": 45, "max_output_tokens": 2048}
    config_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    monkeypatch.setenv("ANIVAG_AGENT_EXECUTION_TIMEOUT_SECONDS", "90")
    monkeypatch.setenv("ANIVAG_AGENT_MAX_OUTPUT_TOKENS", "4096")

    assert resolve_retrieval_model_limits(config_path, request_timeout_seconds=120) == (45, 2048)
    assert resolve_retrieval_model_limits(config_path, request_timeout_seconds=15) == (15, 2048)


@pytest.mark.parametrize("rerank_enabled", [0, 1, "true", None])
def test_retrieval_config_rejects_nonboolean_rerank_flag(rerank_enabled):
    with pytest.raises(ValueError, match="rerank_enabled must be a boolean"):
        RetrievalConfig(rerank_enabled=rerank_enabled)


def test_server_settings_reject_agent_default_above_maximum(monkeypatch) -> None:
    _clean_anivag_environment(monkeypatch)
    monkeypatch.setenv("ANIVAG_AGENT_DEFAULT_TOP_K", "21")
    monkeypatch.setenv("ANIVAG_AGENT_MAX_TOP_K", "20")

    with pytest.raises(ValueError, match="must be at least"):
        ServerSettings.from_environment()


def test_agent_roles_context_budgets_and_timeouts_load_from_settings(monkeypatch):
    _clean_anivag_environment(monkeypatch)
    overrides = {
        "ANIVAG_AGENT_QUERY_MODEL": "query-model",
        "ANIVAG_AGENT_EVALUATOR_MODEL": "review-model",
        "ANIVAG_AGENT_CHAT_MODEL": "output-model",
        "ANIVAG_AGENT_FALLBACK_MODEL": "fallback-model",
        "ANIVAG_AGENT_MAX_LOOP_TURNS": "2",
        "ANIVAG_AGENT_MAX_MODEL_CALLS_PER_TURN": "5",
        "ANIVAG_AGENT_MAXIMUM_MODEL_CALLS_PER_TURN": "9",
        "ANIVAG_AGENT_MAX_TOOL_CALLS_PER_TURN": "3",
        "ANIVAG_AGENT_MAXIMUM_TOOL_CALLS_PER_TURN": "7",
        "ANIVAG_AGENT_LIMITS_ENABLED": "true",
        "ANIVAG_AGENT_MAX_OUTPUT_TOKENS": "2048",
        "ANIVAG_AGENT_MAX_PROMPT_CHARS": "120000",
        "ANIVAG_AGENT_CONTEXT_WINDOW_TOKENS": "32000",
        "ANIVAG_AGENT_CONTEXT_SAFETY_MARGIN_TOKENS": "2048",
        "ANIVAG_AGENT_AUTO_COMPACT_TOKENS": "24000",
        "ANIVAG_AGENT_COMPACT_TARGET_TOKENS": "12000",
        "ANIVAG_AGENT_MAX_TOOL_RESULT_BYTES": "8192",
        "ANIVAG_AGENT_MAX_READ_CHARS_PER_CALL": "4096",
        "ANIVAG_AGENT_MAX_CATALOG_ITEMS_PER_PAGE": "10",
        "ANIVAG_AGENT_STAGE_TIMEOUT_SECONDS": "45",
        "ANIVAG_AGENT_EVALUATOR_STAGE_TIMEOUT_SECONDS": "210",
        "ANIVAG_AGENT_CHAT_TIMEOUT_SECONDS": "150",
        "ANIVAG_AGENT_SUPERVISOR_TIMEOUT_SECONDS": "300",
        "ANIVAG_AGENT_EXECUTION_TIMEOUT_SECONDS": "90",
        "ANIVAG_AGENT_MAX_CONCURRENT_KICKS": "2",
    }
    for name, value in overrides.items():
        monkeypatch.setenv(name, value)
    settings = ServerSettings.from_environment()
    assert (
        settings.agent_query_model,
        settings.agent_evaluator_model,
        settings.agent_chat_model,
        settings.agent_fallback_model,
    ) == ("query-model", "review-model", "output-model", "fallback-model")
    assert settings.agent.max_loop_turns == 2
    assert settings.agent.max_model_calls_per_turn == 5
    assert settings.agent.maximum_model_calls_per_turn == 9
    assert settings.agent.max_tool_calls_per_turn == 3
    assert settings.agent.maximum_tool_calls_per_turn == 7
    assert settings.agent.limits_enabled is True
    assert settings.agent.max_output_tokens == 2048
    assert settings.agent.max_prompt_chars == 120_000
    assert settings.agent.context_window_tokens == 32_000
    assert settings.agent.context_safety_margin_tokens == 2_048
    assert settings.agent.auto_compact_tokens == 24_000
    assert settings.agent.compact_target_tokens == 12_000
    assert settings.agent.max_tool_result_bytes == 8_192
    assert settings.agent.max_read_chars_per_call == 4_096
    assert settings.agent.max_catalog_items_per_page == 10
    assert settings.agent.stage_timeout_seconds == 45
    assert settings.agent.evaluator_stage_timeout_seconds == 210
    assert settings.agent.chat_timeout_seconds == 150
    assert settings.agent.supervisor_timeout_seconds == 300
    assert settings.agent.execution_timeout_seconds == 90
    assert settings.agent.maximum_concurrent_kicks == 2


def test_legacy_deepseek_chat_model_environment_is_still_supported(monkeypatch):
    _clean_anivag_environment(monkeypatch)
    monkeypatch.setenv("ANIVAG_DEEPSEEK_CHAT_MODEL", "legacy-chat-model")

    settings = ServerSettings.from_environment()

    assert settings.agent_chat_model == "legacy-chat-model"

    monkeypatch.setenv("ANIVAG_AGENT_CHAT_MODEL", "current-chat-model")

    settings = ServerSettings.from_environment()

    assert settings.agent_chat_model == "current-chat-model"


@pytest.mark.parametrize("name,value", [
    ("ANIVAG_AGENT_MAX_LOOP_TURNS", "4"),
    ("ANIVAG_AGENT_MAX_MODEL_CALLS_PER_TURN", "0"),
    ("ANIVAG_AGENT_MAXIMUM_MODEL_CALLS_PER_TURN", "0"),
    ("ANIVAG_AGENT_MAX_TOOL_CALLS_PER_TURN", "0"),
    ("ANIVAG_AGENT_MAXIMUM_TOOL_CALLS_PER_TURN", "0"),
    ("ANIVAG_AGENT_LIMITS_ENABLED", "maybe"),
    ("ANIVAG_AGENT_MAX_OUTPUT_TOKENS", "0"),
    ("ANIVAG_AGENT_MAX_PROMPT_CHARS", "0"),
    ("ANIVAG_AGENT_CONTEXT_WINDOW_TOKENS", "4096"),
    ("ANIVAG_AGENT_CONTEXT_SAFETY_MARGIN_TOKENS", "60000"),
    ("ANIVAG_AGENT_AUTO_COMPACT_TOKENS", "60000"),
    ("ANIVAG_AGENT_COMPACT_TARGET_TOKENS", "48000"),
    ("ANIVAG_AGENT_MAX_TOOL_RESULT_BYTES", "511"),
    ("ANIVAG_AGENT_MAX_READ_CHARS_PER_CALL", "0"),
    ("ANIVAG_AGENT_MAX_CATALOG_ITEMS_PER_PAGE", "101"),
    ("ANIVAG_AGENT_STAGE_TIMEOUT_SECONDS", "nan"),
    ("ANIVAG_AGENT_STAGE_TIMEOUT_SECONDS", "0"),
    ("ANIVAG_AGENT_EVALUATOR_STAGE_TIMEOUT_SECONDS", "nan"),
    ("ANIVAG_AGENT_EVALUATOR_STAGE_TIMEOUT_SECONDS", "0"),
    ("ANIVAG_AGENT_CHAT_TIMEOUT_SECONDS", "nan"),
    ("ANIVAG_AGENT_CHAT_TIMEOUT_SECONDS", "0"),
    ("ANIVAG_AGENT_SUPERVISOR_TIMEOUT_SECONDS", "nan"),
    ("ANIVAG_AGENT_SUPERVISOR_TIMEOUT_SECONDS", "0"),
    ("ANIVAG_AGENT_EXECUTION_TIMEOUT_SECONDS", "nan"),
    ("ANIVAG_AGENT_EXECUTION_TIMEOUT_SECONDS", "0"),
    ("ANIVAG_AGENT_MAX_CONCURRENT_KICKS", "0"),
    ("ANIVAG_AGENT_WEB_SEARCH_MAX_RESULTS", "21"),
    ("ANIVAG_AGENT_WEB_SEARCH_TIMEOUT_SECONDS", "61"),
    ("ANIVAG_AGENT_SUPERVISOR_REASONING_EFFORT", "invalid"),
])
def test_invalid_agent_execution_budget_is_rejected(monkeypatch, name, value):
    _clean_anivag_environment(monkeypatch)
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        ServerSettings.from_environment()


def test_global_agent_settings_yaml_overrides_environment_and_keeps_fallbacks(
    monkeypatch,
) -> None:
    _clean_anivag_environment(monkeypatch)
    config_path = Path(os.environ["ANIVAG_MODEL_CONFIG"])
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    payload["agent_runtime"] = {"default_top_k": 5, "stage_timeout_seconds": 75}
    config_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    monkeypatch.setenv("ANIVAG_AGENT_DEFAULT_TOP_K", "2")
    monkeypatch.setenv("ANIVAG_AGENT_MAX_TOOL_RESULT_BYTES", "4096")

    config, revision = read_global_agent_settings(config_path)
    settings = ServerSettings.from_environment()

    assert len(revision) == 64
    assert config.default_top_k == settings.agent.default_top_k == 5
    assert config.stage_timeout_seconds == settings.agent.stage_timeout_seconds == 75
    assert config.max_tool_result_bytes == settings.agent.max_tool_result_bytes == 4096


def test_global_agent_reasoning_settings_support_yaml_patch_and_environment(
    monkeypatch,
) -> None:
    _clean_anivag_environment(monkeypatch)
    config_path = Path(os.environ["ANIVAG_MODEL_CONFIG"])
    monkeypatch.setenv("ANIVAG_AGENT_QUERY_REASONING_EFFORT", " HIGH ")
    config, revision = read_global_agent_settings(config_path)
    assert config.query_reasoning_effort == "high"
    assert config.evaluator_reasoning_effort == "none"
    assert config.chat_reasoning_effort == "none"

    updated, _ = patch_global_agent_settings(
        {
            "query_reasoning_effort": "low",
            "evaluator_reasoning_effort": "high",
            "chat_reasoning_effort": "max",
        },
        revision, config_path,
    )
    assert (
        updated.query_reasoning_effort,
        updated.evaluator_reasoning_effort,
        updated.chat_reasoning_effort,
    ) == ("low", "high", "max")
    assert read_global_agent_settings(config_path)[0] == updated
    runtime_values = yaml.safe_load(config_path.read_text(encoding="utf-8"))[
        "agent_runtime"
    ]
    assert runtime_values == {
        "query_reasoning_effort": "low",
        "evaluator_reasoning_effort": "high",
        "chat_reasoning_effort": "max",
    }


def test_global_agent_settings_patch_preserves_other_sections_and_detects_edits(
    monkeypatch,
) -> None:
    _clean_anivag_environment(monkeypatch)
    config_path = Path(os.environ["ANIVAG_MODEL_CONFIG"])
    before = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    _, revision = read_global_agent_settings(config_path)

    updated, next_revision = patch_global_agent_settings(
        {
            "evaluator_stage_timeout_seconds": 210,
            "chat_timeout_seconds": 300,
            "supervisor_timeout_seconds": 420,
            "max_read_chars_per_call": 4_096,
        },
        revision, config_path,
    )
    after = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert updated.evaluator_stage_timeout_seconds == 210
    assert updated.chat_timeout_seconds == 300
    assert updated.supervisor_timeout_seconds == 420
    assert updated.max_read_chars_per_call == 4_096
    assert next_revision != revision
    assert after["agent_runtime"] == {
        "evaluator_stage_timeout_seconds": 210.0,
        "chat_timeout_seconds": 300.0,
        "supervisor_timeout_seconds": 420.0,
        "max_read_chars_per_call": 4_096,
    }
    for section in before.keys() - {"agent_runtime"}:
        assert after[section] == before[section]

    after["agent_runtime"]["default_top_k"] = 7
    config_path.write_text(yaml.safe_dump(after), encoding="utf-8")
    assert read_global_agent_settings(config_path)[0].default_top_k == 7
    with pytest.raises(GlobalAgentSettingsConflictError):
        patch_global_agent_settings({"default_top_k": 6}, next_revision, config_path)
    assert read_global_agent_settings(config_path)[0].default_top_k == 7


def test_global_agent_settings_patch_preserves_external_yaml_comments(
    monkeypatch,
) -> None:
    _clean_anivag_environment(monkeypatch)
    config_path = Path(os.environ["ANIVAG_MODEL_CONFIG"])
    original = "# model note\n" + config_path.read_text(encoding="utf-8")
    original = original.replace(
        "video_parser:\n",
        "# runtime applies to new messages\nagent_runtime:\n"
        "  default_top_k: 3\n\n# parser note\nvideo_parser:\n",
        1,
    )
    config_path.write_text(original, encoding="utf-8")
    before_runtime, after_runtime = original.split("agent_runtime:\n", 1)
    _, revision = read_global_agent_settings(config_path)

    patch_global_agent_settings({"default_top_k": 5}, revision, config_path)

    updated = config_path.read_text(encoding="utf-8")
    updated_prefix, updated_suffix = updated.split("agent_runtime:\n", 1)
    assert updated_prefix == before_runtime
    assert updated_suffix[updated_suffix.index("# parser note"):] == (
        after_runtime[after_runtime.index("# parser note"):]
    )
    assert read_global_agent_settings(config_path)[0].default_top_k == 5


def test_global_agent_settings_patch_rejects_external_alias_dependency(
    monkeypatch,
) -> None:
    _clean_anivag_environment(monkeypatch)
    config_path = Path(os.environ["ANIVAG_MODEL_CONFIG"])
    original = (
        "agent_runtime:\n  max_output_tokens: &output_budget 4096\n\n"
        + config_path.read_text(encoding="utf-8")
    ).replace("max_output_tokens: 8192", "max_output_tokens: *output_budget", 1)
    config_path.write_text(original, encoding="utf-8")
    _, revision = read_global_agent_settings(config_path)

    with pytest.raises(ValueError, match="cannot be safely edited"):
        patch_global_agent_settings({"default_top_k": 5}, revision, config_path)
    assert config_path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    "values",
    [
        {"unknown": 1},
        {"default_top_k": True},
        {"stage_timeout_seconds": float("nan")},
        {"chat_timeout_seconds": 0},
        {"supervisor_timeout_seconds": float("inf")},
        {"maximum_top_k": 101},
        {"default_top_k": 21, "maximum_top_k": 20},
        {"query_reasoning_effort": "medium"},
        {"evaluator_reasoning_effort": 1},
        {"chat_reasoning_effort": None},
    ],
)
def test_global_agent_settings_reject_invalid_patch_without_writing(
    monkeypatch, values,
) -> None:
    _clean_anivag_environment(monkeypatch)
    config_path = Path(os.environ["ANIVAG_MODEL_CONFIG"])
    original = config_path.read_bytes()
    _, revision = read_global_agent_settings(config_path)
    with pytest.raises(ValueError):
        patch_global_agent_settings(values, revision, config_path)
    assert config_path.read_bytes() == original


def test_global_agent_settings_reject_invalid_manual_yaml(monkeypatch) -> None:
    _clean_anivag_environment(monkeypatch)
    config_path = Path(os.environ["ANIVAG_MODEL_CONFIG"])
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    payload["agent_runtime"] = {"default_top_k": "five"}
    config_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="agent_runtime.default_top_k"):
        read_global_agent_settings(config_path)
