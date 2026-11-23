from __future__ import annotations

import asyncio
import logging
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import timedelta

from engine.common.media_probe import probe_video_duration_seconds
from gateway.config import GatewayConfig
from gateway.model_factory import GatewayClient
from gateway.multimodal import adapter_capabilities
from sqlalchemy.orm import sessionmaker

from .common.retrieval import build_event_retrieval_service, build_query_embedding, build_retrieval_service
from .config.media import ImageUploadConfig
from .config.mq import MQConsumerConfig
from .config.parse_profile import ParseConfig, resolve_event_indexing_contract
from .config.retrieval import RetrievalConfig
from .config.upload import UploadServiceConfig
from .config.usage import load_model_pricing
from .mq.video_parse_consumer import VideoParseConsumer, VideoParseConsumerHandlers
from .mq.video_parse_queue import VideoParseQueue
from .repository.job import ParseJobRepository
from .repository.library import LibraryRepository
from .repository.model_usage import ModelUsageRepository
from .repository.parse_output_draft import ParseOutputDraftRepository
from .repository.upload import UploadRepository
from .service.job import ParseJobService
from .service.library import LibraryService
from .service.media import MediaService
from .service.parse_output_draft import ParseOutputDraftService
from .service.parse_usage import ParseUsageService
from .service.upload import UploadService
from .service.video_event_retrieval import VideoEventRetrievalService
from .service.video_parse import VideoParseRuntimeService
from .service.video_parse_dispatch import ParseDispatch
from .service.video_parse_media import VideoParseMediaService
from .service.video_parse_persistence import VideoParsePersistenceService
from .service.video_retrieval import VideoParseRetrievalService
from .storage.artifact import LocalArtifactStore

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class VideoServices:
    library: LibraryService
    uploads: UploadService
    jobs: ParseJobService
    media: MediaService
    retrieval: VideoParseRetrievalService | None
    event_retrieval: VideoEventRetrievalService | None
    persistence: VideoParsePersistenceService
    usage: ParseUsageService
    runtime: VideoParseRuntimeService
    queue: VideoParseQueue


