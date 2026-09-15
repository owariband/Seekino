from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from arkruntime import Ark
from gateway.config import load_config
from gateway.agent_model import SDKQueryAgentModel
from gateway.models import ArkMultimodalModel
from gateway.multimodal_embedder import DoubaoMultimodalEmbedder
from gateway.schemas import EmbeddingResult
from qdrant_client import QdrantClient
from sqlalchemy import Engine

from .config import ServerSettings
from .config.usage import load_model_pricing
from .agent.runtime import AgentRuntime
from .database import create_database_engine, create_session_factory
from .repository.agent import AgentRepository
from .repository.index_sync import IndexSyncRepository
from .repository.job import ParseJobRepository
from .repository.library import LibraryRepository
from .repository.parse_usage import ParseUsageRepository
from .repository.upload import UploadRepository
from .repository.video import VideoRepository
from .service.agent_chat import AgentChatService
from .service.index_sync import IndexSyncService
from .service.job import ParseJobService
from .service.library import LibraryService
from .service.media import MediaService
from .service.parse_usage import ParseUsageService
from .service.upload import UploadService
from .service.video_retrieval import VideoRetrievalService
from .storage.artifact import LocalArtifactStore
from .storage.qdrant import QdrantVideoChunkStore


@dataclass(slots=True)
class ApplicationServices:
    library: LibraryService
    uploads: UploadService
    jobs: ParseJobService
    media: MediaService
    retrieval: VideoRetrievalService
    agent: AgentChatService
    index_sync: IndexSyncService
    engine: Engine | None = None
    qdrant_client: Any | None = None
    parse_usage: ParseUsageService | None = None

    def close(self) -> None:
        try:
            self.agent.close()
        finally:
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
        pool_recycle_seconds=settings.database_pool_recycle_seconds,
    )
    session_factory = create_session_factory(engine)
    artifact_store = LocalArtifactStore(
        settings.artifact_root,
        environment=settings.environment,
    )
    library = LibraryService(LibraryRepository(session_factory), artifact_store)
    uploads = UploadService(
        UploadRepository(session_factory),
        artifact_store,
        config=settings.upload,
    )
    jobs = ParseJobService(ParseJobRepository(session_factory), library)
    parse_usage = ParseUsageService(
        ParseUsageRepository(session_factory), jobs,
        load_model_pricing(settings.model_pricing_path),
    )
    video_repository = VideoRepository(session_factory)
    media = MediaService(library, artifact_store, video_repository, config=settings.images)

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
        model=settings.video_embedding_model or embedding_config.model,
        dimensions=settings.text_vector_dimensions,
        fps=(
            settings.video_embedding_fps
            if settings.video_embedding_fps is not None
            else embedding_config.fps
        ),
        request_timeout_seconds=min(
            settings.agent_request_timeout_seconds, settings.agent.execution_timeout_seconds,
        ),
    )
    retrieval = VideoRetrievalService(
        video_repository,
        vector_store,
        embedder,
        channel_top_k=settings.retrieval.channel_top_k,
        rrf_k=settings.retrieval.rrf_k,
        text_weight=settings.retrieval.text_weight,
        vision_weight=settings.retrieval.vision_weight,
    )
    chat_model_name = (
        settings.agent_chat_model or gateway_config.video_parser.transcript_model
    )
    evaluator_model_name = settings.agent_evaluator_model or chat_model_name
    query_model_name = settings.agent_query_model or chat_model_name
    request_timeout = min(
        settings.agent_request_timeout_seconds, settings.agent.execution_timeout_seconds,
    )
    runtime = AgentRuntime(
        SDKQueryAgentModel(
            model=query_model_name,
            request_timeout_seconds=request_timeout,
            max_output_tokens=settings.agent.max_output_tokens,
        ),
        ArkMultimodalModel(
            model=evaluator_model_name,
            request_timeout_seconds=request_timeout,
            max_output_tokens=settings.agent.max_output_tokens,
        ),
        ArkMultimodalModel(
            model=chat_model_name,
            request_timeout_seconds=request_timeout,
            max_output_tokens=settings.agent.max_output_tokens,
        ),
        retrieval.search,
        max_loop_turns=settings.agent.max_loop_turns,
        max_model_calls_per_turn=settings.agent.max_model_calls_per_turn,
        max_tool_calls_per_turn=settings.agent.max_tool_calls_per_turn,
        maximum_top_k=settings.agent.maximum_top_k,
        execution_timeout_seconds=settings.agent.execution_timeout_seconds,
    )
    agent = AgentChatService(
        AgentRepository(session_factory), library, runtime,
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
        parse_usage=parse_usage,
    )


class _LazyQueryEmbedder:
    """Keep upload/library APIs usable when only the Worker has model secrets."""

    def __init__(
        self, *, model: str, dimensions: int, fps: float,
        request_timeout_seconds: float,
    ) -> None:
        self._model = model
        self._dimensions = dimensions
        self._fps = fps
        self._request_timeout_seconds = request_timeout_seconds

    def embed_query(self, query: str) -> EmbeddingResult:
        client = Ark.volc(timeout=self._request_timeout_seconds, max_retries=0)
        try:
            delegate = DoubaoMultimodalEmbedder(
                self._model, output_dimensionality=self._dimensions,
                fps=self._fps, client=client,
            )
            return delegate.embed_query(query)
        finally:
            client.close()
