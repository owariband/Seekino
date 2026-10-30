from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from datetime import timedelta

from agent.chat.supervisor import SupervisorRuntime
from agent.chat.query_hook import QueryHook
from agent.chat.realtime_understanding import RealtimeUnderstandingRuntime
from agent.chat.video_search.runtime import AgentRuntime
from gateway.agent_model.context import ContextWindowPolicy
from gateway.agent_model.factory import AgentModelFactory
from gateway.config import (
    ClaudeCodeModelSourceConfig, CodexModelSourceConfig, GatewayConfig, load_config,
)
from gateway.model_factory import GatewayClient
from gateway.multimodal import adapter_capabilities
from gateway.query_hook import QueryHookModels
from gateway.tracing import LangfuseTracing
from gateway.web_search import WebSearcher

from .config import ServerSettings
from .caller import CallerResources
from .common.retrieval import build_event_retrieval_service, build_retrieval_service, validate_rerank_source
from .common.storage import (
    build_artifact_storage,
)
from .config.agent import AgentServiceConfig
from .config.global_agent_settings import read_global_agent_settings
from .config.parse_profile import ParseConfig, resolve_event_indexing_contract
from .config.usage import load_model_pricing
from .mq.video_parse_consumer import (
    VideoParseConsumer,
    VideoParseConsumerHandlers,
)
from .mq.video_parse_queue import MySqlVideoParseQueue
from .repository.agent import AgentRepository
from .repository.identity import IdentityRepository
from .repository.job import ParseJobRepository
from .repository.library import LibraryRepository
from .repository.model_usage import ModelUsageRepository
from .repository.upload import UploadRepository
from .service.video_parse_persistence import VideoParsePersistenceService
from .service.agent_checkpoint import create_agent_checkpoint_store
from .service.agent_chat import AgentChatService
from .service.agent_attachments import AgentAttachmentService
from .service.auth import AuthService
from .service.job import ParseJobService
from .service.library import LibraryService
from .service.media import MediaService
from .service.mcp_retrieval import McpRetrievalService
from .service.parse_usage import ParseUsageService
from .service.realtime_understanding import RealtimeUnderstandingService
from .service.upload import UploadService
from .service.video_parse import VideoParseRuntimeService
from .service.video_parse_media import VideoParseMediaService
from .service.video_summarize import VideoSummarizeService
from .service.video_frame_reader import VideoFrameReader
from .service.video_retrieval import VideoParseRetrievalService


@dataclass(slots=True)
class ApplicationServices:
    library: LibraryService
    uploads: UploadService
    jobs: ParseJobService
    media: MediaService
    retrieval: VideoParseRetrievalService
    agent: AgentChatService
    parse_consumer: VideoParseConsumer | None = None
    parse_usage: ParseUsageService | None = None
    agent_model_factory: AgentModelFactory | None = None
    gateway_client: GatewayClient | None = None
    tracing: LangfuseTracing | None = None
    realtime_understanding: RealtimeUnderstandingService | None = None
    auth: AuthService | None = None
    agent_attachments: AgentAttachmentService | None = None
    mcp_retrieval: McpRetrievalService | None = None
    _closed: bool = field(default=False, init=False, repr=False)

    def start(self) -> None:
        if self.parse_consumer is not None:
            self.parse_consumer.start()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Register in reverse shutdown order so all business users finish
        # before the process releases their shared Caller resources.
        with ExitStack() as cleanup:
            if self.parse_usage is not None:
                cleanup.callback(self.parse_usage.close)
            if self.tracing is not None:
                cleanup.callback(self.tracing.close)
            if self.agent_model_factory is not None:
                cleanup.callback(self.agent_model_factory.close)
            if self.gateway_client is not None:
                cleanup.callback(self.gateway_client.close)
            if self.realtime_understanding is not None:
                cleanup.callback(self.realtime_understanding.close)
            cleanup.callback(self.agent.close)
            if self.parse_consumer is not None:
                cleanup.callback(self.parse_consumer.close)


