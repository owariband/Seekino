"""Durable, targeted base Parse and request-scoped visual understanding."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import tempfile
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from agent.chat.realtime_understanding.state import (
    ChunkEvidence, ChunkTarget, ContextSummary, FrameEvidence, RealtimeContext,
    RealtimeRequest, VisualEvidence, VisualObservation,
)
from agent.video_parse_v2.input import ParseConfig
from engine.common.frame_sampler import extract_video_frames
from engine.parsev2.planning import interval_size, plan_chunks, plan_intervals
from gateway.config import GatewayConfig
from gateway.multimodal import ImageInput, TextInput

from ..model.dto.jobDTO import ChunkParseLease
from ..mq.video_parse_queue import MySqlVideoParseQueue
from ..repository.video_parse import (
    VideoParseConflictError, VideoParseLeaseLostError, VideoParseRepository,
)
from ..storage.artifact import ArtifactStore
from ..utils.time import now_utc
from .video_parse import VideoParseRuntimeService

_logger = logging.getLogger(__name__)


class RealtimeUnderstandingService:
    """Serve the Chat graph using the same task identity and base Parse worker.

    Base jobs belong to this service's executor, not a Chat waiter. Their real
    execution leases and model budgets continue when a waiter is cancelled.
    Refine and index delivery remain gated by full Parse acceptance in the DB.
    """

    def __init__(
        self,
        repository: VideoParseRepository,
        queue: MySqlVideoParseQueue,
        parse_runtime: VideoParseRuntimeService,
        artifact_store: ArtifactStore,
        *,
        gateway_config: GatewayConfig,
        default_parse_config: ParseConfig,
        worker_id: str,
        lease_duration: timedelta,
        heartbeat_interval: timedelta,
        max_concurrent_chunks: int = 3,
    ) -> None:
        if max_concurrent_chunks <= 0 or not worker_id.strip():
            raise ValueError("realtime requires a worker and positive Chunk concurrency")
        if not timedelta(0) < heartbeat_interval < lease_duration:
            raise ValueError("heartbeat_interval must be positive and shorter than lease_duration")
        self._repository = repository
        self._queue = queue
        self._runtime = parse_runtime
        self._artifacts = artifact_store
        self._gateway_config = gateway_config
        self._default_config = default_parse_config
        self._worker_id = worker_id
        self._lease_duration = lease_duration
        self._heartbeat_seconds = heartbeat_interval.total_seconds()
        self._max_concurrent_chunks = max_concurrent_chunks
        self._executor = ThreadPoolExecutor(max_workers=max_concurrent_chunks, thread_name_prefix="realtime-base")
        self._derived_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="realtime-derived")
        self._lock = threading.RLock()
        self._jobs: dict[str, Future] = {}
        self._frames: OrderedDict[tuple, FrameEvidence] = OrderedDict()
        self._derived: OrderedDict[tuple, object] = OrderedDict()
        self._deriving: dict[tuple, Future] = {}
        self._closed = False

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=True)
        self._derived_executor.shutdown(wait=True)

    async def prepare_context(self, request: RealtimeRequest) -> RealtimeContext:
        self._check(request)
        task_input = await asyncio.to_thread(
            self._repository.ensure_realtime_task,
            tenant_id=request.tenant_id, video_id=request.video_id,
            default_parse_config=self._default_config,
        )
        if request.anchor_ms > task_input.duration_seconds:
            raise ValueError("playback position exceeds the Video duration")
        config = ParseConfig.model_validate(task_input.task.parse_config)
        while True:
            self._check(request)
            rows = await asyncio.to_thread(
                self._repository.load_realtime_chunks, task_input.task.video_parse_task_id,
                tenant_id=request.tenant_id, video_id=request.video_id,
                task_version=task_input.task.version,
            )
            current_input = await asyncio.to_thread(
                self._repository.load_task_input, task_input.task.video_parse_task_id,
            )
            if len(rows) == task_input.task.chunk_count and current_input.task.status != "PREPARING":
                break
            if current_input.task.status in {"PARSING_FAILED", "INDEXING_FAILED"}:
                raise VideoParseConflictError("full Parse task requires retry before its topology is available")
            lease = await asyncio.to_thread(
                self._queue.claim_realtime_preparation, task_input.task.video_parse_task_id,
                self._worker_id, lease_duration=self._lease_duration,
            )
            if lease is None:
                await asyncio.sleep(min(0.1, self._remaining(request)))
                continue
            # No media/model work runs under this brief topology-only lease.
            plans = plan_chunks(duration_seconds=task_input.duration_seconds, chunk_duration=config.chunk_duration)
            chunks = await asyncio.to_thread(
                self._repository.ensure_video_chunks, lease,
                [(item.sequence_no, item.start_seconds, item.end_seconds) for item in plans],
            )
            intervals = plan_intervals(chunk_count=len(chunks), size=interval_size(
                interval_duration=config.interval_duration, chunk_duration=config.chunk_duration,
            ))
            await asyncio.to_thread(
                self._repository.ensure_parse_tasks, lease,
                [item.video_chunk_id for item in chunks],
                [(item.start_sequence_no, item.chunk_count) for item in intervals],
            )
            await asyncio.to_thread(self._repository.finish_realtime_preparation, lease)
        anchor = min(request.anchor_ms, task_input.duration_seconds - 1)
        center = next((row.sequence_no for row in rows if row.start_ms <= anchor < row.end_ms), None)
        if center is None:
            raise VideoParseConflictError("Video has no fixed Chunk at the playback position")
        targets = tuple(ChunkTarget(
            chunk_id=row.video_chunk_id, chunk_parse_task_id=row.chunk_parse_task_id,
            sequence_no=row.sequence_no, start_ms=row.start_ms, end_ms=row.end_ms,
        ) for row in rows if abs(row.sequence_no - center) <= 1)
        return RealtimeContext(
            request=request, series_id=task_input.series_id,
            video_parse_task_id=task_input.task.video_parse_task_id,
            task_version=task_input.task.version, duration_ms=task_input.duration_seconds,
            targets=targets, source_uri=task_input.original_source_uri,
        )

    async def ensure_chunk(self, context: RealtimeContext, target: ChunkTarget) -> ChunkEvidence:
        if target not in context.targets:
            raise ValueError("Chunk target is outside the realtime context")
        while True:
            self._check(context.request)
            rows = await self._load_chunks(context)
            row = next((item for item in rows if item.chunk_parse_task_id == target.chunk_parse_task_id), None)
            if row is None or row.video_chunk_id != target.chunk_id:
                raise VideoParseConflictError("Realtime Chunk identity changed")
            if row.content is not None:
                return ChunkEvidence(target, row.content, row.content_version)
            if row.status == "FAILED":
                return ChunkEvidence(target, error=str(row.last_error or "base Parse exhausted its retry budget"))
            if row.status == "COMPLETE":
                return ChunkEvidence(target, error="completed base Parse has no valid content")
            if row.parent_error is not None and row.status != "UNDERSTANDING":
                return ChunkEvidence(target, error=f"full Parse requires retry ({row.parent_error})")
            await asyncio.to_thread(self._start_chunk, target.chunk_parse_task_id)
            await asyncio.sleep(min(0.1, self._remaining(context.request)))

    async def observe_frames(self, context: RealtimeContext, *, prompt: str) -> VisualEvidence:
        self._check(context.request)
        task_input = await asyncio.to_thread(self._repository.load_task_input, context.video_parse_task_id)
        rows = await self._load_chunks(context)  # fence against task replacement before using media
        config = ParseConfig.model_validate(task_input.task.parse_config)
        frame = await asyncio.to_thread(self._extract_anchor, context, config.visual_input.max_height, task_input.content_hash)
        observation = await self._derive(
            context, config, rows, prompt, [ImageInput(frame.image_path), TextInput(prompt)],
            schema_name="RealtimeVisualObservation", result_type=VisualObservation,
            frame_refs=(frame.artifact_uri,),
        )
        caption = next((item.caption for item in observation.captions if item.frame_index == 0), "")
        self._check(context.request)
        return VisualEvidence((replace(frame, caption=caption),), observation.summary)

    async def compose_context(self, context: RealtimeContext, *, prompt: str) -> ContextSummary:
        self._check(context.request)
        rows = await self._load_chunks(context)
        task_input = await asyncio.to_thread(self._repository.load_task_input, context.video_parse_task_id)
        config = ParseConfig.model_validate(task_input.task.parse_config)
        return await self._derive(
            context, config, rows, prompt, [TextInput(prompt)],
            schema_name="RealtimeContextSummary", result_type=ContextSummary,
        )

    async def _load_chunks(self, context: RealtimeContext):
        return await asyncio.to_thread(
            self._repository.load_realtime_chunks, context.video_parse_task_id,
            tenant_id=context.request.tenant_id, video_id=context.request.video_id,
            task_version=context.task_version,
        )

    def _start_chunk(self, chunk_task_id: str) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("realtime understanding service is closed")
            if chunk_task_id in self._jobs or len(self._jobs) >= self._max_concurrent_chunks:
                return
            lease = self._queue.claim_realtime_chunk(
                chunk_task_id, self._worker_id, lease_duration=self._lease_duration,
            )
            if lease is None:
                return
            future = self._executor.submit(lambda: asyncio.run(self._run_base(lease)))
            self._jobs[chunk_task_id] = future
            future.add_done_callback(lambda _future: self._job_done(chunk_task_id))

    async def _derive(
        self, context, config, rows, prompt, inputs, *, schema_name, result_type,
        frame_refs=(),
    ):
        self._check(context.request)
        key = (
            context.request.tenant_id, context.request.video_id,
            context.video_parse_task_id, context.task_version, context.source_uri,
            context.request.anchor_ms,
            hashlib.sha256(context.request.question.encode()).hexdigest(), schema_name,
            tuple((row.chunk_parse_task_id, row.video_chunk_id, row.content_version)
                  for row in rows if row.chunk_parse_task_id in {item.chunk_parse_task_id for item in context.targets}),
            frame_refs, config.model_dump_json(), hashlib.sha256(prompt.encode()).hexdigest(),
        )
        with self._lock:
            if self._closed:
                raise RuntimeError("realtime understanding service is closed")
            cached = self._derived.get(key)
            if cached is not None:
                self._derived.move_to_end(key)
                return cached
            future = self._deriving.get(key)
            if future is None:
                if len(self._deriving) >= 16:
                    raise RuntimeError("realtime derived model capacity is busy")
                caller_context = copy_context()
                # The initiating deadline bounds shared work; cancelling one
                # waiter only stops that waiter. Exceptions never enter LRU.
                future = self._derived_executor.submit(caller_context.run, lambda: asyncio.run(
                    self._runtime.generate_realtime(
                        config.models.understanding, inputs,
                        schema_name=schema_name, result_type=result_type,
                        deadline=context.request.deadline, check_control=lambda: None,
                    )
                ))
                self._deriving[key] = future
                future.add_done_callback(lambda completed: self._derived_done(key, completed))
        while not future.done():
            self._check(context.request)
            await asyncio.sleep(min(0.05, self._remaining(context.request)))
        self._check(context.request)
        result = future.result()
        await self._load_chunks(context)
        return result

    def _derived_done(self, key: tuple, future: Future) -> None:
        with self._lock:
            self._deriving.pop(key, None)
            if future.cancelled() or future.exception() is not None:
                return
            self._derived[key] = future.result()
            self._derived.move_to_end(key)
            while len(self._derived) > 128:
                self._derived.popitem(last=False)

    def _job_done(self, chunk_task_id: str) -> None:
        with self._lock:
            self._jobs.pop(chunk_task_id, None)

    async def _run_base(self, lease: ChunkParseLease) -> None:
        job = asyncio.create_task(self._runtime.understand(lease))

        async def heartbeat() -> None:
            while not job.done():
                await asyncio.sleep(self._heartbeat_seconds)
                if job.done():
                    return
                active = await asyncio.to_thread(
                    self._queue.heartbeat_chunk, lease, lease_duration=self._lease_duration,
                )
                if not active:
                    job.cancel()
                    raise VideoParseLeaseLostError("realtime base Chunk lease was lost")

        pulse = asyncio.create_task(heartbeat())
        try:
            await job
        except (Exception, asyncio.CancelledError) as error:
            task_input = await asyncio.to_thread(self._repository.load_task_input, lease.video_parse_task_id)
            execution = ParseConfig.model_validate(task_input.task.parse_config).execution
            delay = min(execution.retry_max_delay_seconds, execution.retry_initial_delay_seconds * 2 ** lease.retry_count)
            await asyncio.to_thread(self._queue.retry_chunk, lease, error=str(error), retry_at=now_utc() + timedelta(seconds=delay))
            _logger.warning("realtime base Chunk failed: %s", lease.chunk_parse_task_id, exc_info=True)
        finally:
            pulse.cancel()
            await asyncio.gather(pulse, return_exceptions=True)

    def _extract_anchor(self, context: RealtimeContext, max_height: int, content_hash: str) -> FrameEvidence:
        self._check(context.request)
        timestamp = min(context.request.anchor_ms, context.duration_ms - 1)
        target = next(item for item in context.targets if item.start_ms <= timestamp < item.end_ms)
        key = (context.request.tenant_id, context.request.video_id, content_hash, context.source_uri, timestamp, max_height)
        with self._lock:
            cached = self._frames.get(key)
            if cached is not None and cached.image_path.is_file():
                self._frames.move_to_end(key)
                return replace(cached, chunk_id=target.chunk_id)
        source = self._artifacts.resolve(context.source_uri)
        with tempfile.TemporaryDirectory(prefix="anivag-realtime-frame-") as directory:
            extracted = extract_video_frames(
                source, [timestamp], output_dir=directory, max_height=max_height,
                timeout_seconds=self._remaining(context.request),
            )
            if len(extracted) != 1 or extracted[0][0] != timestamp:
                raise RuntimeError("source anchor frame extraction returned an invalid timestamp")
            self._check(context.request)
            frame_path = Path(extracted[0][1])
            with frame_path.open("rb") as image:
                checksum = hashlib.file_digest(image, "sha256").hexdigest()
            artifact = self._artifacts.publish_video_artifact(
                frame_path, series_id=context.series_id, video_id=context.request.video_id,
                category="keyframes", filename=f"realtime-{timestamp}-{checksum}.jpg",
            )
        frame = FrameEvidence(timestamp, target.chunk_id, artifact.uri, self._artifacts.resolve(artifact.uri))
        with self._lock:
            self._frames[key] = frame
            self._frames.move_to_end(key)
            while len(self._frames) > 64:
                self._frames.popitem(last=False)
        return frame

    @staticmethod
    def _remaining(request: RealtimeRequest) -> float:
        remaining = request.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("realtime understanding reached the Chat deadline")
        return remaining

    @classmethod
    def _check(cls, request: RealtimeRequest) -> None:
        request.check_control()
        cls._remaining(request)


__all__ = ["RealtimeUnderstandingService"]
