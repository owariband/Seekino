from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from gateway.config import load_config
from server import cli
from server.common import retrieval as retrieval_bootstrap
from server.caller import elasticsearch as elasticsearch_caller, mysql as mysql_caller
from server.config.parse_profile import default_video_parse_v2_config
from server.repository.video_parse import VideoParseRetrievalScope
from server.service.video_retrieval import RetrievalIndexConsistencyError


def test_hydrate_keeps_database_evidence_output(monkeypatch, capsys) -> None:
    factory = object()
    chunk_ids = ["chunk-1", "chunk-2"]

    class Repository:
        def __init__(self, session_factory):
            assert session_factory is factory

        def hydrate_video_chunks(self, selected_ids):
            assert selected_ids == chunk_ids
            return [SimpleNamespace(model_dump=lambda **kwargs: {
                "videoChunkId": "chunk-1", "retrievalText": "原有数据库证据",
            })]

    monkeypatch.setattr(mysql_caller, "create_database_engine", lambda *_args, **_kwargs: SimpleNamespace(dispose=lambda: None))
    monkeypatch.setattr(mysql_caller, "create_session_factory", lambda _engine: factory)
    monkeypatch.setattr(cli, "VideoRepository", Repository)
    monkeypatch.setattr(sys, "argv", [
        "anivag-server", "hydrate", *chunk_ids,
        "--database-url", "sqlite+pysqlite:///:memory:", "--environment", "test",
    ])

    cli.main()

    assert json.loads(capsys.readouterr().out) == [{
        "videoChunkId": "chunk-1", "retrievalText": "原有数据库证据",
    }]


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
    monkeypatch.setattr(elasticsearch_caller, "Elasticsearch", Client)
    monkeypatch.setattr(elasticsearch_caller, "ElasticsearchVideoChunkStore", Store)
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


def test_init_event_index_uses_separate_target_and_full_contract_without_database(monkeypatch, capsys):
    from contextlib import contextmanager

    calls = {}
    client = object()

    @contextmanager
    def storage(config, **kwargs):
        calls["config"] = config
        calls["storage"] = kwargs
        yield client, object()  # The Chunk store must not be initialized by this command.

    class EventStore:
        index_name = "seekino_video_events_test_v1"

        def ensure_index(self, contract):
            calls["contract"] = contract

    def build_event_store(selected, config, **kwargs):
        assert selected is client
        assert config.event_index_for_environment(cli.DatabaseEnvironment.TEST) == EventStore.index_name
        calls["event_storage"] = kwargs
        return EventStore()

    monkeypatch.setattr(cli, "open_elasticsearch_storage", storage)
    monkeypatch.setattr(cli, "build_event_elasticsearch_storage", build_event_store)
    monkeypatch.setattr(cli, "open_database_storage", lambda *_args, **_kwargs: pytest.fail("used database"))
    monkeypatch.setattr(sys, "argv", [
        "anivag-server", "init-event-index", "--environment", "test",
        "--elasticsearch-url", "http://127.0.0.1:9200",
        "--elasticsearch-index", "anivag_video_chunks_test",
        "--event-index", EventStore.index_name, "--max-height", "480",
    ])
    cli.main()

    contract = calls["contract"]
    assert contract.capture.max_height == 480
    assert contract.semantic_encoder == contract.vision_encoder
    assert calls["event_storage"] == calls["storage"]
    assert json.loads(capsys.readouterr().out) == {
        "schemaReady": True, "index": EventStore.index_name, "contractId": contract.contract_id,
    }


def test_event_status_is_read_only_and_does_not_allocate_index_or_models(monkeypatch, capsys):
    from contextlib import contextmanager

    factory = object()

    @contextmanager
    def database(*_args, **_kwargs):
        yield object(), factory

    class Repository:
        def __init__(self, sessions):
            assert sessions is factory

        def event_index_status(self, task_id):
            assert task_id == "task-1"
            return {"target": None, "covered_chunks": 0}

    monkeypatch.setattr(cli, "open_database_storage", database)
    monkeypatch.setattr(cli, "VideoParseRepository", Repository)
    monkeypatch.setattr(cli, "open_elasticsearch_storage", lambda *_args, **_kwargs: pytest.fail("used ES"))
    monkeypatch.setattr(cli, "load_config", lambda *_args, **_kwargs: pytest.fail("loaded model"))
    monkeypatch.setattr(sys, "argv", ["anivag-server", "event-index-status", "--environment", "test",
                                     "--database-url", "sqlite+pysqlite:///:memory:",
                                     "--video-parse-task-id", "task-1"])
    cli.main()
    assert json.loads(capsys.readouterr().out) == {"target": None, "covered_chunks": 0}