def _validate_cloud_agent_timeout(
    settings: ServerSettings, agent_config: AgentServiceConfig,
) -> None:
    if settings.auth.mode == "cloud":
        if not agent_config.total_timeout_enabled:
            raise ValueError("cloud Agent requires total_timeout_enabled for the query lease")
        if agent_config.execution_timeout_seconds >= settings.auth.query_lease_seconds:
            raise ValueError("cloud Agent execution timeout must be shorter than the query lease")


def prepare_service_configuration(
    settings: ServerSettings,
) -> tuple[GatewayConfig, ParseConfig]:
    if settings.parse_execution_mode != "v2":
        raise ValueError("formal Server execution supports only Video Parse V2")
    if settings.elasticsearch is None:
        raise ValueError(
            "Video Parse V2 requires SEEKINO_ELASTICSEARCH_URL and "
            "SEEKINO_ELASTICSEARCH_INDEX"
        )
    _validate_cloud_agent_timeout(settings, settings.agent)
    gateway_config = load_config(settings.model_config_path)
    _validate_cloud_supervisor_harness(settings, gateway_config)
    validate_rerank_source(gateway_config, settings.retrieval)
    parse_defaults = ParseConfig.from_defaults(
        runtime=settings.parse,
        mq=settings.mq,
        gateway=gateway_config,
        upload=settings.upload,
        images=settings.images,
        embedding_model=settings.video_embedding_model,
        embedding_dimensions=settings.text_vector_dimensions,
        embedding_fps=settings.video_embedding_fps,
        sparse_enabled=settings.retrieval.sparse_weight > 0,
        index_generation=settings.elasticsearch.index_generation,
    )
    return gateway_config, parse_defaults


def _validate_cloud_supervisor_harness(settings: ServerSettings, config: GatewayConfig) -> None:
    if settings.auth.mode != "cloud":
        return
    if config.supervisor_harness != "deepagents":
        raise ValueError("cloud Server requires supervisor_harness: deepagents; set SEEKINO_MODEL_CONFIG to an API-backed cloud configuration")
    for role, binding in config.normalized_models.items():
        while binding is not None:
            if isinstance(config.source(binding.source), (CodexModelSourceConfig, ClaudeCodeModelSourceConfig)):
                raise ValueError(f"cloud models.{role} must use API sources; native Codex/Claude processes are local-only")
            binding = getattr(binding, "fallback", None)


def build_application_services(
    settings: ServerSettings, resources: CallerResources, *,
    gateway_config: GatewayConfig, parse_defaults: ParseConfig,
) -> ApplicationServices:
    with ExitStack() as construction_cleanup:
        services = _build_application_services(
            settings, resources, construction_cleanup,
            gateway_config=gateway_config, parse_defaults=parse_defaults,
        )
        construction_cleanup.pop_all()
        return services


