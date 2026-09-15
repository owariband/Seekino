from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from threading import Lock
from typing import Any, TypeVar

from gateway.config import load_config
from gateway.models import ArkMultimodalModel, MultimodalInput
from gateway.multimodal_embedder import DoubaoMultimodalEmbedder
from gateway.schemas import EmbeddingResult
from pydantic import BaseModel
from qdrant_client import QdrantClient
from sqlalchemy import Engine

from ..config import ServerSettings
from ..database import create_database_engine, create_session_factory
from ..repository.agent import AgentRepository
from ..repository.index_sync import IndexSyncRepository
from ..repository.job import ParseJobRepository
from ..repository.library import LibraryRepository
from ..repository.upload import UploadRepository
from ..repository.video import VideoRepository
from ..service.agent import AgentConversationService
from ..service.index_sync import IndexSyncService
from ..service.job import ParseJobService
from ..service.library import LibraryService
from ..service.media import MediaService
from ..service.upload import UploadService
from ..service.video_retrieval import VideoRetrievalService
from ..storage.artifact import LocalArtifactStore
from ..storage.qdrant import QdrantVideoChunkStore


@dataclass(slots=True)
class ApplicationServices:
    library: LibraryService
    uploads: UploadService
    jobs: ParseJobService
    media: MediaService
    retrieval: VideoRetrievalService
    agent: AgentConversationService
    index_sync: IndexSyncService
    engine: Engine | None = None
    qdrant_client: Any | None = None

    def close(self) -> None:
        try:
            if self.qdrant_client is not None:
                self.qdrant_client.close()
        finally:
            if self.engine is not None:
                self.engine.dispose()


def build_application_services(settings: ServerSettings) -> ApplicationServices:
    engine = create_database_engine(
        settings.database_url,
        environment=settings.environment,
    )
    session_factory = create_session_factory(engine)
    artifact_store = LocalArtifactStore(
        settings.artifact_root,
        environment=settings.environment,
    )
    library = LibraryService(LibraryRepository(session_factory))
    uploads = UploadService(
        UploadRepository(session_factory),
        artifact_store,
        config=settings.upload,
    )
    jobs = ParseJobService(ParseJobRepository(session_factory))
    media = MediaService(library, artifact_store)

    qdrant_client = QdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key,
    )
    vector_store = QdrantVideoChunkStore(
        qdrant_client,
        collection_name=settings.qdrant_collection,
        environment=settings.environment,
        text_dimensions=settings.text_vector_dimensions,
        vision_dimensions=settings.vision_vector_dimensions,
    )
    gateway_config = load_config(settings.model_config_path)
    embedding_config = gateway_config.multimodal_embedding
    embedder = _LazyQueryEmbedder(
        model=(
            os.getenv("ANIVAG_VIDEO_EMBEDDING_MODEL")
            or embedding_config.model
        ),
        dimensions=settings.text_vector_dimensions,
        fps=float(
            os.getenv(
                "ANIVAG_VIDEO_EMBEDDING_FPS",
                str(embedding_config.fps),
            )
        ),
    )
    video_repository = VideoRepository(session_factory)
    retrieval = VideoRetrievalService(
        video_repository,
        vector_store,
        embedder,
    )
    chat_model_name = (
        settings.agent_chat_model or gateway_config.video_parser.transcript_model
    )
    evaluator_model_name = settings.agent_evaluator_model or chat_model_name
    agent = AgentConversationService(
        AgentRepository(session_factory),
        library,
        retrieval,
        _LazyStructuredAgentModel(
            model=chat_model_name,
            request_timeout_seconds=settings.agent_request_timeout_seconds,
        ),
        _LazyStructuredAgentModel(
            model=evaluator_model_name,
            request_timeout_seconds=settings.agent_request_timeout_seconds,
        ),
        config=settings.agent,
    )
    index_sync = IndexSyncService(
        IndexSyncRepository(session_factory),
        vector_store,
        retry_base_seconds=int(settings.worker_retry_base.total_seconds()),
        retry_max_seconds=int(settings.worker_retry_max.total_seconds()),
    )
    return ApplicationServices(
        library=library,
        uploads=uploads,
        jobs=jobs,
        media=media,
        retrieval=retrieval,
        agent=agent,
        index_sync=index_sync,
        engine=engine,
        qdrant_client=qdrant_client,
    )


class _LazyQueryEmbedder:
    """Keep upload/library APIs usable when only the Worker has model secrets."""

    def __init__(self, *, model: str, dimensions: int, fps: float) -> None:
        self._model = model
        self._dimensions = dimensions
        self._fps = fps
        self._delegate: DoubaoMultimodalEmbedder | None = None
        self._lock = Lock()

    def embed_query(self, query: str) -> EmbeddingResult:
        delegate = self._delegate
        if delegate is None:
            with self._lock:
                delegate = self._delegate
                if delegate is None:
                    delegate = DoubaoMultimodalEmbedder(
                        self._model,
                        output_dimensionality=self._dimensions,
                        fps=self._fps,
                    )
                    self._delegate = delegate
        return delegate.embed_query(query)


_ResultModel = TypeVar("_ResultModel", bound=BaseModel)


class _LazyStructuredAgentModel:
    """Keep non-Agent APIs available until the first real Agent message."""

    provider_name = "ark"

    def __init__(self, *, model: str, request_timeout_seconds: float) -> None:
        self.model_name = model
        self._request_timeout_seconds = request_timeout_seconds
        self._delegate: ArkMultimodalModel | None = None
        self._lock = Lock()

    def generate(
        self,
        inputs: Sequence[MultimodalInput],
        *,
        schema_name: str,
        result_type: type[_ResultModel],
    ) -> _ResultModel:
        delegate = self._delegate
        if delegate is None:
            with self._lock:
                delegate = self._delegate
                if delegate is None:
                    delegate = ArkMultimodalModel(
                        self.model_name,
                        request_timeout_seconds=self._request_timeout_seconds,
                    )
                    self._delegate = delegate
        return delegate.generate(
            inputs,
            schema_name=schema_name,
            result_type=result_type,
        )
