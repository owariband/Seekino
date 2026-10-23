"""Parse execution, model calls and transactional result persistence."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import tempfile
import threading
import time
import wave
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import ExitStack, asynccontextmanager, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Literal, TypeVar

from langchain_core.tools import BaseTool
from pydantic import BaseModel

from agent.video_parse_v2 import ParseAgent
from agent.video_parse_v2.errors import RefineProviderRetryExhaustedError
from agent.utils.context_canvas import ContextCanvas
from agent.utils.errors import AgentToolFeedbackError
from agent.utils.tool import as_langchain_tools
from agent.utils.tool_result import JsonToolResult
from agent.utils.video_frames import build_inspect_video_frames_tool
from agent.video_parse_v2.timelineEvent_canvas import (
    CanvasCompletion, ParseContextCanvas, PrefixCompactCompletion,
)
from agent.video_parse_v2.input import (
    GenerativeModelConfig,
    ParseInput,
    SpeakerEmbeddingModelConfig,
)
from agent.video_parse_v2.prefix.context import PrefixContext
from agent.video_parse_v2.refine.input import RefineInput
from agent.video_parse_v2.voiceprint import VoiceprintInput
from agent.video_parse_v2.tools import (
    build_understanding_tools, build_refine_tools, build_prefix_tools, build_voiceprint_tools,
)
from agent.video_parse_v2.prompts import build_ocr_prompt
from agent.video_parse_v2.state import (
    ChunkExecution,
    ChunkScheduleEntry,
    ChunkScheduleSnapshot,
    ChunkTaskStatus,
    LeaseToken,
    VideoChunk,
    VideoRunSnapshot,
    VideoTaskStatus,
)
from engine.common.transcription import TranscriptAnalysis, VideoASRAgent, normalize_transcription
from engine.parsev2.observations import ASRResult, OCRResult
from engine.parsev2.planning import ChunkPlan, IntervalPlan
from engine.parsev2.schemas import VideoParseResult
from engine.parsev2.speaker_change import detect_speaker_changes
from engine.parsev2.validation import (
    ValidatedIntervalRefinement,
    ValidatedPrefixRefinement,
    parse_content_fingerprint,
    validate_member_scopes,
)
from engine.rag import (
    ChunkIndexingContract,
    EncoderContract,
    SparseEncoderContract,
    VideoClipInputContract,
    VIDEO_CLIP_PREPROCESS_REVISION,
)
from engine.rag.indexing.events import EventIndexingContract
from gateway.config import EmbeddingModelBinding, GatewayConfig, ModelBinding
from gateway.agent_model.errors import ModelFailureError, ModelFailureReason, QueryModelIncompleteError
from gateway.embedding import EmbeddingModel
from gateway.model_factory import GatewayClient
from gateway.speaker_embedding import SpeakerEmbeddingModel, SpeakerEmbeddingResult
from gateway.multimodal import ImageInput, TextInput, VideoInput, adapter_capabilities
from gateway.tracing import LangfuseTracing
from gateway.usage import ModelCallCancelledError, model_call_context

from ..config.parse_profile import index_contract_fingerprint
from ..constants.parse import (
    PARSE_RETRIEVAL_CONTRACT, VOICEPRINT_B0_METHOD_VERSION, VOICEPRINT_B0_LEGACY_METHOD_VERSION,
    VOICEPRINT_B0_COHERENCE_METHOD_VERSION, VOICEPRINT_B0_INCLUSIVE_TAIL_METHOD_VERSION,
    VOICEPRINT_B0_DIMENSIONS,
    VOICEPRINT_B0_MIN_AUDIO_MS, VOICEPRINT_B0_TRIM_FRACTION, VOICEPRINT_B0_ACCEPT_GAP,
    VOICEPRINT_B0_ACCEPT_DURATION_MS, VOICEPRINT_B0_ACCEPT_MIN_CUES,
    VOICEPRINT_B0_CONFLICT_GAP, VOICEPRINT_B0_NO_VOICE_DURATION_MS,
    VOICEPRINT_B0_WINDOW_MS, VOICEPRINT_B0_DIRECT_MAX_MS,
    VOICEPRINT_B0_LABEL_MIN_RANK, VOICEPRINT_B0_LABEL_MIN_MARGIN,
    VOICEPRINT_B0_LABEL_MIN_DURATION_MS, VOICEPRINT_B0_LABEL_MIN_REF_COUNT,
    VOICEPRINT_B0_LABEL_MIN_REF_HIGHER_FRACTION, VOICEPRINT_B0_LABEL_TAIL_MIN_REFS,
    REFINE_PROVIDER_RETRY_DELAYS_SECONDS,
    SPEAKER_CONSISTENCY_METHOD, SPEAKER_CONSISTENCY_WINDOW_MS, SPEAKER_CONSISTENCY_HOP_MS,
    SPEAKER_CONSISTENCY_PARAMETERS,
)
from ..model.dto.jobDTO import (
    ChunkExecutionState,
    ChunkParseLease,
    IntervalParseLease,
    PrefixParseLease,
    VideoParseLease,
    VideoParseTaskInputState,
)
from ..mq.video_parse_queue import MySqlVideoParseQueue
from ..repository.video_parse import (
    ModelCallBudgetExhaustedError,
    VideoParseLeaseLostError,
    VideoParseRepository,
    VideoParseSnapshotConflictError,
    VoiceprintRegistrationRejectedError,
)
from ..storage.artifact import ArtifactIntegrityError, ArtifactStore
from ..storage.elasticsearch import ElasticsearchVideoChunkStore
from ..storage.elasticsearch_events import ElasticsearchVideoEventStore
from ..utils.execution import (
    BlockingCallCancelled, ExecutionCalls, cleanup_execution, execution_calls, run_blocking,
)
from .parse_usage import ParseUsageService
from .video_parse_index_coordinator import (
    VideoParseIndexCoordinator,
)
from .video_parse_media import (
    MediaLeaseIdentity,
    PreparedVideoParseMedia,
    VideoParseMediaService,
)
from .video_parse_model_calls import VideoParseModelCallManager


_Result = TypeVar("_Result", bound=BaseModel)
_T = TypeVar("_T")
_logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _ChunkBinding:
    lease: ChunkParseLease
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    state: ChunkExecutionState | None = None
    media_context: object | None = None
    media: PreparedVideoParseMedia | None = None


class VideoParseRuntimeService:
    """Run Parse with its existing repository, media and Gateway dependencies."""

    def __init__(
        self,
        repository: VideoParseRepository,
        queue: MySqlVideoParseQueue,
        artifact_store: ArtifactStore,
        media_service: VideoParseMediaService,
        gateway_config: GatewayConfig,
        elasticsearch_store: ElasticsearchVideoChunkStore,
        *,
        worker_id: str,
        lease_duration: timedelta,
        heartbeat_interval: timedelta,
        parse_usage: ParseUsageService | None = None,
        tracing: LangfuseTracing | None = None,
        gateway_client: GatewayClient | None = None,
        event_store: ElasticsearchVideoEventStore | None = None,
        event_contract: EventIndexingContract | None = None,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id must not be blank")
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        if not timedelta(0) < heartbeat_interval < lease_duration:
            raise ValueError("heartbeat_interval must be below the lease duration")
        if (event_store is None) != (event_contract is None):
            raise ValueError("Event indexing requires both a store and its configured contract")
        self._repository = repository
        self._queue = queue
        self._artifact_store = artifact_store
        self._media_service = media_service
        self._gateway_config = gateway_config
        self._gateway = gateway_client or GatewayClient(gateway_config)
        self._elasticsearch_store = elasticsearch_store
        self._event_store = event_store
        self._event_contract = event_contract
        self._worker_id = worker_id.strip()
        self._lease_duration = lease_duration
        self._heartbeat_interval = heartbeat_interval
        self._parse_usage = parse_usage
        self._tracing = tracing
        self._video_lease: ContextVar[VideoParseLease | None] = ContextVar(
            "video_parse_v2_video_lease", default=None
        )
        self._chunk_binding: ContextVar[_ChunkBinding | None] = ContextVar(
            "video_parse_v2_chunk_binding", default=None
        )
        self._interval_lease: ContextVar[IntervalParseLease | None] = ContextVar(
            "video_parse_v2_interval_lease", default=None
        )
        self._prefix_lease: ContextVar[PrefixParseLease | None] = ContextVar(
            "video_parse_v2_prefix_lease", default=None
        )
        self._model_manager: ContextVar[VideoParseModelCallManager | None] = ContextVar(
            "video_parse_v2_model_manager", default=None
        )
        self._change_events: dict[str, threading.Event] = {}
        self.agent = ParseAgent(runtime=self)

    # Consumer entry points -------------------------------------------------

    async def prepare(self, lease: VideoParseLease) -> None:
        token = self._video_lease.set(lease)
        try:
            await self.agent.InvokeVideoParse(lease.video_parse_task_id)
        except asyncio.CancelledError as error:
            if not getattr(error, "voiceprint_failed", False):
                try:
                    await asyncio.to_thread(self._repository.stop_voiceprint_preparation, lease)
                except VideoParseLeaseLostError:
                    pass
            raise
        finally:
            self._video_lease.reset(token)

    async def understand(self, lease: ChunkParseLease) -> None:
        binding = _ChunkBinding(lease)
        token = self._chunk_binding.set(binding)
        try:
            state = await asyncio.to_thread(
                self._repository.load_chunk_execution, lease
            )
            binding.state = state
            task_input = state.task_input
            async with self._model_stage(
                lease,
                task_input,
                sequence_no=state.sequence_no,
                category="parse",
                check_active=lambda: self._require_chunk_active(lease),
            ):
                await self.agent.chunk_workflow.InvokeUnderstanding(lease.chunk_parse_task_id)
        finally:
            self._chunk_binding.reset(token)

    async def refine(self, lease: IntervalParseLease) -> None:
        token = self._interval_lease.set(lease)
        try:
            task_input = await asyncio.to_thread(
                self._repository.load_task_input, lease.video_parse_task_id
            )
            async with self._model_stage(
                lease,
                task_input,
                sequence_no=None,
                category="parse",
                check_active=lambda: self._require_interval_active(lease),
            ) as manager:
                while True:
                    try:
                        await self.agent.interval_refine.InvokeRefine(lease.interval_parse_task_id)
                        return
                    except ModelFailureError as error:
                        if (task_input.task.parse_config.get("voiceprint_mode", "off") == "off" or error.reason not in {
                            ModelFailureReason.PROVIDER_CAPACITY, ModelFailureReason.PROVIDER_RATE_LIMIT,
                        }):
                            raise
                        manager.check_active()
                        retry = await asyncio.to_thread(
                            self._repository.reserve_refine_provider_retry, lease,
                            max_retries=len(REFINE_PROVIDER_RETRY_DELAYS_SECONDS),
                        )
                        if retry is None:
                            raise RefineProviderRetryExhaustedError(
                                "Refine transient provider retry budget exhausted",
                                reason=error.reason, retries=len(REFINE_PROVIDER_RETRY_DELAYS_SECONDS),
                            ) from error
                        delay = REFINE_PROVIDER_RETRY_DELAYS_SECONDS[retry - 1]
                        _logger.info(
                            "event=refine_provider_retry task=%s interval=%s lease=%s "
                            "reason=%s retry=%s max_retries=%s delay_seconds=%s",
                            lease.video_parse_task_id, lease.interval_parse_task_id, lease.lease_version,
                            error.reason, retry, len(REFINE_PROVIDER_RETRY_DELAYS_SECONDS), delay,
                        )
                        remaining = delay
                        while remaining > 0:
                            manager.check_active()
                            step = min(1.0, remaining)
                            await asyncio.sleep(step)
                            remaining -= step
                        manager.check_active()
        finally:
            self._interval_lease.reset(token)

    async def index(self, lease: ChunkParseLease) -> None:
        token = self._chunk_binding.set(_ChunkBinding(lease))
        try:
            task_input = await asyncio.to_thread(
                self._repository.load_task_input, lease.video_parse_task_id
            )
            inputs = self._parse_input(task_input)
            async with self._model_stage(
                lease,
                task_input,
                sequence_no=None,
                category="embedding",
                check_active=lambda: self._require_chunk_active(lease),
            ) as manager:
                coordinator, client = self._build_index_coordinator(
                    inputs,
                    check_active=manager.check_active,
                    index_generation=task_input.task.target_index_generation,
                    **({"lease": lease} if self._event_store is not None else {}),
                )
                try:
                    await self._run_blocking(coordinator.index_chunk, lease)
                finally:
                    await cleanup_execution(client.close)
            self._notify_change(lease.video_parse_task_id)
        finally:
            self._chunk_binding.reset(token)

    async def run_next_prefix(self) -> bool:
        lease = await asyncio.to_thread(
            self._repository.claim_next_prefix_any,
            self._worker_id,
            lease_duration=self._lease_duration,
        )
        if lease is None:
            return False
        await self._execute_prefix(lease)
        return True

    async def reap_stale_media(self) -> int:
        """Reap only old workspaces whose exact durable lease is no longer live."""

        predicate_errors: list[BaseException] = []

        def is_active(identity: MediaLeaseIdentity) -> bool:
            try:
                return self._queue.is_execution_lease_active(
                    identity.video_parse_task_id,
                    identity.execution_kind,
                    identity.execution_task_id,
                    identity.lease_version,
                )
            except BaseException as error:
                # Fail closed: a database read failure must retain the media.
                predicate_errors.append(error)
                return True

        reaped = await run_blocking(
            self._media_service.reap_stale,
            older_than_seconds=self._lease_duration.total_seconds() * 2,
            is_active=is_active,
        )
        if predicate_errors:
            _logger.warning(
                "Video Parse V2 media reaper retained workspaces after %d "
                "lease-check error(s)",
                len(predicate_errors),
                exc_info=(
                    type(predicate_errors[0]),
                    predicate_errors[0],
                    predicate_errors[0].__traceback__,
                ),
            )
        return reaped

    # Agent runtime: Video coordinator -------------------------------------

    async def load_video_run(self, video_parse_task_id: str) -> VideoRunSnapshot:
        task_input = await asyncio.to_thread(
            self._repository.load_task_input, video_parse_task_id
        )
        status = VideoTaskStatus(task_input.task.status)
        lease = self._video_lease.get()
        token = None
        if status != VideoTaskStatus.COMPLETE:
            current = self._require_video_lease()
            if current.video_parse_task_id != video_parse_task_id:
                raise VideoParseLeaseLostError("Video coordinator lease targets another task")
            token = self._lease_token(current)
        return VideoRunSnapshot(
            video_parse_task_id=video_parse_task_id,
            status=status,
            inputs=self._parse_input(task_input),
            duration_seconds=task_input.duration_seconds,
            lease=token,
        )

    async def ensure_video_chunks(
        self,
        video_parse_task_id: str,
        lease: LeaseToken,
        plans: Sequence[ChunkPlan],
    ) -> tuple[VideoChunk, ...]:
        current = self._require_video_token(video_parse_task_id, lease)
        rows = await asyncio.to_thread(
            self._repository.ensure_video_chunks,
            current,
            tuple((item.sequence_no, item.start_seconds, item.end_seconds) for item in plans),
        )
        return tuple(
            VideoChunk(
                video_chunk_id=item.video_chunk_id,
                sequence_no=item.sequence_no,
                start_seconds=item.start_seconds,
                end_seconds=item.end_seconds,
            )
            for item in rows
        )

    async def ensure_parse_tasks(
        self,
        video_parse_task_id: str,
        lease: LeaseToken,
        chunks: Sequence[VideoChunk],
        intervals: Sequence[IntervalPlan],
    ) -> None:
        current = self._require_video_token(video_parse_task_id, lease)
        await asyncio.to_thread(
            self._repository.ensure_parse_tasks,
            current,
            tuple(item.video_chunk_id for item in chunks),
            tuple((item.start_sequence_no, item.chunk_count) for item in intervals),
        )

    async def activate_video_task(
        self, video_parse_task_id: str, lease: LeaseToken
    ) -> None:
        current = self._require_video_token(video_parse_task_id, lease)
        await asyncio.to_thread(self._repository.activate_video_task, current)

    async def load_chunk_schedule(
        self, video_parse_task_id: str
    ) -> ChunkScheduleSnapshot:
        state = await asyncio.to_thread(
            self._queue.load_chunk_schedule, video_parse_task_id
        )
        return ChunkScheduleSnapshot(
            tasks=tuple(
                ChunkScheduleEntry(
                    chunk_parse_task_id=item.chunk_parse_task_id,
                    sequence_no=item.sequence_no,
                    status=ChunkTaskStatus(item.status),
                    phase=item.phase,
                    understanding_committed=item.understanding_committed,
                )
                for item in state.tasks
            ),
            wait_timeout_seconds=state.wait_timeout_seconds,
            terminal_error=state.terminal_error,
        )

    async def wait_for_video_change(
        self, video_parse_task_id: str, *, timeout_seconds: float
    ) -> None:
        event = self._change_events.setdefault(video_parse_task_id, threading.Event())
        try:
            await run_blocking(event.wait, timeout_seconds)
        finally:
            event.clear()

    async def generate_realtime(
        self,
        binding: GenerativeModelConfig,
        inputs: Sequence[ImageInput | VideoInput | TextInput],
        *,
        schema_name: str,
        result_type: type[_Result],
        deadline: float,
        check_control: Callable[[], None],
    ) -> _Result:
        """Reuse Gateway adapters for Chat's derived output under its own budget.

        Base understanding uses ``understand`` and the durable model-call
        manager. This entry point only produces question-dependent Chat output.
        """

        async def invoke(selected) -> _Result:
            check_control()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("realtime model call reached the Chat deadline")
            bounded = selected.model_copy(update={
                "request_timeout_seconds": min(selected.request_timeout_seconds, remaining),
            })
            try:
                result = await asyncio.wait_for(run_blocking(
                    self._invoke_multimodal, bounded, inputs,
                    schema_name=schema_name, result_type=result_type,
                    check_control=check_control,
                ), timeout=remaining)
            except asyncio.CancelledError:
                # The isolated call retains a pending-operation cancellation
                # marker; wait_for only converts plain CancelledError itself.
                if time.monotonic() >= deadline:
                    raise TimeoutError("realtime model call reached the Chat deadline") from None
                raise
            check_control()
            if time.monotonic() >= deadline:
                raise TimeoutError("realtime model call reached the Chat deadline")
            return result

        return await invoke(binding)

    async def advance_video_task(
        self, video_parse_task_id: str, lease: LeaseToken
    ) -> VideoTaskStatus:
        current = self._require_video_token(video_parse_task_id, lease)
        state = await asyncio.to_thread(self._repository.aggregate_task, current)
        return VideoTaskStatus(state.status)

    # Agent runtime: Voiceprint preparation -------------------------------

    async def load_voiceprint_input(
        self, video_parse_task_id: str, lease: LeaseToken,
    ) -> VoiceprintInput | None:
        current = self._require_video_token(video_parse_task_id, lease)
        source = await asyncio.to_thread(self._repository.load_task_input, video_parse_task_id)
        try:
            snapshot = await self._run_blocking(self._repository.begin_voiceprint_preparation, current)
        except BlockingCallCancelled as error:
            # A queued transaction may bind its phase after the cancellation's
            # immediate stop. Clear it again before Consumer's deferred release.
            def stop_after_binding(_future) -> None:
                try:
                    self._repository.stop_voiceprint_preparation(current)
                except VideoParseLeaseLostError:
                    pass
                except Exception:
                    _logger.exception("Deferred Voiceprint preparation stop failed; expiry will recover it")
            error.completion.add_done_callback(stop_after_binding)
            raise
        if snapshot is None:
            return None
        return {
            "video_parse_task_id": video_parse_task_id,
            "inputs": self._parse_input(source),
            "member_sequence_nos": snapshot["member_sequence_nos"],
            "member_contents": {key: VideoParseResult.model_validate(value)
                                for key, value in snapshot["member_contents"].items()},
            "member_versions": snapshot["member_versions"],
            "member_evidence": snapshot["member_evidence"],
            "sources": snapshot["sources"], "background": source.background,
            "failed_chunks": snapshot.get("failed_chunks", []),
            "timing": snapshot.get("timing", {}),
        }

    async def prepare_voiceprint_scoring(
        self, video_parse_task_id: str, lease: LeaseToken,
    ) -> None:
        """Build B0 in memory; publish its complete frozen result and ready together."""
        snapshot = await self.load_voiceprint_input(video_parse_task_id, lease)
        if snapshot is None:
            return
        current = self._require_video_token(video_parse_task_id, lease)
        source = await asyncio.to_thread(self._repository.load_task_input, video_parse_task_id)
        config = snapshot["inputs"].parse_config
        async with self._model_stage(
            current, source, sequence_no=None, category="parse", stage="voiceprint",
            check_active=lambda: self._require_video_active(current),
        ) as manager:
            encoder = self._speaker_encoder(config.models.speaker_embedding)
            timeout = asyncio.timeout(
                config.voiceprint.execution_timeout_seconds if config.voiceprint is not None
                else config.execution.parse_execution_timeout_seconds
            )
            pending: BlockingCallCancelled | None = None
            try:
                async with timeout:
                    try:
                        await self._run_blocking(
                            self._repository.bind_voiceprint_encoder, current,
                            model={"name": encoder.model_name, "encoder_revision": encoder.encoder_revision},
                        )
                        preparation = await self._run_blocking(
                            self._collect_voiceprint_scores, snapshot, encoder, source, current,
                            check_active=manager.check_active,
                        )
                        preparation.update(video_parse_task_id=video_parse_task_id, video_id=source.task.video_id)
                        artifact = await self._run_blocking(
                            self._store_voiceprint_scoring, source, current, preparation,
                            check_active=manager.check_active,
                        )
                        manager.check_active()
                        await self._run_blocking(
                            self._repository.finish_voiceprint_scoring, current,
                            preparation=preparation, artifact=artifact, check_active=manager.raise_if_cancelled,
                        )
                    except asyncio.CancelledError as error:
                        manager.cancel()
                        if isinstance(error, BlockingCallCancelled):
                            pending = error
                        if timeout.expired():
                            error.voiceprint_failed = True
                        raise
            except TimeoutError:
                if pending is not None:
                    pending.voiceprint_failed = True
                    raise pending
                raise
            finally:
                await cleanup_execution(encoder.close)
        self._notify_change(video_parse_task_id)

    def _collect_voiceprint_scores(
        self, snapshot: dict, encoder: SpeakerEmbeddingModel,
        source: VideoParseTaskInputState, lease: VideoParseLease, *,
        check_active: Callable[[], None],
    ) -> dict:
        from engine.parsev2.voiceprint_matching import score_voiceprint_batch

        started = time.monotonic()
        if encoder.dimensions != VOICEPRINT_B0_DIMENSIONS:
            raise ValueError("B0 requires the frozen 192-dimensional CAM++ encoder")
        cues = {}
        for member in snapshot["member_evidence"].values():
            for cue in ((member.get("evidence", member).get("asr") or {}).get("transcript", [])):
                identity = [cue["start_seconds"], cue["end_seconds"], cue["text"]]
                cue_id = "asr:" + hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
                cues[cue_id] = (cue["start_seconds"], cue["end_seconds"])
        # Source descriptions are annotations, not an acoustic song detector.
        song_pattern = re.compile(
            r"歌唱|歌声|歌う|歌词|演唱|唱歌|哼唱|吟唱|唱[出到着起]|片[头尾]曲|主题曲|singing|vocals|\bsings\b",
            re.I,
        )
        song_ranges = [(event.start_seconds, event.end_seconds)
                       for member in snapshot["member_contents"].values()
                       for event in member.timeline if song_pattern.search(event.description)]
        samples = []
        for source_id, line in snapshot["sources"].items():
            check_active()
            start, end = line["start_ms"], line["end_ms"]
            sample = {**{key: value for key, value in line.items() if key != "text_variants"},
                      "id": source_id, "source_id": source_id,
                      "position": line["event_index"], "label": line["speaker"],
                      "understanding_speaker": line["speaker"], "duration_ms": end - start,
                      "model": encoder.model_name, "encoder_revision": encoder.encoder_revision,
                      "cue_ids": sorted(key for key, (left, right) in cues.items()
                                        if left < end and start < right),
                      "song": any(left < end and start < right for left, right in song_ranges)}
            if not sample["cue_ids"]:
                sample["cue_ids"] = [f"dialogue:{start}:{end}"]
                sample["cue_source"] = "dialogue_fallback"
            else:
                sample["cue_source"] = "retained_asr"
            if end - start >= VOICEPRINT_B0_MIN_AUDIO_MS:
                embedding, digest = self._embed_voiceprint_sample(
                    encoder, source, lease, start_ms=start, end_ms=end, check_active=check_active,
                )
                sample.update(vector=embedding.vector, pcm_sha256=digest)
            samples.append(sample)
        for sample in samples:
            sample["overlap"] = any(other["id"] != sample["id"]
                                    and other["start_ms"] < sample["end_ms"]
                                    and sample["start_ms"] < other["end_ms"] for other in samples)
        parameters = self._voiceprint_parameters()
        batch = score_voiceprint_batch(
            samples, model=encoder.model_name, encoder_revision=encoder.encoder_revision,
            **{key: parameters[key] for key in (
                "dimensions", "min_duration_ms", "trim_fraction", "label_min_rank", "label_min_margin",
                "label_min_duration_ms", "label_min_ref_count", "label_min_ref_higher_fraction",
                "label_tail_min_refs",
            )},
        )
        lines = []
        for sample, score in zip(samples, batch["scores"], strict=True):
            check_active()
            lines.append(self._score_voiceprint_sample(sample, samples, parameters=parameters, score=score))
        return {"method_version": VOICEPRINT_B0_METHOD_VERSION,
                "parameters": parameters, "hygiene": batch["hygiene"],
                "model": {"name": encoder.model_name, "encoder_revision": encoder.encoder_revision},
                "samples": [sample for sample in samples if "vector" in sample],
                "lines": lines, "failed_chunks": snapshot["failed_chunks"],
                "timing": {**snapshot["timing"],
                           "preparation_elapsed_ms": int((time.monotonic() - started) * 1000)}}

    def _store_voiceprint_scoring(
        self, source: VideoParseTaskInputState, lease: VideoParseLease, preparation: dict, *,
        check_active: Callable[[], None],
    ) -> dict:
        content = json.dumps(preparation, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode()
        checksum = hashlib.sha256(content).hexdigest()
        check_active()
        with tempfile.TemporaryDirectory(prefix="anivag-voiceprint-") as directory:
            path = Path(directory) / "scoring.json"
            path.write_bytes(content)
            check_active()
            stored = self._artifact_store.publish_video_artifact(
                path, series_id=source.series_id, video_id=source.task.video_id,
                category="voiceprints", filename=f"{lease.video_parse_task_id}-{checksum}.json",
            )
        check_active()
        if stored.sha256 != checksum:
            raise ArtifactIntegrityError("Published voiceprint checksum differs from the complete preparation")
        return {"location": stored.uri, "checksum": checksum}

    async def _load_voiceprint_scoring(self, lease: VideoParseLease | IntervalParseLease) -> dict | None:
        manifest = await asyncio.to_thread(self._repository.load_voiceprint_scoring, lease)
        if manifest is None:
            return None
        return await self._run_blocking(self._read_voiceprint_scoring, lease, manifest)

    def _read_voiceprint_scoring(self, lease: VideoParseLease | IntervalParseLease, manifest: dict) -> dict:
        content = self._artifact_store.resolve(manifest["location"]).read_bytes()
        if hashlib.sha256(content).hexdigest() != manifest["checksum"]:
            raise ArtifactIntegrityError("Voiceprint scoring artifact checksum changed")
        result = json.loads(content)
        if (result.get("video_parse_task_id") != lease.video_parse_task_id
            or any(result.get(key) != manifest[key] for key in ("method_version", "model", "parameters"))
            or len(result.get("lines", [])) != manifest["line_count"]
            or len(result.get("samples", [])) != manifest["sample_count"]):
            raise ArtifactIntegrityError("Voiceprint scoring artifact differs from its task manifest")
        return result

    @staticmethod
    def _voiceprint_sources(samples: list[dict], reference_ids: set[str]) -> dict:
        return {sample["id"]: {key: value for key, value in sample.items()
                               if key not in {"vector", "text_variants"}}
                for sample in samples if sample["id"] in reference_ids}

    @staticmethod
    def _voiceprint_parameters() -> dict:
        return {"dimensions": VOICEPRINT_B0_DIMENSIONS, "min_duration_ms": VOICEPRINT_B0_MIN_AUDIO_MS,
                "trim_fraction": VOICEPRINT_B0_TRIM_FRACTION, "accept_gap": VOICEPRINT_B0_ACCEPT_GAP,
                "accept_duration_ms": VOICEPRINT_B0_ACCEPT_DURATION_MS,
                "accept_min_cues": VOICEPRINT_B0_ACCEPT_MIN_CUES,
                "conflict_gap": VOICEPRINT_B0_CONFLICT_GAP,
                "no_voice_duration_ms": VOICEPRINT_B0_NO_VOICE_DURATION_MS,
                "label_min_rank": VOICEPRINT_B0_LABEL_MIN_RANK,
                "label_min_margin": VOICEPRINT_B0_LABEL_MIN_MARGIN,
                "label_min_duration_ms": VOICEPRINT_B0_LABEL_MIN_DURATION_MS,
                "label_min_ref_count": VOICEPRINT_B0_LABEL_MIN_REF_COUNT,
                "label_min_ref_higher_fraction": VOICEPRINT_B0_LABEL_MIN_REF_HIGHER_FRACTION,
                "label_tail_min_refs": VOICEPRINT_B0_LABEL_TAIL_MIN_REFS}

    @staticmethod
    def _score_voiceprint_sample(
        query: dict, samples: list[dict], *, parameters: dict | None = None,
        excluded_reference_ids: Sequence[str] = (), method_version: str | None = None,
        score: dict | None = None,
    ) -> dict:
        from engine.parsev2.voiceprint_matching import classify_voiceprint_result, score_voiceprint_line

        version = VOICEPRINT_B0_METHOD_VERSION if method_version is None else method_version
        label_keys = ("label_min_rank", "label_min_margin", "label_min_duration_ms", "label_min_ref_count")
        policy_keys = {
            VOICEPRINT_B0_LEGACY_METHOD_VERSION: (),
            VOICEPRINT_B0_COHERENCE_METHOD_VERSION: (*label_keys, "label_min_consistency"),
            VOICEPRINT_B0_INCLUSIVE_TAIL_METHOD_VERSION: (*label_keys, "label_min_ref_higher_fraction"),
            VOICEPRINT_B0_METHOD_VERSION: (*label_keys, "label_min_ref_higher_fraction", "label_tail_min_refs"),
        }
        if version not in policy_keys:
            raise ValueError("Unsupported frozen B0 scoring method: " + str(version))
        if parameters is None:
            if method_version is not None:
                raise ValueError("An explicit B0 method requires its frozen parameters")
            parameters = VideoParseRuntimeService._voiceprint_parameters()
        scoring_keys = ("dimensions", "min_duration_ms", "trim_fraction")
        band_keys = ("accept_gap", "accept_duration_ms", "accept_min_cues", "conflict_gap", "no_voice_duration_ms")
        missing = set((*scoring_keys, *band_keys, *policy_keys[version])) - parameters.keys()
        if missing:
            raise ValueError("Frozen B0 scoring parameters are incomplete: " + ", ".join(sorted(missing)))
        if score is None:
            score = score_voiceprint_line(
                query, samples, model=query["model"], encoder_revision=query["encoder_revision"],
                excluded_reference_ids=excluded_reference_ids,
                **{key: parameters[key] for key in scoring_keys},
                **{key: parameters[key] for key in policy_keys[version]},
            )
        band = classify_voiceprint_result(
            score, current_label=query.get("label"), song=query.get("song", False),
            overlap=query.get("overlap", False),
            **{key: parameters[key] for key in band_keys},
        )
        return {**{key: value for key, value in query.items() if key != "vector"},
                **score, **band, "method_version": version,
                "similarity_metric": "cosine", "similarity_is_probability": False}

    async def generate_voiceprint_preparation(
        self, snapshot: VoiceprintInput, *, prompt: str, canvas: ContextCanvas,
    ) -> CanvasCompletion:
        lease = self._require_video_lease()
        if lease.video_parse_task_id != snapshot["video_parse_task_id"]:
            raise VideoParseLeaseLostError("Voiceprint execution targets another task")
        task_input = await asyncio.to_thread(self._repository.load_task_input, lease.video_parse_task_id)
        config = snapshot["inputs"].parse_config
        if config.voiceprint is None:
            raise ValueError("Voiceprint preparation is disabled")
        async with self._model_stage(
            lease, task_input, sequence_no=None, category="parse", stage="voiceprint",
            check_active=lambda: self._require_video_active(lease),
        ) as manager:
            encoder = self._speaker_encoder(config.models.speaker_embedding)
            preparation_lock = threading.RLock()
            failure_lock = threading.Lock()
            tool_calls = 0
            execution_error: Exception | None = None

            def raise_execution_error() -> None:
                with failure_lock:
                    error = execution_error
                if error is not None:
                    raise error

            def fail_execution(error: Exception) -> None:
                nonlocal execution_error
                with failure_lock:
                    if execution_error is None:
                        execution_error = error
                manager.cancel()

            def check_preparation() -> None:
                raise_execution_error()
                manager.check_active()

            def invoke_host(callback: Callable[..., dict], **values) -> dict:
                # Finish cannot overtake an in-flight registration or its failure.
                with preparation_lock:
                    check_preparation()
                    try:
                        return callback(**values)
                    except (AgentToolFeedbackError, ModelCallCancelledError, VideoParseLeaseLostError):
                        raise
                    except VoiceprintRegistrationRejectedError as error:
                        raise AgentToolFeedbackError(error.code, str(error), error.retry_hint) from error
                    except Exception as error:
                        fail_execution(error)
                        raise

            def count_tool(_name: str) -> None:
                nonlocal tool_calls
                with preparation_lock:
                    check_preparation()
                    if tool_calls >= config.voiceprint.max_tool_calls:
                        error = ModelCallBudgetExhaustedError("Voiceprint max_tool_calls exhausted")
                        fail_execution(error)
                        raise error
                    tool_calls += 1

            def register_voiceprint(*, source_ref: str, speaker: str, start_ms: int,
                                    end_ms: int, selection_basis: str, confidence: float | None = None,
                                    replace_source_ref: str | None = None) -> dict:
                source = snapshot["sources"].get(source_ref)
                if (source is None or source["speaker"] != speaker
                        or not source["start_ms"] <= start_ms < end_ms <= source["end_ms"]):
                    raise AgentToolFeedbackError(
                        "INVALID_VOICEPRINT_SOURCE", "Use a named authorized source and a subrange inside it.",
                        "Read sources from get_context_canvas; preserve its speaker and absolute milliseconds.",
                    )
                replacement = {"replace_source_ref": replace_source_ref} if replace_source_ref is not None else {}
                result = self._repository.precheck_voiceprint_registration(
                    lease, source_ref=source_ref, speaker=speaker, start_ms=start_ms, end_ms=end_ms,
                    model=encoder.model_name, encoder_revision=encoder.encoder_revision, **replacement,
                )
                if result is None:
                    embedding, pcm_sha256 = self._embed_speaker_segment(
                        encoder, task_input, lease, start_ms=start_ms, end_ms=end_ms,
                        check_active=manager.check_active,
                    )
                    manager.check_active()
                    result = self._repository.register_voiceprint(
                        lease, source_ref=source_ref, speaker=speaker, start_ms=start_ms, end_ms=end_ms,
                        selection_basis=selection_basis, confidence=confidence,
                        model=embedding.model, encoder_revision=embedding.encoder_revision,
                        vector=embedding.vector, pcm_sha256=pcm_sha256, duration_ms=embedding.duration_ms,
                        **replacement,
                    )
                    _logger.info(
                        "event=voiceprint_registered task=%s lease=%s source=%s start_ms=%s end_ms=%s model=%s revision=%s pcm=%s replaced_source=%s",
                        lease.video_parse_task_id, lease.lease_version, source_ref, start_ms, end_ms,
                        embedding.model, embedding.encoder_revision, pcm_sha256, replace_source_ref,
                    )
                return {**result, "reference": {key: value for key, value in result["reference"].items()
                                                if key != "vector"}, "dimension": encoder.dimensions}

            def unregister_voiceprint(*, source_ref: str) -> dict:
                result = self._repository.unregister_voiceprint(lease, source_ref=source_ref)
                _logger.info(
                    "event=voiceprint_unregistered task=%s lease=%s source=%s removed=%s refs_count=%s",
                    lease.video_parse_task_id, lease.lease_version, source_ref, result["removed"], result["refs_count"],
                )
                return result

            def finish_voiceprint_preparation(*, complete: bool, skip_reasons: dict[str, str]) -> dict:
                del complete  # The Tool schema accepts only true; persistence decides readiness.
                result = invoke_host(
                    lambda: self._repository.finish_voiceprint_preparation(lease, skip_reasons=skip_reasons),
                )
                _logger.info(
                    "event=voiceprint_preparation_finished task=%s lease=%s ready=%s registered=%s required=%s suspects=%s",
                    lease.video_parse_task_id, lease.lease_version, result["ready"],
                    result["registered_speaker_count"], result["minimum_speaker_count"],
                    [(item["speaker"], item["source_ref"], item["start_ms"], item["end_ms"],
                      round(item["candidate_mean"], 6))
                     for item in result.get("suspect_references", [])],
                )
                if result["ready"]:
                    try:
                        self._notify_change(lease.video_parse_task_id)
                    except Exception:
                        _logger.exception(
                            "Voiceprint preparation committed; scheduler notification failed task=%s lease=%s",
                            lease.video_parse_task_id, lease.lease_version,
                        )
                return result

            tools = as_langchain_tools(build_voiceprint_tools(
                canvas, get_speaker_list=lambda: invoke_host(
                    lambda: self._repository.get_voiceprint_speaker_list(lease),
                ),
                register_voiceprint=lambda **values: invoke_host(register_voiceprint, **values),
                unregister_voiceprint=lambda **values: invoke_host(unregister_voiceprint, **values),
                finish_voiceprint_preparation=finish_voiceprint_preparation,
                check_control=check_preparation, on_call=count_tool,
            ))
            pending: BlockingCallCancelled | None = None
            timeout = asyncio.timeout(config.voiceprint.execution_timeout_seconds)
            try:
                async with timeout:
                    try:
                        result = await self._generate_text_structured(
                            config.models.voiceprint, prompt, tools=tools, canvas=canvas,
                            result_type=CanvasCompletion, max_model_calls=config.execution.max_model_calls,
                            enforce_model_call_limit=True, tool_harness="deepagents",
                        )
                    except asyncio.CancelledError as error:
                        if isinstance(error, BlockingCallCancelled):
                            pending = error
                        with failure_lock:
                            failed = execution_error
                        if timeout.expired() or failed is not None:
                            error.voiceprint_failed = True
                        if failed is not None:
                            raise error from failed
                        raise
                    except Exception:
                        raise_execution_error()
                        raise
                    raise_execution_error()
                    return result
            except TimeoutError:
                if pending is not None:
                    # Keep the existing execution slot until native work exits. Release
                    # charges the still-active phase once; normal cancellation clears it.
                    pending.voiceprint_failed = True
                    raise pending
                raise
            finally:
                await cleanup_execution(encoder.close)

    async def ensure_voiceprint_prepared(self, snapshot: VoiceprintInput) -> None:
        lease = self._require_video_lease()
        self._require_video_active(lease)
        source = await asyncio.to_thread(self._repository.load_task_input, snapshot["video_parse_task_id"])
        if dict(source.task.detail.get("voiceprint") or {}).get("ready") is not True:
            raise ValueError("Voiceprint Agent returned without a successful finish Tool commit")

    async def notify_voiceprint_prepared(self, snapshot: VoiceprintInput) -> None:
        self._notify_change(snapshot["video_parse_task_id"])

    # Agent runtime: Chunk execution ---------------------------------------

    async def load_chunk_execution(
        self, chunk_parse_task_id: str
    ) -> ChunkExecution:
        binding = self._require_chunk_binding()
        if binding.lease.chunk_parse_task_id != chunk_parse_task_id:
            raise VideoParseLeaseLostError("Chunk lease targets another task")
        state = await asyncio.to_thread(
            self._repository.load_chunk_execution, binding.lease
        )
        binding.state = state
        return ChunkExecution(
            chunk_parse_task_id=state.chunk_parse_task_id,
            video_parse_task_id=state.video_parse_task_id,
            sequence_no=state.sequence_no,
            chunk_start_seconds=state.start_seconds,
            chunk_end_seconds=state.end_seconds,
            allowed_start_seconds=state.analysis_window_start_seconds,
            allowed_end_seconds=state.analysis_window_end_seconds,
            inputs=self._parse_input(state.task_input),
            lease=self._lease_token(binding.lease),
            background=state.task_input.background,
            source_media_ref=state.source_media_ref,
        )

    async def prepare_chunk_visual(self, execution: ChunkExecution) -> None:
        await self._ensure_chunk_media(execution)

    async def transcribe_chunk(self, execution: ChunkExecution) -> ASRResult:
        media = await self._ensure_chunk_media(execution)
        model = execution.inputs.parse_config.models.asr
        if model.mode == "none":
            return {"transcript": []}
        duration = execution.allowed_end_seconds - execution.allowed_start_seconds
        definition = ModelBinding.model_validate({
            key: value for key, value in model.model_dump(exclude_none=True).items()
            if key not in {"mode", "fps"}
        }).model_copy(update={"kind": "transcription"})
        transport = self._gateway.create_transcription_model(definition)
        try:
            if transport.input_mode == "audio":
                payload = await self._run_blocking(transport.transcribe, media.audio_path)
                analysis = normalize_transcription(payload, duration_seconds=duration)
            else:
                agent = VideoASRAgent(transport, fps=model.fps)
                analysis = await self._run_blocking(
                    agent.transcribe, media.video_path, duration_seconds=duration,
                )
        finally:
            await cleanup_execution(transport.close)
        if not isinstance(analysis, TranscriptAnalysis):
            analysis = TranscriptAnalysis.model_validate(analysis)
        return {
            "transcript": [
                {
                    "start_seconds": cue.start_seconds + execution.allowed_start_seconds,
                    "end_seconds": cue.end_seconds + execution.allowed_start_seconds,
                    "text": cue.text,
                    "speaker_id": cue.speaker_id,
                }
                for cue in analysis.transcript
            ]
        }

    async def read_chunk_text(self, execution: ChunkExecution) -> OCRResult:
        media = await self._ensure_chunk_media(execution)
        binding = execution.inputs.parse_config.models.ocr
        if binding is None:
            raise ValueError("OCR is enabled without a fixed OCR model")
        frames = [(timestamp, path) for timestamp, path in (media.ocr_frames if media.ocr_frames is not None else media.frames)
                  if execution.allowed_start_seconds <= timestamp < execution.allowed_end_seconds]
        timestamps = [timestamp for timestamp, _path in frames]
        prompt = build_ocr_prompt(timestamps)
        return await self._generate_multimodal(
            binding,
            [
                *(ImageInput(path=path) for _timestamp, path in frames),
                TextInput(text=prompt),
            ],
            schema_name="video_parse_v2_ocr",
            result_type=OCRResult,
        )

    @contextmanager
    def _understanding_audio_tools(
        self, execution: ChunkExecution, media: PreparedVideoParseMedia,
        canvas: ParseContextCanvas, asr_result: ASRResult,
    ):
        """Own one encoder and original PCM for this Understanding execution."""
        config = execution.inputs.parse_config
        manager = self._model_manager.get()
        encoder_binding = config.models.speaker_embedding
        asr_binding = config.models.asr
        if encoder_binding is None and asr_binding.mode == "none":
            yield build_understanding_tools(
                canvas, check_control=manager.check_active if manager is not None else None,
            ), "Audio tools unavailable: no frozen speaker encoder; ASR mode is none."
            return
        binding = self._require_chunk_execution(execution)
        if binding.state is None:
            raise RuntimeError("Chunk execution state was not loaded")
        # source_media_ref may be an inference proxy; speaker evidence uses the original.
        task_input = binding.state.task_input
        source = self._artifact_store.resolve(task_input.original_source_uri)
        check_active = manager.check_active if manager is not None else lambda: self._require_chunk_active(binding.lease)
        bounds = (execution.allowed_start_seconds, execution.allowed_end_seconds)
        lock = threading.Lock()
        encoder = None
        pcm = None
        audio_path = None
        embedding_cache: dict[str, list[float]] = {}
        analysis_cache: dict[tuple[int, int], dict] = {}

        def validate_range(start_ms: int, end_ms: int) -> None:
            check_active()
            if (type(start_ms) is not int or type(end_ms) is not int
                    or not bounds[0] <= start_ms < end_ms <= bounds[1]):
                raise AgentToolFeedbackError(
                    "INVALID_AUDIO_RANGE", "Audio tools are limited to this Chunk.",
                    f"Use absolute integer milliseconds within [{bounds[0]}, {bounds[1]}).",
                )

        def record(kind: str, start_ms: int, end_ms: int, payload: dict) -> dict:
            value = {
                "member_ref": execution.chunk_parse_task_id, "kind": kind,
                "start_ms": start_ms, "end_ms": end_ms,
                "source_content_hash": task_input.content_hash, **payload,
            }
            reference = kind + ":" + hashlib.sha256(
                json.dumps(value, sort_keys=True, ensure_ascii=False).encode(),
            ).hexdigest()
            check_active()
            canvas.register_evidence(reference, value)
            return {**value, "evidence_ref": reference, "revision": canvas.revision}

        with ExitStack() as resources:
            def analyze_speaker_consistency(*, start_ms: int, end_ms: int) -> dict:
                nonlocal encoder, pcm, audio_path
                validate_range(start_ms, end_ms)
                with lock:
                    check_active()
                    if (start_ms, end_ms) in analysis_cache:
                        return {**analysis_cache[start_ms, end_ms], "revision": canvas.revision}
                    if pcm is None:
                        audio_path = resources.enter_context(self._media_service.prepare_chunk_tool_media(
                            media, source_video=source, start_ms=bounds[0], end_ms=bounds[1],
                            input_mode="audio", max_height=config.visual_input.max_height,
                            check_active=check_active,
                        ))
                        with wave.open(str(audio_path), "rb") as audio:
                            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 16000):
                                raise ValueError("Speaker consistency requires 16 kHz mono PCM16")
                            pcm = audio.readframes(audio.getnframes())
                    available_end = min(end_ms, bounds[0] + len(pcm) // 32)
                    windows, vectors = [], []
                    unencodable = []
                    for start in range(start_ms, available_end - SPEAKER_CONSISTENCY_WINDOW_MS + 1,
                                       SPEAKER_CONSISTENCY_HOP_MS):
                        end = start + SPEAKER_CONSISTENCY_WINDOW_MS
                        fragment = pcm[(start - bounds[0]) * 32:(end - bounds[0]) * 32]
                        digest = hashlib.sha256(fragment).hexdigest()
                        windows.append({"start_ms": start, "end_ms": end, "pcm_sha256": digest})
                        # CAM++ rejects constant PCM. Keep the complete grid; do not
                        # silently remove these windows or pass a fake vector.
                        if fragment == fragment[:2] * (len(fragment) // 2):
                            unencodable.append({"start_ms": start, "end_ms": end, "reason": "constant_pcm"})
                    if unencodable:
                        payload = record("speaker_consistency", start_ms, end_ms, {
                            "method": SPEAKER_CONSISTENCY_METHOD, "analysis_status": "unavailable",
                            "parameters": {**SPEAKER_CONSISTENCY_PARAMETERS,
                                           "window_ms": SPEAKER_CONSISTENCY_WINDOW_MS,
                                           "hop_ms": SPEAKER_CONSISTENCY_HOP_MS, "vad_filter": False},
                            "model": encoder_binding.model, "encoder_revision": None,
                            "pcm_sha256": hashlib.sha256(pcm[
                                (start_ms - bounds[0]) * 32:(end_ms - bounds[0]) * 32
                            ]).hexdigest(),
                            "window_count": len(windows), "unencodable_windows": unencodable,
                            "candidates": [], "uncovered": [{"start_ms": start_ms, "end_ms": end_ms}],
                            "score_is_probability": False, "single_speaker_verified": False,
                            "interpretation": "This range was not scored: CAM++ cannot encode constant PCM windows. "
                                              "No windows were filtered; use media or local ASR to review it.",
                        })
                        analysis_cache[start_ms, end_ms] = payload
                        return payload
                    for window in windows:
                        check_active()
                        digest = window["pcm_sha256"]
                        if digest not in embedding_cache:
                            if encoder is None:
                                encoder = self._speaker_encoder(encoder_binding)
                                resources.callback(encoder.close)
                            fragment = pcm[(window["start_ms"] - bounds[0]) * 32:
                                           (window["end_ms"] - bounds[0]) * 32]
                            window_path = audio_path.with_name("consistency-window.wav")
                            with wave.open(str(window_path), "wb") as output:
                                output.setnchannels(1)
                                output.setsampwidth(2)
                                output.setframerate(16000)
                                output.writeframes(fragment)
                            result = encoder.embed_audio(window_path)
                            check_active()
                            if (result.model != encoder.model_name
                                    or result.encoder_revision != encoder.encoder_revision
                                    or result.dimension != VOICEPRINT_B0_DIMENSIONS
                                    or result.duration_ms != SPEAKER_CONSISTENCY_WINDOW_MS
                                    or not math.isclose(math.hypot(*result.vector), 1.0, abs_tol=1e-5)):
                                raise ValueError("Speaker consistency embedding differs from the frozen encoder")
                            embedding_cache[digest] = result.vector
                        vectors.append(embedding_cache[digest])
                    analysis = detect_speaker_changes(
                        windows, vectors, start_ms=start_ms, end_ms=end_ms,
                        **SPEAKER_CONSISTENCY_PARAMETERS,
                    )
                    reason_counts: dict[str, int] = {}
                    for scan in analysis["scans"]:
                        reason_counts[scan["reason"]] = reason_counts.get(scan["reason"], 0) + 1
                    payload = record("speaker_consistency", start_ms, end_ms, {
                        "method": SPEAKER_CONSISTENCY_METHOD, "analysis_status": "completed",
                        "parameters": {**analysis["parameters"], "window_ms": SPEAKER_CONSISTENCY_WINDOW_MS,
                                       "hop_ms": SPEAKER_CONSISTENCY_HOP_MS, "vad_filter": False},
                        "model": encoder.model_name if encoder is not None else encoder_binding.model,
                        "encoder_revision": encoder.encoder_revision if encoder is not None else None,
                        "pcm_sha256": hashlib.sha256(pcm[
                            (start_ms - bounds[0]) * 32:(end_ms - bounds[0]) * 32
                        ]).hexdigest(),
                        "window_count": len(windows), "scan_reason_counts": reason_counts,
                        "candidates": analysis["candidates"], "uncovered": analysis["uncovered"],
                        "candidate_statistics": [
                            scan for scan in analysis["scans"]
                            if scan["time_ms"] in {item["peak_ms"] for item in analysis["candidates"]}
                        ],
                        "score_is_probability": False, "single_speaker_verified": False,
                        "interpretation": "Review suggestions only; silence, prosody and clipping may trigger them. "
                                          "No candidate does not verify one speaker. Simultaneous overlap is not classified.",
                    })
                    analysis_cache[start_ms, end_ms] = payload
                    return payload

            def transcribe_segment(*, start_ms: int, end_ms: int) -> dict:
                validate_range(start_ms, end_ms)
                with lock:
                    check_active()
                    definition = ModelBinding.model_validate({
                        key: value for key, value in asr_binding.model_dump(exclude_none=True).items()
                        if key not in {"mode", "fps"}
                    }).model_copy(update={"kind": "transcription"})
                    transport = self._gateway.create_transcription_model(definition)
                    try:
                        with self._media_service.prepare_chunk_tool_media(
                            media, source_video=source, start_ms=start_ms, end_ms=end_ms,
                            input_mode=transport.input_mode, max_height=config.visual_input.max_height,
                            check_active=check_active,
                        ) as path:
                            digest = hashlib.sha256(path.read_bytes()).hexdigest()
                            if transport.input_mode == "audio":
                                analysis = normalize_transcription(
                                    transport.transcribe(path), duration_seconds=end_ms - start_ms,
                                )
                            else:
                                analysis = VideoASRAgent(transport, fps=asr_binding.fps).transcribe(
                                    path, duration_seconds=end_ms - start_ms,
                                )
                    finally:
                        transport.close()
                    check_active()
                    analysis = TranscriptAnalysis.model_validate(analysis)
                    return record("local_asr", start_ms, end_ms, {
                        "model": definition.model_dump(mode="json"), "media_sha256": digest,
                        "asr": {"transcript": [{
                            "start_seconds": cue.start_seconds + start_ms,
                            "end_seconds": cue.end_seconds + start_ms,
                            "text": cue.text, "speaker_id": cue.speaker_id,
                        } for cue in analysis.transcript]},
                        "single_speaker_verified": False,
                    })

            summaries = []
            if encoder_binding is not None:
                for cue in asr_result.get("transcript", []):
                    start, end = cue["start_seconds"], cue["end_seconds"]
                    # Invalid observations remain visible as ASR; do not invent an audio range.
                    if (type(start) is int and type(end) is int and bounds[0] <= start < end <= bounds[1]):
                        result = analyze_speaker_consistency(start_ms=start, end_ms=end)
                        summaries.append({
                            key: result[key] for key in (
                                "start_ms", "end_ms", "evidence_ref", "analysis_status", "candidates", "window_count",
                            )
                        })
            yield build_understanding_tools(
                canvas, analyze_speaker_consistency=analyze_speaker_consistency if encoder_binding is not None else None,
                transcribe_segment=transcribe_segment if asr_binding.mode != "none" else None,
                check_control=check_active,
            ), "\nHost audio review: " + json.dumps({
                "analyze_speaker_consistency_available": encoder_binding is not None,
                "transcribe_segment_available": asr_binding.mode != "none",
                "original_asr_reviews": summaries, "vad_filter": False,
                "instruction": "Read these evidence refs as needed. Review suggested ranges against media or local ASR. "
                               "You may split or retime within the Chunk even without a candidate. "
                               "No result certifies atomic dialogue or speaker identity.",
            }, ensure_ascii=False)

    async def understand_chunk(
        self,
        execution: ChunkExecution,
        *,
        asr_result: ASRResult,
        ocr_result: OCRResult | None,
        prompt: str,
        canvas: ParseContextCanvas,
    ) -> VideoParseResult:
        del ocr_result
        media = await self._ensure_chunk_media(execution)
        context = self._understanding_audio_tools(execution, media, canvas, asr_result)
        try:
            tools, audio_summary = await self._run_blocking(context.__enter__)
            prompt += "\n" + audio_summary
            visual = execution.inputs.parse_config.visual_input
            visual_start = media.visual_start_ms if media.visual_start_ms is not None else execution.allowed_start_seconds
            visual_end = media.visual_end_ms if media.visual_end_ms is not None else execution.allowed_end_seconds
            prompt += (
                f"\nHost visual input range: [{visual_start}, {visual_end}) ms. "
                f"Writable chunk range: [{execution.allowed_start_seconds}, {execution.allowed_end_seconds}) ms. "
                f"For native video, absolute_ms = {visual_start} + round(local_seconds * 1000). "
                "ASR/OCR timestamps remain absolute source-video milliseconds."
            )
            if (visual_start, visual_end) != (execution.allowed_start_seconds, execution.allowed_end_seconds):
                prompt += (
                    "\nWe supplied additional visual context outside your chunk to help interpret transitions. "
                    "Use it only as context; do not write events outside the writable chunk. "
                    "Additional images do not establish a voice identity; uncertain speakers must remain null."
                )
            media_inputs: list[ImageInput | VideoInput | TextInput]
            if visual.mode == "ordered_frames":
                media_inputs = [
                    *(ImageInput(path=path) for _timestamp, path in media.frames),
                    TextInput(text=(
                        prompt
                        + "\nOrdered frame timestamps: "
                        + str([timestamp for timestamp, _path in media.frames])
                    )),
                ]
            else:
                media_inputs = [
                    VideoInput(path=media.visual_video_path or media.video_path, fps=visual.fps),
                    TextInput(text=prompt),
                ]
            fallback_inputs = [
                *(ImageInput(path=path) for _timestamp, path in media.frames),
                TextInput(text=(
                    prompt
                    + "\nFallback ordered frame timestamps: "
                    + str([timestamp for timestamp, _path in media.frames])
                )),
            ]
            receipt = await self._generate_multimodal(
                execution.inputs.parse_config.models.understanding,
                media_inputs,
                fallback_inputs=fallback_inputs,
                schema_name="video_parse_v2_chunk",
                result_type=CanvasCompletion,
                tools=as_langchain_tools(tools), canvas=canvas,
                max_model_calls=execution.inputs.parse_config.execution.max_model_calls,
                tool_harness="deepagents",
            )
            CanvasCompletion.model_validate(receipt)
            return canvas.result(execution.chunk_parse_task_id)
        finally:
            await cleanup_execution(context.__exit__, None, None, None)

    async def commit_chunk_result(
        self,
        execution: ChunkExecution,
        result: VideoParseResult,
        *,
        evidence: dict[str, object],
    ) -> None:
        binding = self._require_chunk_execution(execution)
        content = result.model_dump(mode="json")
        if execution.inputs.parse_config.voiceprint_mode != "off":
            for event in content["timeline"]:
                if event["event_type"] == "DIALOGUE":
                    event["speaker_source"] = "understanding"
        await asyncio.to_thread(
            self._repository.commit_chunk_result,
            binding.lease,
            content=content,
            detail={"evidence": evidence},
        )

    async def notify_chunk_committed(self, execution: ChunkExecution) -> None:
        self._require_chunk_execution(execution)
        self._notify_change(execution.video_parse_task_id)

    async def cleanup_chunk_execution(self, execution: ChunkExecution) -> None:
        binding = self._require_chunk_execution(execution)
        async with binding.lock:
            context = binding.media_context
            binding.media_context = None
            binding.media = None
            if context is not None:
                await cleanup_execution(context.__exit__, None, None, None)

    # Agent runtime: Refine -------------------------------------------------

    async def load_refine_input(
        self, interval_parse_task_id: str
    ) -> RefineInput:
        lease = self._require_interval_lease(interval_parse_task_id, phase="REFINING")
        snapshot, task_input = await asyncio.gather(
            asyncio.to_thread(self._repository.load_interval_members, lease),
            asyncio.to_thread(
                self._repository.load_task_input, lease.video_parse_task_id
            ),
        )
        inputs = self._parse_input(task_input)
        by_id = {item.chunk_parse_task_id: item for item in snapshot.members}
        member_sequence_nos = {
            task_id: item.sequence_no for task_id, item in by_id.items()
        }
        duration = task_input.duration_seconds
        chunk_duration = inputs.parse_config.chunk_duration * 1000
        raw_scopes = {
            item.sequence_no: item.detail.get("scope", [item.sequence_no])
            for task_id, item in by_id.items()
        }
        validated_scopes = validate_member_scopes(
            raw_scopes, member_sequence_nos=list(range(snapshot.start_sequence_no,
                                                     snapshot.start_sequence_no + snapshot.chunk_count)),
        )
        if any(scope[-1] >= task_input.task.chunk_count for scope in validated_scopes.values()):
            raise ValueError("Refine member scope exceeds the fixed Video topology")
        member_scopes = {task_id: list(validated_scopes[item.sequence_no]) for task_id, item in by_id.items()}
        member_evidence = {
            task_id: dict(item.detail.get("evidence") or {"asr": None, "ocr": None, "availability": "not_retained"})
            for task_id, item in by_id.items()
        }
        if inputs.parse_config.voiceprint_mode != "off":
            scoring = await self._load_voiceprint_scoring(lease)
            if scoring is None:
                raise ValueError("Refine requires the committed B0 preparation")
            for task_id, item in by_id.items():
                lines = [line for line in scoring["lines"] if line["chunk_task_id"] == task_id]
                reference_ids = {reference_id for line in lines for candidate in line["candidates"]
                                 for reference_id in candidate["reference_ids"]}
                member_evidence[task_id]["voiceprint"] = {
                    "mode": inputs.parse_config.voiceprint_mode,
                    "method_version": scoring["method_version"], "lines": lines,
                    "samples": self._voiceprint_sources(scoring["samples"], reference_ids),
                    "conflicts": [line["id"] for line in lines if line["band"] == "CONFLICT"],
                    "accept_disagreements": [line["id"] for line in lines
                                             if line["band"] == "ACCEPT" and line["conflict"]],
                    "doubtful_labels": [line["id"] for line in lines if line.get("label_doubtful") is True],
                    "previous_decisions": list(item.detail.get("voiceprint_decisions", [])),
                }
        return {
            "interval_parse_task_id": snapshot.interval_parse_task_id,
            "video_parse_task_id": snapshot.video_parse_task_id,
            "inputs": inputs,
            "lease": self._lease_token(lease),
            "start_sequence_no": snapshot.start_sequence_no,
            "chunk_count": snapshot.chunk_count,
            "member_sequence_nos": member_sequence_nos,
            "member_bounds": {
                task_id: (
                    min(member_scopes[task_id]) * chunk_duration,
                    min(duration, (max(member_scopes[task_id]) + 1) * chunk_duration),
                )
                for task_id, item in by_id.items()
            },
            "member_contents": {
                task_id: VideoParseResult.model_validate(item.content)
                for task_id, item in by_id.items()
            },
            "member_versions": {
                task_id: item.version for task_id, item in by_id.items()
            },
            "member_scopes": member_scopes,
            "member_evidence": member_evidence,
            "member_fingerprints": {
                task_id: parse_content_fingerprint(item.content) for task_id, item in by_id.items()
            },
            "background": task_input.background,
            "write_bounds": (snapshot.start_sequence_no * chunk_duration,
                             min(duration, (snapshot.start_sequence_no + snapshot.chunk_count) * chunk_duration)),
            "voiceprint_references": [],
        }

    async def generate_interval_refinement(
        self,
        snapshot: RefineInput,
        *,
        prompt: str,
        canvas: ParseContextCanvas,
    ) -> CanvasCompletion:
        binding = (
            snapshot["inputs"].parse_config.models.refine
            or snapshot["inputs"].parse_config.models.understanding
        )
        manager = self._model_manager.get()
        check_control = manager.check_active if manager is not None else None
        finish_callback = None
        compare_callback = None
        frame_tool = None
        voice_review_enabled = snapshot["inputs"].parse_config.voiceprint_mode != "off"
        stop_feedback = None
        if voice_review_enabled:
            def voice_review_feedback() -> str:
                details = canvas.voiceprint_review_diagnostics()
                return (
                    "Voice review has not accepted the current Canvas: "
                    + json.dumps({
                        "revision": details["revision"], "gate_failure": details["gate_failure"],
                        "missing_count": len(details["missing"]), "inconsistent_count": len(details["inconsistent"]),
                        "first_unresolved_event": next(iter(details["events"]), None),
                    }, ensure_ascii=False, sort_keys=True)
                    + ". Keep the current draft and successful tool receipts. Resolve the listed decision "
                    "(edit the speaker only if your decision changes it); review any other conflicts, then call "
                    "finish_voiceprint_review again before completing."
                )

            def stop_feedback() -> str | None:
                manager = self._model_manager.get()
                if manager is not None:
                    manager.check_active()
                if canvas.voiceprint_review_passed():
                    return None
                _logger.info(
                    "event=refine_voice_review_completion_deferred task=%s interval=%s details=%s",
                    snapshot["video_parse_task_id"], snapshot["interval_parse_task_id"],
                    json.dumps(canvas.voiceprint_review_diagnostics(), ensure_ascii=False, sort_keys=True),
                )
                return voice_review_feedback()

            def finish_voiceprint_review(*, complete: bool) -> JsonToolResult:
                del complete
                manager = self._model_manager.get()
                if manager is not None:
                    manager.check_active()
                feedback = canvas.finish_voiceprint_review()
                _logger.info(
                    "event=refine_voice_review_checked task=%s interval=%s ready=%s missing=%s inconsistent=%s",
                    snapshot["video_parse_task_id"], snapshot["interval_parse_task_id"],
                    feedback["ready"], feedback["missing"], feedback["inconsistent"],
                )
                if not feedback["ready"]:
                    _logger.info(
                        "event=refine_voice_review_unresolved task=%s interval=%s details=%s",
                        snapshot["video_parse_task_id"], snapshot["interval_parse_task_id"],
                        json.dumps(canvas.voiceprint_review_diagnostics(), ensure_ascii=False, sort_keys=True),
                    )
                    raise AgentToolFeedbackError(
                        "VOICEPRINT_REVIEW_REQUIRED", voice_review_feedback(),
                        "Record consistent voice_decisions (edit speakers only when needed), then call "
                        "finish_voiceprint_review again. Do not complete until the gate succeeds.",
                    )
                return JsonToolResult(success=True, payload=feedback)

            finish_callback = finish_voiceprint_review
        encoder: SpeakerEmbeddingModel | None = None
        encoder_binding: SpeakerEmbeddingModelConfig | None = None
        if snapshot["inputs"].parse_config.voiceprint_enabled:
            lease = self._require_interval_token(
                snapshot["interval_parse_task_id"], snapshot["lease"], phase="REFINING",
            )
            source = await asyncio.to_thread(self._repository.load_task_input, snapshot["video_parse_task_id"])
            b0 = snapshot["inputs"].parse_config.voiceprint_mode != "off"
            scoring = (await self._load_voiceprint_scoring(lease)) if b0 else None
            references = (scoring["samples"] if scoring is not None else
                          await asyncio.to_thread(self._repository.load_voiceprint_references, lease))
            encoder_binding = snapshot["inputs"].parse_config.models.speaker_embedding
            audio_lock = threading.Lock()
            manager = self._model_manager.get()
            check_active = manager.check_active if manager is not None else lambda: self._require_interval_active(lease)

            def compare_speaker(*, start_ms: int, end_ms: int) -> dict:
                if encoder is None:
                    raise RuntimeError("Refine speaker encoder was not initialized")
                bounds = (min(value[0] for value in snapshot["member_bounds"].values()),
                          max(value[1] for value in snapshot["member_bounds"].values()))
                if not bounds[0] <= start_ms < end_ms <= min(bounds[1], source.duration_seconds):
                    raise AgentToolFeedbackError(
                        "INVALID_VOICEPRINT_TARGET", "The target is outside this Refine's authorized range.",
                        f"Use absolute milliseconds within [{bounds[0]}, {bounds[1]}).",
                    )
                with audio_lock:
                    if b0:
                        if scoring is None:
                            raise ValueError("B0 preparation is not ready")
                        if scoring["model"] != {"name": encoder.model_name, "encoder_revision": encoder.encoder_revision}:
                            raise ValueError("Refine encoder differs from the task's frozen B0 encoder")
                        result = self._compare_speaker_b0(
                            encoder, source, lease, references,
                            start_ms=start_ms, end_ms=end_ms, check_active=check_active,
                            parameters=scoring["parameters"], lines=scoring["lines"],
                            excluded_reference_ids=scoring.get("hygiene", {}).get("excluded_reference_ids", []),
                            method_version=scoring.get("method_version", VOICEPRINT_B0_LEGACY_METHOD_VERSION),
                        )
                        reference_ids = {reference_id for candidate in result["candidates"]
                                         for reference_id in candidate["reference_ids"]}
                        result["samples"] = self._voiceprint_sources(references, reference_ids)
                        evidence_ref = "voice-compare:" + hashlib.sha256(
                            json.dumps(result, sort_keys=True, ensure_ascii=False).encode(),
                        ).hexdigest()
                        canvas.register_evidence(evidence_ref, {"voiceprint_comparison": result})
                        return {
                            **{key: result[key] for key in (
                                "start_ms", "end_ms", "duration_ms", "method", "method_version",
                                "model", "encoder_revision", "gap", "candidate_margin",
                                "reason", "candidate_count", "similarity_metric",
                                "similarity_is_probability",
                                "label_score", "label_rank", "label_count", "label_margin",
                                "label_ref_count", "label_ref_consistency", "label_doubtful",
                                "label_ref_higher_count", "label_ref_higher_fraction",
                                "label_doubtful_basis",
                            ) if key in result},
                            "candidates": [{key: value for key, value in candidate.items()
                                            if key not in {"src", "reference_ids"}}
                                           for candidate in result["candidates"]],
                            "evidence_ref": evidence_ref, "evidence_section": "voiceprint_comparison",
                            "revision": canvas.revision,
                        }
                    return self._compare_speaker(
                        encoder, source, lease, references,
                        snapshot.setdefault("voiceprint_references", []),
                        start_ms=start_ms, end_ms=end_ms, check_active=check_active,
                    )

            compare_callback = compare_speaker
            check_control = check_active
        route = [binding]
        if binding.fallback is not None:
            route.append(binding.fallback)
        if all(model.supports_vision_input is not False
               and adapter_capabilities(self._gateway_config.source(model.source).adapter).visual_input_mode is not None
               for model in route):
            lease = self._require_interval_token(
                snapshot["interval_parse_task_id"], snapshot["lease"], phase="REFINING",
            )
            source = await asyncio.to_thread(
                self._repository.load_task_input, snapshot["video_parse_task_id"],
            )
            if (source.series_id != snapshot["inputs"].series_id
                    or source.task.video_id != snapshot["inputs"].video_id):
                raise VideoParseLeaseLostError("Refine source does not match the bound task")
            bounds = (min(value[0] for value in snapshot["member_bounds"].values()),
                      max(value[1] for value in snapshot["member_bounds"].values()))
            if not 0 <= bounds[0] < bounds[1] <= source.duration_seconds:
                raise ValueError("Refine frame scope exceeds its source video")
            manager = self._model_manager.get()
            frame_tool = build_inspect_video_frames_tool(
                source_path=self._artifact_store.resolve(source.original_source_uri),
                read_bounds=bounds, max_height=snapshot["inputs"].parse_config.visual_input.max_height,
                timeout_seconds=binding.request_timeout_seconds,
                check_control=manager.check_active if manager is not None else lambda: self._require_interval_active(lease),
                trace_context={"video_parse_task_id": snapshot["video_parse_task_id"],
                               "interval_parse_task_id": snapshot["interval_parse_task_id"],
                               "video_id": snapshot["inputs"].video_id, "content_hash": source.content_hash},
            )
            fallback = binding.fallback
            if fallback is not None:
                fallback = fallback.model_copy(update={"supports_vision_input": True})
            binding = binding.model_copy(update={"supports_vision_input": True, "fallback": fallback})
        tools = as_langchain_tools(build_refine_tools(
            canvas, compare_speaker=compare_callback, finish_voiceprint_review=finish_callback,
            inspect_video_frames=frame_tool, check_control=check_control,
        ))
        try:
            if encoder_binding is not None:
                encoder = self._speaker_encoder(encoder_binding)
            receipt = await self._generate_text_structured(
                binding,
                prompt,
                tools=tools, canvas=canvas,
                result_type=CanvasCompletion,
                max_model_calls=snapshot["inputs"].parse_config.execution.max_model_calls,
                tool_harness="deepagents",
                **({"stop_feedback": stop_feedback, "enforce_model_call_limit": True} if voice_review_enabled else {}),
            )
            if voice_review_enabled and (canvas.voiceprint_review() is None or not canvas.voiceprint_review_passed()):
                _logger.info(
                    "event=refine_voice_review_completion_rejected task=%s interval=%s details=%s",
                    snapshot["video_parse_task_id"], snapshot["interval_parse_task_id"],
                    json.dumps(canvas.voiceprint_review_diagnostics(), ensure_ascii=False, sort_keys=True),
                )
                raise QueryModelIncompleteError(
                    "Refine completed before finish_voiceprint_review accepted the current Canvas: "
                    + json.dumps(canvas.voiceprint_review_status(), ensure_ascii=False, sort_keys=True),
                    reason=ModelFailureReason.REQUIRED_TOOL_MISSING,
                )
            return receipt
        finally:
            if encoder is not None:
                await cleanup_execution(encoder.close)

    async def commit_interval_refinement(
        self,
        snapshot: RefineInput,
        result: ValidatedIntervalRefinement,
        *,
        voiceprint_decisions: list[dict] | None = None,
    ) -> bool:
        lease = self._require_interval_token(
            snapshot["interval_parse_task_id"], snapshot["lease"], phase="REFINING"
        )
        task_ids = {
            sequence: task_id
            for task_id, sequence in snapshot["member_sequence_nos"].items()
        }
        member_updates: dict[str, dict[str, object]] = {}
        detail = {"input_versions": snapshot["member_versions"], "input_scopes": snapshot["member_scopes"]}
        if snapshot["inputs"].parse_config.voiceprint_mode != "off":
            if voiceprint_decisions is None:
                raise QueryModelIncompleteError(
                    "Refine commit requires the completed Canvas voice decisions",
                    reason=ModelFailureReason.REQUIRED_TOOL_MISSING,
                )
            detail["voiceprint_decisions"] = voiceprint_decisions
        for update in result.updates:
            task_id = task_ids[update.sequence_no]
            values = update.model_dump(
                mode="json", exclude={"sequence_no"}, exclude_unset=True
            )
            if values:
                member_updates[task_id] = values
        try:
            await asyncio.to_thread(
                self._repository.commit_interval_refinement, lease,
                interval_content=result.model_dump(mode="json"), member_updates=member_updates,
                input_versions=snapshot["member_versions"],
                input_fingerprints=snapshot["member_fingerprints"],
                input_contents={
                    task_id: content.model_dump(mode="json")
                    for task_id, content in snapshot["member_contents"].items()
                },
                detail=detail,
            )
        except VideoParseSnapshotConflictError as error:
            _logger.info(
                "Refine snapshot changed; refreshing interval %s event=refine_commit_conflict "
                "task=%s video=%s lease=%s reason=%s details=%s",
                snapshot["interval_parse_task_id"], snapshot["video_parse_task_id"],
                snapshot["inputs"].video_id, lease.lease_version, error.reason,
                json.dumps({"message": str(error), "context": error.context,
                            "input_versions": snapshot["member_versions"],
                            "updated_members": sorted(member_updates),
                            "used_reference_count": len(snapshot.get("voiceprint_references", []))},
                           ensure_ascii=False, sort_keys=True),
            )
            return False
        return True

    async def notify_interval_committed(self, snapshot: RefineInput) -> None:
        self._notify_change(snapshot["video_parse_task_id"])

    # Agent runtime: Prefix -------------------------------------------------

    async def load_prefix_context(
        self, chunk_parse_task_id: str
    ) -> PrefixContext:
        lease = self._prefix_lease.get()
        if lease is None or lease.chunk_parse_task_id != chunk_parse_task_id:
            raise VideoParseLeaseLostError("Prefix lease is not bound to this task")
        task_input, content = await asyncio.gather(
            asyncio.to_thread(self._repository.load_task_input, lease.video_parse_task_id),
            asyncio.to_thread(self._repository.load_prefix_content, lease),
        )
        inputs = self._parse_input(task_input)
        scope = list(validate_member_scopes(
            {lease.sequence_no: content.detail.get("scope", [lease.sequence_no])},
            member_sequence_nos=[lease.sequence_no],
        )[lease.sequence_no])
        if scope[-1] >= task_input.task.chunk_count:
            raise ValueError("Prefix scope exceeds the fixed Video topology")
        start = min(scope) * inputs.parse_config.chunk_duration * 1000
        end = min(task_input.duration_seconds, (max(scope) + 1) * inputs.parse_config.chunk_duration * 1000)
        return {
            "chunk_parse_task_id": lease.chunk_parse_task_id,
            "video_parse_task_id": lease.video_parse_task_id,
            "inputs": inputs,
            "lease": self._lease_token(lease),
            "sequence_no": lease.sequence_no,
            "allowed_start_seconds": start,
            "allowed_end_seconds": end,
            "scope": scope,
            "content": VideoParseResult.model_validate(content.content),
            "content_version": content.version,
            "content_fingerprint": parse_content_fingerprint(content.content),
            "evidence": content.detail.get("evidence", {"asr": None, "ocr": None, "availability": "not_retained"}),
            "background": task_input.background,
            "write_bounds": (lease.sequence_no * inputs.parse_config.chunk_duration * 1000,
                             min(task_input.duration_seconds, (lease.sequence_no + 1) * inputs.parse_config.chunk_duration * 1000)),
            "series_prefix_compact": lease.series_prefix_compact,
            "video_prefix_compact": lease.video_prefix_compact,
        }

    async def generate_prefix_refinement(
        self,
        context: PrefixContext,
        *,
        prompt: str,
        canvas: ParseContextCanvas,
    ) -> CanvasCompletion:
        binding = context["inputs"].parse_config.models.prefix_context
        if binding is None:
            raise ValueError("Prefix execution has no fixed model binding")
        prefix = context["inputs"].parse_config.prefix_context
        maximum_tools = prefix.max_tool_calls if prefix is not None else 0
        if maximum_tools == 0:
            compact = await self._generate_text_structured(
                binding,
                prompt + "\nTool access is disabled. Return complete:true with series_compact and video_compact only; event updates are forbidden.",
                tools=(), result_type=PrefixCompactCompletion, max_model_calls=1,
            )
            compact = PrefixCompactCompletion.model_validate(compact)
            canvas.write(role="prefix", batch_id="compact-only-completion", expected_revision=canvas.revision,
                         operations=[{"op": "set_compact", "series_compact": compact.series_compact,
                                      "video_compact": compact.video_compact}])
            return CanvasCompletion(complete=True)
        tool_calls = 0

        def count_tool(_name):
            nonlocal tool_calls
            if tool_calls >= maximum_tools:
                raise ModelCallBudgetExhaustedError("Prefix max_tool_calls exhausted")
            tool_calls += 1
        return await self._generate_text_structured(
            binding,
            prompt,
            tools=as_langchain_tools(build_prefix_tools(
                canvas, on_call=count_tool,
                check_control=self._model_manager.get().check_active if self._model_manager.get() else None,
            )), canvas=canvas,
            result_type=CanvasCompletion,
            max_model_calls=context["inputs"].parse_config.execution.max_model_calls,
        )

    async def commit_prefix_refinement(
        self,
        context: PrefixContext,
        result: ValidatedPrefixRefinement,
    ) -> bool:
        lease = self._require_prefix_token(
            context["chunk_parse_task_id"], context["lease"]
        )
        updates = (
            result.update.model_dump(
                mode="json", exclude={"sequence_no"}, exclude_unset=True
            )
            if result.update is not None
            else {}
        )
        try:
            await asyncio.to_thread(
                self._repository.commit_prefix, lease, content_updates=updates,
                video_prefix_compact=result.video_compact, series_prefix_compact=result.series_compact,
                input_version=context["content_version"], input_fingerprint=context["content_fingerprint"],
            )
        except VideoParseSnapshotConflictError:
            _logger.info("Prefix snapshot changed; refreshing chunk %s", context["chunk_parse_task_id"])
            return False
        return True

    async def notify_prefix_committed(self, context: PrefixContext) -> None:
        self._notify_change(context["video_parse_task_id"])

    # Internal execution contexts ------------------------------------------

    async def _execute_prefix(self, lease: PrefixParseLease) -> None:
        token = self._prefix_lease.set(lease)
        try:
            task_input = await asyncio.to_thread(
                self._repository.load_task_input, lease.video_parse_task_id
            )
            async with self._model_stage(
                lease,
                task_input,
                sequence_no=lease.sequence_no,
                category="parse",
                check_active=lambda: self._require_prefix_active(lease),
            ):
                await self.agent.prefix_context_agent.InvokePrefix(
                    lease.chunk_parse_task_id
                )
        except asyncio.CancelledError as error:
            if isinstance(error, BlockingCallCancelled):
                def release_after_exit(_future) -> None:
                    try:
                        self._repository.release_prefix(lease)
                    except Exception:
                        _logger.exception("Deferred Prefix lease release failed; expiry will recover it")
                error.completion.add_done_callback(release_after_exit)
            else:
                await asyncio.to_thread(self._repository.release_prefix, lease)
            raise
        except Exception as error:
            await asyncio.to_thread(
                self._repository.retry_prefix,
                lease,
                error=str(error),
            )
        finally:
            self._prefix_lease.reset(token)

    @asynccontextmanager
    async def _model_stage(
        self,
        lease: VideoParseLease | ChunkParseLease | IntervalParseLease | PrefixParseLease,
        task_input: VideoParseTaskInputState,
        *,
        sequence_no: int | None,
        category: Literal["parse", "embedding"],
        check_active: Callable[[], None],
        stage: str | None = None,
    ) -> AsyncIterator[VideoParseModelCallManager]:
        manager = VideoParseModelCallManager(
            self._repository,
            execution_lease=lease,
            lease_duration=self._lease_duration,
            check_active=check_active,
        )
        usage = (
            self._parse_usage.capture_video(
                lease,
                task_input,
                sequence_no=sequence_no,
                category=category,
            )
            if self._parse_usage is not None
            else nullcontext()
        )
        context = {
            "video_parse_task_id": lease.video_parse_task_id,
            "run_id": lease.video_parse_task_id,
            "lease_version": lease.lease_version,
            "sequence_no": sequence_no,
            "category": category,
            "stage": stage or (
                "index" if category == "embedding"
                else "refine" if isinstance(lease, IntervalParseLease)
                else "prefix" if isinstance(lease, PrefixParseLease)
                else "understand"
            ),
        }
        for name in (
            "chunk_parse_task_id", "interval_parse_task_id", "video_chunk_id",
            "attempt_count", "retry_count",
        ):
            value = getattr(lease, name, None)
            if value is not None:
                context[name] = value
        if task_input is not None:
            context.update(
                tenant_id=task_input.tenant_id,
                series_id=task_input.series_id,
                video_id=task_input.task.video_id,
                session_id=task_input.task.video_id,
            )
        tracing = (
            self._tracing.scope(context)
            if self._tracing is not None else nullcontext()
        )
        manager_token = self._model_manager.set(manager)
        calls = ExecutionCalls()
        calls_token = execution_calls.set(calls)
        stopped = asyncio.Event()
        heartbeat_errors: list[BaseException] = []
        track_execution = (
            isinstance(lease, ChunkParseLease)
            or isinstance(lease, IntervalParseLease) and category == "parse"
        )
        timing_started_at = time.time_ns() // 1_000_000
        timing_started = time.monotonic()

        async def sample_timing(*, finished: bool = False, outcome: str = "running") -> None:
            if not track_execution:
                return
            elapsed_ms = int((time.monotonic() - timing_started) * 1_000)
            try:
                recorded = await asyncio.to_thread(
                    self._repository.record_execution_timing,
                    lease, started_at_ms=timing_started_at, elapsed_ms=elapsed_ms,
                    finished=finished, outcome=outcome,
                )
                if finished or elapsed_ms == 0:
                    _logger.info(
                        "event=video_parse_execution_trace task=%s phase=%s "
                        "execution=%s lease=%s elapsed_ms=%s outcome=%s recorded=%s",
                        lease.video_parse_task_id, context["stage"],
                        context.get("chunk_parse_task_id") or context.get("interval_parse_task_id"),
                        lease.lease_version, elapsed_ms, outcome, recorded,
                    )
            except Exception as error:
                # Observability must not replace the business result or its error.
                _logger.warning(
                    "Video Parse execution timing append failed task=%s phase=%s "
                    "lease=%s error=%s",
                    lease.video_parse_task_id, context["stage"],
                    lease.lease_version, type(error).__name__,
                )

        async def heartbeat() -> None:
            try:
                while True:
                    try:
                        await asyncio.wait_for(
                            stopped.wait(),
                            timeout=self._heartbeat_interval.total_seconds(),
                        )
                        return
                    except TimeoutError:
                        pass
                    if not await asyncio.to_thread(manager.heartbeat):
                        raise VideoParseLeaseLostError(
                            "model-call reservation heartbeat was lost"
                        )
                    await sample_timing()
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                try:
                    committed = await asyncio.to_thread(
                        self._queue.is_execution_lease_committed,
                        lease,
                    )
                except BaseException:
                    committed = False
                if committed:
                    return
                heartbeat_errors.append(error)
                manager.cancel()

        try:
            await sample_timing()
            pulse = asyncio.create_task(heartbeat())
            body_error: BaseException | None = None
            try:
                with model_call_context(context), tracing, usage, manager.capture():
                    yield manager
            except BaseException as error:
                body_error = error
                manager.cancel()
                raise
            finally:
                stopped.set()
                await asyncio.gather(pulse, return_exceptions=True)
                manager.cancel()
                if body_error is None and heartbeat_errors:
                    try:
                        committed = await asyncio.to_thread(
                            self._queue.is_execution_lease_committed, lease,
                        )
                    except Exception:
                        committed = False
                    if committed:
                        heartbeat_errors.clear()
                await sample_timing(
                    finished=not calls.pending,
                    outcome=(
                        "cancelled" if isinstance(body_error, asyncio.CancelledError)
                        else "failed" if body_error is not None or heartbeat_errors
                        else "completed"
                    ),
                )
                if body_error is None and heartbeat_errors:
                    raise RuntimeError(
                        "Video Parse V2 model-call heartbeat failed"
                    ) from heartbeat_errors[0]
        finally:
            if calls.pending:
                _logger.warning(
                    "Video Parse execution revoked with blocking work still running task=%s phase=%s lease=%s",
                    lease.video_parse_task_id, context["stage"], lease.lease_version,
                )
            execution_calls.reset(calls_token)
            self._model_manager.reset(manager_token)

    async def _run_blocking(
        self,
        operation: Callable[..., _T],
        /,
        *args: object,
        **kwargs: object,
    ) -> _T:
        """Keep long calls off the database/heartbeat executor."""
        try:
            return await run_blocking(operation, *args, **kwargs)
        except asyncio.CancelledError:
            manager = self._model_manager.get()
            if manager is not None:
                manager.cancel()
            raise

    async def _ensure_chunk_media(
        self, execution: ChunkExecution
    ) -> PreparedVideoParseMedia:
        binding = self._require_chunk_execution(execution)
        async with binding.lock:
            if binding.media is not None:
                return binding.media
            if binding.state is None:
                raise RuntimeError("Chunk execution state was not loaded")
            config = execution.inputs.parse_config
            identity = MediaLeaseIdentity(
                execution.video_parse_task_id,
                execution.chunk_parse_task_id,
                execution.lease.version,
                execution_kind="chunk",
            )
            overlap_ms = config.visual_input.context_overlap_seconds * 1000 if config.understanding_enabled else 0
            context = self._media_service.prepare_chunk(
                identity,
                source_video=self._artifact_store.resolve(
                    binding.state.source_media_ref
                ),
                start_seconds=execution.allowed_start_seconds,
                end_seconds=execution.allowed_end_seconds,
                visual_start_ms=max(0, execution.allowed_start_seconds - overlap_ms),
                visual_end_ms=min(binding.state.task_input.duration_seconds,
                                  execution.allowed_end_seconds + overlap_ms),
                visual_mode=config.visual_input.mode,
                frame_fps=config.visual_input.fps,
                max_frames=config.visual_input.max_frames_per_chunk,
                max_height=config.visual_input.max_height,
                ocr_enabled=config.ocr_enabled,
                # A visual fallback consumes extended frames; OCR consumes core frames.
                visual_fallback_enabled=config.models.understanding.fallback is not None,
                check_active=lambda: self._require_chunk_active(binding.lease),
            )
            try:
                media = await self._run_blocking(context.__enter__)
            except BaseException:
                await cleanup_execution(context.__exit__, None, None, None)
                raise
            binding.media_context = context
            binding.media = media
            return media

    # Model and indexing assembly ------------------------------------------

    def _speaker_encoder(self, binding: SpeakerEmbeddingModelConfig | None) -> SpeakerEmbeddingModel:
        if binding is None:
            raise ValueError("Voiceprint execution has no frozen speaker encoder binding")
        return self._gateway.create_speaker_embedding_model(ModelBinding.model_validate({
            **binding.model_dump(mode="json"), "kind": "speaker_embedding",
        }))

    def _embed_speaker_segment(
        self, encoder: SpeakerEmbeddingModel, source: VideoParseTaskInputState,
        lease: VideoParseLease | IntervalParseLease, *, start_ms: int, end_ms: int,
        check_active: Callable[[], None],
    ) -> tuple[SpeakerEmbeddingResult, str]:
        if not 0 <= start_ms < end_ms <= source.duration_seconds:
            raise ValueError("Speaker audio exceeds the bound source video")
        identity = MediaLeaseIdentity(
            lease.video_parse_task_id,
            lease.interval_parse_task_id if isinstance(lease, IntervalParseLease) else lease.video_parse_task_id,
            lease.lease_version,
            execution_kind="interval" if isinstance(lease, IntervalParseLease) else "video",
        )
        with self._media_service.prepare_speaker_audio(
            identity, source_video=self._artifact_store.resolve(source.original_source_uri),
            start_ms=start_ms, end_ms=end_ms, check_active=check_active,
        ) as audio_path:
            with wave.open(str(audio_path), "rb") as audio:
                pcm_sha256 = hashlib.sha256(audio.readframes(audio.getnframes())).hexdigest()
            result = encoder.embed_audio(audio_path)
            check_active()
            if (result.model != encoder.model_name or result.encoder_revision != encoder.encoder_revision
                    or result.dimension != 192):
                raise ValueError("Speaker embedding differs from its encoder contract")
            return result, pcm_sha256

    def _embed_voiceprint_sample(
        self, encoder: SpeakerEmbeddingModel, source: VideoParseTaskInputState,
        lease: VideoParseLease | IntervalParseLease, *, start_ms: int, end_ms: int,
        check_active: Callable[[], None],
    ) -> tuple[SpeakerEmbeddingResult, str]:
        if end_ms - start_ms <= VOICEPRINT_B0_DIRECT_MAX_MS:
            result, digest = self._embed_speaker_segment(
                encoder, source, lease, start_ms=start_ms, end_ms=end_ms, check_active=check_active,
            )
            if not math.isclose(math.hypot(*result.vector), 1.0, abs_tol=1e-5):
                raise ValueError("B0 requires L2-normalized embeddings")
            return result, digest
        # Match the offline long-cue rule without changing the encoder or its limit.
        identity = MediaLeaseIdentity(
            lease.video_parse_task_id,
            lease.interval_parse_task_id if isinstance(lease, IntervalParseLease) else lease.video_parse_task_id,
            lease.lease_version,
            execution_kind="interval" if isinstance(lease, IntervalParseLease) else "video",
        )
        with self._media_service.prepare_speaker_audio(
            identity, source_video=self._artifact_store.resolve(source.original_source_uri),
            start_ms=start_ms, end_ms=end_ms, check_active=check_active,
        ) as audio_path:
            with wave.open(str(audio_path), "rb") as audio:
                pcm = audio.readframes(audio.getnframes())
            vectors = []
            duration = end_ms - start_ms
            offsets = list(range(0, duration - VOICEPRINT_B0_WINDOW_MS + 1, VOICEPRINT_B0_WINDOW_MS))
            if offsets[-1] + VOICEPRINT_B0_WINDOW_MS < duration:
                offsets.append(duration - VOICEPRINT_B0_WINDOW_MS)
            window_path = Path(audio_path).with_name("b0-window.wav")
            for offset in offsets:
                check_active()
                with wave.open(str(window_path), "wb") as window:
                    window.setnchannels(1)
                    window.setsampwidth(2)
                    window.setframerate(16000)
                    window.writeframes(pcm[offset * 32:(offset + VOICEPRINT_B0_WINDOW_MS) * 32])
                result = encoder.embed_audio(window_path)
                if (result.model != encoder.model_name or result.encoder_revision != encoder.encoder_revision
                        or result.dimension != VOICEPRINT_B0_DIMENSIONS
                        or not math.isclose(math.hypot(*result.vector), 1.0, abs_tol=1e-5)):
                    raise ValueError("Speaker embedding differs from its encoder contract")
                vectors.append(result.vector)
            centroid = [sum(values) / len(vectors) for values in zip(*vectors)]
            norm = math.hypot(*centroid)
            if not norm:
                raise ValueError("Long cue has no usable mean embedding")
            return SpeakerEmbeddingResult(
                model=encoder.model_name, encoder_revision=encoder.encoder_revision,
                vector=[value / norm for value in centroid], duration_ms=duration,
            ), hashlib.sha256(pcm).hexdigest()

    def _compare_speaker_b0(
        self, encoder: SpeakerEmbeddingModel, source: VideoParseTaskInputState,
        lease: IntervalParseLease, samples: list[dict], *, start_ms: int, end_ms: int,
        check_active: Callable[[], None], parameters: dict | None = None, lines: list[dict] | None = None,
        excluded_reference_ids: Sequence[str] = (), method_version: str | None = None,
    ) -> dict:
        matching = [row for row in (lines if lines is not None else samples)
                    if row["start_ms"] < end_ms and start_ms < row["end_ms"]]
        query = {"id": f"compare:{start_ms}:{end_ms}", "start_ms": start_ms, "end_ms": end_ms,
                 "model": encoder.model_name, "encoder_revision": encoder.encoder_revision,
                 "label": None, "cue_ids": sorted({cue for row in matching for cue in row["cue_ids"]}),
                 "song": any(row["song"] for row in matching),
                 "overlap": any(row["overlap"] for row in matching)}
        if end_ms - start_ms >= VOICEPRINT_B0_MIN_AUDIO_MS:
            embedding, digest = self._embed_voiceprint_sample(
                encoder, source, lease, start_ms=start_ms, end_ms=end_ms, check_active=check_active,
            )
            query.update(vector=embedding.vector, pcm_sha256=digest)
        result = self._score_voiceprint_sample(
            query, samples, parameters=parameters, excluded_reference_ids=excluded_reference_ids,
            method_version=method_version,
        )
        # A subrange may span several edited events and has no single current label.
        for key in ("band", "conflict", "review_reason"):
            result.pop(key, None)
        result["candidate_margin"] = result["gap"]
        return result

    def _compare_speaker(
        self, encoder: SpeakerEmbeddingModel, source: VideoParseTaskInputState,
        lease: IntervalParseLease, references: list[dict], used_references: list[dict], *,
        start_ms: int, end_ms: int, check_active: Callable[[], None],
    ) -> dict:
        metadata = {"start_ms": start_ms, "end_ms": end_ms,
                    "model": encoder.model_name, "encoder_revision": encoder.encoder_revision,
                    "similarity_metric": "cosine", "similarity_is_probability": False}
        eligible = [ref for ref in references
                    if ref["model"] == {"name": encoder.model_name, "encoder_revision": encoder.encoder_revision}
                    and not (ref["start_ms"] < end_ms and start_ms < ref["end_ms"])
                    and not (ref["source_start_ms"] < end_ms and start_ms < ref["source_end_ms"])]
        if not eligible:
            return {**metadata, "candidates": [], "candidate_margin": None,
                    "quality": {"reason": "no_eligible_reference"}}
        embedding, target_hash = self._embed_speaker_segment(
            encoder, source, lease, start_ms=start_ms, end_ms=end_ms, check_active=check_active,
        )
        # Conflicting labels for identical PCM cannot provide independent identity evidence.
        labels: dict[str, set[str]] = {}
        for ref in eligible:
            labels.setdefault(ref["pcm_sha256"], set()).add(ref["speaker"])
        groups: dict[str, list[dict]] = {}
        seen: set[str] = set()
        for ref in eligible:
            digest = ref["pcm_sha256"]
            if digest == target_hash or digest in seen or len(labels[digest]) != 1:
                continue
            seen.add(digest)
            groups.setdefault(ref["speaker"], []).append(ref)
        target_norm = math.hypot(*embedding.vector)
        target = [value / target_norm for value in embedding.vector]
        candidates = []
        for speaker, refs in groups.items():
            vectors = [[value / math.hypot(*ref["vector"]) for value in ref["vector"]] for ref in refs]
            centroid = [sum(values) / len(vectors) for values in zip(*vectors)]
            norm = math.hypot(*centroid)
            if norm == 0:
                continue
            similarity = max(-1.0, min(1.0, sum(a * b for a, b in zip(target, centroid)) / norm))
            candidates.append({"speaker": speaker, "similarity": similarity, "reference_count": len(refs),
                               "references": [{key: value for key, value in ref.items() if key != "vector"}
                                              for ref in refs]})
            for ref in refs:
                # 仅保留比较诊断；可信声源不作为 Refine 提交时的动态依赖。
                if ref not in used_references:
                    used_references.append(ref)
        candidates.sort(key=lambda item: (-item["similarity"], item["speaker"]))
        _logger.info(
            "event=voiceprint_compared task=%s interval=%s lease=%s start_ms=%s end_ms=%s pcm=%s candidates=%s refs=%s",
            lease.video_parse_task_id, lease.interval_parse_task_id, lease.lease_version,
            start_ms, end_ms, target_hash,
            [(item["speaker"], round(item["similarity"], 6)) for item in candidates[:3]],
            sum(item["reference_count"] for item in candidates),
        )
        return {**metadata, "candidates": candidates,
                "candidate_margin": (candidates[0]["similarity"] - candidates[1]["similarity"]
                                     if len(candidates) >= 2 else None),
                "quality": {"duration_ms": embedding.duration_ms, "pcm_sha256": target_hash,
                            "requested_duration_ms": end_ms - start_ms,
                            "reason": "compared" if candidates else "no_independent_reference"}}

    async def _generate_multimodal(
        self,
        binding: GenerativeModelConfig,
        inputs: Sequence[ImageInput | VideoInput | TextInput],
        *,
        fallback_inputs: Sequence[ImageInput | VideoInput | TextInput] | None = None,
        schema_name: str,
        result_type: type[_Result],
        tools: Sequence[BaseTool] = (),
        canvas: ContextCanvas | None = None,
        max_model_calls: int | None = None,
        enforce_model_call_limit: bool = False,
        tool_harness: Literal["legacy", "deepagents"] = "legacy",
        stop_feedback: Callable[[], str | None] | None = None,
    ) -> _Result:
        manager = self._model_manager.get()
        if tools:
            return await self._run_blocking(
                self._invoke_multimodal, binding, inputs,
                schema_name=schema_name, result_type=result_type,
                tools=tools, max_model_calls=max_model_calls,
                context_checkpoint=canvas.checkpoint if canvas is not None else None,
                check_control=manager.check_active if manager is not None else None,
                fallback_inputs=fallback_inputs,
                tool_harness=tool_harness,
                enforce_model_call_limit=enforce_model_call_limit,
                **({"stop_feedback": stop_feedback} if stop_feedback is not None else {}),
            )
        return await self._run_blocking(
            self._invoke_multimodal, binding, inputs,
            schema_name=schema_name, result_type=result_type,
            fallback_inputs=fallback_inputs,
            check_control=manager.check_active if manager is not None else None,
            **({"stop_feedback": stop_feedback} if stop_feedback is not None else {}),
        )

    async def _generate_text_structured(
        self,
        binding: GenerativeModelConfig,
        prompt: str,
        *,
        tools: Sequence[BaseTool],
        result_type: type[_Result],
        max_model_calls: int,
        enforce_model_call_limit: bool = False,
        canvas: ContextCanvas | None = None,
        tool_harness: Literal["legacy", "deepagents"] = "legacy",
        stop_feedback: Callable[[], str | None] | None = None,
    ) -> _Result:
        return await self._generate_multimodal(
            binding, [TextInput(prompt)], schema_name=result_type.__name__,
            result_type=result_type, tools=tools, canvas=canvas, max_model_calls=max_model_calls,
            tool_harness=tool_harness,
            enforce_model_call_limit=enforce_model_call_limit,
            **({"stop_feedback": stop_feedback} if stop_feedback is not None else {}),
        )

    def _invoke_multimodal(
        self,
        binding,
        inputs: Sequence[ImageInput | VideoInput | TextInput],
        *,
        schema_name: str,
        result_type: type[_Result],
        tools: Sequence[BaseTool] = (),
        max_model_calls: int | None = None,
        enforce_model_call_limit: bool = False,
        context_checkpoint=None,
        check_control=None,
        fallback_inputs=None,
        tool_harness: Literal["legacy", "deepagents"] = "legacy",
        stop_feedback: Callable[[], str | None] | None = None,
    ) -> _Result:
        definition = ModelBinding.model_validate(binding.model_dump(exclude_none=True))
        requires_vision = any(isinstance(item, (ImageInput, VideoInput)) for item in inputs)
        definition = definition.model_copy(update={
            "kind": "generative",
            "supports_vision_input": (
                requires_vision if binding.supports_vision_input is None
                else definition.supports_vision_input
            ),
            "fallback": (
                definition.fallback.model_copy(update={
                    "kind": "generative",
                    "supports_vision_input": (
                        any(isinstance(item, (ImageInput, VideoInput))
                            for item in (fallback_inputs or inputs))
                        if binding.fallback.supports_vision_input is None
                        else definition.fallback.supports_vision_input
                    ),
                }) if definition.fallback is not None else None
            ),
        })
        model = self._gateway.create_generative_model(definition, mcp=False)
        try:
            return model.generate(
                inputs, schema_name=schema_name, result_type=result_type,
                tools=tools,
                # Pass an explicit loop ceiling only for stages that opt into it.
                max_model_calls=max_model_calls if enforce_model_call_limit else None,
                check_control=check_control, context_checkpoint=context_checkpoint,
                fallback_inputs=fallback_inputs, tool_harness=tool_harness,
                **({"stop_feedback": stop_feedback} if stop_feedback is not None else {}),
            )
        finally:
            model.close()

    def _build_index_coordinator(
        self,
        inputs: ParseInput,
        *,
        check_active: Callable[[], None],
        index_generation: int,
        lease: ChunkParseLease | None = None,
    ) -> tuple[VideoParseIndexCoordinator, EmbeddingModel]:
        embedding = inputs.parse_config.models.embedding
        definition = EmbeddingModelBinding.model_validate(embedding.model_dump(exclude_none=True))
        embedder = self._gateway.create_embedding_model(definition)
        provider = embedder.provider_name
        dimensions = embedder.dimensions
        try:
            fingerprint = index_contract_fingerprint(
                embedding.model_copy(update={
                    "encoder_model": embedder.encoder_model, "dimensions": dimensions,
                }),
                provider=provider, encoder_revision=embedder.encoder_revision,
                max_height=inputs.parse_config.visual_input.max_height,
            )
            if fingerprint != inputs.parse_config.indexing.contract_fingerprint:
                raise ValueError("index encoder contract differs from the accepted task snapshot")
            event_options = {}
            if self._event_store is not None:
                if lease is None:
                    raise ValueError("Event indexing requires the current INDEX lease")
                encoder = EncoderContract(
                    provider=provider, model=embedder.encoder_model,
                    encoder_revision=embedder.encoder_revision, dimensions=dimensions,
                )
                event_contract = self._event_contract
                assert event_contract is not None
                if (encoder != event_contract.semantic_encoder or encoder != event_contract.vision_encoder):
                    raise ValueError("task encoder differs from the configured Event index")
                configured = {"index_name": self._event_store.index_name,
                              "contract_id": event_contract.contract_id, "contract": event_contract.as_dict()}
                frozen = self._repository.bind_event_index_target(lease, configured)
                accepted = EventIndexingContract.model_validate(frozen["contract"])
                if (accepted.semantic_encoder != event_contract.semantic_encoder
                        or accepted.vision_encoder != event_contract.vision_encoder):
                    raise ValueError("Event encoder differs from the frozen task target")
                event_options = {"event_store": self._event_store, "event_target": frozen}
            coordinator = VideoParseIndexCoordinator(
                self._repository,
                self._elasticsearch_store,
                embedder,
                media_resolver=self._artifact_store.resolve,
                media_service=self._media_service,
                check_active=check_active,
                **event_options,
                contract=ChunkIndexingContract(
                    retrieval_contract_id=PARSE_RETRIEVAL_CONTRACT,
                    semantic_encoder=EncoderContract(
                        provider=provider,
                        model=embedder.encoder_model,
                        encoder_revision=embedder.encoder_revision,
                        dimensions=dimensions,
                    ),
                    vision_encoder=EncoderContract(
                        provider=provider,
                        model=embedder.encoder_model,
                        encoder_revision=embedder.encoder_revision,
                        dimensions=dimensions,
                    ),
                    visual_input_contract=VideoClipInputContract(
                        fps=embedding.video_fps,
                        max_height=inputs.parse_config.visual_input.max_height,
                        mime_type="video/mp4",
                        preprocess_revision=VIDEO_CLIP_PREPROCESS_REVISION,
                    ),
                    index_generation=index_generation,
                    sparse_encoder=(
                        SparseEncoderContract(
                            provider=provider,
                            model=embedder.encoder_model,
                            encoder_revision=embedder.encoder_revision,
                        )
                        if inputs.parse_config.indexing.sparse_enabled else None
                    ),
                ),
            )
        except BaseException:
            embedder.close()
            raise
        return coordinator, embedder

    # Lease and identity helpers -------------------------------------------

    @staticmethod
    def _parse_input(task_input: VideoParseTaskInputState) -> ParseInput:
        return ParseInput.model_validate({
            "series_id": task_input.series_id,
            "video_id": task_input.task.video_id,
            "parse_config": task_input.task.parse_config,
        })

    @staticmethod
    def _lease_token(
        lease: VideoParseLease | ChunkParseLease | IntervalParseLease | PrefixParseLease,
    ) -> LeaseToken:
        return LeaseToken(
            owner=lease.worker_id,
            version=lease.lease_version,
            expires_at=lease.lease_expire_time,
        )

    def _require_video_lease(self) -> VideoParseLease:
        lease = self._video_lease.get()
        if lease is None:
            raise VideoParseLeaseLostError("No Video coordinator lease is bound")
        return lease

    def _require_video_token(
        self, video_parse_task_id: str | None, token: LeaseToken
    ) -> VideoParseLease:
        lease = self._require_video_lease()
        if video_parse_task_id is not None and lease.video_parse_task_id != video_parse_task_id:
            raise VideoParseLeaseLostError("Video coordinator lease targets another task")
        self._match_token(lease, token)
        return lease

    def _require_chunk_binding(self) -> _ChunkBinding:
        binding = self._chunk_binding.get()
        if binding is None:
            raise VideoParseLeaseLostError("No Chunk lease is bound")
        return binding

    def _require_chunk_execution(self, execution: ChunkExecution) -> _ChunkBinding:
        binding = self._require_chunk_binding()
        if (
            binding.lease.chunk_parse_task_id != execution.chunk_parse_task_id
            or binding.lease.video_parse_task_id != execution.video_parse_task_id
        ):
            raise VideoParseLeaseLostError("Chunk execution uses another lease")
        self._match_token(binding.lease, execution.lease)
        return binding

    def _require_interval_lease(
        self, interval_parse_task_id: str, *, phase: str
    ) -> IntervalParseLease:
        lease = self._interval_lease.get()
        if (
            lease is None
            or lease.interval_parse_task_id != interval_parse_task_id
            or lease.phase != phase
        ):
            raise VideoParseLeaseLostError("Interval lease is not current for this phase")
        return lease

    def _require_interval_token(
        self, interval_parse_task_id: str, token: LeaseToken, *, phase: str
    ) -> IntervalParseLease:
        lease = self._require_interval_lease(interval_parse_task_id, phase=phase)
        self._match_token(lease, token)
        return lease

    def _require_prefix_token(
        self, chunk_parse_task_id: str, token: LeaseToken
    ) -> PrefixParseLease:
        lease = self._prefix_lease.get()
        if lease is None or lease.chunk_parse_task_id != chunk_parse_task_id:
            raise VideoParseLeaseLostError("Prefix lease targets another task")
        self._match_token(lease, token)
        return lease

    @staticmethod
    def _match_token(
        lease: VideoParseLease | ChunkParseLease | IntervalParseLease | PrefixParseLease,
        token: LeaseToken,
    ) -> None:
        if lease.worker_id != token.owner or lease.lease_version != token.version:
            raise VideoParseLeaseLostError("Execution lease token is stale")

    def _require_chunk_active(self, lease: ChunkParseLease) -> None:
        if not self._queue.heartbeat_chunk(
            lease, lease_duration=self._lease_duration
        ):
            raise VideoParseLeaseLostError("Chunk lease is no longer current")

    def _require_video_active(self, lease: VideoParseLease) -> None:
        if not self._queue.heartbeat_video(lease, lease_duration=self._lease_duration):
            raise VideoParseLeaseLostError("Video coordinator lease is no longer current")

    def _require_interval_active(self, lease: IntervalParseLease) -> None:
        if not self._queue.heartbeat_interval(
            lease, lease_duration=self._lease_duration
        ):
            raise VideoParseLeaseLostError("Interval lease is no longer current")

    def _require_prefix_active(self, lease: PrefixParseLease) -> None:
        if not self._repository.heartbeat_prefix(
            lease, lease_duration=self._lease_duration
        ):
            raise VideoParseLeaseLostError("Prefix lease is no longer current")

    def _notify_change(self, video_parse_task_id: str) -> None:
        event = self._change_events.get(video_parse_task_id)
        if event is not None:
            event.set()


__all__ = ["VideoParseRuntimeService"]
