from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from server import cli


def test_init_es_index_is_explicit_and_needs_no_database(monkeypatch, capsys) -> None:
    created = []

    class Client:
        def __init__(self, url, *, api_key):
            self.url = url
            self.api_key = api_key
            self.closed = False
            created.append(self)

        def close(self):
            self.closed = True

    class Store:
        def __init__(self, client, **kwargs):
            assert client is created[0]
            assert kwargs == {
                "index_name": "anivag_video_chunks_test",
                "environment": cli.DatabaseEnvironment.TEST,
                "semantic_dimensions": 2,
                "vision_dimensions": 3,
            }

        def ensure_index(self):
            created[0].ready = True

    monkeypatch.delenv("ANIVAG_DATABASE_URL", raising=False)
    monkeypatch.setattr(cli, "Elasticsearch", Client)
    monkeypatch.setattr(cli, "ElasticsearchVideoChunkStore", Store)
    monkeypatch.setattr(sys, "argv", [
        "anivag-server", "init-es-index", "--environment", "test",
        "--elasticsearch-url", "http://127.0.0.1:9200",
        "--elasticsearch-index", "anivag_video_chunks_test",
        "--elasticsearch-api-key", "test-key",
        "--elasticsearch-index-generation", "7",
        "--text-vector-dimensions", "2",
        "--vision-vector-dimensions", "3",
    ])

    cli.main()

    assert created[0].url == "http://127.0.0.1:9200"
    assert created[0].api_key == "test-key"
    assert created[0].ready and created[0].closed
    assert json.loads(capsys.readouterr().out) == {
        "schemaReady": True,
        "index": "anivag_video_chunks_test",
        "indexGeneration": 7,
    }


def test_init_es_index_rejects_wrong_environment_before_connecting(monkeypatch) -> None:
    monkeypatch.setattr(cli, "Elasticsearch", lambda *_args, **_kwargs: pytest.fail("connected"))
    monkeypatch.setattr(sys, "argv", [
        "anivag-server", "init-es-index", "--environment", "test",
        "--elasticsearch-url", "http://127.0.0.1:9200",
        "--elasticsearch-index", "anivag_video_chunks_development",
    ])

    with pytest.raises(ValueError, match="test component"):
        cli.main()


def test_init_es_index_closes_client_on_schema_error(monkeypatch) -> None:
    clients = []

    class Client:
        def __init__(self, *_args, **_kwargs):
            self.closed = False
            clients.append(self)

        def close(self):
            self.closed = True

    class Store:
        def __init__(self, *_args, **_kwargs):
            pass

        def ensure_index(self):
            raise RuntimeError("mapping differs")

    monkeypatch.setattr(cli, "Elasticsearch", Client)
    monkeypatch.setattr(cli, "ElasticsearchVideoChunkStore", Store)
    monkeypatch.setattr(sys, "argv", [
        "anivag-server", "init-es-index", "--environment", "test",
        "--elasticsearch-url", "http://127.0.0.1:9200",
        "--elasticsearch-index", "anivag_video_chunks_test",
    ])

    with pytest.raises(RuntimeError, match="mapping differs"):
        cli.main()
    assert clients[0].closed


def test_request_es_index_rebuild_schedules_one_v2_task(
    monkeypatch, capsys,
) -> None:
    calls = {}
    engine = object()
    factory = object()

    class Repository:
        def __init__(self, session_factory):
            assert session_factory is factory

        def request_index_rebuild(self, video_parse_task_id):
            calls["task_id"] = video_parse_task_id
            return ("interval-1", "interval-2")

    monkeypatch.setattr(
        cli,
        "create_database_engine",
        lambda url, *, environment: (
            calls.update(database_url=url, environment=environment) or engine
        ),
    )
    monkeypatch.setattr(
        cli,
        "create_session_factory",
        lambda selected: factory if selected is engine else pytest.fail("wrong engine"),
    )
    monkeypatch.setattr(cli, "VideoParseRepository", Repository)
    monkeypatch.setattr(sys, "argv", [
        "anivag-server",
        "request-es-index-rebuild",
        "--database-url",
        "mysql+pymysql://root@127.0.0.1:3306/anivag_test",
        "--environment",
        "test",
        "--video-parse-task-id",
        "task-1",
    ])

    cli.main()

    assert calls == {
        "database_url": "mysql+pymysql://root@127.0.0.1:3306/anivag_test",
        "environment": cli.DatabaseEnvironment.TEST,
        "task_id": "task-1",
    }
    assert json.loads(capsys.readouterr().out) == {
        "videoParseTaskId": "task-1",
        "scheduledIntervalParseTaskIds": ["interval-1", "interval-2"],
    }


