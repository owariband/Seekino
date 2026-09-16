from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pytest

from server.config import ServerSettings
from server.database import DatabaseEnvironment


def _clean_anivag_environment(monkeypatch) -> None:
    for name in tuple(os.environ):
        if name.startswith("ANIVAG_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(
        "ANIVAG_DATABASE_URL",
        "mysql+pymysql://user:password@127.0.0.1/anivag_development",
    )
    monkeypatch.setenv("ANIVAG_QDRANT_URL", "http://127.0.0.1:6333")
    monkeypatch.setenv(
        "ANIVAG_QDRANT_COLLECTION",
        "anivag_video_chunks_development",
    )


def test_server_settings_load_upload_parse_agent_and_mq_defaults(monkeypatch) -> None:
    _clean_anivag_environment(monkeypatch)

    settings = ServerSettings.from_environment()

    assert settings.environment is DatabaseEnvironment.DEVELOPMENT
    assert settings.upload.preferred_segment_size_bytes == 8 * 1024 * 1024
    assert settings.parse.chunk_duration_ms == 60_000
    assert settings.parse.context_overlap_ms == 3_000
    assert settings.agent.history_message_limit == 12
    assert settings.agent.default_top_k == 8
    assert settings.agent.maximum_top_k == 20
    assert settings.agent.max_loop_turns == 3
    assert settings.agent.max_model_calls_per_turn == 6
    assert settings.agent.max_tool_calls_per_turn == 4
    assert settings.agent.max_output_tokens == 4096
    assert settings.agent.max_prompt_chars == 200_000
    assert settings.agent.execution_timeout_seconds == 180
    assert settings.agent.maximum_concurrent_kicks == 4
    assert settings.agent_query_model is None
    assert settings.agent_chat_model is None
    assert settings.agent_evaluator_model is None
    assert settings.mq.heartbeat_interval < settings.mq.lease_duration
    assert settings.mq.poll_interval_seconds == 2.0
    assert settings.mq.retry_base == timedelta(seconds=30)
    assert settings.mq.retry_max == timedelta(seconds=900)
    assert settings.index_sync.retry_base_seconds == 30
    assert settings.index_sync.retry_max_seconds == 900
    assert settings.text_vector_dimensions == settings.vision_vector_dimensions
    assert settings.sse.poll_interval_seconds == 1.0
    assert settings.sse.keepalive_interval_seconds == 15.0
    assert settings.retrieval.channel_top_k == 20
    assert settings.retrieval.rrf_k == 60
    assert settings.retrieval.text_weight == settings.retrieval.vision_weight == 1.0
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
        "ANIVAG_RETRIEVAL_TEXT_WEIGHT": "0",
        "ANIVAG_RETRIEVAL_VISION_WEIGHT": "1.5",
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
        "ANIVAG_INDEX_SYNC_RETRY_BASE_SECONDS": "7",
        "ANIVAG_INDEX_SYNC_RETRY_MAX_SECONDS": "70",
    }
    for name, value in overrides.items():
        monkeypatch.setenv(name, value)

    settings = ServerSettings.from_environment()

    assert settings.sse.poll_interval_seconds == 0.5
    assert settings.sse.keepalive_interval_seconds == 8
    assert settings.retrieval.channel_top_k == 32
    assert settings.retrieval.rrf_k == 45
    assert settings.retrieval.text_weight == 0
    assert settings.retrieval.vision_weight == 1.5
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
    assert settings.index_sync.retry_base_seconds == 7
    assert settings.index_sync.retry_max_seconds == 70


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
    assert settings.index_sync.retry_base_seconds == 20
    assert settings.index_sync.retry_max_seconds == 200


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ANIVAG_SSE_POLL_SECONDS", "0"),
        ("ANIVAG_SSE_KEEPALIVE_SECONDS", "-1"),
        ("ANIVAG_SSE_KEEPALIVE_SECONDS", "nan"),
        ("ANIVAG_VIDEO_EMBEDDING_FPS", "inf"),
        ("ANIVAG_RETRIEVAL_CHANNEL_TOP_K", "0"),
        ("ANIVAG_RETRIEVAL_RRF_K", "-1"),
        ("ANIVAG_RETRIEVAL_TEXT_WEIGHT", "nan"),
        ("ANIVAG_RETRIEVAL_VISION_WEIGHT", "-1"),
        ("ANIVAG_DATABASE_POOL_RECYCLE_SECONDS", "0"),
        ("ANIVAG_MAX_IMAGE_UPLOAD_SIZE_BYTES", "0"),
        ("ANIVAG_MAX_IMAGE_PIXELS", "-1"),
        ("ANIVAG_BACKGROUND_IMAGE_MAX_EDGE", "0"),
        ("ANIVAG_LOG_LEVEL", "invalid"),
        ("ANIVAG_LOG_MAX_BYTES", "0"),
        ("ANIVAG_LOG_BACKUP_COUNT", "-1"),
        ("ANIVAG_MQ_POLL_SECONDS", "0"),
        ("ANIVAG_INDEX_SYNC_RETRY_BASE_SECONDS", "0"),
        ("ANIVAG_INDEX_SYNC_RETRY_MAX_SECONDS", "0"),
    ],
)
def test_server_settings_reject_invalid_runtime_tuning(monkeypatch, name, value):
    _clean_anivag_environment(monkeypatch)
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError):
        ServerSettings.from_environment()


