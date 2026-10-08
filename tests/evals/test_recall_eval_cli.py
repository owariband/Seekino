import pytest
from types import SimpleNamespace

from evals.recall import cli

from evals.recall.cli import (
    _build_parser,
    _resolve_elasticsearch_options,
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
    monkeypatch.setenv("ANIVAG_RECALL_EVAL_ELASTICSEARCH_URL", "http://127.0.0.1:9200")
    monkeypatch.setenv("ANIVAG_RECALL_EVAL_INDEX", "anivag_recall_eval_test")

    assert _resolve_elasticsearch_options(_args()) is None


def test_elasticsearch_backend_uses_explicit_dataset_and_eval_environment(monkeypatch) -> None:
    monkeypatch.setenv("ANIVAG_RECALL_EVAL_ELASTICSEARCH_URL", "http://127.0.0.1:9200")
    monkeypatch.setenv("ANIVAG_RECALL_EVAL_INDEX", "anivag_recall_eval_test")

    options = _resolve_elasticsearch_options(
        _args(
            "--retrieval-backend",
            "elasticsearch",
            "--dataset-id",
            "test:mygo:ep07:60s:v1",
        )
    )

    assert options == (
        "http://127.0.0.1:9200",
        "anivag_recall_eval_test",
        "test:mygo:ep07:60s:v1",
    )


def test_elasticsearch_backend_requires_complete_isolation_settings(monkeypatch) -> None:
    monkeypatch.delenv("ANIVAG_RECALL_EVAL_ELASTICSEARCH_URL", raising=False)
    monkeypatch.delenv("ANIVAG_RECALL_EVAL_INDEX", raising=False)

    with pytest.raises(ValueError, match="URL, dedicated index, and dataset ID"):
        _resolve_elasticsearch_options(_args("--retrieval-backend", "elasticsearch"))


def test_local_backend_rejects_elasticsearch_connection_arguments() -> None:
    with pytest.raises(ValueError, match="require --retrieval-backend elasticsearch"):
        _resolve_elasticsearch_options(_args("--dataset-id", "test:mygo:ep07"))


def test_local_backend_rejects_elasticsearch_mode() -> None:
    with pytest.raises(ValueError, match="search mode requires"):
        _resolve_elasticsearch_options(_args("--elasticsearch-search-mode", "approximate"))


def test_qdrant_backend_is_not_an_execution_choice() -> None:
    with pytest.raises(SystemExit):
        _args("--retrieval-backend", "qdrant")


@pytest.mark.parametrize("duration_delta", [500, 1000, 1001])
def test_cli_video_duration_tolerance_remains_one_second(monkeypatch, duration_delta):
    args = _args()
    monkeypatch.setattr(cli, "_build_parser", lambda: SimpleNamespace(parse_args=lambda: args))
    monkeypatch.setattr(cli, "load_parse_result", lambda path: SimpleNamespace(
        source_video=path.parent / "episode.mp4", duration_seconds=8008
    ))
    monkeypatch.setattr(cli, "load_recall_fixture", lambda path: object())
    monkeypatch.setattr(cli, "probe_video_duration_seconds", lambda path: 8008 + duration_delta)

    def stop_before_model_configuration(path):
        raise RuntimeError("duration check passed")

    monkeypatch.setattr(cli, "load_config", stop_before_model_configuration)
    message = "duration check passed" if duration_delta <= 1000 else "duration does not match"
    with pytest.raises(RuntimeError, match=message):
        cli.main()