def _build_application_services(
    settings: ServerSettings, resources: CallerResources,
    construction_cleanup: ExitStack, *, gateway_config: GatewayConfig,
    parse_defaults: ParseConfig,
) -> ApplicationServices:
    session_factory = resources.session_factory
    elasticsearch_store = resources.elasticsearch_store
    agent_model_config = gateway_config.agent_model
    configured_fallback = agent_model_config.fallback
    fallback_agent_model = (
        settings.agent_fallback_model
        or (configured_fallback.model if configured_fallback is not None else None)
    )
    supervisor_sessions = None
    if gateway_config.supervisor_harness == "deepagents":
        supervisor_sessions = create_agent_checkpoint_store(settings.database_url, engine=resources.engine)
        construction_cleanup.callback(supervisor_sessions.close)
    gateway_client = GatewayClient(gateway_config, supervisor_sessions=supervisor_sessions)
    construction_cleanup.callback(gateway_client.close)
    agent_binding = gateway_config.resolve_model_binding("agent")
    if fallback_agent_model is not None and agent_binding.fallback is not None:
        agent_binding = agent_binding.model_copy(update={
            "fallback": agent_binding.fallback.model_copy(update={"model": fallback_agent_model}),
        })
    agent_model_factory = gateway_client.create_agent_factory(agent_binding)
    independent_supervisor = "supervisor" in gateway_config.normalized_models
    supervisor_binding = (
        gateway_config.resolve_model_binding("supervisor")
        if independent_supervisor else agent_binding
    )
    supervisor_model_factory = (
        gateway_client.create_agent_factory(supervisor_binding)
        if independent_supervisor else agent_model_factory
    )
    query_hook_model = (
        gateway_client.create_generative_model("query_hook")
        if gateway_config.query_hook.enabled and "query_hook" in gateway_config.normalized_models
        else None
    )
    auth = AuthService(
        IdentityRepository(session_factory), settings.auth,
        redis=resources.redis_client,
    )
    auth.bootstrap()
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
    parse_usage = ParseUsageService(
        ModelUsageRepository(session_factory),
        pricing=load_model_pricing(settings.model_pricing_path),
    )
    construction_cleanup.callback(parse_usage.close)
    parse_persistence = VideoParsePersistenceService(session_factory)
    media = MediaService(
        library, artifact_store, config=settings.images,
        parse_persistence=parse_persistence,
    )

    assert settings.elasticsearch is not None
    retrieval, video_parse_query_encoder = build_retrieval_service(
        parse_persistence, elasticsearch_store,
        gateway_config=gateway_config, embedding_config=parse_defaults.embedding,
        retrieval_config=settings.retrieval,
        index_generation=settings.elasticsearch.index_generation,
        request_timeout_seconds=min(
            timeout for timeout in (
                settings.agent_request_timeout_seconds, settings.agent.execution_timeout_seconds,
            ) if timeout is not None
        ),
        max_output_tokens=settings.agent.max_output_tokens, tracing=tracing,
        gateway_client=gateway_client,
    )
    event_store = resources.elasticsearch_event_store
    event_contract = None
    event_retrieval = None
    if event_store is not None:
        event_contract = resolve_event_indexing_contract(
            video_parse_query_encoder, max_height=parse_defaults.window.analysis_max_height,
            vision_enabled=parse_defaults.embedding.input_mode == "multimodal",
        )
        event_retrieval = build_event_retrieval_service(
            parse_persistence, event_store, gateway_config=gateway_config,
            embedding_config=parse_defaults.embedding, retrieval_config=settings.retrieval,
            contract=event_contract,
            request_timeout_seconds=min(timeout for timeout in (
                settings.agent_request_timeout_seconds, settings.agent.execution_timeout_seconds,
            ) if timeout is not None), tracing=tracing, gateway_client=gateway_client,
        )
    uploads = UploadService(
        UploadRepository(session_factory),
        artifact_store,
        config=settings.upload,
        video_parse_index_store=elasticsearch_store,
        parse_defaults=parse_defaults,
        video_parse_query_encoder=video_parse_query_encoder,
        video_parse_query_encoder_source=parse_defaults.embedding.source,
        video_parse_query_encoder_supports_sparse=adapter_capabilities(
            gateway_config.source(parse_defaults.embedding.source).adapter,
        ).sparse_embedding,
        video_event_index_store=event_store,
    )
    v2_queue = MySqlVideoParseQueue(session_factory, video_concurrency=settings.parse.video_concurrency)
    v2_runtime = VideoParseRuntimeService(
        parse_persistence,
        v2_queue,
        artifact_store,
        VideoParseMediaService(
            artifact_store.base_directory / "work" / "video-parse-v2"
        ),
        gateway_config,
        elasticsearch_store,
        worker_id=settings.mq.consumer_id,
        lease_duration=settings.mq.lease_duration,
        heartbeat_interval=settings.mq.heartbeat_interval,
        parse_usage=parse_usage,
        tracing=tracing,
        gateway_client=gateway_client,
        event_store=event_store,
        event_contract=event_contract,
    )
    summary_service = (
        VideoSummarizeService(
            parse_persistence, artifact_store, gateway_client,
            gateway_config=gateway_config, model=gateway_config.resolve_model_binding("summarize"),
            config=settings.summarize, worker_id=settings.mq.consumer_id,
            lease_duration=settings.mq.lease_duration,
            heartbeat_interval=settings.mq.heartbeat_interval, parse_usage=parse_usage,
        )
        if settings.summarize.enabled and "summarize" in gateway_config.normalized_models else None
    )
    realtime_service = RealtimeUnderstandingService(
        parse_persistence, v2_queue, v2_runtime, artifact_store,
        gateway_config=gateway_config,
        default_parse_config=parse_defaults.to_video_parse_v2(),
        worker_id=settings.mq.consumer_id,
        lease_duration=settings.mq.lease_duration,
        heartbeat_interval=settings.mq.heartbeat_interval,
    )
    construction_cleanup.callback(realtime_service.close)
    query_model_name = settings.agent_query_model or agent_model_config.model
    chat_model_name = settings.agent_chat_model or agent_model_config.model
    def build_agent_runtime(agent_config: AgentServiceConfig) -> AgentRuntime:
        _validate_cloud_agent_timeout(settings, agent_config)
        if settings.auth.mode == "cloud":
            agent_config = replace(agent_config, limits_enabled=True)
        request_timeout = min(
            timeout for timeout in (
                settings.agent_request_timeout_seconds,
                agent_config.execution_timeout_seconds,
                agent_model_config.request_timeout_seconds,
            ) if timeout is not None
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
                mcp=False,
            ),
            retrieval.search,
            event_retrieve=event_retrieval.search if event_retrieval is not None else None,
            frame_reader=VideoFrameReader(parse_persistence, artifact_store),
            frame_max_height=agent_config.frame_max_height,
            maximum_frame_inspections=agent_config.maximum_frame_inspections,
            max_model_calls_per_turn=agent_config.max_model_calls_per_turn,
            max_tool_calls_per_turn=agent_config.max_tool_calls_per_turn,
            limits_enabled=agent_config.limits_enabled,
            max_prompt_chars=agent_config.max_prompt_chars,
            max_tool_result_bytes=agent_config.max_tool_result_bytes,
            max_read_chars_per_call=agent_config.max_read_chars_per_call,
            max_catalog_items_per_page=agent_config.max_catalog_items_per_page,
            stage_timeout_seconds=agent_config.stage_timeout_seconds,
            execution_timeout_seconds=agent_config.execution_timeout_seconds,
            total_timeout_enabled=agent_config.total_timeout_enabled,
            retrieval_channel="lexical",
        )
    def build_supervisor_runtime(
        video_runtime: AgentRuntime, agent_config: AgentServiceConfig,
    ) -> SupervisorRuntime:
        _validate_cloud_agent_timeout(settings, agent_config)
        if settings.auth.mode == "cloud":
            agent_config = replace(agent_config, limits_enabled=True)
        request_timeout = min(
            timeout for timeout in (
                settings.agent_request_timeout_seconds,
                agent_config.execution_timeout_seconds if agent_config.total_timeout_enabled else None,
                supervisor_binding.request_timeout_seconds,
            ) if timeout is not None
        )
        max_output_tokens = min(
            agent_config.max_output_tokens,
            supervisor_binding.max_output_tokens,
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
            supervisor_model_factory.build_supervisor(
                harness=gateway_config.supervisor_harness,
                mcp=gateway_config.supervisor_harness == "codex",
                model=supervisor_binding.model if independent_supervisor else chat_model_name,
                request_timeout_seconds=request_timeout,
                max_output_tokens=max_output_tokens,
                max_input_chars=agent_config.max_prompt_chars,
                context_window_policy=context_window_policy,
                total_timeout_enabled=agent_config.total_timeout_enabled,
                reasoning_effort=(
                    supervisor_binding.reasoning_effort
                    if independent_supervisor and supervisor_binding.reasoning_effort is not None
                    else agent_config.supervisor_reasoning_effort
                ),
            ),
            WebSearcher(),
            realtime_runtime=RealtimeUnderstandingRuntime(
                host=realtime_service,
                execution_timeout_seconds=agent_config.execution_timeout_seconds,
                max_prompt_chars=agent_config.max_prompt_chars,
            ),
            max_web_results=agent_config.web_search_max_results,
            web_timeout_seconds=agent_config.web_search_timeout_seconds,
            execution_timeout_seconds=agent_config.execution_timeout_seconds,
            supervisor_timeout_seconds=agent_config.supervisor_timeout_seconds,
            total_timeout_enabled=agent_config.total_timeout_enabled,
            limits_enabled=agent_config.limits_enabled,
            max_tool_result_bytes=agent_config.max_tool_result_bytes,
            max_prompt_chars=agent_config.max_prompt_chars,
            maximum_parallel_queries=agent_config.maximum_parallel_queries,
            query_hook=QueryHook(
                gateway_config.query_hook, QueryHookModels(gateway_config.query_hook),
                decision_model=query_hook_model,
            ) if gateway_config.query_hook.enabled else None,
        )

    def build_chat_runtime(agent_config: AgentServiceConfig) -> SupervisorRuntime:
        query_runtime = build_agent_runtime(agent_config)
        try:
            return build_supervisor_runtime(query_runtime, agent_config)
        except Exception:
            query_runtime.close()
            raise

    runtime = build_chat_runtime(settings.agent)

    def load_agent_config() -> tuple[AgentServiceConfig, str]:
        return read_global_agent_settings(settings.model_config_path)

    _, config_revision = load_agent_config()
    agent_repository = AgentRepository(session_factory)
    agent_attachments = AgentAttachmentService(
        agent_repository, artifact_store, config=settings.images,
    )
    agent = AgentChatService(
        agent_repository, library, runtime,
        config=settings.agent,
        config_loader=load_agent_config,
        runtime_factory=build_chat_runtime,
        config_revision=config_revision,
        tracing=tracing,
        parse_usage=parse_usage,
        attachments=agent_attachments,
    )
    construction_cleanup.callback(agent.close)
    parse_consumer = VideoParseConsumer(
        v2_queue,
        VideoParseConsumerHandlers(
            prepare=v2_runtime.prepare,
            understand=v2_runtime.understand,
            failure=v2_runtime.notify_task_failed,
            refine=v2_runtime.refine,
            index=v2_runtime.index,
            prefix=v2_runtime.run_next_prefix,
            maintenance=v2_runtime.reap_stale_media,
            summarize=summary_service.run_next if summary_service is not None else None,
        ),
        worker_id=settings.mq.consumer_id,
        lease_duration=settings.mq.lease_duration,
        heartbeat_interval=settings.mq.heartbeat_interval,
        poll_interval=timedelta(seconds=settings.mq.poll_interval_seconds),
        maintenance_interval=settings.mq.lease_duration,
        video_concurrency=settings.parse.video_concurrency,
        summarize_concurrency=settings.summarize.concurrency,
    )
    construction_cleanup.callback(parse_consumer.close)

    uploads.reconcile_deletions()
    agent_attachments.reconcile_deletions()
    return ApplicationServices(
        library=library,
        uploads=uploads,
        jobs=jobs,
        media=media,
        retrieval=retrieval,
        agent=agent,
        agent_attachments=agent_attachments,
        parse_consumer=parse_consumer,
        parse_usage=parse_usage,
        agent_model_factory=agent_model_factory,
        gateway_client=gateway_client,
        tracing=tracing,
        realtime_understanding=realtime_service,
        auth=auth,
        mcp_retrieval=McpRetrievalService(
            library=library, media=media, event_retrieval=event_retrieval,
            parse_persistence=parse_persistence, local_tenant_id=settings.auth.local_tenant_id,
        ) if settings.auth.mode == "local" else None,
    )
