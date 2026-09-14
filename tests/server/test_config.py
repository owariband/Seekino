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


def test_server_settings_load_upload_parse_and_worker_defaults(monkeypatch) -> None:
    _clean_anivag_environment(monkeypatch)

    settings = ServerSettings.from_environment()

    assert settings.environment is DatabaseEnvironment.DEVELOPMENT
    assert settings.upload.preferred_segment_size_bytes == 8 * 1024 * 1024
    assert settings.parse.chunk_duration_ms == 60_000
    assert settings.parse.context_overlap_ms == 3_000
    assert settings.worker_heartbeat_interval < settings.worker_lease_duration
    assert settings.text_vector_dimensions == settings.vision_vector_dimensions


def test_server_settings_reject_cross_modal_dimension_mismatch(monkeypatch) -> None:
    _clean_anivag_environment(monkeypatch)
    monkeypatch.setenv("ANIVAG_TEXT_VECTOR_DIMENSIONS", "1024")
    monkeypatch.setenv("ANIVAG_VISION_VECTOR_DIMENSIONS", "768")

    with pytest.raises(ValueError, match="must match"):
        ServerSettings.from_environment()
