"""Parse execution, model calls and transactional result persistence."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import threading
import time
import wave
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Literal, TypeVar

from langchain_core.tools import BaseTool
from pydantic import BaseModel

from agent.video_parse_v2 import ParseAgent
from agent.utils.context_canvas_tools import build_context_canvas_tools
from agent.utils.context_canvas import ContextCanvas
from agent.utils.errors import AgentToolFeedbackError
from agent.utils.tool import as_langchain_tools
from agent.utils.video_frames import build_inspect_video_frames_tool
from agent.video_parse_v2.timelineEvent_canvas import (
    CanvasCompletion, ParseCanvasWrite, ParseContextCanvas, PrefixCompactCompletion,
)
from agent.video_parse_v2.input import (
    GenerativeModelConfig,
    ParseInput,
    SpeakerEmbeddingModelConfig,
)
from agent.video_parse_v2.prefix.context import PrefixContext
from agent.video_parse_v2.refine.input import RefineInput
from agent.video_parse_v2.voiceprint import (
    VoiceprintInput, build_compare_speaker_tool, build_tools as build_voiceprint_tools,
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
from gateway.config import EmbeddingModelBinding, GatewayConfig, ModelBinding
from gateway.embedding import EmbeddingModel
from gateway.model_factory import GatewayClient
from gateway.speaker_embedding import SpeakerEmbeddingModel, SpeakerEmbeddingResult
from gateway.multimodal import ImageInput, TextInput, VideoInput, adapter_capabilities
from gateway.tracing import LangfuseTracing
from gateway.usage import ModelCallCancelledError, model_call_context

from ..config.parse_profile import index_contract_fingerprint
from ..constants.parse import PARSE_RETRIEVAL_CONTRACT
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
from ..storage.artifact import ArtifactStore
from ..storage.elasticsearch import ElasticsearchVideoChunkStore
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
    ) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id must not be blank")
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        if not timedelta(0) < heartbeat_interval < lease_duration:
            raise ValueError("heartbeat_interval must be below the lease duration")
        self._repository = repository
        self._queue = queue
        self._artifact_store = artifact_store
        self._media_service = media_service
        self._gateway_config = gateway_config
        self._gateway = gateway_client or GatewayClient(gateway_config)
        self._elasticsearch_store = elasticsearch_store
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
            ):
                await self.agent.interval_refine.InvokeRefine(lease.interval_parse_task_id)
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
        }

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
                                    end_ms: int, selection_basis: str, confidence: float | None = None) -> dict:
                source = snapshot["sources"].get(source_ref)
                if (source is None or source["speaker"] != speaker
                        or not source["start_ms"] <= start_ms < end_ms <= source["end_ms"]):
                    raise AgentToolFeedbackError(
                        "INVALID_VOICEPRINT_SOURCE", "Use a named authorized source and a subrange inside it.",
                        "Read sources from get_context_canvas; preserve its speaker and absolute milliseconds.",
                    )
                result = self._repository.precheck_voiceprint_registration(
                    lease, source_ref=source_ref, speaker=speaker, start_ms=start_ms, end_ms=end_ms,
                    model=encoder.model_name, encoder_revision=encoder.encoder_revision,
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
                    )
                    _logger.info(
                        "event=voiceprint_registered task=%s lease=%s source=%s start_ms=%s end_ms=%s model=%s revision=%s pcm=%s",
                        lease.video_parse_task_id, lease.lease_version, source_ref, start_ms, end_ms,
                        embedding.model, embedding.encoder_revision, pcm_sha256,
                    )
                return {**result, "reference": {key: value for key, value in result["reference"].items()
                                                if key != "vector"}, "dimension": encoder.dimensions}

            def finish_voiceprint_preparation(*, complete: bool, skip_reasons: dict[str, str]) -> dict:
                del complete  # The Tool schema accepts only true; persistence decides readiness.
                result = invoke_host(
                    lambda: self._repository.finish_voiceprint_preparation(lease, skip_reasons=skip_reasons),
                )
                _logger.info(
                    "event=voiceprint_preparation_finished task=%s lease=%s ready=%s registered=%s required=%s",
                    lease.video_parse_task_id, lease.lease_version, result["ready"],
                    result["registered_speaker_count"], result["minimum_speaker_count"],
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
        timestamps = [timestamp for timestamp, _path in media.frames]
        prompt = build_ocr_prompt(timestamps)
        return await self._generate_multimodal(
            binding,
            [
                *(ImageInput(path=path) for _timestamp, path in media.frames),
                TextInput(text=prompt),
            ],
            schema_name="video_parse_v2_ocr",
            result_type=OCRResult,
        )

    async def understand_chunk(
        self,
        execution: ChunkExecution,
        *,
        asr_result: ASRResult,
        ocr_result: OCRResult | None,
        prompt: str,
        canvas: ParseContextCanvas,
    ) -> VideoParseResult:
        del asr_result, ocr_result
        media = await self._ensure_chunk_media(execution)
        visual = execution.inputs.parse_config.visual_input
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
                VideoInput(path=media.video_path, fps=visual.fps),
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
            tools=self._canvas_tools(canvas), canvas=canvas,
            max_model_calls=execution.inputs.parse_config.execution.max_model_calls,
            tool_harness="deepagents",
        )
        CanvasCompletion.model_validate(receipt)
        return canvas.result(execution.chunk_parse_task_id)

    async def commit_chunk_result(
        self,
        execution: ChunkExecution,
        result: VideoParseResult,
        *,
        evidence: dict[str, object],
    ) -> None:
        binding = self._require_chunk_execution(execution)
        await asyncio.to_thread(
            self._repository.commit_chunk_result,
            binding.lease,
            content=result.model_dump(mode="json"),
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
            "member_evidence": {
                task_id: item.detail.get("evidence", {"asr": None, "ocr": None, "availability": "not_retained"})
                for task_id, item in by_id.items()
            },
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
        tools = self._canvas_tools(canvas)
        encoder: SpeakerEmbeddingModel | None = None
        encoder_binding: SpeakerEmbeddingModelConfig | None = None
        if snapshot["inputs"].parse_config.voiceprint is not None:
            lease = self._require_interval_token(
                snapshot["interval_parse_task_id"], snapshot["lease"], phase="REFINING",
            )
            source = await asyncio.to_thread(self._repository.load_task_input, snapshot["video_parse_task_id"])
            references = await asyncio.to_thread(self._repository.load_voiceprint_references, lease)
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
                    return self._compare_speaker(
                        encoder, source, lease, references,
                        snapshot.setdefault("voiceprint_references", []),
                        start_ms=start_ms, end_ms=end_ms, check_active=check_active,
                    )

            tools.extend(as_langchain_tools([build_compare_speaker_tool(
                compare_speaker=compare_speaker, check_control=check_active,
            )]))
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
            tools.extend(as_langchain_tools([build_inspect_video_frames_tool(
                source_path=self._artifact_store.resolve(source.original_source_uri),
                read_bounds=bounds, max_height=snapshot["inputs"].parse_config.visual_input.max_height,
                timeout_seconds=binding.request_timeout_seconds,
                check_control=manager.check_active if manager is not None else lambda: self._require_interval_active(lease),
                trace_context={"video_parse_task_id": snapshot["video_parse_task_id"],
                               "interval_parse_task_id": snapshot["interval_parse_task_id"],
                               "video_id": snapshot["inputs"].video_id, "content_hash": source.content_hash},
            )]))
            fallback = binding.fallback
            if fallback is not None:
                fallback = fallback.model_copy(update={"supports_vision_input": True})
            binding = binding.model_copy(update={"supports_vision_input": True, "fallback": fallback})
        try:
            if encoder_binding is not None:
                encoder = self._speaker_encoder(encoder_binding)
            return await self._generate_text_structured(
                binding,
                prompt,
                tools=tools, canvas=canvas,
                result_type=CanvasCompletion,
                max_model_calls=snapshot["inputs"].parse_config.execution.max_model_calls,
                tool_harness="deepagents",
            )
        finally:
            if encoder is not None:
                await cleanup_execution(encoder.close)

    async def commit_interval_refinement(
        self,
        snapshot: RefineInput,
        result: ValidatedIntervalRefinement,
    ) -> bool:
        lease = self._require_interval_token(
            snapshot["interval_parse_task_id"], snapshot["lease"], phase="REFINING"
        )
        task_ids = {
            sequence: task_id
            for task_id, sequence in snapshot["member_sequence_nos"].items()
        }
        member_updates: dict[str, dict[str, object]] = {}
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
                detail={"input_versions": snapshot["member_versions"], "input_scopes": snapshot["member_scopes"]},
                voiceprint_references=snapshot.get("voiceprint_references", []),
            )
        except VideoParseSnapshotConflictError:
            _logger.info("Refine snapshot changed; refreshing interval %s", snapshot["interval_parse_task_id"])
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
            tools=self._canvas_tools(canvas, on_call=count_tool), canvas=canvas,
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
            context = self._media_service.prepare_chunk(
                identity,
                source_video=self._artifact_store.resolve(
                    binding.state.source_media_ref
                ),
                start_seconds=execution.allowed_start_seconds,
                end_seconds=execution.allowed_end_seconds,
                visual_mode=config.visual_input.mode,
                frame_fps=config.visual_input.fps,
                max_frames=config.visual_input.max_frames_per_chunk,
                max_height=config.visual_input.max_height,
                # A visual fallback consumes ordered frames even when the
                # primary stage sends the prepared video directly.
                ocr_enabled=(
                    config.ocr_enabled
                    or config.models.understanding.fallback is not None
                ),
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

    def _canvas_tools(self, canvas: ParseContextCanvas, *, on_call=None) -> list[BaseTool]:
        manager = self._model_manager.get()
        return as_langchain_tools(build_context_canvas_tools(
            canvas, role=canvas.stage,
            read_collections=("members", "events", "evidence", "background", "compact"),
            write_args_schema=ParseCanvasWrite, maximum_result_bytes=32_000,
            maximum_read_chars=12_000, maximum_catalog_items=100,
            check_control=manager.check_active if manager is not None else None,
            on_call=on_call,
        ))

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
            )
        return await self._run_blocking(
            self._invoke_multimodal, binding, inputs,
            schema_name=schema_name, result_type=result_type,
            fallback_inputs=fallback_inputs,
            check_control=manager.check_active if manager is not None else None,
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
    ) -> _Result:
        return await self._generate_multimodal(
            binding, [TextInput(prompt)], schema_name=result_type.__name__,
            result_type=result_type, tools=tools, canvas=canvas, max_model_calls=max_model_calls,
            tool_harness=tool_harness,
            enforce_model_call_limit=enforce_model_call_limit,
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
        model = self._gateway.create_generative_model(definition)
        try:
            return model.generate(
                inputs, schema_name=schema_name, result_type=result_type,
                tools=tools,
                # Existing task request budgets are enforced by the call manager.
                max_model_calls=max_model_calls if enforce_model_call_limit else None,
                check_control=check_control, context_checkpoint=context_checkpoint,
                fallback_inputs=fallback_inputs, tool_harness=tool_harness,
            )
        finally:
            model.close()

    def _build_index_coordinator(
        self,
        inputs: ParseInput,
        *,
        check_active: Callable[[], None],
        index_generation: int,
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
            coordinator = VideoParseIndexCoordinator(
                self._repository,
                self._elasticsearch_store,
                embedder,
                media_resolver=self._artifact_store.resolve,
                media_service=self._media_service,
                check_active=check_active,
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
