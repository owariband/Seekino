from __future__ import annotations

import os

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


def test_server_settings_load_upload_parse_agent_and_worker_defaults(monkeypatch) -> None:
    _clean_anivag_environment(monkeypatch)

    settings = ServerSettings.from_environment()

    assert settings.environment is DatabaseEnvironment.DEVELOPMENT
    assert settings.upload.preferred_segment_size_bytes == 8 * 1024 * 1024
    assert settings.parse.chunk_duration_ms == 60_000
    assert settings.parse.context_overlap_ms == 3_000
    assert settings.agent.history_message_limit == 12
    assert settings.agent.default_top_k == 8
    assert settings.agent.maximum_top_k == 20
    assert settings.agent_chat_model is None
    assert settings.agent_evaluator_model is None
    assert settings.worker_heartbeat_interval < settings.worker_lease_duration
    assert settings.text_vector_dimensions == settings.vision_vector_dimensions
    assert settings.sse.poll_interval_seconds == 1.0
    assert settings.sse.keepalive_interval_seconds == 15.0
    assert settings.retrieval.channel_top_k == 20
    assert settings.retrieval.rrf_k == 60
    assert settings.retrieval.text_weight == settings.retrieval.vision_weight == 1.0
    assert settings.database_pool_recycle_seconds == 3600
    assert settings.video_embedding_model is None
    assert settings.video_embedding_fps is None


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
    ],
)
def test_server_settings_reject_invalid_runtime_tuning(monkeypatch, name, value):
    _clean_anivag_environment(monkeypatch)
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError):
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
