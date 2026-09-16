from __future__ import annotations

import os
import sys
from dataclasses import replace

import pytest

from server import bootstrap
from server.api.app import create_app
from server.config import ServerSettings
from server.config.retrieval import RetrievalConfig
from server.config.agent import AgentServiceConfig
from server.config.media import ImageUploadConfig
from server.config.sse import SSEConfig
from server.database import create_schema
from server.mq import __main__ as mq_main


class _QdrantClient:
    def __init__(self, **_kwargs):
        self.closed = False

    def close(self):
        self.closed = True


def test_process_assembly_receives_runtime_configuration(monkeypatch, tmp_path):
    for name in tuple(os.environ):
        if name.startswith("ANIVAG_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANIVAG_ENVIRONMENT", "test")
    monkeypatch.setenv(
        "ANIVAG_DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'bootstrap.sqlite'}"
    )
    monkeypatch.setenv("ANIVAG_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("ANIVAG_QDRANT_URL", "http://127.0.0.1:6333")
    monkeypatch.setenv("ANIVAG_QDRANT_COLLECTION", "bootstrap_test")
    settings = replace(
        ServerSettings.from_environment(),
        sse=SSEConfig(poll_interval_seconds=0.5, keepalive_interval_seconds=9),
        retrieval=RetrievalConfig(
            channel_top_k=35, rrf_k=42, text_weight=0.25, vision_weight=1.5
        ),
        database_pool_recycle_seconds=1200,
        video_embedding_model="configured-embedding-model",
        video_embedding_fps=2.0,
        agent_query_model="configured-query-model",
        agent_evaluator_model="configured-review-model",
        agent_chat_model="configured-chat-model",
        agent_request_timeout_seconds=120,
        images=ImageUploadConfig(maximum_upload_size_bytes=2048, maximum_pixels=1_000_000, model_max_edge=512),
        agent=AgentServiceConfig(
            max_loop_turns=2, max_model_calls_per_turn=4,
            max_tool_calls_per_turn=2, max_output_tokens=2048,
            execution_timeout_seconds=90, maximum_concurrent_kicks=2,
        ),
    )
    # Change the environment after loading Settings to prove assembly uses it.
    monkeypatch.setenv("ANIVAG_VIDEO_EMBEDDING_MODEL", "unexpected-env-model")
    monkeypatch.setenv("ANIVAG_VIDEO_EMBEDDING_FPS", "0.5")
    monkeypatch.setenv("ANIVAG_BACKGROUND_IMAGE_MAX_EDGE", "2048")
    monkeypatch.setattr(bootstrap, "QdrantClient", _QdrantClient)
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
        }
        embedder = received["query_embedder"]
        assert embedder._model == settings.video_embedding_model
        assert embedder._fps == settings.video_embedding_fps
        assert embedder._request_timeout_seconds == 90
        assert received["processing_embedder"] == {
            "model": settings.video_embedding_model,
            "fps": settings.video_embedding_fps,
            "dimensions": settings.text_vector_dimensions,
        }
        assert type(received["processor"]).__module__ == "server.service.video_processing"
        assert received["consumer"]["worker_id"] == settings.mq.consumer_id
        assert received["consumer"]["poll_interval_seconds"] == settings.mq.poll_interval_seconds
        assert received["consumer"]["lease_duration"] == settings.mq.lease_duration
        runtime = services.agent._runtime
        assert runtime._query_model.model_name == "configured-query-model"
        assert runtime._evaluator_model.model_name == "configured-review-model"
        assert runtime._chat_model.model_name == "configured-chat-model"
        assert runtime._query_model.max_output_tokens == 2048
        assert runtime._chat_model.max_output_tokens == 2048
        assert runtime._evaluator_model.max_output_tokens == 2048
        assert runtime._max_loop_turns == 2
        assert runtime._max_model_calls == 4
        assert runtime._max_tool_calls == 2
        assert runtime._execution_timeout == 90
        assert services.agent._executor._max_workers == 2
        app = create_app(services, settings=settings)
        assert app.state.sse_config == settings.sse
    finally:
        services.close()
    assert services.qdrant_client.closed
    assert received["pool_recycle_seconds"] == settings.database_pool_recycle_seconds


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
    monkeypatch.setattr(sys, "argv", ["python -m server.mq", "--once"])

    mq_main.main()

    assert events == [("mq", "test", settings.logging), "run_once", "close"]


def test_mq_entrypoint_rejects_persistent_process_mode(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["python -m server.mq"])

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
        qdrant_client=SimpleNamespace(close=lambda: order.append("vectors")),
        engine=SimpleNamespace(dispose=lambda: order.append("database")),
    )
    services.close()
    assert order == ["consumer", "agent", "vectors", "database"]


def test_query_embedder_uses_a_client_per_call_and_always_closes(monkeypatch):
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
        def embed_query(self, query):
            if query == "fail":
                raise RuntimeError("embedding failed")
            return self.client
    monkeypatch.setattr(bootstrap, "Ark", SimpleNamespace(volc=create_client))
    monkeypatch.setattr(bootstrap, "DoubaoMultimodalEmbedder", Embedder)
    embedder = bootstrap._LazyQueryEmbedder(model="embedding", dimensions=1024, fps=1, request_timeout_seconds=30)
    assert clients == []
    assert embedder.embed_query("first") is not embedder.embed_query("second")
    with pytest.raises(RuntimeError, match="embedding failed"):
        embedder.embed_query("fail")
    assert len(clients) == 3 and all(client.closed for client in clients)
    assert all(client.options == {"timeout": 30, "max_retries": 0} for client in clients)


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
