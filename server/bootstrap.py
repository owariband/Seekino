from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
import logging
from pathlib import Path
from typing import Any, Literal, TypeVar, cast

from agent.chat.supervisor import SupervisorRuntime
from agent.chat.video_search.runtime import AgentRuntime
from arkruntime import Ark
from elasticsearch import Elasticsearch
from engine.parsev1.runtime import EngineVideoParseRuntime, VideoParseRuntimeConfig
from engine.rag import EncoderContract
from gateway.agent_model.context import ContextWindowPolicy
from gateway.agent_model.factory import AgentModelFactory
from gateway.ark.embedding import (
    DOUBAO_MULTIMODAL_EMBEDDING_REVISION,
    DoubaoMultimodalEmbedder,
)
from gateway.config import ArkModelSourceConfig, OpenAIModelSourceConfig, load_config
from gateway.constants import DEEPSEEK_PROVIDER, OPENAI_DEEPSEEK_PROFILE
from gateway.embedding import EmbeddingResult, SparseEmbeddingResult
from gateway.model_sources import ark_options, source_key
from gateway.multimodal import MultimodalInput
from gateway.openai.multimodal import DeepSeekMultimodalModel
from gateway.rerank import StructuredTextReranker
from gateway.tracing import LangfuseTracing
from gateway.web_search import WebSearcher
from qdrant_client import QdrantClient
from pydantic import BaseModel
from sqlalchemy import Engine

from .config import ServerSettings
from .config.agent import AgentServiceConfig
from .config.global_agent_settings import read_global_agent_settings
from .config.parse_profile import ParseConfig
from .config.video_processing import VideoProcessingConfig
from .database import create_database_engine, create_session_factory
from .mq.consumer import ParseJobConsumer
from .mq.mysql_queue import MySqlParseJobQueue
from .mq.video_parse_consumer import (
    VideoParseConsumer,
    VideoParseConsumerHandlers,
)
from .mq.video_parse_queue import MySqlVideoParseQueue
from .repository.agent import AgentRepository
from .repository.index_sync import IndexSyncRepository
from .repository.job import ParseJobRepository
from .repository.library import LibraryRepository
from .repository.upload import UploadRepository
from .repository.video import VideoRepository
from .repository.video_parse import VideoParseRepository
from .service.agent_chat import AgentChatService
from .service.parsev1.index_sync import IndexSyncService
from .service.job import ParseJobService
from .service.library import LibraryService
from .service.media import MediaService
from .service.parse_usage import ParseUsageService
from .storage.langfuse import LangfuseObservationStore
from .service.qdrant_mutation_lock import QdrantMutationLock
from .service.upload import UploadService
from .service.video_parse import VideoParseRuntimeService
from .service.video_parse_media import VideoParseMediaService
from .service.parsev1.video_processing import VideoProcessingService
from .service.video_retrieval import (
    VideoParseRetrievalService,
    VideoRetrievalService,
)
from .storage.artifact import LocalArtifactStore
from .storage.elasticsearch import ElasticsearchVideoChunkStore
from .storage.qdrant import QdrantVideoChunkStore

_logger = logging.getLogger(__name__)
_ResultModel = TypeVar("_ResultModel", bound=BaseModel)


@dataclass(slots=True)
class ApplicationServices:
    library: LibraryService
    uploads: UploadService
    jobs: ParseJobService
    media: MediaService
    retrieval: VideoRetrievalService | VideoParseRetrievalService
    agent: AgentChatService
    index_sync: IndexSyncService
    parse_consumer: ParseJobConsumer | VideoParseConsumer | None = None
    engine: Engine | None = None
    qdrant_client: Any | None = None
    elasticsearch_client: Any | None = None
    parse_usage: ParseUsageService | None = None
    agent_model_factory: AgentModelFactory | None = None
    tracing: LangfuseTracing | None = None

    def start(self) -> None:
        if self.parse_consumer is not None:
            self.parse_consumer.start()

    def close(self) -> None:
        # The Consumer and its heartbeat must release shared clients before
        # Agent, Qdrant, or database teardown begins.
        try:
            if self.parse_consumer is not None:
                self.parse_consumer.close()
        finally:
            try:
                self.agent.close()
            finally:
                try:
                    if self.agent_model_factory is not None:
                        self.agent_model_factory.close()
                finally:
                    try:
                        if self.tracing is not None:
                            self.tracing.close()
                    finally:
                        try:
                            if self.parse_usage is not None:
                                self.parse_usage.close()
                        finally:
                            try:
                                if self.elasticsearch_client is not None:
                                    self.elasticsearch_client.close()
                            finally:
                                try:
                                    if self.qdrant_client is not None:
                                        self.qdrant_client.close()
                                finally:
                                    if self.engine is not None:
                                        self.engine.dispose()