def test_server_settings_rejects_invalid_mq_and_index_retry_ranges(monkeypatch):
    _clean_anivag_environment(monkeypatch)
    monkeypatch.setenv("ANIVAG_MQ_LEASE_SECONDS", "60")
    monkeypatch.setenv("ANIVAG_MQ_HEARTBEAT_SECONDS", "60")
    with pytest.raises(ValueError, match="heartbeat_interval"):
        ServerSettings.from_environment()

    monkeypatch.setenv("ANIVAG_MQ_HEARTBEAT_SECONDS", "30")
    monkeypatch.setenv("ANIVAG_INDEX_SYNC_RETRY_BASE_SECONDS", "90")
    monkeypatch.setenv("ANIVAG_INDEX_SYNC_RETRY_MAX_SECONDS", "30")
    with pytest.raises(ValueError, match="retry_max_seconds"):
        ServerSettings.from_environment()


def test_server_settings_reject_disabling_both_retrieval_channels(monkeypatch):
    _clean_anivag_environment(monkeypatch)
    monkeypatch.setenv("ANIVAG_RETRIEVAL_TEXT_WEIGHT", "0")
    monkeypatch.setenv("ANIVAG_RETRIEVAL_VISION_WEIGHT", "0")

    with pytest.raises(ValueError, match="at least one channel weight"):
        ServerSettings.from_environment()


def test_server_settings_reject_agent_default_above_maximum(monkeypatch) -> None:
    _clean_anivag_environment(monkeypatch)
    monkeypatch.setenv("ANIVAG_AGENT_DEFAULT_TOP_K", "21")
    monkeypatch.setenv("ANIVAG_AGENT_MAX_TOP_K", "20")

    with pytest.raises(ValueError, match="must be at least"):
        ServerSettings.from_environment()


def test_agent_roles_and_execution_budgets_load_from_settings(monkeypatch):
    _clean_anivag_environment(monkeypatch)
    overrides = {
        "ANIVAG_AGENT_QUERY_MODEL": "query-model",
        "ANIVAG_AGENT_EVALUATOR_MODEL": "review-model",
        "ANIVAG_AGENT_CHAT_MODEL": "output-model",
        "ANIVAG_AGENT_MAX_LOOP_TURNS": "2",
        "ANIVAG_AGENT_MAX_MODEL_CALLS_PER_TURN": "5",
        "ANIVAG_AGENT_MAX_TOOL_CALLS_PER_TURN": "3",
        "ANIVAG_AGENT_MAX_OUTPUT_TOKENS": "2048",
        "ANIVAG_AGENT_MAX_PROMPT_CHARS": "120000",
        "ANIVAG_AGENT_EXECUTION_TIMEOUT_SECONDS": "90",
        "ANIVAG_AGENT_MAX_CONCURRENT_KICKS": "2",
    }
    for name, value in overrides.items():
        monkeypatch.setenv(name, value)
    settings = ServerSettings.from_environment()
    assert (settings.agent_query_model, settings.agent_evaluator_model, settings.agent_chat_model) == ("query-model", "review-model", "output-model")
    assert settings.agent.max_loop_turns == 2
    assert settings.agent.max_model_calls_per_turn == 5
    assert settings.agent.max_tool_calls_per_turn == 3
    assert settings.agent.max_output_tokens == 2048
    assert settings.agent.max_prompt_chars == 120_000
    assert settings.agent.execution_timeout_seconds == 90
    assert settings.agent.maximum_concurrent_kicks == 2


@pytest.mark.parametrize("name,value", [
    ("ANIVAG_AGENT_MAX_LOOP_TURNS", "4"),
    ("ANIVAG_AGENT_MAX_MODEL_CALLS_PER_TURN", "0"),
    ("ANIVAG_AGENT_MAX_TOOL_CALLS_PER_TURN", "0"),
    ("ANIVAG_AGENT_MAX_OUTPUT_TOKENS", "0"),
    ("ANIVAG_AGENT_MAX_PROMPT_CHARS", "0"),
    ("ANIVAG_AGENT_EXECUTION_TIMEOUT_SECONDS", "nan"),
    ("ANIVAG_AGENT_EXECUTION_TIMEOUT_SECONDS", "0"),
    ("ANIVAG_AGENT_MAX_CONCURRENT_KICKS", "0"),
])
def test_invalid_agent_execution_budget_is_rejected(monkeypatch, name, value):
    _clean_anivag_environment(monkeypatch)
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        ServerSettings.from_environment()
