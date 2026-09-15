from __future__ import annotations

import argparse
import signal
import threading

from engine.parse.runtime import EngineVideoParseRuntime
from gateway.config import load_config
from gateway.multimodal_embedder import DoubaoMultimodalEmbedder
from qdrant_client import QdrantClient

from ..config import ServerSettings
from ..config.video_processing import VideoProcessingConfig
from ..database import create_database_engine, create_schema, create_session_factory
from ..repository.index_sync import IndexSyncRepository
from ..repository.job import ParseJobRepository
from ..repository.video import VideoRepository
from ..service.index_sync import IndexSyncService
from ..service.video_processing import VideoProcessingService
from ..storage.artifact import LocalArtifactStore
from ..storage.qdrant import QdrantVideoChunkStore
from .runtime import ParseWorker


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m server.worker",
        description="Run the durable AniVAG video parse/index Worker.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Claim at most one due job and then exit.",
    )
    parser.add_argument(
        "--init-db",
        action="store_true",
        help="Create missing tables before starting the Worker.",
    )
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    settings = ServerSettings.from_environment()
    engine = create_database_engine(
        settings.database_url,
        environment=settings.environment,
        pool_recycle_seconds=settings.database_pool_recycle_seconds,
    )
    if args.init_db:
        create_schema(engine)
    session_factory = create_session_factory(engine)
    jobs = ParseJobRepository(session_factory)
    artifact_store = LocalArtifactStore(
        settings.artifact_root,
        environment=settings.environment,
    )
    qdrant_client = QdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key,
    )
    try:
        qdrant_store = QdrantVideoChunkStore(
            qdrant_client,
            collection_name=settings.qdrant_collection,
            environment=settings.environment,
            text_dimensions=settings.text_vector_dimensions,
            vision_dimensions=settings.vision_vector_dimensions,
        )
        index_sync = IndexSyncService(
            IndexSyncRepository(session_factory),
            qdrant_store,
            retry_base_seconds=int(settings.worker_retry_base.total_seconds()),
            retry_max_seconds=int(settings.worker_retry_max.total_seconds()),
        )
        embedding_config = load_config(
            settings.model_config_path
        ).multimodal_embedding
        embedder = DoubaoMultimodalEmbedder(
            settings.video_embedding_model or embedding_config.model,
            output_dimensionality=settings.text_vector_dimensions,
            fps=(
                settings.video_embedding_fps
                if settings.video_embedding_fps is not None
                else embedding_config.fps
            ),
        )
        processor = VideoProcessingService(
            jobs,
            VideoRepository(session_factory),
            index_sync,
            artifact_store,
            EngineVideoParseRuntime(
                model_config_path=settings.model_config_path,
                config=settings.parse,
                on_log=_log,
            ),
            embedder,
            config=VideoProcessingConfig(
                embedding_dimensions=settings.text_vector_dimensions,
                canonical_max_height=settings.parse.max_height,
            ),
        )
        worker = ParseWorker(
            jobs,
            processor,
            worker_id=settings.worker_id,
            lease_duration=settings.worker_lease_duration,
            heartbeat_interval=settings.worker_heartbeat_interval,
            retry_base=settings.worker_retry_base,
            retry_max=settings.worker_retry_max,
            poll_interval_seconds=settings.worker_poll_interval_seconds,
            on_log=_log,
        )
        if args.once:
            worker.run_once()
            return

        stop = threading.Event()

        def request_stop(_signum, _frame) -> None:
            stop.set()

        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGTERM, request_stop)
        _log(f"worker started id={settings.worker_id}")
        worker.run_forever(stop)
    finally:
        qdrant_client.close()
        engine.dispose()


def _log(message: str) -> None:
    print(message, flush=True)


if __name__ == "__main__":
    main()