def test_event_backfill_uses_task_encoder_and_shared_event_capture_settings(monkeypatch, capsys):
    from contextlib import contextmanager

    config = default_video_parse_v2_config().model_copy(update={
        "visual_input": default_video_parse_v2_config().visual_input.model_copy(update={"max_height": 360}),
    })
    calls = {}

    @contextmanager
    def database(*_args, **_kwargs):
        yield object(), object()

    class Repository:
        def __init__(self, _sessions):
            pass

        def load_task_input(self, task_id):
            assert task_id == "task-1"
            return SimpleNamespace(task=SimpleNamespace(parse_config=config.model_dump(mode="json")))

        def request_index_rebuild(self, task_id, *, event_target):
            calls.update(task_id=task_id, target=event_target)
            return ("chunk-1",)

    monkeypatch.setattr(cli, "open_database_storage", database)
    monkeypatch.setattr(cli, "VideoParseRepository", Repository)
    monkeypatch.setenv("ANIVAG_PARSE_MAX_HEIGHT", "480")
    monkeypatch.setattr(cli, "open_elasticsearch_storage", lambda *_args, **_kwargs: pytest.fail("used ES"))
    monkeypatch.setattr(sys, "argv", ["anivag-server", "request-es-index-rebuild", "--environment", "test",
        "--database-url", "sqlite+pysqlite:///:memory:", "--video-parse-task-id", "task-1", "--with-events",
        "--elasticsearch-url", "http://127.0.0.1:9200", "--elasticsearch-index", "anivag_video_chunks_test",
        "--event-index", "seekino_video_events_test_v1"])
    cli.main()
    target = calls["target"]
    assert target["contract"]["capture"]["max_height"] == 480
    assert target["contract"]["semantic_encoder"]["model"] == (
        config.models.embedding.encoder_model or config.models.embedding.model
    )
    assert target["index_name"] == "seekino_video_events_test_v1"
    assert json.loads(capsys.readouterr().out)["scheduledChunkTaskIds"] == ["chunk-1"]


def test_init_es_index_rejects_wrong_environment_before_connecting(monkeypatch) -> None:
    monkeypatch.setattr(elasticsearch_caller, "Elasticsearch", lambda *_args, **_kwargs: pytest.fail("connected"))
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

    monkeypatch.setattr(elasticsearch_caller, "Elasticsearch", Client)
    monkeypatch.setattr(elasticsearch_caller, "ElasticsearchVideoChunkStore", Store)
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
    engine = SimpleNamespace(dispose=lambda: None)
    factory = object()

    class Repository:
        def __init__(self, session_factory):
            assert session_factory is factory

        def request_index_rebuild(self, video_parse_task_id):
            calls["task_id"] = video_parse_task_id
            return ("interval-1", "interval-2")

    monkeypatch.setattr(
        mysql_caller,
        "create_database_engine",
        lambda url, *, environment, pool_recycle_seconds: (
            calls.update(database_url=url, environment=environment) or engine
        ),
    )
    monkeypatch.setattr(
        mysql_caller,
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
        "scheduledChunkTaskIds": ["interval-1", "interval-2"],
    }


@pytest.mark.parametrize("configured_mode", [None, "V2"])
def test_business_commands_default_execution_mode_from_environment(
    monkeypatch, configured_mode,
) -> None:
    if configured_mode is None:
        monkeypatch.delenv("ANIVAG_PARSE_EXECUTION_MODE", raising=False)
    else:
        monkeypatch.setenv("ANIVAG_PARSE_EXECUTION_MODE", configured_mode)
    parser = cli._build_parser()

    search = parser.parse_args([
        "search", "--query", "crying", "--series-id", "series-1",
    ])
    rebuild = parser.parse_args(["rebuild-index"])

    assert cli.resolve_execution_mode(search.execution_mode) == "v2"
    assert cli.resolve_execution_mode(rebuild.execution_mode) == "v2"