def build_video_services(
    session_factory: sessionmaker, artifact_store: LocalArtifactStore, chunk_store, event_store, *,
    gateway_client: GatewayClient, gateway_config: GatewayConfig, parse_defaults: ParseConfig,
    parse_media: VideoParseMediaService, mq: MQConsumerConfig, video_concurrency: int,
    upload_config: UploadServiceConfig, images: ImageUploadConfig, retrieval_config: RetrievalConfig,
    request_timeout_seconds: float, max_output_tokens: int, construction_cleanup: ExitStack,
    index_generation: int, probe_duration=probe_video_duration_seconds, model_pricing_path=None,
    local_publisher=None, reference_store=None,
) -> VideoServices:
    library = LibraryService(
        LibraryRepository(
            session_factory,
            execution_mode="v2",
        ),
        artifact_store,
    )
    job_repository = ParseJobRepository(session_factory)
    jobs = ParseJobService(
        job_repository,
        library,
        execution_mode="v2",
        video_parse_index_store=chunk_store,
        video_event_index_store=event_store,
    )
    parse_usage = ParseUsageService(
        ModelUsageRepository(session_factory),
        drafts=ParseOutputDraftService(ParseOutputDraftRepository(session_factory)),
        pricing=load_model_pricing(model_pricing_path),
    )
    construction_cleanup.callback(parse_usage.close)
    parse_dispatch = ParseDispatch(session_factory, reconcile_interval=mq.heartbeat_interval)
    parse_persistence = VideoParsePersistenceService(session_factory, dispatch=parse_dispatch)
    media = MediaService(
        library, artifact_store, config=images,
        parse_persistence=parse_persistence,
        reference_index_name=reference_store.index_name if reference_store is not None else None,
    )

    retrieval = None
    if local_publisher is None:
        retrieval, video_parse_query_encoder = build_retrieval_service(
            parse_persistence, chunk_store,
            gateway_config=gateway_config, embedding_config=parse_defaults.embedding,
            retrieval_config=retrieval_config,
            index_generation=index_generation,
            request_timeout_seconds=request_timeout_seconds,
            max_output_tokens=max_output_tokens,
            gateway_client=gateway_client,
        )
    else:
        video_parse_query_encoder, _ = build_query_embedding(
            gateway_client, parse_defaults.embedding, request_timeout_seconds=request_timeout_seconds,
        )
    event_contract = None
    event_retrieval = None
    if event_store is not None:
        event_contract = resolve_event_indexing_contract(
            video_parse_query_encoder, max_height=parse_defaults.window.analysis_max_height,
            vision_enabled=parse_defaults.embedding.input_mode == "multimodal", preprocessing=parse_media.preprocessing,
        )
        if local_publisher is not None:
            event_store.bind_contract(event_contract, create=True)
        event_retrieval = build_event_retrieval_service(
            parse_persistence, event_store, gateway_config=gateway_config,
            embedding_config=parse_defaults.embedding, retrieval_config=retrieval_config,
            contract=event_contract,
            request_timeout_seconds=request_timeout_seconds, gateway_client=gateway_client,
        )
    uploads = UploadService(
        UploadRepository(session_factory),
        artifact_store,
        config=upload_config,
        probe_duration=probe_duration, preprocessing=parse_media.preprocessing,
        video_parse_index_store=chunk_store,
        parse_defaults=parse_defaults,
        video_parse_query_encoder=video_parse_query_encoder,
        video_parse_query_encoder_source=parse_defaults.embedding.source,
        video_parse_query_encoder_supports_sparse=adapter_capabilities(
            gateway_config.source(parse_defaults.embedding.source).adapter,
        ).sparse_embedding,
        video_event_index_store=event_store,
    )
    v2_queue = VideoParseQueue(session_factory, video_concurrency=video_concurrency,
                               dispatch=parse_dispatch)
    v2_runtime = VideoParseRuntimeService(
        parse_persistence,
        v2_queue,
        artifact_store,
        parse_media,
        gateway_config,
        chunk_store,
        worker_id=mq.consumer_id,
        lease_duration=mq.lease_duration,
        heartbeat_interval=mq.heartbeat_interval,
        parse_usage=parse_usage,
        gateway_client=gateway_client,
        event_store=event_store,
        event_contract=event_contract,
        reference_store=reference_store,
        local_publisher=local_publisher, preprocessing=parse_media.preprocessing,
    )
    return VideoServices(library, uploads, jobs, media, retrieval, event_retrieval,
                         parse_persistence, parse_usage, v2_runtime, v2_queue)


def build_video_consumer(video: VideoServices, mq: MQConsumerConfig, *, video_concurrency: int,
                         summarize=None, summarize_concurrency: int = 1) -> VideoParseConsumer:
    async def maintenance() -> None:
        # Both cleanups are best-effort and isolated so one failure cannot
        # stall the other's next maintenance cycle.
        try:
            await video.runtime.reap_stale_media()
        except Exception:
            _logger.exception("Video Parse V2 media reaper failed")
        try:
            await asyncio.to_thread(video.uploads.reconcile_deletions)
        except Exception:
            _logger.exception("upload deletion reconcile failed")

    return VideoParseConsumer(
        video.queue,
        VideoParseConsumerHandlers(
            prepare=video.runtime.prepare,
            understand=video.runtime.understand,
            failure=video.runtime.notify_task_failed,
            refine=video.runtime.refine,
            index=video.runtime.index,
            prefix=video.runtime.run_next_prefix,
            maintenance=maintenance,
            summarize=summarize,
        ),
        worker_id=mq.consumer_id,
        lease_duration=mq.lease_duration,
        heartbeat_interval=mq.heartbeat_interval,
        poll_interval=timedelta(seconds=mq.poll_interval_seconds),
        maintenance_interval=mq.lease_duration,
        video_concurrency=video_concurrency,
        summarize_concurrency=summarize_concurrency,
    )
