"""Production host adapter for the Video Parse V2 Agent harness."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Literal, TypeVar

from arkruntime import Ark
from langchain_core.tools import BaseTool
from pydantic import BaseModel

from agent.video_parse_v2 import ParseAgent
from agent.video_parse_v2.input import (
    ASRModelConfig,
    GenerativeModelConfig,
    ParseInput,
)
from agent.video_parse_v2.prefix.context import PrefixContext
from agent.video_parse_v2.refine.input import RefineInput
from agent.video_parse_v2.prompts import build_ocr_prompt
from agent.video_parse_v2.host import (
    ChunkExecution,
    ChunkScheduleEntry,
    ChunkScheduleSnapshot,
    ChunkTaskStatus,
    LeaseToken,
    VideoChunk,
    VideoRunSnapshot,
    VideoTaskStatus,
)
from engine.common.model_factory import build_asr_agent, build_openai_vision
from engine.common.transcription import TranscriptAnalysis, normalize_transcription
from engine.parsev2.observations import ASRResult, OCRResult
from engine.parsev2.planning import ChunkPlan, IntervalPlan
from engine.parsev2.schemas import VideoParseResult
from engine.parsev2.validation import (
    IntervalRefinementProposal,
    PrefixRefinementProposal,
    ValidatedIntervalRefinement,
    ValidatedPrefixRefinement,
)
from engine.rag import (
    ChunkIndexingContract,
    EncoderContract,
    SparseEncoderContract,
    VideoClipInputContract,
    VIDEO_CLIP_PREPROCESS_REVISION,
)
from gateway.agent_model.context import ContextWindowPolicy
from gateway.agent_model.factory import AgentModelFactory
from gateway.ark.embedding import (
    DOUBAO_MULTIMODAL_EMBEDDING_REVISION,
    DoubaoMultimodalEmbedder,
)
from gateway.ark.multimodal import ArkMultimodalModel
from gateway.config import (
    AgentFallbackConfig,
    AgentModelConfig,
    ArkModelSourceConfig,
    GatewayConfig,
    OpenAIAudioSourceConfig,
    OpenAIModelSourceConfig,
    OpenAIVisionSourceConfig,
)
from gateway.errors import ContentFilterError
from gateway.model_sources import ModelReference, ark_options, source_key
from gateway.multimodal import ImageInput, TextInput, VideoInput
from gateway.openai.audio import transcribe_audio
from gateway.openai.multimodal import DeepSeekMultimodalModel
from gateway.tracing import LangfuseTracing
from gateway.usage import model_call_context

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
    VideoParseLeaseLostError,
    VideoParseRepository,
)
from ..storage.artifact import ArtifactStore
from ..storage.elasticsearch import ElasticsearchVideoChunkStore
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
_RETRIEVAL_CONTRACT = "parse-v2-retrieval-v1"
_logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _ChunkBinding:
    lease: ChunkParseLease
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    state: ChunkExecutionState | None = None
    media_context: object | None = None
    media: PreparedVideoParseMedia | None = None


class VideoParseRuntimeService:
    """Connect the Agent protocol to durable Server and Gateway capabilities."""

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
        index_generation: int = 1,
        sparse_enabled: bool = False,
        parse_usage: ParseUsageService | None = None,
        tracing: LangfuseTracing | None = None,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id must not be blank")
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        if not timedelta(0) < heartbeat_interval < lease_duration:
            raise ValueError("heartbeat_interval must be below the lease duration")
        if (
            isinstance(index_generation, bool)
            or not isinstance(index_generation, int)
            or not 1 <= index_generation <= (1 << 31) - 1
        ):
            raise ValueError(
                "index_generation must fit the Elasticsearch version contract"
            )
        if not isinstance(sparse_enabled, bool):
            raise ValueError("sparse_enabled must be a boolean")
        self._repository = repository
        self._queue = queue
        self._artifact_store = artifact_store
        self._media_service = media_service
        self._gateway_config = gateway_config
        self._elasticsearch_store = elasticsearch_store
        self._worker_id = worker_id.strip()
        self._lease_duration = lease_duration
        self._heartbeat_interval = heartbeat_interval
        self._index_generation = index_generation
        self._sparse_enabled = sparse_enabled
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
        self._change_events: dict[str, asyncio.Event] = {}
        self.agent = ParseAgent(runtime=self)

    # Consumer entry points -------------------------------------------------

    async def prepare(self, lease: VideoParseLease) -> None:
        token = self._video_lease.set(lease)
        try:
            await self.agent.run(lease.video_parse_task_id)
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
                await self.agent.chunk_workflow.run(lease.chunk_parse_task_id)
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
                await self.agent.interval_refine.run(lease.interval_parse_task_id)
        finally:
            self._interval_lease.reset(token)

    async def index(self, lease: IntervalParseLease) -> None:
        token = self._interval_lease.set(lease)
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
                check_active=lambda: self._require_interval_active(lease),
            ) as manager:
                coordinator, client = self._build_index_coordinator(
                    inputs,
                    check_active=manager.check_active,
                )
                try:
                    await self._run_blocking(coordinator.index_interval, lease)
                finally:
                    await asyncio.to_thread(client.close)
            self._notify_change(lease.video_parse_task_id)
        finally:
            self._interval_lease.reset(token)

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

        reaped = await asyncio.to_thread(
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
                    dispatchable=item.dispatchable,
                )
                for item in state.tasks
            ),
            wait_timeout_seconds=state.wait_timeout_seconds,
            terminal_error=state.terminal_error,
        )

    async def dispatch_chunk(
        self, chunk_parse_task_id: str, lease: LeaseToken
    ) -> bool:
        current = self._require_video_token(None, lease)
        return await asyncio.to_thread(
            self._queue.dispatch_chunk, chunk_parse_task_id, current
        )

    async def wait_for_video_change(
        self, video_parse_task_id: str, *, timeout_seconds: float
    ) -> None:
        event = self._change_events.setdefault(video_parse_task_id, asyncio.Event())
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout_seconds)
        except TimeoutError:
            pass
        finally:
            event.clear()

    async def advance_video_task(
        self, video_parse_task_id: str, lease: LeaseToken
    ) -> VideoTaskStatus:
        current = self._require_video_token(video_parse_task_id, lease)
        state = await asyncio.to_thread(self._repository.aggregate_task, current)
        return VideoTaskStatus(state.status)

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
        )

    async def prepare_chunk_visual(self, execution: ChunkExecution) -> None:
        await self._ensure_chunk_media(execution)

    async def transcribe_chunk(self, execution: ChunkExecution) -> ASRResult:
        media = await self._ensure_chunk_media(execution)
        model = execution.inputs.parse_config.models.asr
        if model.mode == "none":
            return {"transcript": []}
        duration = execution.allowed_end_seconds - execution.allowed_start_seconds
        source = self._source(model.source)
        if isinstance(source, OpenAIAudioSourceConfig):
            payload = await self._run_blocking(
                transcribe_audio,
                media.audio_path,
                model=model.model,
                provider=source.provider,
                base_url=source.base_url,
                api_key=source_key(source),
                timeout=model.request_timeout_seconds,
                response_format=source.response_format,
            )
            analysis = normalize_transcription(payload, duration_seconds=duration)
        else:
            agent = self._build_asr(model)
            analysis = await self._run_blocking(
                agent.transcribe,
                media.video_path,
                duration_seconds=duration,
            )
        if not isinstance(analysis, TranscriptAnalysis):
            analysis = TranscriptAnalysis.model_validate(analysis)
        return {
            "transcript": [
                {
                    "start_seconds": cue.start_seconds + execution.allowed_start_seconds,
                    "end_seconds": cue.end_seconds + execution.allowed_start_seconds,
                    "text": cue.text,
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
        return await self._generate_multimodal(
            execution.inputs.parse_config.models.understanding,
            media_inputs,
            fallback_inputs=fallback_inputs,
            schema_name="video_parse_v2_chunk",
            result_type=VideoParseResult,
        )

    async def commit_chunk_result(
        self,
        execution: ChunkExecution,
        result: VideoParseResult,
    ) -> None:
        binding = self._require_chunk_execution(execution)
        await asyncio.to_thread(
            self._repository.commit_chunk_result,
            binding.lease,
            content=result.model_dump(mode="json"),
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
                await asyncio.to_thread(context.__exit__, None, None, None)

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
                    item.sequence_no * chunk_duration,
                    min(duration, (item.sequence_no + 1) * chunk_duration),
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
            "member_scopes": {
                task_id: list(item.detail.get("scope", [item.sequence_no]))
                for task_id, item in by_id.items()
            },
        }

    async def generate_interval_refinement(
        self,
        snapshot: RefineInput,
        *,
        prompt: str,
    ) -> IntervalRefinementProposal:
        binding = (
            snapshot["inputs"].parse_config.models.refine
            or snapshot["inputs"].parse_config.models.understanding
        )
        return await self._generate_text_structured(
            binding,
            prompt,
            tools=(),
            result_type=IntervalRefinementProposal,
            max_model_calls=1,
        )

    async def commit_interval_refinement(
        self,
        snapshot: RefineInput,
        result: ValidatedIntervalRefinement,
    ) -> None:
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
        await asyncio.to_thread(
            self._repository.commit_interval_refinement,
            lease,
            interval_content=result.model_dump(mode="json"),
            member_updates=member_updates,
            detail={
                "input_versions": snapshot["member_versions"],
                "input_scopes": snapshot["member_scopes"],
            },
        )

    async def notify_interval_committed(self, snapshot: RefineInput) -> None:
        self._notify_change(snapshot["video_parse_task_id"])

    # Agent runtime: Prefix -------------------------------------------------

    async def load_prefix_context(
        self, chunk_parse_task_id: str
    ) -> PrefixContext:
        lease = self._prefix_lease.get()
        if lease is None or lease.chunk_parse_task_id != chunk_parse_task_id:
            raise VideoParseLeaseLostError("Prefix lease is not bound to this task")
        task_input = await asyncio.to_thread(
            self._repository.load_task_input, lease.video_parse_task_id
        )
        inputs = self._parse_input(task_input)
        start = lease.sequence_no * inputs.parse_config.chunk_duration * 1000
        end = min(
            task_input.duration_seconds,
            (lease.sequence_no + 1) * inputs.parse_config.chunk_duration * 1000,
        )
        return {
            "chunk_parse_task_id": lease.chunk_parse_task_id,
            "video_parse_task_id": lease.video_parse_task_id,
            "inputs": inputs,
            "lease": self._lease_token(lease),
            "sequence_no": lease.sequence_no,
            "allowed_start_seconds": start,
            "allowed_end_seconds": end,
            "scope": list(lease.content_detail.get("scope", [lease.sequence_no])),
            "content": VideoParseResult.model_validate(lease.content),
            "content_version": lease.content_version,
            "series_prefix_compact": lease.series_prefix_compact,
            "video_prefix_compact": lease.video_prefix_compact,
        }

    async def generate_prefix_refinement(
        self,
        context: PrefixContext,
        *,
        prompt: str,
        tools: Sequence[BaseTool],
    ) -> PrefixRefinementProposal:
        binding = context["inputs"].parse_config.models.prefix_context
        if binding is None:
            raise ValueError("Prefix execution has no fixed model binding")
        prefix = context["inputs"].parse_config.prefix_context
        max_calls = 1 + (prefix.max_tool_calls if prefix is not None else 0)
        return await self._generate_text_structured(
            binding,
            prompt,
            tools=tools,
            result_type=PrefixRefinementProposal,
            max_model_calls=max_calls,
        )

    async def commit_prefix_refinement(
        self,
        context: PrefixContext,
        result: ValidatedPrefixRefinement,
    ) -> None:
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
        await asyncio.to_thread(
            self._repository.commit_prefix,
            lease,
            content_updates=updates,
            video_prefix_compact=result.video_compact,
            series_prefix_compact=result.series_compact,
        )

    async def notify_prefix_committed(self, context: PrefixContext) -> None:
        self._notify_change(context["video_parse_task_id"])

    # Internal execution contexts ------------------------------------------

    async def _execute_prefix(self, lease: PrefixParseLease) -> None:
        token = self._prefix_lease.set(lease)
        task_input: VideoParseTaskInputState | None = None
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
                await self.agent.prefix_context_agent.run(
                    lease.chunk_parse_task_id
                )
        except asyncio.CancelledError:
            await asyncio.to_thread(self._repository.release_prefix, lease)
            raise
        except Exception as error:
            if task_input is None:
                retry_seconds = 1.0
            else:
                execution = self._parse_input(task_input).parse_config.execution
                retry_seconds = min(
                    execution.retry_max_delay_seconds,
                    execution.retry_initial_delay_seconds
                    * (2 ** min(lease.retry_count, 30)),
                )
            from ..utils.time import now_utc

            await asyncio.to_thread(
                self._repository.retry_prefix,
                lease,
                error=str(error),
                retry_at=now_utc() + timedelta(seconds=retry_seconds),
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
            "stage": (
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
                workspace_id=task_input.workspace_id,
                series_id=task_input.series_id,
                video_id=task_input.task.video_id,
                session_id=task_input.task.video_id,
            )
        tracing = (
            self._tracing.scope(context)
            if self._tracing is not None else nullcontext()
        )
        manager_token = self._model_manager.set(manager)
        stopped = asyncio.Event()
        heartbeat_errors: list[BaseException] = []

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
                    raise RuntimeError(
                        "Video Parse V2 model-call heartbeat failed"
                    ) from heartbeat_errors[0]
        finally:
            self._model_manager.reset(manager_token)

    async def _run_blocking(
        self,
        operation: Callable[..., _T],
        /,
        *args: object,
        **kwargs: object,
    ) -> _T:
        """Wait for an executor call to stop before its media/client is released."""

        task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            manager = self._model_manager.get()
            if manager is not None:
                manager.cancel()
            await asyncio.gather(task, return_exceptions=True)
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
            enter = asyncio.create_task(asyncio.to_thread(context.__enter__))
            try:
                media = await asyncio.shield(enter)
            except BaseException:
                await asyncio.gather(enter, return_exceptions=True)
                await asyncio.to_thread(context.__exit__, None, None, None)
                raise
            binding.media_context = context
            binding.media = media
            return media

    # Model and indexing assembly ------------------------------------------

    def _build_asr(self, binding: ASRModelConfig):
        parser = self._gateway_config.video_parser.model_copy(update={
            "transcription_source": binding.source,
        })
        config = self._gateway_config.model_copy(update={"video_parser": parser})
        return build_asr_agent(
            config,
            model=binding.model,
            fps=binding.fps,
            timeout=binding.request_timeout_seconds,
            max_output_tokens=binding.max_output_tokens,
        )

    async def _generate_multimodal(
        self,
        binding: GenerativeModelConfig,
        inputs: Sequence[ImageInput | VideoInput | TextInput],
        *,
        fallback_inputs: Sequence[ImageInput | VideoInput | TextInput] | None = None,
        schema_name: str,
        result_type: type[_Result],
    ) -> _Result:
        try:
            return await self._run_blocking(
                self._invoke_multimodal,
                binding,
                inputs,
                schema_name=schema_name,
                result_type=result_type,
            )
        except ContentFilterError:
            fallback = binding.fallback
            if fallback is None:
                raise
            return await self._run_blocking(
                self._invoke_multimodal,
                fallback,
                fallback_inputs or inputs,
                schema_name=schema_name,
                result_type=result_type,
            )

    async def _generate_text_structured(
        self,
        binding: GenerativeModelConfig,
        prompt: str,
        *,
        tools: Sequence[BaseTool],
        result_type: type[_Result],
        max_model_calls: int,
    ) -> _Result:
        source = self._source(binding.source)
        if not tools:
            return await self._generate_multimodal(
                binding,
                [TextInput(prompt)],
                schema_name=result_type.__name__,
                result_type=result_type,
            )
        if isinstance(source, OpenAIVisionSourceConfig):
            raise TypeError("openai_vision does not support Prefix tools")
        max_output_tokens = binding.max_output_tokens or 8192
        model_config = AgentModelConfig(
            source=binding.source,
            model=binding.model,
            request_timeout_seconds=binding.request_timeout_seconds,
            max_output_tokens=max_output_tokens,
            fallback=(
                AgentFallbackConfig(
                    source=binding.fallback.source,
                    model=binding.fallback.model,
                    request_timeout_seconds=(
                        binding.fallback.request_timeout_seconds
                    ),
                    max_output_tokens=binding.fallback.max_output_tokens,
                    reasoning_effort=binding.fallback.reasoning_effort,
                )
                if binding.fallback is not None
                else None
            ),
        )
        factory = AgentModelFactory(model_config, self._gateway_config.model_sources)
        try:
            model = factory.build_structured(
                model=binding.model,
                reasoning_effort=binding.reasoning_effort or "none",
                request_timeout_seconds=binding.request_timeout_seconds,
                max_output_tokens=max_output_tokens,
                max_input_chars=400_000,
                context_window_policy=ContextWindowPolicy(
                    context_window_tokens=128_000,
                    output_reserve_tokens=max_output_tokens,
                    safety_margin_tokens=2_048,
                    auto_compact_tokens=100_000,
                    compact_target_tokens=80_000,
                ),
            )
            return await self._run_blocking(
                model.run_structured,
                prompt,
                tools=tools,
                result_type=result_type,
                max_model_calls=max_model_calls,
                request_timeout_seconds=binding.request_timeout_seconds,
            )
        finally:
            factory.close()

    def _invoke_multimodal(
        self,
        binding,
        inputs: Sequence[ImageInput | VideoInput | TextInput],
        *,
        schema_name: str,
        result_type: type[_Result],
    ) -> _Result:
        source = self._source(binding.source)
        model: object
        close: Callable[[], None] | None = None
        if isinstance(source, ArkModelSourceConfig):
            model = ArkMultimodalModel(
                binding.model,
                request_timeout_seconds=binding.request_timeout_seconds,
                max_output_tokens=binding.max_output_tokens,
                **ark_options(source),
            )
        elif isinstance(source, OpenAIVisionSourceConfig):
            model = build_openai_vision(
                source,
                model=binding.model,
                timeout=binding.request_timeout_seconds,
                max_output_tokens=binding.max_output_tokens,
            )
        elif isinstance(source, OpenAIModelSourceConfig):
            model = DeepSeekMultimodalModel(
                binding.model,
                base_url=source.base_url,
                api_key=source_key(source),
                max_output_tokens=binding.max_output_tokens or 8192,
                request_timeout_seconds=binding.request_timeout_seconds,
                reasoning_effort=binding.reasoning_effort or "none",
            )
            close = model.close
        else:
            raise TypeError(
                f"model source {binding.source!r} cannot produce structured media output"
            )
        try:
            return model.generate(
                inputs,
                schema_name=schema_name,
                result_type=result_type,
            )
        finally:
            if close is not None:
                close()

    def _build_index_coordinator(
        self,
        inputs: ParseInput,
        *,
        check_active: Callable[[], None],
    ) -> tuple[VideoParseIndexCoordinator, Ark]:
        embedding = inputs.parse_config.models.embedding
        reference = ModelReference.resolve(
            embedding.model,
            encoder_model=embedding.encoder_model,
            default_encoder_model=self._gateway_config.multimodal_embedding.model,
        )
        source = self._source(embedding.source)
        if not isinstance(source, ArkModelSourceConfig):
            raise TypeError("Parse V2 multimodal embedding requires an Ark source")
        client = Ark.volc(
            timeout=embedding.request_timeout_seconds,
            max_retries=0,
            **ark_options(source),
        )
        embedder = DoubaoMultimodalEmbedder(
            reference.request_model,
            output_dimensionality=embedding.dimensions,
            fps=embedding.video_fps,
            request_timeout_seconds=embedding.request_timeout_seconds,
            client=client,
        )
        provider = source.provider
        dimensions = embedding.dimensions
        coordinator = VideoParseIndexCoordinator(
            self._repository,
            self._elasticsearch_store,
            embedder,
            media_resolver=self._artifact_store.resolve,
            media_service=self._media_service,
            check_active=check_active,
            contract=ChunkIndexingContract(
                retrieval_contract_id=_RETRIEVAL_CONTRACT,
                semantic_encoder=EncoderContract(
                    provider=provider,
                    model=reference.model,
                    encoder_revision=DOUBAO_MULTIMODAL_EMBEDDING_REVISION,
                    dimensions=dimensions,
                ),
                vision_encoder=EncoderContract(
                    provider=provider,
                    model=reference.model,
                    encoder_revision=DOUBAO_MULTIMODAL_EMBEDDING_REVISION,
                    dimensions=dimensions,
                ),
                visual_input_contract=VideoClipInputContract(
                    fps=embedding.video_fps,
                    max_height=inputs.parse_config.visual_input.max_height,
                    mime_type="video/mp4",
                    preprocess_revision=VIDEO_CLIP_PREPROCESS_REVISION,
                ),
                index_generation=self._index_generation,
                sparse_encoder=(
                    SparseEncoderContract(
                        provider=provider,
                        model=reference.model,
                        encoder_revision=DOUBAO_MULTIMODAL_EMBEDDING_REVISION,
                    )
                    if self._sparse_enabled else None
                ),
            ),
        )
        return coordinator, client

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

    def _source(self, name: str):
        try:
            return self._gateway_config.model_sources[name]
        except KeyError as error:
            raise ValueError(
                f"fixed model source {name!r} is absent from the active Gateway config"
            ) from error

    def _notify_change(self, video_parse_task_id: str) -> None:
        event = self._change_events.get(video_parse_task_id)
        if event is not None:
            event.set()


__all__ = ["VideoParseRuntimeService"]
