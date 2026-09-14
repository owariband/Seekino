import pytest

from evals.recall.cli import (
    _build_parser,
    _resolve_qdrant_options,
)


def _args(*extra: str):
    return _build_parser().parse_args(
        [
            "--parse-result",
            "parse.json",
            "--fixture",
            "fixture.json",
            "--output-dir",
            "output",
            *extra,
        ]
    )


def test_local_backend_does_not_activate_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("ANIVAG_QDRANT_URL", "http://127.0.0.1:6333")
    monkeypatch.setenv("ANIVAG_QDRANT_COLLECTION", "video_chunks_test")

    assert _resolve_qdrant_options(_args()) is None


def test_qdrant_backend_uses_explicit_dataset_and_environment(monkeypatch) -> None:
    monkeypatch.setenv("ANIVAG_QDRANT_URL", "http://127.0.0.1:6333")
    monkeypatch.setenv("ANIVAG_QDRANT_COLLECTION", "video_chunks_test")

    options = _resolve_qdrant_options(
        _args(
            "--retrieval-backend",
            "qdrant",
            "--dataset-id",
            "test:mygo:ep07:60s:v1",
        )
    )

    assert options == (
        "http://127.0.0.1:6333",
        "video_chunks_test",
        "test:mygo:ep07:60s:v1",
    )


def test_qdrant_backend_requires_complete_isolation_settings(monkeypatch) -> None:
    monkeypatch.delenv("ANIVAG_QDRANT_URL", raising=False)
    monkeypatch.delenv("ANIVAG_QDRANT_COLLECTION", raising=False)

    with pytest.raises(ValueError, match="URL, Collection, and dataset ID"):
        _resolve_qdrant_options(_args("--retrieval-backend", "qdrant"))


def test_local_backend_rejects_qdrant_connection_arguments() -> None:
    with pytest.raises(ValueError, match="require --retrieval-backend qdrant"):
        _resolve_qdrant_options(_args("--dataset-id", "test:mygo:ep07"))