def build_application_services(settings: ServerSettings) -> ApplicationServices:
    gateway_config = load_config(settings.model_config_path)
    if settings.parse_execution_mode == "v2" and settings.elasticsearch is None:
        raise ValueError(
            "Video Parse V2 requires ANIVAG_ELASTICSEARCH_URL and "
            "ANIVAG_ELASTICSEARCH_INDEX"
        )
    if settings.parse_execution_mode == "v1" and (
        settings.retrieval.sparse_weight > 0 or settings.retrieval.rerank_enabled
    ):
        raise ValueError("sparse retrieval and reranking require Video Parse V2")
    agent_model_config = gateway_config.agent_model
    rerank_source = gateway_config.model_sources[agent_model_config.source]
    if settings.retrieval.rerank_enabled and (
        not isinstance(rerank_source, OpenAIModelSourceConfig)
        or rerank_source.provider != DEEPSEEK_PROVIDER
        or rerank_source.profile != OPENAI_DEEPSEEK_PROFILE
    ):
        raise ValueError("structured text reranking requires a DeepSeek model source")
    configured_fallback = agent_model_config.fallback
    fallback_agent_model = (
        settings.agent_fallback_model
        or (configured_fallback.model if configured_fallback is not None else None)
    )
    agent_model_factory = AgentModelFactory(
        agent_model_config,
        gateway_config.model_sources,
        fallback_model=fallback_agent_model,
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
    library = LibraryService(
        LibraryRepository(
            session_factory,
            execution_mode=settings.parse_execution_mode,
        ),
        artifact_store,
    )
    job_repository = ParseJobRepository(session_factory)
    jobs = ParseJobService(
        job_repository,
        library,
        execution_mode=settings.parse_execution_mode,
    )
    tracing = LangfuseTracing(gateway_config.tracing)
    parse_usage = ParseUsageService(
        LangfuseObservationStore(gateway_config.tracing),
        tracing=tracing,
    )
    video_repository = VideoRepository(session_factory)
    video_parse_repository = VideoParseRepository(session_factory)
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
    embedding_config = gateway_config.multimodal_embedding
    embedding_model = settings.video_embedding_model or embedding_config.model
    embedding_fps = (
        settings.video_embedding_fps
        if settings.video_embedding_fps is not None
        else embedding_config.fps
    )
    parse_defaults = ParseConfig.from_defaults(
        runtime=settings.parse,
        gateway=gateway_config,
        upload=settings.upload,
        images=settings.images,
        embedding_model=embedding_model,
        embedding_dimensions=settings.text_vector_dimensions,
        embedding_fps=embedding_fps,
    )
    elasticsearch_client = None
    elasticsearch_store = None
    if settings.elasticsearch is not None:
        elasticsearch_client = Elasticsearch(
            settings.elasticsearch.url,
            api_key=settings.elasticsearch.api_key,
        )
        elasticsearch_store = ElasticsearchVideoChunkStore(
            elasticsearch_client,
            index_name=settings.elasticsearch.index_name,
            environment=settings.environment,
            semantic_dimensions=parse_defaults.embedding.dimensions,
            vision_dimensions=parse_defaults.embedding.dimensions,
        )
    video_parse_query_encoder = None
    video_parse_query_encoder_source = None
    if elasticsearch_store is not None:
        video_parse_query_encoder_source = parse_defaults.embedding.source
        embedding_source = gateway_config.model_sources.get(
            video_parse_query_encoder_source
        )
        if not isinstance(embedding_source, ArkModelSourceConfig):
            raise ValueError("Video Parse V2 embedding requires an Ark source")
        video_parse_query_encoder = EncoderContract(
            provider=embedding_source.provider,
            model=parse_defaults.embedding.model,
            encoder_revision=DOUBAO_MULTIMODAL_EMBEDDING_REVISION,
            dimensions=parse_defaults.embedding.dimensions,
        )
    uploads = UploadService(
        UploadRepository(session_factory),
        artifact_store,
        config=settings.upload,
        video_chunk_store=vector_store,
        video_parse_index_store=elasticsearch_store,
        qdrant_mutation_lock=qdrant_mutation_lock,
        parse_defaults=parse_defaults,
        execution_mode=settings.parse_execution_mode,
        video_parse_query_encoder=video_parse_query_encoder,
        video_parse_query_encoder_source=video_parse_query_encoder_source,
    )
    embedder = _LazyQueryEmbedder(
        model=(
            parse_defaults.embedding.model
            if settings.parse_execution_mode == "v2" else embedding_model
        ),
        dimensions=(
            parse_defaults.embedding.dimensions
            if settings.parse_execution_mode == "v2" else settings.text_vector_dimensions
        ),
        fps=(
            parse_defaults.embedding.video_fps
            if settings.parse_execution_mode == "v2" else embedding_fps
        ),
        source=(embedding_source if settings.parse_execution_mode == "v2" else None),
        request_timeout_seconds=min(
            settings.agent_request_timeout_seconds, settings.agent.execution_timeout_seconds,
        ),
    )
    if settings.parse_execution_mode == "v1":
        retrieval: VideoRetrievalService | VideoParseRetrievalService = (
            VideoRetrievalService(
                video_repository,
                vector_store,
                embedder,
                channel_top_k=settings.retrieval.channel_top_k,
                rrf_k=settings.retrieval.rrf_k,
                text_weight=settings.retrieval.text_weight,
                vision_weight=settings.retrieval.vision_weight,
                tracing=tracing,
            )
        )
    else:
        # V2 retrieval is fail-closed: it never substitutes the V1/Qdrant
        # service when ES or the selected task contract is unavailable.
        assert settings.elasticsearch is not None
        assert elasticsearch_store is not None
        assert video_parse_query_encoder is not None
        reranker = None
        if settings.retrieval.rerank_enabled:
            assert isinstance(rerank_source, OpenAIModelSourceConfig)
            reranker = StructuredTextReranker(_LazyTextRerankModel(
                source=rerank_source,
                model=agent_model_config.model,
                max_output_tokens=min(
                    settings.agent.max_output_tokens, agent_model_config.max_output_tokens,
                ),
                request_timeout_seconds=min(
                    settings.agent_request_timeout_seconds,
                    settings.agent.execution_timeout_seconds,
                    agent_model_config.request_timeout_seconds,
                ),
            ))
        retrieval = VideoParseRetrievalService(
            video_parse_repository,
            elasticsearch_store,
            embedder,
            semantic_encoder=video_parse_query_encoder,
            expected_index_generation=settings.elasticsearch.index_generation,
            expected_embedding_source=parse_defaults.embedding.source,
            channel_top_k=settings.retrieval.channel_top_k,
            rrf_k=settings.retrieval.rrf_k,
            lexical_weight=settings.retrieval.lexical_weight,
            text_weight=settings.retrieval.text_weight,
            vision_weight=settings.retrieval.vision_weight,
            candidate_budget=settings.retrieval.candidate_budget,
            sparse_weight=settings.retrieval.sparse_weight,
            reranker=reranker,
            tracing=tracing,
        )
    query_model_name = settings.agent_query_model or agent_model_config.model
    evaluator_model_name = settings.agent_evaluator_model or agent_model_config.model
    chat_model_name = settings.agent_chat_model or agent_model_config.model
    def build_agent_runtime(agent_config: AgentServiceConfig) -> AgentRuntime:
        request_timeout = min(
            settings.agent_request_timeout_seconds,
            agent_config.execution_timeout_seconds,
            agent_model_config.request_timeout_seconds,
        )
        max_output_tokens = min(
            agent_config.max_output_tokens,
            agent_model_config.max_output_tokens,
        )
        context_window_policy = ContextWindowPolicy(
            context_window_tokens=agent_config.context_window_tokens,
            output_reserve_tokens=max_output_tokens,
            safety_margin_tokens=agent_config.context_safety_margin_tokens,
            auto_compact_tokens=agent_config.auto_compact_tokens,
            compact_target_tokens=agent_config.compact_target_tokens,
        )
        return AgentRuntime(
            agent_model_factory.build_structured(
                model=query_model_name,
                request_timeout_seconds=request_timeout,
                max_output_tokens=max_output_tokens,
                max_input_chars=agent_config.max_prompt_chars,
                context_window_policy=context_window_policy,
                reasoning_effort=agent_config.query_reasoning_effort,
            ),
            agent_model_factory.build_structured(
                model=evaluator_model_name,
                request_timeout_seconds=request_timeout,
                max_output_tokens=max_output_tokens,
                max_input_chars=agent_config.max_prompt_chars,
                context_window_policy=context_window_policy,
                reasoning_effort=agent_config.evaluator_reasoning_effort,
            ),
            agent_model_factory.build_loop(
                model=chat_model_name,
                request_timeout_seconds=request_timeout,
                max_output_tokens=max_output_tokens,
                max_input_chars=agent_config.max_prompt_chars,
                context_window_policy=context_window_policy,
                reasoning_effort=agent_config.chat_reasoning_effort,
            ),
            retrieval.search,
            max_loop_turns=agent_config.max_loop_turns,
            max_model_calls_per_turn=agent_config.max_model_calls_per_turn,
            max_tool_calls_per_turn=agent_config.max_tool_calls_per_turn,
            limits_enabled=agent_config.limits_enabled,
            maximum_top_k=agent_config.maximum_top_k,
            max_prompt_chars=agent_config.max_prompt_chars,
            max_tool_result_bytes=agent_config.max_tool_result_bytes,
            max_read_chars_per_call=agent_config.max_read_chars_per_call,
            max_catalog_items_per_page=agent_config.max_catalog_items_per_page,
            stage_timeout_seconds=agent_config.stage_timeout_seconds,
            evaluator_stage_timeout_seconds=agent_config.evaluator_stage_timeout_seconds,
            chat_timeout_seconds=agent_config.chat_timeout_seconds,
            execution_timeout_seconds=agent_config.execution_timeout_seconds,
            retrieval_channel=(
                "lexical"
                if settings.parse_execution_mode == "v2"
                else "transcript"
            ),
        )
    def build_supervisor_runtime(
        video_runtime: AgentRuntime, agent_config: AgentServiceConfig,
    ) -> SupervisorRuntime:
        request_timeout = min(
            settings.agent_request_timeout_seconds,
            agent_config.execution_timeout_seconds,
            agent_model_config.request_timeout_seconds,
        )
        max_output_tokens = min(
            agent_config.max_output_tokens,
            agent_model_config.max_output_tokens,
        )
        context_window_policy = ContextWindowPolicy(
            context_window_tokens=agent_config.context_window_tokens,
            output_reserve_tokens=max_output_tokens,
            safety_margin_tokens=agent_config.context_safety_margin_tokens,
            auto_compact_tokens=agent_config.auto_compact_tokens,
            compact_target_tokens=agent_config.compact_target_tokens,
        )
        return SupervisorRuntime(
            video_runtime,
            agent_model_factory.build_loop(
                model=chat_model_name,
                request_timeout_seconds=request_timeout,
                max_output_tokens=max_output_tokens,
                max_input_chars=agent_config.max_prompt_chars,
                context_window_policy=context_window_policy,
                reasoning_effort=agent_config.supervisor_reasoning_effort,
            ),
            WebSearcher(),
            max_web_results=agent_config.web_search_max_results,
            web_timeout_seconds=agent_config.web_search_timeout_seconds,
            execution_timeout_seconds=agent_config.execution_timeout_seconds,
            supervisor_timeout_seconds=agent_config.supervisor_timeout_seconds,
            limits_enabled=agent_config.limits_enabled,
            max_tool_result_bytes=agent_config.max_tool_result_bytes,
            max_prompt_chars=agent_config.max_prompt_chars,
        )

    runtime = build_agent_runtime(settings.agent)
    supervisor_runtime = build_supervisor_runtime(runtime, settings.agent)

    def load_agent_config() -> tuple[AgentServiceConfig, str]:
        return read_global_agent_settings(settings.model_config_path)

    _, config_revision = load_agent_config()
    agent = AgentChatService(
        AgentRepository(session_factory), library, runtime,
        supervisor_runtime=supervisor_runtime,
        config=settings.agent,
        config_loader=load_agent_config,
        runtime_factory=build_agent_runtime,
        supervisor_factory=build_supervisor_runtime,
        config_revision=config_revision,
        tracing=tracing,
    )
    index_sync = IndexSyncService(
        IndexSyncRepository(session_factory),
        vector_store,
        mutation_lock=qdrant_mutation_lock,
        retry_base_seconds=settings.index_sync.retry_base_seconds,
        retry_max_seconds=settings.index_sync.retry_max_seconds,
    )
    def processor_for_config(selected: ParseConfig) -> VideoProcessingService:
        selected.require_active_index(
            embedding_model=embedding_model,
            dimensions=settings.text_vector_dimensions,
            image_max_edge=settings.images.model_max_edge,
        )
        fallback = gateway_config.video_parser.fallback
        deepseek = fallback.deepseek.model_copy(update={
            "model": selected.fallback.model,
            "reasoning_effort": selected.fallback.reasoning_effort,
            "max_output_tokens": selected.fallback.max_output_tokens,
            "request_timeout_seconds": selected.fallback.request_timeout_seconds,
        })
        fallback = fallback.model_copy(update={
            "enabled": selected.fallback.enabled,
            "frame_fps": selected.fallback.frame_fps,
            "group_duration_seconds": selected.fallback.group_duration_seconds,
            "max_frames_per_group": selected.fallback.max_frames_per_group,
            "max_height": selected.fallback.max_height,
            "deepseek": deepseek,
        })
        runtime = EngineVideoParseRuntime(
            model_config_path=settings.model_config_path,
            config=VideoParseRuntimeConfig(
                chunk_duration_seconds=selected.window.chunk_duration_seconds,
                context_overlap_seconds=selected.window.context_overlap_seconds,
                max_height=selected.window.analysis_max_height,
                asr_concurrency=selected.asr.concurrency,
                asr_mode=selected.asr.mode,
                request_timeout_seconds=selected.execution.request_timeout_seconds,
                max_context_events=selected.context.max_events,
                max_context_chars=selected.context.max_chars,
                transcript_model=selected.asr.model,
                video_model=selected.vision.model,
                transcript_fps=selected.asr.fps,
                video_fps=selected.vision.fps,
                fallback_enabled=selected.fallback.enabled,
                fallback_config=fallback,
                video_input_mode=selected.vision.input_mode,
                video_max_frames_per_window=selected.vision.max_frames_per_window,
                transcript_max_output_tokens=selected.asr.max_output_tokens,
                video_max_output_tokens=selected.vision.max_output_tokens,
            ),
            on_log=_logger.info,
        )
        embedding_agent = DoubaoMultimodalEmbedder(
            selected.embedding.model,
            output_dimensionality=selected.embedding.dimensions,
            fps=selected.embedding.video_fps,
        )
        return VideoProcessingService(
            job_repository,
            video_repository,
            index_sync,
            artifact_store,
            pipeline=runtime.build_pipeline(
                embedding_agent,
                selected.embedding.dimensions,
            ),
            parse_usage=parse_usage,
            config=VideoProcessingConfig(
                embedding_dimensions=selected.embedding.dimensions,
                chunk_duration_seconds=selected.window.chunk_duration_seconds,
                context_overlap_seconds=selected.window.context_overlap_seconds,
                analysis_max_height=selected.window.analysis_max_height,
                canonical_max_height=selected.window.canonical_max_height,
                asr_concurrency=selected.asr.concurrency,
                asr_mode=selected.asr.mode,
            ),
            parse_config=selected,
        )

    parse_consumer: ParseJobConsumer | VideoParseConsumer
    if settings.parse_execution_mode == "v1":
        processing_embedder = DoubaoMultimodalEmbedder(
            embedding_model,
            output_dimensionality=settings.text_vector_dimensions,
            fps=embedding_fps,
        )
        parse_runtime = EngineVideoParseRuntime(
            model_config_path=settings.model_config_path,
            config=settings.parse,
            on_log=_logger.info,
        )
        processor = VideoProcessingService(
            job_repository,
            video_repository,
            index_sync,
            artifact_store,
            pipeline=parse_runtime.build_pipeline(
                processing_embedder,
                settings.text_vector_dimensions,
            ),
            parse_usage=parse_usage,
            config=VideoProcessingConfig(
                embedding_dimensions=settings.text_vector_dimensions,
                chunk_duration_seconds=settings.parse.chunk_duration_seconds,
                context_overlap_seconds=settings.parse.context_overlap_seconds,
                analysis_max_height=settings.parse.max_height,
                canonical_max_height=settings.parse.max_height,
                asr_concurrency=settings.parse.asr_concurrency,
                asr_mode=settings.parse.asr_mode,
            ),
            processor_factory=processor_for_config,
        )
        parse_consumer = ParseJobConsumer(
            MySqlParseJobQueue(session_factory),
            processor,
            worker_id=settings.mq.consumer_id,
            lease_duration=settings.mq.lease_duration,
            heartbeat_interval=settings.mq.heartbeat_interval,
            retry_base=settings.mq.retry_base,
            retry_max=settings.mq.retry_max,
            poll_interval_seconds=settings.mq.poll_interval_seconds,
        )
    else:
        # The mode guard above makes both values non-null here.  Keep the
        # assertion next to the adapter construction so V2 can never fall back
        # to a partially configured writer.
        assert settings.elasticsearch is not None
        assert elasticsearch_client is not None
        assert elasticsearch_store is not None
        v2_repository = video_parse_repository
        v2_queue = MySqlVideoParseQueue(session_factory)
        v2_runtime = VideoParseRuntimeService(
            v2_repository,
            v2_queue,
            artifact_store,
            VideoParseMediaService(
                artifact_store.base_directory / "work" / "video-parse-v2"
            ),
            gateway_config,
            elasticsearch_store,
            index_generation=settings.elasticsearch.index_generation,
            sparse_enabled=settings.retrieval.sparse_weight > 0,
            worker_id=settings.mq.consumer_id,
            lease_duration=settings.mq.lease_duration,
            heartbeat_interval=settings.mq.heartbeat_interval,
            parse_usage=parse_usage,
            tracing=tracing,
        )
        parse_consumer = VideoParseConsumer(
            v2_queue,
            VideoParseConsumerHandlers(
                prepare=v2_runtime.prepare,
                understand=v2_runtime.understand,
                refine=v2_runtime.refine,
                index=v2_runtime.index,
                prefix=v2_runtime.run_next_prefix,
                maintenance=v2_runtime.reap_stale_media,
            ),
            worker_id=settings.mq.consumer_id,
            lease_duration=settings.mq.lease_duration,
            heartbeat_interval=settings.mq.heartbeat_interval,
            retry_initial_delay=settings.mq.retry_base,
            retry_max_delay=settings.mq.retry_max,
            poll_interval=timedelta(seconds=settings.mq.poll_interval_seconds),
            maintenance_interval=settings.mq.lease_duration,
            max_concurrent_handlers=max(2, settings.parse.asr_concurrency + 1),
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
        elasticsearch_client=elasticsearch_client,
        parse_usage=parse_usage,
        agent_model_factory=agent_model_factory,
        tracing=tracing,
    )


class _LazyQueryEmbedder:
    """Create one short-lived SDK client for each query embedding call."""

    def __init__(
        self, *, model: str, dimensions: int, fps: float,
        request_timeout_seconds: float,
        source: ArkModelSourceConfig | None = None,
    ) -> None:
        self._model = model
        self._dimensions = dimensions
        self._fps = fps
        self._request_timeout_seconds = request_timeout_seconds
        self._source = source

    def embed_query(self, query: str) -> EmbeddingResult:
        return cast(EmbeddingResult, self._embed("embed_query", query))

    def embed_image(self, image: Path) -> EmbeddingResult:
        return cast(EmbeddingResult, self._embed("embed_image", image))

    def embed_sparse(self, text: str) -> SparseEmbeddingResult:
        return cast(SparseEmbeddingResult, self._embed("embed_sparse", text))

    def _embed(
        self, method: Literal["embed_query", "embed_image", "embed_sparse"], value: str | Path,
    ) -> EmbeddingResult | SparseEmbeddingResult:
        options = ark_options(self._source) if self._source is not None else {}
        client = Ark.volc(
            timeout=self._request_timeout_seconds, max_retries=0, **options,
        )
        try:
            delegate = DoubaoMultimodalEmbedder(
                self._model, output_dimensionality=self._dimensions,
                fps=self._fps, client=client,
            )
            return getattr(delegate, method)(value)
        finally:
            client.close()


class _LazyTextRerankModel:
    """Use the selected text model with a client owned by each scoring call."""

    provider_name = DEEPSEEK_PROVIDER

    def __init__(
        self, *, source: OpenAIModelSourceConfig, model: str,
        max_output_tokens: int, request_timeout_seconds: float,
    ) -> None:
        self.model_name = model
        self._source = source
        self._max_output_tokens = max_output_tokens
        self._request_timeout_seconds = request_timeout_seconds

    def generate(
        self, inputs: Sequence[MultimodalInput], *, schema_name: str,
        result_type: type[_ResultModel],
    ) -> _ResultModel:
        delegate = DeepSeekMultimodalModel(
            self.model_name,
            base_url=self._source.base_url,
            api_key=source_key(self._source),
            max_output_tokens=self._max_output_tokens,
            request_timeout_seconds=self._request_timeout_seconds,
        )
        try:
            return delegate.generate(inputs, schema_name=schema_name, result_type=result_type)
        finally:
            delegate.close()
