from __future__ import annotations

import os
import sys
from dataclasses import replace

import pytest

from server import bootstrap
from server.api.app import create_app
from server.config import ServerSettings
from server.config.retrieval import RetrievalConfig
from server.config.sse import SSEConfig
from server.mq import __main__ as worker_main


class _QdrantClient:
    def __init__(self, **_kwargs):
        self.closed = False

    def close(self):
        self.closed = True


@pytest.mark.parametrize("entrypoint", ["api", "worker"])
def test_process_assembly_receives_runtime_configuration(monkeypatch, tmp_path, entrypoint):
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
    )
    # Change the environment after loading Settings to prove assembly uses it.
    monkeypatch.setenv("ANIVAG_VIDEO_EMBEDDING_MODEL", "unexpected-env-model")
    monkeypatch.setenv("ANIVAG_VIDEO_EMBEDDING_FPS", "0.5")
    module = bootstrap if entrypoint == "api" else worker_main
    monkeypatch.setattr(module, "QdrantClient", _QdrantClient)
    received = {}
    real_create_engine = module.create_database_engine

    def create_engine(*args, **kwargs):
        received["pool_recycle_seconds"] = kwargs["pool_recycle_seconds"]
        return real_create_engine(*args, **kwargs)

    monkeypatch.setattr(module, "create_database_engine", create_engine)
    if entrypoint == "api":
        real_retrieval = bootstrap.VideoRetrievalService

        def create_retrieval(*args, **kwargs):
            received["retrieval"] = kwargs
            received["embedder"] = args[2]
            return real_retrieval(*args, **kwargs)

        monkeypatch.setattr(bootstrap, "VideoRetrievalService", create_retrieval)
        services = bootstrap.build_application_services(settings)
        try:
            assert received["retrieval"] == {
                "channel_top_k": 35,
                "rrf_k": 42,
                "text_weight": 0.25,
                "vision_weight": 1.5,
            }
            embedder = received["embedder"]
            assert embedder._model == settings.video_embedding_model
            assert embedder._fps == settings.video_embedding_fps
            app = create_app(services, settings=settings)
            assert app.state.sse_config == settings.sse
        finally:
            services.close()
        assert services.qdrant_client.closed
    else:
        class Embedder:
            def __init__(self, model, **kwargs):
                received["model"] = model
                received["fps"] = kwargs["fps"]

        class Worker:
            def __init__(self, _jobs, processor, **kwargs):
                received["processor"] = processor
                received["worker"] = kwargs

            def run_once(self):
                received["ran_once"] = True

        monkeypatch.setattr(worker_main, "DoubaoMultimodalEmbedder", Embedder)
        monkeypatch.setattr(worker_main, "ParseWorker", Worker)
        monkeypatch.setattr(
            ServerSettings, "from_environment", classmethod(lambda _cls: settings)
        )
        monkeypatch.setattr(sys, "argv", ["python -m server.mq", "--once"])
        worker_main.main()
        assert received["model"] == settings.video_embedding_model
        assert received["fps"] == settings.video_embedding_fps
        assert type(received["processor"]).__module__ == "server.service.video_processing"
        assert received["worker"]["poll_interval_seconds"] == settings.worker_poll_interval_seconds
        assert received["worker"]["lease_duration"] == settings.worker_lease_duration
        assert received["ran_once"]
    assert received["pool_recycle_seconds"] == settings.database_pool_recycle_seconds