def test_rebuild_index_v2_requests_task_without_index_client(
    monkeypatch, capsys,
) -> None:
    calls = {}
    engine = SimpleNamespace(dispose=lambda: None)
    factory = object()

    class Repository:
        def __init__(self, session_factory):
            assert session_factory is factory

        def request_index_rebuild(self, video_parse_task_id):
            calls["task_id"] = video_parse_task_id
            return ("interval-3",)

    monkeypatch.setattr(
        mysql_caller,
        "create_database_engine",
        lambda url, *, environment, pool_recycle_seconds: (
            calls.update(database_url=url, environment=environment) or engine
        ),
    )
    monkeypatch.setattr(
        mysql_caller,
        "create_session_factory",
        lambda selected: factory if selected is engine else pytest.fail("wrong engine"),
    )
    monkeypatch.setattr(cli, "VideoParseRepository", Repository)
    monkeypatch.setattr(
        elasticsearch_caller, "Elasticsearch", lambda *_args, **_kwargs: pytest.fail("used ES"),
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
        "scheduledChunkTaskIds": ["interval-3"],
    }


def test_rebuild_index_v2_requires_video_parse_task_id(monkeypatch) -> None:
    monkeypatch.setattr(mysql_caller, "create_database_engine", lambda *_args, **_kwargs: SimpleNamespace(dispose=lambda: None))
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
    engine = SimpleNamespace(dispose=lambda: None)
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
        def __init__(self, *, gateway_client, binding):
            calls["embedder"] = (binding.model, binding.dimensions, binding.video_fps)

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
        mysql_caller,
        "create_database_engine",
        lambda url, *, environment, pool_recycle_seconds: (
            calls.update(database_url=url, environment=environment) or engine
        ),
    )
    monkeypatch.setattr(
        mysql_caller,
        "create_session_factory",
        lambda selected: factory if selected is engine else pytest.fail("wrong engine"),
    )
    monkeypatch.setattr(elasticsearch_caller, "Elasticsearch", Client)
    monkeypatch.setattr(elasticsearch_caller, "ElasticsearchVideoChunkStore", Store)
    monkeypatch.setattr(cli, "VideoParseRepository", Repository)
    monkeypatch.setattr(retrieval_bootstrap, "_LazyQueryEmbedder", Embedder)
    monkeypatch.setattr(retrieval_bootstrap, "VideoParseRetrievalService", Retrieval)
    configured = load_config()
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda _path: configured.model_copy(update={
            "normalized_models": {
                **configured.normalized_models,
                "embedding": configured.resolve_embedding_binding(environ={}).model_copy(
                    update={"model": "configured-model", "video_fps": 2.0},
                ),
            },
        }),
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
        "expected_embedding_source": "ark_default",
        "lexical_weight": 1.0,
        "candidate_budget": None,
        "sparse_weight": 0.0,
        "reranker": None,
        "tracing": None,
        "channel_top_k": 8,
        "rrf_k": 30,
        "text_weight": 2.0,
        "vision_weight": 3.0,
        "exact": True,
    }
    assert calls["search"] == (
        "why are they crying",
        {
            "tenant_id": "workspace-1",
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

    monkeypatch.setattr(mysql_caller, "create_database_engine", lambda *_args, **_kwargs: SimpleNamespace(dispose=lambda: None))
    monkeypatch.setattr(mysql_caller, "create_session_factory", lambda _engine: object())
    monkeypatch.setattr(elasticsearch_caller, "Elasticsearch", Client)
    monkeypatch.setattr(elasticsearch_caller, "ElasticsearchVideoChunkStore", lambda *_args, **_kwargs: SimpleNamespace(dispose=lambda: None))
    monkeypatch.setattr(cli, "VideoParseRepository", lambda _factory: object())
    monkeypatch.setattr(retrieval_bootstrap, "_LazyQueryEmbedder", lambda *_args, **_kwargs: SimpleNamespace(dispose=lambda: None))
    monkeypatch.setattr(retrieval_bootstrap, "VideoParseRetrievalService", Retrieval)
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda _path: load_config().model_copy(update={
            "multimodal_embedding": load_config().multimodal_embedding.model_copy(
                update={"model": "configured-model", "fps": 2.0},
            ),
        }),
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


@pytest.mark.parametrize("command", ["search", "rebuild-index"])
def test_business_commands_reject_retired_execution_mode(monkeypatch, command):
    monkeypatch.delenv("ANIVAG_PARSE_EXECUTION_MODE", raising=False)
    arguments = [command, "--execution-mode", "v1"]
    if command == "search":
        arguments.extend(["--query", "crying", "--series-id", "series-1"])
    with pytest.raises(SystemExit) as error:
        cli._build_parser().parse_args(arguments)
    assert error.value.code == 2


@pytest.mark.parametrize("command", ["sync-index", "reconcile-index"])
def test_cli_does_not_expose_retired_qdrant_commands(monkeypatch, command):
    monkeypatch.delenv("ANIVAG_PARSE_EXECUTION_MODE", raising=False)
    with pytest.raises(SystemExit) as error:
        cli._build_parser().parse_args([command])
    assert error.value.code == 2


def test_cli_rejects_retired_environment_mode_for_search(monkeypatch):
    monkeypatch.setenv("ANIVAG_PARSE_EXECUTION_MODE", "v1")
    monkeypatch.setattr(sys, "argv", [
        "anivag-server", "search", "--query", "query", "--series-id", "series",
    ])
    with pytest.raises(ValueError, match="V1 execution is retired"):
        cli.main()


def test_compose_defaults_to_mysql_and_elasticsearch() -> None:
    compose = yaml.safe_load((Path(__file__).resolve().parents[2] / "compose.yaml").read_text())
    services = compose["services"]
    assert "profiles" not in services["mysql"]
    assert "qdrant" not in services
    assert "profiles" not in services["elasticsearch"]
    assert services["elasticsearch"]["image"].endswith(":8.19.22")
    assert "elasticsearch_data" in compose["volumes"]


@pytest.mark.parametrize("fails", [False, True])
def test_init_db_ignores_unrelated_configuration_and_disposes_engine(
    monkeypatch, capsys, fails,
):
    closed = []
    engine = SimpleNamespace(dispose=lambda: closed.append("database"))
    monkeypatch.setenv("ANIVAG_MODEL_CONFIG", "/missing/model-config.yaml")
    monkeypatch.setenv("ANIVAG_PARSE_EXECUTION_MODE", "v1")
    monkeypatch.setenv("ANIVAG_ELASTICSEARCH_INDEX_GENERATION", "invalid")
    monkeypatch.setenv("ANIVAG_TEXT_VECTOR_DIMENSIONS", "invalid")
    monkeypatch.setenv("ANIVAG_RETRIEVAL_TEXT_WEIGHT", "invalid")
    monkeypatch.setattr(mysql_caller, "create_database_engine", lambda *args, **kwargs: engine)
    monkeypatch.setattr(mysql_caller, "create_session_factory", lambda _engine: object())
    monkeypatch.setattr(elasticsearch_caller, "Elasticsearch", lambda *args, **kwargs: pytest.fail("used ES"))
    monkeypatch.setattr(cli, "load_config", lambda _path: pytest.fail("loaded model config"))

    def create_schema(selected):
        assert selected is engine
        if fails:
            raise RuntimeError("schema failed")

    monkeypatch.setattr(cli, "create_schema", create_schema)
    monkeypatch.setattr(sys, "argv", [
        "anivag-server", "init-db", "--database-url", "sqlite+pysqlite:///:memory:",
        "--environment", "test",
    ])
    if fails:
        with pytest.raises(RuntimeError, match="schema failed"):
            cli.main()
    else:
        cli.main()
        assert json.loads(capsys.readouterr().out) == {
            "schemaCreated": True, "environment": "test",
        }
    assert closed == ["database"]


@pytest.mark.parametrize("failure", ["sessions", "store"])
def test_cli_closes_resources_when_storage_construction_fails(monkeypatch, failure):
    closed = []

    def fail(*args, **kwargs):
        raise RuntimeError("construction failed")

    if failure == "sessions":
        engine = SimpleNamespace(dispose=lambda: closed.append("database"))
        monkeypatch.setattr(mysql_caller, "create_database_engine", lambda *args, **kwargs: engine)
        monkeypatch.setattr(mysql_caller, "create_session_factory", fail)
        argv = ["init-db", "--database-url", "sqlite+pysqlite:///:memory:"]
    else:
        client = SimpleNamespace(close=lambda: closed.append("elasticsearch"))
        monkeypatch.setattr(elasticsearch_caller, "Elasticsearch", lambda *args, **kwargs: client)
        monkeypatch.setattr(elasticsearch_caller, "ElasticsearchVideoChunkStore", fail)
        argv = [
            "init-es-index", "--elasticsearch-url", "http://127.0.0.1:9200",
            "--elasticsearch-index", "construction_test",
        ]
    monkeypatch.setattr(sys, "argv", ["anivag-server", *argv, "--environment", "test"])
    with pytest.raises(RuntimeError, match="construction failed"):
        cli.main()
    assert closed == ["database" if failure == "sessions" else "elasticsearch"]


@pytest.mark.parametrize("failure", [None, "model", "sdk", "source"])
@pytest.mark.parametrize(
    "timeout_override,rerank_timeout,expected_timeout,expected_rerank_timeout",
    [(None, 600, 480, 480), (45, 600, 45, 45), (None, 30, 480, 30)],
)
def test_cli_search_uses_real_endpoint_contract_selected_key_and_shared_policy(
    monkeypatch, tmp_path, capsys, failure,
    timeout_override, rerank_timeout, expected_timeout, expected_rerank_timeout,
):
    actual_model = "doubao-embedding-vision-250615"
    series_id = "11111111-1111-4111-8111-111111111111"
    video_id = "22222222-2222-4222-8222-222222222222"
    task_id = "33333333-3333-4333-8333-333333333333"
    config_path = tmp_path / "models.yaml"
    payload = yaml.safe_load((Path(__file__).resolve().parents[2] / "config.yaml").read_text())
    payload["model_sources"]["ark_default"]["api_key_env"] = "CLI_EMBEDDING_KEY"
    payload["agent_runtime"]["execution_timeout_seconds"] = 480
    payload["models"]["rerank"]["request_timeout_seconds"] = rerank_timeout
    config_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.setenv("CLI_EMBEDDING_KEY", "selected-key")
    if timeout_override is None:
        monkeypatch.delenv("ANIVAG_AGENT_REQUEST_TIMEOUT_SECONDS", raising=False)
    else:
        monkeypatch.setenv("ANIVAG_AGENT_REQUEST_TIMEOUT_SECONDS", str(timeout_override))
    monkeypatch.setenv("ANIVAG_VIDEO_EMBEDDING_FPS", "2")
    monkeypatch.setenv("ANIVAG_RETRIEVAL_CHANNEL_TOP_K", "12")
    monkeypatch.setenv("ANIVAG_RETRIEVAL_RRF_K", "48")
    monkeypatch.setenv("ANIVAG_RETRIEVAL_LEXICAL_WEIGHT", "0.5")
    monkeypatch.setenv("ANIVAG_RETRIEVAL_TEXT_WEIGHT", "0.4")
    monkeypatch.setenv("ANIVAG_RETRIEVAL_VISION_WEIGHT", "0.6")
    monkeypatch.setenv("ANIVAG_RETRIEVAL_CANDIDATE_BUDGET", "22")
    monkeypatch.setenv("ANIVAG_RETRIEVAL_RERANK_ENABLED", "true")
    parsed = default_video_parse_v2_config()
    parsed = parsed.model_copy(update={"models": parsed.models.model_copy(update={
        "embedding": parsed.models.embedding.model_copy(update={
            "model": "ep-A", "encoder_model": actual_model, "dimensions": 2,
            "source": "wrong-source" if failure == "source" else "ark_default",
        }),
    })})
    scope = VideoParseRetrievalScope(
        series_ids=(series_id,), searched_video_ids=(video_id,),
        parse_tasks_by_video_id={video_id: task_id}, series_by_video_id={video_id: series_id},
        parse_config=parsed, index_generation=1,
    )
    events, requests, options, services, timeouts = [], [], [], [], []

    class Repository:
        def __init__(self, _sessions):
            pass

        def resolve_retrieval_scope(self, selected_ids, *, tenant_id):
            assert selected_ids == [series_id] and tenant_id == "workspace"
            return scope

    class Store:
        semantic_dimensions = vision_dimensions = 2

        def __init__(self, *_args, **_kwargs):
            pass

        def recall(self, request, *, exact):
            assert exact is True
            return ()

    def embed(**kwargs):
        requests.append(kwargs)
        if failure == "sdk":
            raise RuntimeError("embedding failed")
        return SimpleNamespace(
            model="wrong-model" if failure == "model" else actual_model,
            data=SimpleNamespace(embedding=[1.0, 0.5]),
        )

    def sdk_client(**kwargs):
        options.append(kwargs)
        return SimpleNamespace(
            multimodal_embeddings=SimpleNamespace(create=embed),
            close=lambda: events.append("sdk"),
        )

    engine = SimpleNamespace(dispose=lambda: events.append("database"))
    monkeypatch.setattr(mysql_caller, "create_database_engine", lambda *args, **kwargs: engine)
    monkeypatch.setattr(mysql_caller, "create_session_factory", lambda _engine: object())
    monkeypatch.setattr(elasticsearch_caller, "Elasticsearch", lambda *args, **kwargs: SimpleNamespace(
        close=lambda: events.append("elasticsearch"),
    ))
    monkeypatch.setattr(elasticsearch_caller, "ElasticsearchVideoChunkStore", Store)
    monkeypatch.setattr(cli, "VideoParseRepository", Repository)
    monkeypatch.setattr("arkruntime.Ark.volc", sdk_client)
    build = cli.build_retrieval_service

    def build_and_record(*args, **kwargs):
        timeouts.append(kwargs["request_timeout_seconds"])
        service, encoder = build(*args, **kwargs)
        services.append(service)
        return service, encoder

    monkeypatch.setattr(cli, "build_retrieval_service", build_and_record)
    monkeypatch.setattr(sys, "argv", [
        "anivag-server", "search", "--environment", "test",
        "--database-url", "sqlite+pysqlite:///:memory:",
        "--elasticsearch-url", "http://127.0.0.1:9200",
        "--elasticsearch-index", "endpoint_test",
        "--text-vector-dimensions", "2", "--vision-vector-dimensions", "2",
        "--config", str(config_path), "--embedding-model", "ep-A",
        "--workspace-id", "workspace", "--series-id", series_id,
        "--query", "query", "--channel-top-k", "8", "--exact",
    ])
    if failure:
        message = {"model": "response model differs", "sdk": "embedding failed",
                   "source": "embedding source differs"}[failure]
        error = RuntimeError if failure == "sdk" else RetrievalIndexConsistencyError
        with pytest.raises(error, match=message):
            cli.main()
    else:
        cli.main()
        assert json.loads(capsys.readouterr().out)["hits"] == []
    service = services[0]
    assert timeouts == [expected_timeout]
    assert service._semantic_encoder.model == actual_model
    assert service._expected_embedding_source == "ark_default"
    assert service._embedder._binding.video_fps == 2
    assert service._policy.channel_top_k == 8 and service._policy.rrf_k == 48
    assert service._policy.lexical_weight == 0.5
    assert service._policy.semantic_weight == 0.4 and service._policy.vision_weight == 0.6
    assert service._candidate_budget == 22 and service._reranker is not None
    assert service._reranker._delegate._binding.request_timeout_seconds == expected_rerank_timeout
    if failure == "source":
        assert requests == [] and options == []
        assert events == ["elasticsearch", "database"]
    else:
        assert [request["model"] for request in requests] == ["ep-A"]
        assert options == [{"timeout": expected_timeout, "max_retries": 0, "api_key": "selected-key"}]
        assert events == ["sdk", "elasticsearch", "database"]


def test_cli_and_shared_factories_import_without_agent_runtime():
    script = """
import sys
import server.cli
blocked = ('langgraph', 'agent.chat.supervisor', 'agent.chat.video_search.runtime',
           'agent.video_parse_v2.runtime', 'qdrant_client')
assert not any(name == prefix or name.startswith(prefix + '.')
               for name in sys.modules for prefix in blocked)
"""
    result = subprocess.run([sys.executable, "-B", "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_import_command_passes_series_bible_path_to_importer(monkeypatch, tmp_path, capsys):
    from server.service.parsev1 import parse_import

    received = {}
    engine = SimpleNamespace(dispose=lambda: received.update(disposed=True))
    monkeypatch.setattr(mysql_caller, "create_database_engine", lambda *args, **kwargs: engine)
    monkeypatch.setattr(mysql_caller, "create_session_factory", lambda _engine: object())

    def prepare(*args, **kwargs):
        received.update(kwargs)
        return "prepared"

    monkeypatch.setattr(parse_import, "prepare_video_parse_import", prepare)
    monkeypatch.setattr(cli, "VideoRepository", lambda _sessions: SimpleNamespace(
        import_video_parse=lambda _prepared: SimpleNamespace(model_dump=lambda **kwargs: {"imported": True}),
    ))
    bible = tmp_path / "bible.md"
    monkeypatch.setattr(sys, "argv", [
        "anivag-server", "import-parse", "--database-url", "sqlite+pysqlite:///:memory:",
        "--environment", "test", "--parse-result", "result.json",
        "--canonical-chunk-dir", "chunks", "--series-name", "Series",
        "--episode-order", "1", "--pipeline-version", "v1",
        "--series-bible-file", str(bible),
    ])
    cli.main()
    assert received["series_bible_file"] == bible and received["disposed"] is True
    assert "important_extra_data" not in received
    assert json.loads(capsys.readouterr().out) == {"imported": True}


def test_backfill_cli_uses_configured_media_limits_without_creating_dry_run_storage(
    monkeypatch, tmp_path, capsys,
):
    received = {}
    engine = SimpleNamespace(dispose=lambda: received.update(disposed=True))
    monkeypatch.setenv("ANIVAG_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("ANIVAG_PARSE_MAX_HEIGHT", "480")
    monkeypatch.setattr(mysql_caller, "create_database_engine", lambda *args, **kwargs: engine)
    monkeypatch.setattr(mysql_caller, "create_session_factory", lambda _engine: object())
    monkeypatch.setattr(cli, "VideoRepository", lambda _sessions: "repository")

    def artifact(root, **kwargs):
        received["artifacts"] = (root, kwargs)
        return "artifacts"

    class Backfill:
        def __init__(self, repository, artifacts, *, max_height):
            assert (repository, artifacts) == ("repository", "artifacts")
            received["max_height"] = max_height

        def backfill(self, **kwargs):
            received["backfill"] = kwargs
            return SimpleNamespace(model_dump=lambda **kwargs: {"backfilled": 0})

    monkeypatch.setattr(cli, "build_artifact_storage", artifact)
    monkeypatch.setattr(cli, "KeyframeBackfillService", Backfill)
    monkeypatch.setattr(sys, "argv", [
        "anivag-server", "backfill-keyframes", "--environment", "test",
        "--database-url", "sqlite+pysqlite:///:memory:", "--dry-run",
        "--workspace-id", "workspace", "--series-id", "series", "--limit", "25",
    ])
    cli.main()
    assert received["artifacts"] == (
        tmp_path / "artifacts", {"environment": cli.DatabaseEnvironment.TEST,
                                 "create_base_directory": False},
    )
    assert received["max_height"] == 480
    assert received["backfill"] == {
        "limit": 25, "dry_run": True, "tenant_id": "workspace",
        "series_id": "series", "video_id": None,
    }
    assert received["disposed"] is True
    assert json.loads(capsys.readouterr().out) == {"backfilled": 0}
