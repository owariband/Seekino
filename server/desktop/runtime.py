"""Python entry point used by the native host. All operations use one local library."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import timedelta
from functools import wraps
from hashlib import file_digest, sha256
import os
import sys
from pathlib import Path
from threading import Condition
from uuid import uuid4

from engine.common.config import VideoParseRuntimeConfig
from engine.common.pyav_media import PyAVMedia
from gateway.config import load_config
from gateway.model_factory import GatewayClient

from ..common.storage import build_artifact_storage
from ..config.agent import AgentServiceConfig
from ..config.media import ImageUploadConfig
from ..config.mq import MQConsumerConfig
from ..config.parse_profile import ParseConfig
from ..config.retrieval import RetrievalConfig
from ..config.storage import STORAGE_CONFIG, desktop_storage
from ..config.upload import UploadServiceConfig
from ..constants.identity import LOCAL_USER_ID
from ..constants.tenant import DEFAULT_LOCAL_TENANT_ID
from ..database import DatabaseEnvironment
from ..repository.upload import UploadRepository
from ..service.mcp_evidence import EvidenceRef, evidence_identity
from ..service.video_parse_media import VideoParseMediaService
from ..storage.sqlite_index import SQLiteSearchIndex
from ..video_services import VideoServices, build_video_consumer, build_video_services
from .database import open_desktop_database


@dataclass(frozen=True, slots=True)
class PlaybackRange:
    path: Path
    start_ms: int
    end_ms: int
    duration_ms: int = 0


@dataclass(frozen=True, slots=True)
class SearchHit:
    source_ref: str
    video_id: str
    start_ms: int
    end_ms: int
    description: str


@dataclass(frozen=True, slots=True)
class SearchResults:
    hits: tuple[SearchHit, ...]
    partial: bool


def _while_open(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self._lifetime:
            if self._closing:
                raise RuntimeError("the Seekino library is closed")
            self._active_calls += 1
        try:
            return method(self, *args, **kwargs)
        finally:
            with self._lifetime:
                self._active_calls -= 1
                if not self._active_calls:
                    self._lifetime.notify_all()
    return guarded


class DesktopLibrary:
    def __init__(self, video: VideoServices, resources: ExitStack):
        self._video = video
        self._resources = resources
        self._closed = False
        self._closing = False
        self._active_calls = 0
        self._lifetime = Condition()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self) -> None:
        with self._lifetime:
            if self._closing:
                self._lifetime.wait_for(lambda: self._closed)
                return
            self._closing = True
            self._lifetime.wait_for(lambda: self._active_calls == 0)
        try:
            self._resources.close()
        finally:
            with self._lifetime:
                self._closed = True
                self._lifetime.notify_all()

    @_while_open
    def create_series(self, name: str):
        return self._video.library.create_series(name=name, tenant_id=DEFAULT_LOCAL_TENANT_ID)

    @_while_open
    def list_series(self, *, query: str | None = None, offset: int = 0, limit: int = 100):
        return self._video.library.list_series(tenant_id=DEFAULT_LOCAL_TENANT_ID, query=query,
                                               offset=offset, limit=limit)

    @_while_open
    def series(self, series_id: str):
        return self._video.library.get_series(series_id, tenant_id=DEFAULT_LOCAL_TENANT_ID)

    @_while_open
    def list_tasks(self, *, offset: int = 0, limit: int = 100):
        return self._video.jobs.list_video_parse_tasks(
            tenant_id=DEFAULT_LOCAL_TENANT_ID, offset=offset, limit=limit,
        )

    @_while_open
    def video(self, video_id: str):
        return self._video.library.get_video(video_id, tenant_id=DEFAULT_LOCAL_TENANT_ID)

    @_while_open
    def thumbnail(self, video):
        image = self._video.media.thumbnail_view(video.video_id, tenant_id=DEFAULT_LOCAL_TENANT_ID, video=video)
        if image is None:
            return None
        try:
            return self._video.media.get_image(image.image_id, tenant_id=DEFAULT_LOCAL_TENANT_ID).path
        except FileNotFoundError:
            return None

    @_while_open
    def open_video(self, video_id: str) -> PlaybackRange:
        video = self._video.library.get_video(video_id, tenant_id=DEFAULT_LOCAL_TENANT_ID)
        original = self._video.media.get_original(video_id, tenant_id=DEFAULT_LOCAL_TENANT_ID, video=video)
        return PlaybackRange(original.path, 0, video.duration_seconds, video.duration_seconds)

    @_while_open
    def import_file(self, path: Path, *, series_id: str, episode_order: int,
                    idempotency_key: str, on_progress: Callable[[int, int], None] | None = None):
        """Use a stable request key to retry an interrupted or completed upload."""
        path = Path(path)
        types = {".mp4": "video/mp4", ".mov": "video/quicktime", ".avi": "video/x-msvideo"}
        media_type = types.get(path.suffix.lower())
        if not media_type:
            raise ValueError("import expects an MP4, MOV or AVI video")
        uploads = self._video.uploads
        with path.open("rb") as source:
            size = os.fstat(source.fileno()).st_size
            uploads.validate_size(size)
            digest = file_digest(source, "sha256").hexdigest()
            source.seek(0)
            state = uploads.create_session(
                tenant_id=DEFAULT_LOCAL_TENANT_ID, created_by_user_id=LOCAL_USER_ID,
                series_id=series_id, episode_order=episode_order, original_filename=path.name,
                media_type=media_type, total_size_bytes=size, idempotency_key=idempotency_key,
                content_sha256=digest,
            )
            if state.status != "COMPLETED":
                detail = uploads.get_detail(state.upload_session_id, tenant_id=DEFAULT_LOCAL_TENANT_ID)
                transferred = detail.session.uploaded_size_bytes
                if on_progress:
                    on_progress(transferred, size)
                for number in detail.missing_segment_numbers:
                    start = number * state.preferred_segment_size_bytes
                    source.seek(start)
                    data = source.read(min(state.preferred_segment_size_bytes, size - start))
                    uploads.upload_segment(state.upload_session_id, number, data, tenant_id=DEFAULT_LOCAL_TENANT_ID,
                                           checksum_sha256=sha256(data).hexdigest(), start_byte=start,
                                           end_byte_exclusive=start + len(data), total_size_bytes=size)
                    if on_progress:
                        transferred += len(data)
                        on_progress(transferred, size)
        return uploads.complete(state.upload_session_id, tenant_id=DEFAULT_LOCAL_TENANT_ID)

    @_while_open
    def task(self, task_id: str):
        return self._video.jobs.get_video_parse_task(task_id, tenant_id=DEFAULT_LOCAL_TENANT_ID)

    @_while_open
    def set_paused(self, task_id: str, *, paused: bool, expected_version: int, expected_lease_version: int):
        return self._video.jobs.set_paused(task_id, tenant_id=DEFAULT_LOCAL_TENANT_ID, paused=paused,
                                           expected_version=expected_version, expected_lease_version=expected_lease_version)

    @_while_open
    def retry_parse(self, upload_id: str, *, expected_job_version: int, expected_lease_version: int):
        return self._video.uploads.retry_parse(upload_id, tenant_id=DEFAULT_LOCAL_TENANT_ID, actor_user_id=LOCAL_USER_ID,
                                               expected_job_version=expected_job_version, expected_lease_version=expected_lease_version)

    @_while_open
    def reparse(self, upload_id: str, *, expected_job_version: int):
        return self._video.uploads.reparse(upload_id, tenant_id=DEFAULT_LOCAL_TENANT_ID, actor_user_id=LOCAL_USER_ID,
                                           expected_job_version=expected_job_version)

    @_while_open
    def delete_import(self, upload_id: str):
        return self._video.uploads.delete_session(upload_id, tenant_id=DEFAULT_LOCAL_TENANT_ID)

    @_while_open
    def rebuild_index(self, task_id: str) -> tuple[str, ...]:
        self._video.jobs.get_video_parse_task(task_id, tenant_id=DEFAULT_LOCAL_TENANT_ID)
        # Keep the Event contract frozen when the upload was accepted.
        return self._video.persistence.request_index_rebuild(task_id)

    @_while_open
    def search(self, query: str, *, series_ids: Sequence[str], keywords_only: bool = False,
               top_k: int = 10) -> SearchResults:
        weights = {"lexical_weight": 1.0, "text_weight": 0.0, "vision_weight": 0.0} if keywords_only else {}
        result = self._video.event_retrieval.search(query, tenant_id=DEFAULT_LOCAL_TENANT_ID, series_ids=series_ids,
                                                    top_k=top_k, **weights)
        parents = {hit.evidence.video_chunk_id: hit.evidence for hit in result.chunk_result.hits}
        hits = []
        for match in result.matches:
            evidence = parents[match.video_chunk_id]
            event = evidence.all_time_events[match.timeline_index]
            ref = EvidenceRef(identity=evidence_identity(evidence, DEFAULT_LOCAL_TENANT_ID),
                              video_chunk_id=match.video_chunk_id, event_id=match.event_id,
                              timeline_index=match.timeline_index)
            hits.append(SearchHit(ref.encode(), evidence.video_id, event["start_seconds"],
                                  event["end_seconds"], event["description"]))
        return SearchResults(tuple(hits), result.chunk_result.partial)

    @_while_open
    def open_result(self, source_ref: str) -> PlaybackRange:
        ref = EvidenceRef.decode(source_ref)
        evidence = ref.resolve(self._video.persistence, tenant_id=DEFAULT_LOCAL_TENANT_ID)
        event = evidence.all_time_events[ref.timeline_index]
        original = self._video.media.get_original(evidence.video_id, tenant_id=DEFAULT_LOCAL_TENANT_ID, video=evidence)
        return PlaybackRange(original.path, event["start_seconds"], event["end_seconds"], evidence.duration_seconds)


def open_library(library_root: Path | str) -> DesktopLibrary:
    root = Path(library_root).expanduser().resolve()
    storage = desktop_storage()
    # Operators may configure model connections for this library. Storage always comes from the bundle.
    models = root / "models.yaml"
    if not models.is_file():
        models = STORAGE_CONFIG.parents[1] / "deploy" / "cloud" / "models.example.yaml"
    config = load_config(models)
    supported = {"ark", "openai", "chat_api", "openai_audio", "openai_vision", "ollama_embedding"}
    for source in config.model_sources.values():
        if source.adapter not in supported:
            raise ValueError(f"desktop video processing requires an in-process model SDK: {source.adapter}")
    if config.tracing.enabled:
        raise ValueError("desktop video processing requires in-process tracing to be disabled")
    # A paused job loses its lease immediately. Release the desktop consumer's
    # occupied slot on the next heartbeat so a resumed job can run promptly.
    mq = MQConsumerConfig(consumer_id=f"desktop-{uuid4().hex}", heartbeat_interval=timedelta(seconds=2))
    runtime = VideoParseRuntimeConfig(video_concurrency=1)
    upload, images = UploadServiceConfig(), ImageUploadConfig()
    limits = AgentServiceConfig(**config.agent_runtime)
    dimensions = config.resolve_embedding_binding().dimensions
    media = PyAVMedia(video_encoder="libx264") if sys.platform == "win32" else PyAVMedia()
    defaults = ParseConfig.from_defaults(runtime=runtime, gateway=config, upload=upload, images=images,
                                         embedding_model=None, embedding_dimensions=dimensions, embedding_fps=None, mq=mq,
                                         preprocessing=media.preprocessing)
    db_path = root / storage.database.file
    if db_path.is_symlink():
        raise ValueError("desktop database must be within the library")
    with ExitStack() as cleanup:
        database = open_desktop_database(db_path)
        cleanup.callback(database.close)
        UploadRepository(database.session_factory).recover_interrupted_completions()
        index = SQLiteSearchIndex(database.session_factory, semantic_dimensions=dimensions,
                                  vision_dimensions=dimensions, vision_enabled=defaults.embedding.input_mode == "multimodal",
                                  index_name="desktop_events")
        gateway = GatewayClient(config, enable_supervisor=False, allow_local_endpoints=True)
        cleanup.callback(gateway.close)
        artifacts = build_artifact_storage(root / "media", environment=DatabaseEnvironment.PRODUCTION)
        video = build_video_services(
            database.session_factory, artifacts, index, index, gateway_client=gateway, gateway_config=config,
            parse_defaults=defaults, parse_media=VideoParseMediaService(artifacts.base_directory / "work" / "video-parse-v2", media=media),
            mq=mq, video_concurrency=runtime.video_concurrency, upload_config=upload, images=images,
            retrieval_config=RetrievalConfig(), request_timeout_seconds=limits.execution_timeout_seconds,
            max_output_tokens=limits.max_output_tokens, local_publisher=index, probe_duration=media.probe_duration,
            construction_cleanup=cleanup, index_generation=defaults.indexing.index_generation,
        )
        consumer = build_video_consumer(video, mq, video_concurrency=runtime.video_concurrency)
        cleanup.callback(consumer.close)
        video.uploads.reconcile_completed_staging()
        video.uploads.reconcile_deletions()
        consumer.start()
        return DesktopLibrary(video, cleanup.pop_all())