def test_business_commands_default_execution_mode_from_environment(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ANIVAG_PARSE_EXECUTION_MODE", "V2")
    parser = cli._build_parser()

    search = parser.parse_args([
        "search", "--query", "crying", "--series-id", "series-1",
    ])
    rebuild = parser.parse_args(["rebuild-index"])

    assert search.execution_mode == "v2"
    assert rebuild.execution_mode == "v2"


def test_rebuild_index_v2_requests_task_without_index_client(
    monkeypatch, capsys,
) -> None:
    calls = {}
    engine = object()
    factory = object()

    class Repository:
        def __init__(self, session_factory):
            assert session_factory is factory

        def request_index_rebuild(self, video_parse_task_id):
            calls["task_id"] = video_parse_task_id
            return ("interval-3",)

    monkeypatch.setattr(
        cli,
        "create_database_engine",
        lambda url, *, environment: (
            calls.update(database_url=url, environment=environment) or engine
        ),
    )
    monkeypatch.setattr(
        cli,
        "create_session_factory",
        lambda selected: factory if selected is engine else pytest.fail("wrong engine"),
    )
    monkeypatch.setattr(cli, "VideoParseRepository", Repository)
    monkeypatch.setattr(
        cli, "QdrantClient", lambda *_args, **_kwargs: pytest.fail("used Qdrant"),
    )
    monkeypatch.setattr(
        cli, "Elasticsearch", lambda *_args, **_kwargs: pytest.fail("used ES"),
    )
    monkeypatch.setattr(sys, "argv", [
        "anivag-server",
        "rebuild-index",
        "--execution-mode",
        "v2",
        "--database-url",
        "mysql+pymysql://root@127.0.0.1:3306/anivag_test",
        "--environment",
        "test",
        "--video-parse-task-id",
        "task-2",
    ])

    cli.main()

    assert calls == {
        "database_url": "mysql+pymysql://root@127.0.0.1:3306/anivag_test",
        "environment": cli.DatabaseEnvironment.TEST,
        "task_id": "task-2",
    }
    assert json.loads(capsys.readouterr().out) == {
        "videoParseTaskId": "task-2",
        "scheduledIntervalParseTaskIds": ["interval-3"],
    }


def test_rebuild_index_v2_requires_video_parse_task_id(monkeypatch) -> None:
    monkeypatch.setattr(cli, "create_database_engine", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(sys, "argv", [
        "anivag-server",
        "rebuild-index",
        "--execution-mode",
        "v2",
        "--database-url",
        "mysql+pymysql://root@127.0.0.1:3306/anivag_test",
        "--environment",
        "test",
    ])

    with pytest.raises(ValueError, match="--video-parse-task-id"):
        cli.main()


def test_search_v2_uses_only_elasticsearch_and_closes_client(
    monkeypatch, capsys,
) -> None:
    calls = {}
    clients = []
    engine = object()
    factory = object()

    class Client:
        def __init__(self, url, *, api_key):
            calls["client"] = (url, api_key)
            self.closed = False
            clients.append(self)

        def close(self):
            self.closed = True

    class Store:
        def __init__(self, client, **kwargs):
            assert client is clients[0]
            calls["store"] = kwargs

    class Repository:
        def __init__(self, session_factory):
            assert session_factory is factory
            calls["repository"] = "v2"

    class Embedder:
        def __init__(self, model, *, output_dimensionality, fps):
            calls["embedder"] = (model, output_dimensionality, fps)

    class Result:
        def model_dump(self, *, mode, by_alias):
            assert (mode, by_alias) == ("json", True)
            return {"backend": "elasticsearch"}

    class Retrieval:
        def __init__(self, repository, store, embedder, **kwargs):
            assert isinstance(repository, Repository)
            assert isinstance(store, Store)
            assert isinstance(embedder, Embedder)
            calls["retrieval"] = kwargs

        def search(self, query, **kwargs):
            calls["search"] = (query, kwargs)
            return Result()

    monkeypatch.setattr(
        cli,
        "create_database_engine",
        lambda url, *, environment: (
            calls.update(database_url=url, environment=environment) or engine
        ),
    )
    monkeypatch.setattr(
        cli,
        "create_session_factory",
        lambda selected: factory if selected is engine else pytest.fail("wrong engine"),
    )
    monkeypatch.setattr(cli, "Elasticsearch", Client)
    monkeypatch.setattr(cli, "ElasticsearchVideoChunkStore", Store)
    monkeypatch.setattr(cli, "VideoParseRepository", Repository)
    monkeypatch.setattr(cli, "DoubaoMultimodalEmbedder", Embedder)
    monkeypatch.setattr(cli, "VideoParseRetrievalService", Retrieval)
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda path: SimpleNamespace(
            multimodal_embedding=SimpleNamespace(model="configured-model", fps=2.0)
        ),
    )
    monkeypatch.setattr(
        cli, "QdrantClient", lambda *_args, **_kwargs: pytest.fail("used Qdrant"),
    )
    monkeypatch.setattr(sys, "argv", [
        "anivag-server",
        "search",
        "--execution-mode",
        "v2",
        "--database-url",
        "mysql+pymysql://root@127.0.0.1:3306/anivag_test",
        "--environment",
        "test",
        "--elasticsearch-url",
        "http://127.0.0.1:9200",
        "--elasticsearch-index",
        "anivag_video_chunks_test",
        "--elasticsearch-index-generation",
        "7",
        "--elasticsearch-api-key",
        "test-key",
        "--text-vector-dimensions",
        "2",
        "--vision-vector-dimensions",
        "2",
        "--embedding-model",
        "cli-model",
        "--query",
        "why are they crying",
        "--workspace-id",
        "workspace-1",
        "--series-id",
        "series-1",
        "--series-id",
        "series-2",
        "--top-k",
        "4",
        "--channel-top-k",
        "8",
        "--rrf-k",
        "30",
        "--text-weight",
        "2",
        "--vision-weight",
        "3",
        "--exact",
    ])

    cli.main()

    assert calls["client"] == ("http://127.0.0.1:9200", "test-key")
    assert calls["store"] == {
        "index_name": "anivag_video_chunks_test",
        "environment": cli.DatabaseEnvironment.TEST,
        "semantic_dimensions": 2,
        "vision_dimensions": 2,
    }
    assert calls["embedder"] == ("cli-model", 2, 2.0)
    retrieval_options = calls["retrieval"]
    semantic_encoder = retrieval_options.pop("semantic_encoder")
    assert semantic_encoder.claims() == {
        "provider": "ark",
        "model": "cli-model",
        "encoder_revision": "gateway-ark-embedding-v1",
        "dimensions": 2,
        "normalization": "none",
        "output_contract": "float-vector-v1",
    }
    assert retrieval_options == {
        "expected_index_generation": 7,
        "channel_top_k": 8,
        "rrf_k": 30,
        "text_weight": 2.0,
        "vision_weight": 3.0,
        "exact": True,
    }
    assert calls["search"] == (
        "why are they crying",
        {
            "workspace_id": "workspace-1",
            "series_ids": ["series-1", "series-2"],
            "top_k": 4,
        },
    )
    assert clients[0].closed
    assert json.loads(capsys.readouterr().out) == {"backend": "elasticsearch"}


def test_search_v2_closes_elasticsearch_client_on_failure(monkeypatch) -> None:
    clients = []

    class Client:
        def __init__(self, *_args, **_kwargs):
            self.closed = False
            clients.append(self)

        def close(self):
            self.closed = True

    class Retrieval:
        def __init__(self, *_args, **_kwargs):
            pass

        def search(self, *_args, **_kwargs):
            raise RuntimeError("search failed")

    monkeypatch.setattr(cli, "create_database_engine", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(cli, "create_session_factory", lambda _engine: object())
    monkeypatch.setattr(cli, "Elasticsearch", Client)
    monkeypatch.setattr(cli, "ElasticsearchVideoChunkStore", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(cli, "VideoParseRepository", lambda _factory: object())
    monkeypatch.setattr(cli, "DoubaoMultimodalEmbedder", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(cli, "VideoParseRetrievalService", Retrieval)
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda _path: SimpleNamespace(
            multimodal_embedding=SimpleNamespace(model="configured-model", fps=2.0)
        ),
    )
    monkeypatch.setattr(sys, "argv", [
        "anivag-server",
        "search",
        "--execution-mode",
        "v2",
        "--database-url",
        "mysql+pymysql://root@127.0.0.1:3306/anivag_test",
        "--environment",
        "test",
        "--elasticsearch-url",
        "http://127.0.0.1:9200",
        "--elasticsearch-index",
        "anivag_video_chunks_test",
        "--query",
        "query",
        "--series-id",
        "series-1",
    ])

    with pytest.raises(RuntimeError, match="search failed"):
        cli.main()
    assert clients[0].closed


def test_search_v1_uses_only_qdrant_and_closes_client(monkeypatch, capsys) -> None:
    calls = {}
    clients = []
    engine = object()
    factory = object()

    class Client:
        def __init__(self, *, url, api_key):
            calls["client"] = (url, api_key)
            self.closed = False
            clients.append(self)

        def close(self):
            self.closed = True

    class Store:
        def __init__(self, client, **kwargs):
            assert client is clients[0]
            calls["store"] = kwargs

    class Repository:
        def __init__(self, session_factory):
            assert session_factory is factory

    class Result:
        def model_dump(self, *, mode, by_alias):
            assert (mode, by_alias) == ("json", True)
            return {"backend": "qdrant"}

    class Retrieval:
        def __init__(self, repository, store, embedder, **kwargs):
            assert isinstance(repository, Repository)
            assert isinstance(store, Store)
            calls["retrieval"] = kwargs

        def search(self, query, **kwargs):
            calls["search"] = (query, kwargs)
            return Result()

    monkeypatch.setattr(cli, "create_database_engine", lambda *_args, **_kwargs: engine)
    monkeypatch.setattr(
        cli,
        "create_session_factory",
        lambda selected: factory if selected is engine else pytest.fail("wrong engine"),
    )
    monkeypatch.setattr(cli, "QdrantClient", Client)
    monkeypatch.setattr(cli, "QdrantVideoChunkStore", Store)
    monkeypatch.setattr(cli, "VideoRepository", Repository)
    monkeypatch.setattr(cli, "DoubaoMultimodalEmbedder", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(cli, "VideoRetrievalService", Retrieval)
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda _path: SimpleNamespace(
            multimodal_embedding=SimpleNamespace(model="configured-model", fps=2.0)
        ),
    )
    monkeypatch.setattr(
        cli, "Elasticsearch", lambda *_args, **_kwargs: pytest.fail("used ES"),
    )
    monkeypatch.setattr(sys, "argv", [
        "anivag-server",
        "search",
        "--execution-mode",
        "v1",
        "--database-url",
        "mysql+pymysql://root@127.0.0.1:3306/anivag_test",
        "--environment",
        "test",
        "--qdrant-url",
        "http://127.0.0.1:6333",
        "--qdrant-collection",
        "anivag_video_chunks_test",
        "--query",
        "query",
        "--series-id",
        "series-1",
    ])

    cli.main()

    assert calls["client"] == ("http://127.0.0.1:6333", None)
    assert clients[0].closed
    assert json.loads(capsys.readouterr().out) == {"backend": "qdrant"}


def test_compose_keeps_elasticsearch_out_of_default_services() -> None:
    compose = yaml.safe_load((Path(__file__).resolve().parents[2] / "compose.yaml").read_text())
    services = compose["services"]
    assert "profiles" not in services["mysql"]
    assert "profiles" not in services["qdrant"]
    assert services["elasticsearch"]["profiles"] == ["elasticsearch"]
    assert services["elasticsearch"]["image"].endswith(":8.19.22")
    assert "elasticsearch_data" in compose["volumes"]
