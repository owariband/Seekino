from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from typing import Any

from arkruntime import Ark
from engine.parse.runtime import EngineVideoParseRuntime
from gateway.config import load_config
from gateway.agent_model import DeepSeekFunctionCallingModel, SDKQueryAgentModel
from gateway.constants import ARK_PROVIDER, DEEPSEEK_PROVIDER
from gateway.models import ArkMultimodalModel, DeepSeekStructuredTextModel
from gateway.multimodal_embedder import DoubaoMultimodalEmbedder
from gateway.schemas import EmbeddingResult
from qdrant_client import QdrantClient
from sqlalchemy import Engine

from .config import ServerSettings
from .config.usage import load_model_pricing
from .config.video_processing import VideoProcessingConfig
from .agent.runtime import AgentRuntime
from .database import create_database_engine, create_session_factory
from .mq.consumer import ParseJobConsumer
from .mq.mysql_queue import MySqlParseJobQueue
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
from .service.qdrant_mutation_lock import QdrantMutationLock
from .service.upload import UploadService
from .service.video_processing import VideoProcessingService
from .service.video_retrieval import VideoRetrievalService
from .storage.artifact import LocalArtifactStore
from .storage.qdrant import QdrantVideoChunkStore

_logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ApplicationServices:
    library: LibraryService
    uploads: UploadService
    jobs: ParseJobService
    media: MediaService
    retrieval: VideoRetrievalService
    agent: AgentChatService
    index_sync: IndexSyncService
    parse_consumer: ParseJobConsumer | None = None
    engine: Engine | None = None
    qdrant_client: Any | None = None
    parse_usage: ParseUsageService | None = None

    def start(self) -> None:
        if self.parse_consumer is not None:
            self.parse_consumer.start()

    def close(self) -> None:
        # The Consumer and its heartbeat must release shared clients before
        # Agent, Qdrant, or database teardown begins.
        if self.parse_consumer is not None:
            self.parse_consumer.close()
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
    gateway_config = load_config(settings.model_config_path)
    agent_model_config = gateway_config.agent_model
    agent_api_key = os.getenv(agent_model_config.api_key_env, "").strip()
    if not agent_api_key:
        raise RuntimeError(
            "Agent model API key environment variable "
            f"{agent_model_config.api_key_env!r} is not configured"
        )
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
    job_repository = ParseJobRepository(session_factory)
    jobs = ParseJobService(job_repository, library)
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
    qdrant_mutation_lock = QdrantMutationLock(
        engine,
        environment=settings.environment,
        collection_name=settings.qdrant_collection,
    )
    uploads = UploadService(
        UploadRepository(session_factory),
        artifact_store,
        config=settings.upload,
        video_chunk_store=vector_store,
        qdrant_mutation_lock=qdrant_mutation_lock,
    )
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
    fallback_agent_model = (
        settings.agent_fallback_model
        or gateway_config.video_parser.transcript_model
    )
    query_model_name = settings.agent_query_model or agent_model_config.model
    evaluator_model_name = settings.agent_evaluator_model or agent_model_config.model
    chat_model_name = settings.agent_chat_model or agent_model_config.model
    request_timeout = min(
        settings.agent_request_timeout_seconds,
        settings.agent.execution_timeout_seconds,
        agent_model_config.request_timeout_seconds,
    )
    max_output_tokens = min(
        settings.agent.max_output_tokens,
        agent_model_config.max_output_tokens,
    )
    evaluator_fallback = ArkMultimodalModel(
        model=fallback_agent_model,
        request_timeout_seconds=request_timeout,
        max_output_tokens=max_output_tokens,
    )
    runtime = AgentRuntime(
        SDKQueryAgentModel(
            model=query_model_name,
            provider=DEEPSEEK_PROVIDER,
            base_url=agent_model_config.base_url,
            api_key=agent_api_key,
            request_timeout_seconds=request_timeout,
            max_output_tokens=max_output_tokens,
            max_input_chars=settings.agent.max_prompt_chars,
            fallback_model=fallback_agent_model,
            fallback_provider=ARK_PROVIDER,
        ),
        DeepSeekStructuredTextModel(
            model=evaluator_model_name,
            base_url=agent_model_config.base_url,
            api_key=agent_api_key,
            request_timeout_seconds=request_timeout,
            max_output_tokens=max_output_tokens,
            fallback_model=evaluator_fallback,
        ),
        DeepSeekFunctionCallingModel(
            model=chat_model_name,
            base_url=agent_model_config.base_url,
            api_key=agent_api_key,
            fallback_model=fallback_agent_model,
            request_timeout_seconds=request_timeout,
            max_output_tokens=max_output_tokens,
        ),
        retrieval.search,
        max_loop_turns=settings.agent.max_loop_turns,
        max_model_calls_per_turn=settings.agent.max_model_calls_per_turn,
        max_tool_calls_per_turn=settings.agent.max_tool_calls_per_turn,
        maximum_top_k=settings.agent.maximum_top_k,
        max_prompt_chars=settings.agent.max_prompt_chars,
        execution_timeout_seconds=settings.agent.execution_timeout_seconds,
    )
    agent = AgentChatService(
        AgentRepository(session_factory), library, runtime,
        config=settings.agent,
    )
    index_sync = IndexSyncService(
        IndexSyncRepository(session_factory),
        vector_store,
        mutation_lock=qdrant_mutation_lock,
        retry_base_seconds=settings.index_sync.retry_base_seconds,
        retry_max_seconds=settings.index_sync.retry_max_seconds,
    )
    processing_embedder = DoubaoMultimodalEmbedder(
        settings.video_embedding_model or embedding_config.model,
        output_dimensionality=settings.text_vector_dimensions,
        fps=(
            settings.video_embedding_fps
            if settings.video_embedding_fps is not None
            else embedding_config.fps
        ),
    )
    processor = VideoProcessingService(
        job_repository,
        video_repository,
        index_sync,
        artifact_store,
        EngineVideoParseRuntime(
            model_config_path=settings.model_config_path,
            config=settings.parse,
            on_log=_logger.info,
        ),
        processing_embedder,
        parse_usage=parse_usage,
        config=VideoProcessingConfig(
            embedding_dimensions=settings.text_vector_dimensions,
            chunk_duration_ms=settings.parse.chunk_duration_ms,
            context_overlap_ms=settings.parse.context_overlap_ms,
            analysis_max_height=settings.parse.max_height,
            canonical_max_height=settings.parse.max_height,
            asr_concurrency=settings.parse.asr_concurrency,
            asr_mode=settings.parse.asr_mode,
        ),
    )
    queue = MySqlParseJobQueue(session_factory)
    parse_consumer = ParseJobConsumer(
        queue,
        processor,
        worker_id=settings.mq.consumer_id,
        lease_duration=settings.mq.lease_duration,
        heartbeat_interval=settings.mq.heartbeat_interval,
        retry_base=settings.mq.retry_base,
        retry_max=settings.mq.retry_max,
        poll_interval_seconds=settings.mq.poll_interval_seconds,
    )
    uploads.reconcile_deletions()
    return ApplicationServices(
        library=library,
        uploads=uploads,
        jobs=jobs,
        media=media,
        retrieval=retrieval,
        agent=agent,
        index_sync=index_sync,
        parse_consumer=parse_consumer,
        engine=engine,
        qdrant_client=qdrant_client,
        parse_usage=parse_usage,
    )


class _LazyQueryEmbedder:
    """Create one short-lived SDK client for each query embedding call."""

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
