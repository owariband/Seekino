from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from datetime import timedelta
import logging
from typing import Any

from agent.chat.supervisor import SupervisorRuntime
from agent.chat.video_search.runtime import AgentRuntime
from gateway.agent_model.context import ContextWindowPolicy
from gateway.agent_model.factory import AgentModelFactory
from gateway.config import load_config
from gateway.tracing import LangfuseTracing
from gateway.web_search import WebSearcher
from sqlalchemy import Engine

from .config import ServerSettings
from .common.retrieval import build_retrieval_service, validate_rerank_source
from .common.storage import (
    build_artifact_storage,
    build_database_storage,
    build_elasticsearch_storage,
)
from .config.agent import AgentServiceConfig
from .config.global_agent_settings import read_global_agent_settings
from .config.parse_profile import ParseConfig
from .mq.video_parse_consumer import (
    VideoParseConsumer,
    VideoParseConsumerHandlers,
)
from .mq.video_parse_queue import MySqlVideoParseQueue
from .repository.agent import AgentRepository
from .repository.job import ParseJobRepository
from .repository.library import LibraryRepository
from .repository.model_usage import ModelUsageRepository
from .repository.upload import UploadRepository
from .repository.video import VideoRepository
from .repository.video_parse import VideoParseRepository
from .service.agent_chat import AgentChatService
from .service.job import ParseJobService
from .service.library import LibraryService
from .service.media import MediaService
from .service.parse_usage import ParseUsageService
from .service.upload import UploadService
from .service.video_parse import VideoParseRuntimeService
from .service.video_parse_media import VideoParseMediaService
from .service.video_retrieval import VideoParseRetrievalService

_logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ApplicationServices:
    library: LibraryService
    uploads: UploadService
    jobs: ParseJobService
    media: MediaService
    retrieval: VideoParseRetrievalService
    agent: AgentChatService
    parse_consumer: VideoParseConsumer | None = None
    engine: Engine | None = None
    elasticsearch_client: Any | None = None
    parse_usage: ParseUsageService | None = None
    agent_model_factory: AgentModelFactory | None = None
    tracing: LangfuseTracing | None = None

    def start(self) -> None:
        if self.parse_consumer is not None:
            self.parse_consumer.start()

    def close(self) -> None:
        # The Consumer and its heartbeat must release shared clients before
        # Agent, Elasticsearch, or database teardown begins.
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
                                if self.engine is not None:
                                    self.engine.dispose()


def build_application_services(settings: ServerSettings) -> ApplicationServices:
    with ExitStack() as construction_cleanup:
        services = _build_application_services(settings, construction_cleanup)
        construction_cleanup.pop_all()
        return services


def _build_application_services(
    settings: ServerSettings, construction_cleanup: ExitStack,
) -> ApplicationServices:
    if settings.parse_execution_mode != "v2":
        raise ValueError("formal Server execution supports only Video Parse V2")
    if settings.elasticsearch is None:
        raise ValueError(
            "Video Parse V2 requires ANIVAG_ELASTICSEARCH_URL and "
            "ANIVAG_ELASTICSEARCH_INDEX"
        )
    gateway_config = load_config(settings.model_config_path)
    agent_model_config = gateway_config.agent_model
    validate_rerank_source(gateway_config, settings.retrieval)
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
    engine, session_factory = build_database_storage(
        settings.database_url,
        environment=settings.environment,
        pool_recycle_seconds=settings.database_pool_recycle_seconds,
    )
    construction_cleanup.callback(engine.dispose)
    artifact_store = build_artifact_storage(
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
    tracing = None
    parse_usage = ParseUsageService(ModelUsageRepository(session_factory))
    video_repository = VideoRepository(session_factory)
    video_parse_repository = VideoParseRepository(session_factory)
    media = MediaService(
        library, artifact_store, video_repository, config=settings.images,
        video_parse_repository=video_parse_repository if settings.parse_execution_mode == "v2" else None,
    )

    parse_defaults = ParseConfig.from_defaults(
        runtime=settings.parse,
        gateway=gateway_config,
        upload=settings.upload,
        images=settings.images,
        embedding_model=settings.video_embedding_model,
        embedding_dimensions=settings.text_vector_dimensions,
        embedding_fps=settings.video_embedding_fps,
    )
    elasticsearch_client, elasticsearch_store = build_elasticsearch_storage(
        settings.elasticsearch, environment=settings.environment,
        semantic_dimensions=parse_defaults.embedding.dimensions,
        vision_dimensions=parse_defaults.embedding.dimensions,
    )
    construction_cleanup.callback(elasticsearch_client.close)
    retrieval, video_parse_query_encoder = build_retrieval_service(
        video_parse_repository, elasticsearch_store,
        gateway_config=gateway_config, embedding_config=parse_defaults.embedding,
        retrieval_config=settings.retrieval,
        index_generation=settings.elasticsearch.index_generation,
        request_timeout_seconds=min(
            settings.agent_request_timeout_seconds, settings.agent.execution_timeout_seconds,
        ),
        max_output_tokens=settings.agent.max_output_tokens, tracing=tracing,
    )
    uploads = UploadService(
        UploadRepository(session_factory),
        artifact_store,
        config=settings.upload,
        video_parse_index_store=elasticsearch_store,
        parse_defaults=parse_defaults,
        execution_mode=settings.parse_execution_mode,
        video_parse_query_encoder=video_parse_query_encoder,
        video_parse_query_encoder_source=parse_defaults.embedding.source,
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
            retrieval_channel="lexical",
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
        parse_usage=parse_usage,
    )
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
        parse_consumer=parse_consumer,
        engine=engine,
        elasticsearch_client=elasticsearch_client,
        parse_usage=parse_usage,
        agent_model_factory=agent_model_factory,
        tracing=tracing,
    )
