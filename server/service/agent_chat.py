from __future__ import annotations

import logging
from copy import deepcopy
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import asdict, replace
from queue import Empty, Full, Queue
from threading import Event, Lock
from time import monotonic_ns
from uuid import uuid4

from agent.chat.common.errors import (
    AgentContextBudgetError, AgentExecutionTimeoutError,
    AgentModelInvocationError, AgentToolInvocationError,
)
from agent.chat.common.state import AnswerPhase, RunInputs
from agent.chat.supervisor import SupervisorRuntime
from agent.chat.video_search.output import video_range_sources
from agent.chat.video_search.runtime import AgentRuntime
from engine.rag.contracts import VideoChunkEvidence
from gateway.agent_model.contracts import SupervisorSessionContext
from gateway.tracing import LangfuseTracing
from gateway.usage import model_call_context

from ..config.agent import AgentServiceConfig
from ..constants.identity import LOCAL_USER_ID
from ..model.dto.agentDTO import (
    AgentAttachmentReference,
    AgentConversationState,
    AgentIntegerOptionState,
    AgentMessageOptionsState,
    AgentMessageSendState,
    AgentMessageState,
    AgentMessageTimingState,
    AgentPageState,
    AgentRunOptionsState,
    AgentStageTimingState,
)
from ..repository.agent import (
    AgentConflictError, AgentConversationNotFoundError, AgentMessageInProgressError,
    AgentRepository, AgentReferenceInvalidError, AgentMessageNotFoundError,
)
from ..repository.video import SeriesNotReadyError
from ..utils.time import now_unix_time_ms
from ..utils.media import image_uri_to_data_url
from ..utils.validation import canonical_uuid, require_nonempty_text
from .library import LibraryService
from .agent_attachments import AgentAttachmentService
from .pagination import decode_cursor, encode_cursor
from .parse_usage import ParseUsageService

_logger = logging.getLogger(__name__)


class AgentAttachmentInputUnavailableError(RuntimeError):
    """The selected runtime cannot consume persisted image attachments."""


class _AgentExecutionTimer:
    def __init__(
        self,
        *,
        started_at: int,
        started_ns: int,
        monotonic_clock: Callable[[], int],
    ) -> None:
        self.started_at = started_at
        self._monotonic_clock = monotonic_clock
        self._phase = AnswerPhase.QUEUED
        self._phase_started_ns = started_ns
        self._stages_ns: list[tuple[AnswerPhase, int]] = []
        self._finished: AgentMessageTimingState | None = None

    def transition(self, phase: AnswerPhase) -> AgentMessageTimingState:
        if self._finished is not None:
            raise RuntimeError("Agent execution timing is already complete")
        if phase != self._phase:
            self._close_current(self._monotonic_clock())
            self._phase = phase
        return self.snapshot()

    def finish(self) -> AgentMessageTimingState:
        if self._finished is None:
            self._close_current(self._monotonic_clock())
            stages = self._stage_states()
            self._finished = AgentMessageTimingState(
                started_at=self.started_at,
                total_duration_ms=sum(stage.duration_ms for stage in stages),
                stages=stages,
            )
        return self._finished

    def snapshot(self) -> AgentMessageTimingState:
        if self._finished is not None:
            return self._finished
        return AgentMessageTimingState(
            started_at=self.started_at,
            total_duration_ms=None,
            stages=self._stage_states(),
        )

    def _close_current(self, ended_ns: int) -> None:
        duration_ns = max(0, ended_ns - self._phase_started_ns)
        self._stages_ns.append((self._phase, duration_ns))
        self._phase_started_ns = ended_ns

    def _stage_states(self) -> tuple[AgentStageTimingState, ...]:
        if not self._stages_ns:
            return ()
        total_ms = sum(duration for _, duration in self._stages_ns) // 1_000_000
        durations_ms = [duration // 1_000_000 for _, duration in self._stages_ns]
        durations_ms[-1] += total_ms - sum(durations_ms)
        return tuple(
            AgentStageTimingState(phase=phase, duration_ms=duration_ms)
            for (phase, _), duration_ms in zip(
                self._stages_ns,
                durations_ms,
                strict=True,
            )
        )


class AgentMessageExecution:
    """A Service-owned task with an optional, detachable HTTP event consumer."""

    def __init__(
        self,
        initial: AgentMessageSendState,
        *,
        stream: bool,
        timing: _AgentExecutionTimer | None = None,
    ) -> None:
        self.initial = initial
        self.cancellation = Event()
        self.future: Future[AgentMessageSendState] | None = None
        self._events: Queue[dict | None] = Queue(maxsize=256)
        self._lock = Lock()
        self._attached = stream
        self._sequence = 0
        self.reference_context: dict | None = None
        self.timing = timing

    def publish(self, event: dict) -> None:
        with self._lock:
            if not self._attached:
                return
            self._sequence += 1
            payload = {
                **event,
                "conversationId": self.initial.assistant_message.conversation_id,
                "messageId": self.initial.assistant_message.message_id,
                "sequence": self._sequence,
                "occurredAt": now_unix_time_ms(),
            }
            try:
                self._events.put_nowait(payload)
            except Full:
                # Stop buffering for a slow consumer; the execution still commits.
                self._attached = False
                self._drain()
                self._events.put_nowait({
                    "type": "answer.interrupted",
                    "conversationId": payload["conversationId"],
                    "messageId": payload["messageId"],
                    "sequence": self._sequence,
                    "occurredAt": payload["occurredAt"],
                    "error": {"code": "STREAM_INTERRUPTED", "message": "连接未能及时接收回答，请通过会话快照恢复。"},
                })
                self._events.put_nowait(None)

    def finish(self) -> None:
        with self._lock:
            if self._attached:
                try:
                    self._events.put_nowait(None)
                except Full:
                    # The final event is already buffered. A consumer can also
                    # detect normal task completion once its queue is drained.
                    pass

    def next_event(self, timeout: float = 1) -> dict | None:
        try:
            return self._events.get(timeout=timeout)
        except Empty:
            if self.future is not None and self.future.done():
                return None
            raise

    def detach(self) -> None:
        with self._lock:
            self._attached = False
            self._drain()

    def _drain(self) -> None:
        while True:
            try:
                self._events.get_nowait()
            except Empty:
                return

    def result(self) -> AgentMessageSendState:
        assert self.future is not None
        return self.future.result()


class AgentChatService:
    """Own conversations, message transactions, and in-process execution tasks."""

    def __init__(
        self,
        repository: AgentRepository,
        library: LibraryService,
        runtime: AgentRuntime | SupervisorRuntime,
        *,
        supervisor_runtime: SupervisorRuntime | None = None,
        config: AgentServiceConfig | None = None,
        config_loader: Callable[[], tuple[AgentServiceConfig, str]] | None = None,
        runtime_factory: Callable[[AgentServiceConfig], AgentRuntime | SupervisorRuntime] | None = None,
        supervisor_factory: Callable[[AgentRuntime, AgentServiceConfig], SupervisorRuntime] | None = None,
        config_revision: str | None = None,
        tracing: LangfuseTracing | None = None,
        parse_usage: ParseUsageService | None = None,
        attachments: AgentAttachmentService | None = None,
        clock: Callable[[], int] = now_unix_time_ms,
        monotonic_clock: Callable[[], int] = monotonic_ns,
    ) -> None:
        self._repository = repository
        self._library = library
        self._runtime = runtime
        self._supervisor_runtime = supervisor_runtime
        self._tracing = tracing
        self._parse_usage = parse_usage
        self._attachments = attachments
        self._config = config or AgentServiceConfig()
        self._config_loader = config_loader
        self._runtime_factory = runtime_factory
        self._supervisor_factory = supervisor_factory
        self._config_revision = config_revision
        self._runtimes = [supervisor_runtime or runtime]
        self._active_maximum_concurrent_kicks = self._config.maximum_concurrent_kicks
        self._clock = clock
        self._monotonic_clock = monotonic_clock
        self._executor = ThreadPoolExecutor(
            max_workers=self._config.maximum_concurrent_kicks,
            thread_name_prefix="anivag-agent",
        )
        self._lock = Lock()
        self._executions: dict[str, AgentMessageExecution] = {}
        self._closed = False

    @property
    def model_name(self) -> str:
        return self._current_config()[0].model_name

    @property
    def maximum_top_k(self) -> int:
        return self._current_config()[1].maximum_top_k

    @property
    def default_top_k(self) -> int:
        return self._current_config()[1].default_top_k

    @property
    def active_maximum_concurrent_kicks(self) -> int:
        return self._active_maximum_concurrent_kicks

    def message_options(self) -> AgentMessageOptionsState:
        _, config, _ = self._current_config()
        return AgentMessageOptionsState(
            top_k=AgentIntegerOptionState(
                default_value=config.default_top_k,
                minimum=1,
                maximum=config.maximum_top_k,
            ),
            max_model_calls_per_turn=AgentIntegerOptionState(
                default_value=config.max_model_calls_per_turn,
                minimum=1,
                maximum=config.maximum_model_calls_per_turn,
            ),
            max_tool_calls_per_turn=AgentIntegerOptionState(
                default_value=config.max_tool_calls_per_turn,
                minimum=1,
                maximum=config.maximum_tool_calls_per_turn,
            ),
            max_loop_turns=AgentIntegerOptionState(
                default_value=100,
                minimum=1,
                maximum=100,
            ),
        )

    def _current_config(self) -> tuple[AgentRuntime | SupervisorRuntime, AgentServiceConfig, ThreadPoolExecutor]:
        with self._lock:
            if self._closed:
                raise AgentMessageInProgressError("the Agent service is shutting down")
            if self._config_loader is not None:
                config, revision = self._config_loader()
                if revision != self._config_revision:
                    if config != self._config:
                        active_config = replace(
                            config,
                            maximum_concurrent_kicks=self._active_maximum_concurrent_kicks,
                        )
                        previous_config = replace(
                            self._config,
                            maximum_concurrent_kicks=self._active_maximum_concurrent_kicks,
                        )
                        if active_config != previous_config:
                            assert self._runtime_factory is not None
                            new_runtime = self._runtime_factory(config)
                            if self._supervisor_factory is not None:
                                assert isinstance(new_runtime, AgentRuntime)
                                try:
                                    new_supervisor = self._supervisor_factory(
                                        new_runtime, config,
                                    )
                                except Exception:
                                    new_runtime.close()
                                    raise
                            else:
                                new_supervisor = None
                            self._runtime = new_runtime
                            self._supervisor_runtime = new_supervisor
                            self._runtimes.append(new_supervisor or new_runtime)
                        self._config = config
                        _logger.info(
                            "agent_global_settings_reloaded revision=%s "
                            "restart_required=%s",
                            revision[:12],
                            config.maximum_concurrent_kicks != self._active_maximum_concurrent_kicks,
                        )
                    self._config_revision = revision
            return self._supervisor_runtime or self._runtime, self._config, self._executor

    def create_conversation(
        self,
        *,
        tenant_id: str,
        title: str | None = None,
        idempotency_key: str,
        owner_user_id: str = LOCAL_USER_ID,
    ) -> AgentConversationState:
        normalized_workspace = require_nonempty_text(
            tenant_id,
            "tenant_id",
            maximum=64,
        )
        return self._repository.create_conversation(
            conversation_id=str(uuid4()),
            tenant_id=normalized_workspace,
            title="新对话" if title is None else require_nonempty_text(title, "title", maximum=255),
            title_is_auto=title is None,
            idempotency_key=require_nonempty_text(
                idempotency_key,
                "idempotency_key",
                maximum=128,
            ),
            owner_user_id=owner_user_id,
        )

    def list_conversations(
        self,
        *,
        tenant_id: str,
        limit: int = 100,
        owner_user_id: str = LOCAL_USER_ID,
    ) -> list[AgentConversationState]:
        return self._repository.list_conversations(
            tenant_id=require_nonempty_text(tenant_id, "tenant_id", maximum=64),
            limit=_positive_limit(limit),
            owner_user_id=owner_user_id,
        )

    def get_conversation(
        self,
        conversation_id: str,
        *,
        tenant_id: str,
        owner_user_id: str = LOCAL_USER_ID,
    ) -> AgentConversationState:
        return self._repository.get_conversation(
            canonical_uuid(conversation_id, "conversation_id"),
            require_nonempty_text(tenant_id, "tenant_id", maximum=64),
            owner_user_id=owner_user_id,
        )

    def delete_conversation(
        self,
        conversation_id: str,
        *,
        tenant_id: str,
        owner_user_id: str = LOCAL_USER_ID,
    ) -> AgentConversationState:
        normalized_conversation_id = canonical_uuid(
            conversation_id,
            "conversation_id",
        )
        normalized_workspace = require_nonempty_text(
            tenant_id,
            "tenant_id",
            maximum=64,
        )
        with self._lock:
            deleted = self._repository.delete_conversation(
                normalized_conversation_id,
                normalized_workspace,
                owner_user_id=owner_user_id,
            )
            if self._attachments is not None:
                try:
                    self._attachments.delete_conversation_artifacts(normalized_conversation_id)
                except Exception:
                    # Access is already revoked by SQL; startup reconciliation retries cleanup.
                    _logger.warning(
                        "Could not delete conversation attachments conversation_id=%s",
                        normalized_conversation_id,
                        exc_info=True,
                    )
            runtime = self._supervisor_runtime or self._runtime
            if isinstance(runtime, SupervisorRuntime):
                try:
                    runtime.delete_thread(normalized_conversation_id)
                except Exception:
                    _logger.warning(
                        "Could not delete Supervisor thread conversation_id=%s",
                        normalized_conversation_id,
                        exc_info=True,
                    )
            return deleted

    def page_conversations(
        self, *, tenant_id: str, cursor: str | None = None, limit: int = 50,
        series_id: str | None = None, query: str | None = None,
        owner_user_id: str = LOCAL_USER_ID,
    ) -> AgentPageState[AgentConversationState]:
        tenant_id = require_nonempty_text(tenant_id, "tenant_id", maximum=64)
        series_id = canonical_uuid(series_id, "series_id") if series_id is not None else None
        query = query.strip() if query is not None else None
        scope = {"tenantId": tenant_id, "ownerUserId": owner_user_id, "seriesId": series_id, "query": query,
                 "resource": "conversations", "sort": "updatedAt,conversationId:desc"}
        offset = decode_cursor(cursor, scope=scope)
        items, total = self._repository.list_conversation_page(
            tenant_id, offset=offset, limit=_positive_limit(limit, maximum=100),
            series_id=series_id, query=query,
            owner_user_id=owner_user_id,
        )
        return _page(items, total, offset, scope)

    def page_messages(
        self, conversation_id: str, *, tenant_id: str, cursor: str | None = None,
        limit: int = 50, after_sequence_no: int = -1,
        owner_user_id: str = LOCAL_USER_ID,
    ) -> AgentPageState[AgentMessageState]:
        conversation_id = canonical_uuid(conversation_id, "conversation_id")
        tenant_id = require_nonempty_text(tenant_id, "tenant_id", maximum=64)
        if isinstance(after_sequence_no, bool) or not isinstance(after_sequence_no, int) or after_sequence_no < -1:
            raise ValueError("after_sequence_no must be an integer at least -1")
        scope = {"tenantId": tenant_id, "ownerUserId": owner_user_id, "conversationId": conversation_id,
                 "afterSequenceNo": after_sequence_no, "resource": "messages", "sort": "sequenceNo:asc"}
        offset = decode_cursor(cursor, scope=scope)
        items, total = self._repository.list_message_page(
            conversation_id, tenant_id, offset=offset,
            limit=_positive_limit(limit, maximum=100), after_sequence_no=after_sequence_no,
            owner_user_id=owner_user_id,
        )
        return _page(items, total, offset, scope)

    def list_messages(
        self,
        conversation_id: str,
        *,
        tenant_id: str,
        after_sequence_no: int = -1,
        limit: int = 200,
        owner_user_id: str = LOCAL_USER_ID,
    ) -> list[AgentMessageState]:
        if (
            isinstance(after_sequence_no, bool)
            or not isinstance(after_sequence_no, int)
            or after_sequence_no < -1
        ):
            raise ValueError("after_sequence_no must be an integer at least -1")
        return self._repository.list_messages(
            canonical_uuid(conversation_id, "conversation_id"),
            require_nonempty_text(tenant_id, "tenant_id", maximum=64),
            after_sequence_no=after_sequence_no,
            limit=_positive_limit(limit, maximum=500),
            owner_user_id=owner_user_id,
        )

    def list_all_messages(
        self,
        conversation_id: str,
        *,
        tenant_id: str,
        owner_user_id: str = LOCAL_USER_ID,
    ) -> list[AgentMessageState]:
        """Read a complete persisted snapshot, including in-place reply updates."""
        return self._repository.list_messages(
            canonical_uuid(conversation_id, "conversation_id"),
            require_nonempty_text(tenant_id, "tenant_id", maximum=64),
            after_sequence_no=-1,
            limit=None,
            owner_user_id=owner_user_id,
        )

    def send_message(
        self, conversation_id: str, *, tenant_id: str, content: str,
        idempotency_key: str, series_ids: list[str] | None = None, top_k: int | None = None,
        max_model_calls_per_turn: int | None = None,
        max_tool_calls_per_turn: int | None = None,
        max_loop_turns: int | None = None,
        reference: dict[str, str] | None = None,
        extra_context: dict | None = None,
        attachments: list[dict[str, str]] | None = None,
        owner_user_id: str = LOCAL_USER_ID,
        source_tenant_id: str | None = None,
    ) -> AgentMessageSendState:
        return self.start_message(
            conversation_id, tenant_id=tenant_id, content=content,
            idempotency_key=idempotency_key, series_ids=series_ids, top_k=top_k, reference=reference,
            extra_context=extra_context,
            attachments=attachments,
            max_model_calls_per_turn=max_model_calls_per_turn,
            max_tool_calls_per_turn=max_tool_calls_per_turn,
            max_loop_turns=max_loop_turns,
            owner_user_id=owner_user_id,
            source_tenant_id=source_tenant_id,
        ).result()

    def start_message(
        self, conversation_id: str, *, tenant_id: str, content: str,
        idempotency_key: str, series_ids: list[str] | None = None,
        top_k: int | None = None, stream: bool = False,
        max_model_calls_per_turn: int | None = None,
        max_tool_calls_per_turn: int | None = None,
        max_loop_turns: int | None = None,
        reference: dict[str, str] | None = None,
        extra_context: dict | None = None,
        attachments: list[dict[str, str]] | None = None,
        owner_user_id: str = LOCAL_USER_ID,
        source_tenant_id: str | None = None,
    ) -> AgentMessageExecution:
        runtime, config, executor = self._current_config()
        extra_context = deepcopy(extra_context)
        attachment_references = [
            AgentAttachmentReference.model_validate(item).model_dump(by_alias=True)
            for item in ([] if attachments is None else attachments)
        ]
        normalized_workspace = require_nonempty_text(tenant_id, "tenant_id", maximum=64)
        owner_user_id = require_nonempty_text(owner_user_id, "owner_user_id", maximum=64)
        normalized_source = require_nonempty_text(
            source_tenant_id if source_tenant_id is not None else normalized_workspace,
            "source_tenant_id", maximum=64,
        )
        normalized_conversation_id = canonical_uuid(conversation_id, "conversation_id")
        normalized_content = require_nonempty_text(content, "content", maximum=20_000)
        normalized_reference = _canonical_reference(reference)
        requested_scope = _canonical_scope([] if series_ids is None else series_ids)
        playback_series_id = None
        playback = extra_context.get("playback") if isinstance(extra_context, dict) else None
        if isinstance(runtime, SupervisorRuntime) and runtime.realtime_enabled and isinstance(playback, dict):
            video = self._library.get_video(playback.get("videoId"), tenant_id=normalized_source)
            position = playback.get("positionSeconds")
            if (
                type(position) is not int or not 0 <= position <= video.duration_seconds
                or playback.get("seriesId") != video.series_id
                or playback.get("episodeOrder") != video.episode_order
            ):
                raise ValueError("playback context does not match the current video")
            playback_series_id = video.series_id
        requested_top_k = config.default_top_k if top_k is None else top_k
        if (
            isinstance(requested_top_k, bool) or not isinstance(requested_top_k, int)
            or not 0 < requested_top_k <= config.maximum_top_k
        ):
            raise ValueError(f"top_k must be between 1 and {config.maximum_top_k}")
        run_options = AgentRunOptionsState(
            max_model_calls_per_turn=_bounded_run_limit(
                "max_model_calls_per_turn",
                max_model_calls_per_turn,
                default=config.max_model_calls_per_turn,
                maximum=config.maximum_model_calls_per_turn,
            ),
            max_tool_calls_per_turn=_bounded_run_limit(
                "max_tool_calls_per_turn",
                max_tool_calls_per_turn,
                default=config.max_tool_calls_per_turn,
                maximum=config.maximum_tool_calls_per_turn,
            ),
        )
        if max_loop_turns is not None and (
            isinstance(max_loop_turns, bool)
            or not isinstance(max_loop_turns, int)
            or not 0 <= max_loop_turns <= 100
        ):
            raise ValueError("max_loop_turns must be between 0 and 100")
        requested_run_options = asdict(run_options)
        if max_loop_turns is not None:
            requested_run_options["max_loop_turns"] = max_loop_turns
        normalized_key = require_nonempty_text(idempotency_key, "idempotency_key", maximum=128)
        timing_started_at: int | None = None
        timing_started_ns: int | None = None
        with self._lock:
            if self._closed:
                raise AgentMessageInProgressError("the Agent service is shutting down")
            started = self._repository.find_idempotent_message(
                conversation_id=normalized_conversation_id,
                tenant_id=normalized_workspace, content=normalized_content,
                requested_top_k=requested_top_k, idempotency_key=normalized_key,
                requested_series_ids=requested_scope,
                requested_run_options=requested_run_options,
                reference=normalized_reference,
                extra_context=extra_context,
                attachments=attachment_references,
                owner_user_id=owner_user_id,
                source_tenant_id=normalized_source if normalized_source != normalized_workspace else None,
            )
            if started is None:
                if attachment_references:
                    if self._attachments is None:
                        raise AgentAttachmentInputUnavailableError("附件存储服务尚未配置。")
                    self._attachments.canonicalize_references(
                        normalized_conversation_id, attachment_references,
                        tenant_id=normalized_workspace, owner_user_id=owner_user_id,
                    )
                has_images = bool(attachment_references) or self._repository.has_message_attachments(
                    normalized_conversation_id, normalized_workspace,
                    owner_user_id=owner_user_id,
                )
                if has_images and self._attachments is None:
                    raise AgentAttachmentInputUnavailableError(
                        "附件存储服务尚未配置。"
                    )
                scope = self._resolve_scope(
                    requested_scope, normalized_source,
                    allow_empty=isinstance(runtime, SupervisorRuntime),
                    playback_series_id=playback_series_id,
                )
                reference_context = self._load_reference(
                    normalized_reference, normalized_conversation_id, normalized_workspace, scope,
                    owner_user_id=owner_user_id,
                    source_tenant_id=normalized_source,
                )
                timing_started_at = self._clock()
                timing_started_ns = self._monotonic_clock()
                started = self._repository.start_message(
                    conversation_id=normalized_conversation_id,
                    tenant_id=normalized_workspace, content=normalized_content,
                    model_name=require_nonempty_text(runtime.model_name, "chat_model.model_name", maximum=255),
                    requested_top_k=requested_top_k, idempotency_key=normalized_key,
                    series_ids=scope, requested_series_ids=requested_scope,
                    requested_run_options=requested_run_options,
                    user_message_id=str(uuid4()), assistant_message_id=str(uuid4()),
                    now=timing_started_at,
                    reference=normalized_reference,
                    extra_context=extra_context,
                    attachments=attachment_references,
                    owner_user_id=owner_user_id,
                    source_tenant_id=normalized_source if normalized_source != normalized_workspace else None,
                )
            if started.reused and started.assistant_message.status == "PROCESSING":
                raise AgentMessageInProgressError("the idempotent message is still being processed")
            initial = AgentMessageSendState(started.user_message, started.assistant_message)
            timer = None
            if not started.reused:
                assert timing_started_at is not None and timing_started_ns is not None
                timer = _AgentExecutionTimer(
                    started_at=timing_started_at,
                    started_ns=timing_started_ns,
                    monotonic_clock=self._monotonic_clock,
                )
            execution = AgentMessageExecution(initial, stream=stream, timing=timer)
            if not started.reused:
                execution.reference_context = reference_context
            execution.publish({"type": "answer.started", **_message_pair(initial)})
            if started.reused:
                execution.future = Future()
                execution.future.set_result(initial)
                execution.publish(_terminal_event(initial))
                execution.finish()
                return execution
            self._executions[started.assistant_message.message_id] = execution
            try:
                execution.future = executor.submit(
                    self._execute, execution, normalized_source, requested_top_k,
                    run_options, max_loop_turns, runtime, config,
                    owner_user_id=owner_user_id, conversation_tenant_id=normalized_workspace,
                    playback_series_id=playback_series_id,
                )
            except Exception:
                self._executions.pop(started.assistant_message.message_id, None)
                assert execution.timing is not None
                self._repository.fail_assistant_message(
                    started.assistant_message.message_id, error="Agent task could not start",
                    now=self._clock(), timing=execution.timing.finish(),
                )
                raise
            return execution

    def get_message(
        self, conversation_id: str, *, tenant_id: str,
        message_id: str | None = None, idempotency_key: str | None = None,
        owner_user_id: str = LOCAL_USER_ID,
    ) -> AgentMessageSendState:
        if (message_id is None) == (idempotency_key is None):
            raise ValueError("provide exactly one of messageId or idempotencyKey")
        pair = self._repository.get_message_pair(
            canonical_uuid(conversation_id, "conversation_id"),
            require_nonempty_text(tenant_id, "tenant_id", maximum=64),
            message_id=canonical_uuid(message_id, "message_id") if message_id is not None else None,
            idempotency_key=require_nonempty_text(idempotency_key, "idempotency_key", maximum=128)
            if idempotency_key is not None else None,
            owner_user_id=owner_user_id,
        )
        return AgentMessageSendState(pair.user_message, pair.assistant_message)

    def cancel_message(
        self, conversation_id: str, *, tenant_id: str, message_id: str,
        owner_user_id: str = LOCAL_USER_ID,
    ) -> AgentMessageSendState:
        with self._lock:
            pair = self.get_message(
                conversation_id, tenant_id=tenant_id, message_id=message_id,
                owner_user_id=owner_user_id,
            )
            assistant = pair.assistant_message
            if assistant.message_id != canonical_uuid(message_id, "message_id"):
                raise ValueError("only assistant messages can be cancelled")
            if assistant.status != "PROCESSING":
                return pair
            execution = self._executions.get(assistant.message_id)
            if execution is not None:
                execution.cancellation.set()
                return pair
            # A stopped Server can leave a persisted reply without a live task.
            failed = self._repository.fail_assistant_message(
                assistant.message_id, error="Agent execution cancelled",
                now=self._clock(), public_code="AGENT_CANCELLED",
                public_message="已停止生成回答。",
            )
            return AgentMessageSendState(pair.user_message, failed)

    def _load_reference(self, reference, conversation_id, tenant_id, scope, *, owner_user_id: str = LOCAL_USER_ID, source_tenant_id: str | None = None):
        if reference is None:
            return None
        try:
            pair = self._repository.get_message_pair(
                conversation_id, tenant_id, message_id=reference["messageId"],
                owner_user_id=owner_user_id,
            )
            source = pair.assistant_message
            if source.message_id != reference["messageId"] or not source.search_result:
                raise AgentReferenceInvalidError("引用必须来自当前对话的真实候选。")
            source_result = _evidence_with_video_blocks(source)
            hit = next((hit for hit in source.search_result["hits"]
                        if hit["candidate"]["videoChunkId"] == reference["videoChunkId"]), None)
            if hit is None:
                raise AgentReferenceInvalidError("引用的片段不在该回答的候选中。")
            try:
                evidence = VideoChunkEvidence.model_validate(hit["evidence"])
            except (KeyError, TypeError, ValueError) as exc:
                raise AgentReferenceInvalidError("引用的历史证据已损坏。") from exc
            if evidence.tenant_id != (source_tenant_id or tenant_id) or evidence.series_id not in scope:
                raise AgentReferenceInvalidError("引用的片段不属于本次检索范围。")
            selected_range = _validated_stored_video_range(
                source_result,
                reference["videoChunkId"],
                evidence_start_seconds=evidence.start_seconds,
                evidence_end_seconds=evidence.end_seconds,
                conversation_id=source.conversation_id,
                message_id=source.message_id,
            )
            block = next(block for block in source_result["renderBlocks"]
                         if isinstance(block, dict) and block.get("type") == "video"
                         and block.get("videoChunkId") == reference["videoChunkId"])
            supporting_ids = set(block.get("sourceVideoChunkIds") or []) - {reference["videoChunkId"]}
            try:
                supporting = [
                    VideoChunkEvidence.model_validate(item["evidence"]).model_dump(mode="json", by_alias=True)
                    for item in source.search_result["hits"]
                    if item["candidate"]["videoChunkId"] in supporting_ids
                ]
            except (KeyError, TypeError, ValueError) as exc:
                raise AgentReferenceInvalidError("引用的历史证据已损坏。") from exc
            return {"reference": reference, "sourceStatus": source.status,
                    "selectionReason": hit["selectionReason"],
                    "selectedRange": selected_range,
                    "evidence": evidence.model_dump(mode="json", by_alias=True),
                    **({"supportingEvidence": supporting} if supporting else {})}
        except AgentMessageNotFoundError as exc:
            raise AgentReferenceInvalidError("引用的消息已失效。") from exc

    def _resolve_scope(
        self, requested: list[str], tenant_id: str, *, allow_empty: bool,
        playback_series_id: str | None = None,
    ) -> list[str]:
        if requested:
            series = [
                self._library.get_series(series_id, tenant_id=tenant_id)
                for series_id in requested
            ]
            if any(item.status != "READY" and item.series_id != playback_series_id for item in series):
                raise SeriesNotReadyError("selected Series must be READY before retrieval")
            series = [item for item in series if item.status == "READY"]
        else:
            series = [
                item for item in self._library.list_series(tenant_id=tenant_id)
                if item.status == "READY"
            ]
        if not series and not allow_empty:
            raise SeriesNotReadyError("this workspace has no READY Series to retrieve")
        return sorted(item.series_id for item in series)

    def _execute(
        self, execution: AgentMessageExecution, tenant_id: str, requested_top_k: int,
        run_options: AgentRunOptionsState, max_loop_turns: int | None,
        runtime: AgentRuntime | SupervisorRuntime, config: AgentServiceConfig,
        *,
        owner_user_id: str = LOCAL_USER_ID,
        conversation_tenant_id: str | None = None,
        playback_series_id: str | None = None,
    ) -> AgentMessageSendState:
        initial = execution.initial
        assistant_id = initial.assistant_message.message_id
        conversation_id = initial.assistant_message.conversation_id
        pending_answer_events: list[dict] = []
        last_phase = initial.assistant_message.phase
        evidence_hit_count = 0
        snapshot_count = 0
        partial_content = ""
        timer = execution.timing
        assert timer is not None
        terminal_timing: AgentMessageTimingState | None = None
        committed_result: AgentMessageSendState | None = None

        def apply_runtime_event(event):
            nonlocal last_phase, evidence_hit_count, snapshot_count, partial_content
            event_type = event["type"]
            if event_type == "answer.progress":
                with self._lock:
                    if execution.cancellation.is_set():
                        raise AgentModelInvocationError("Agent execution cancelled")
                    last_phase = event["phase"]
                    timing = timer.transition(AnswerPhase(event["phase"]))
                    self._repository.checkpoint_assistant_message(
                        assistant_id, phase=event["phase"], now=self._clock(),
                        timing=timing,
                    )
                    execution.publish({**event, "timing": asdict(timing)})
                return
            elif event_type == "answer.snapshot" and isinstance(runtime, SupervisorRuntime):
                with self._lock:
                    if execution.cancellation.is_set():
                        raise AgentModelInvocationError("Agent execution cancelled")
                    search_result = deepcopy(event["searchResult"])
                    legacy_blocks = (
                        search_result.pop("renderBlocks", None)
                        if search_result is not None else None
                    )
                    assistant = self._repository.checkpoint_assistant_message(
                        assistant_id, content=event["content"],
                        search_result=search_result, now=self._clock(),
                        render_blocks=event.get("renderBlocks", legacy_blocks),
                    )
                    execution.publish({
                        "type": "answer.snapshot",
                        **_message_pair(AgentMessageSendState(initial.user_message, assistant)),
                    })
                snapshot_count += 1
                partial_content = assistant.content or ""
                hits = (assistant.search_result or {}).get("hits")
                evidence_hit_count = len(hits) if isinstance(hits, list) else 0
                _logger.info(
                    "agent_answer_snapshot conversation_id=%s message_id=%s "
                    "snapshot_count=%s output_chars=%s evidence_hits=%s version=%s",
                    conversation_id, assistant_id, snapshot_count, len(partial_content),
                    evidence_hit_count, assistant.version,
                )
                return
            elif event_type in {"answer.evidence", "answer.block", "answer.delta"}:
                if not isinstance(runtime, SupervisorRuntime):
                    pending_answer_events.append(event)
                return
            execution.publish(event)

        def publish_runtime(event):
            # Intermediate message delivery observes execution; it does not own
            # the Tool result. Full snapshots and final commit can recover it.
            try:
                apply_runtime_event(event)
            except (AgentConflictError, AgentMessageNotFoundError) as exc:
                execution.cancellation.set()
                raise AgentModelInvocationError("Agent message is no longer active") from exc
            except AgentModelInvocationError:
                raise
            except Exception:
                if execution.cancellation.is_set():
                    raise AgentModelInvocationError("Agent execution cancelled")
                _logger.exception(
                    "event=agent_message_update_failed conversation_id=%s message_id=%s event_type=%s",
                    conversation_id, assistant_id, event.get("type"),
                )

        try:
            history = self._repository.list_recent_completed_messages(
                conversation_id, conversation_tenant_id or tenant_id,
                before_sequence_no=initial.user_message.sequence_no,
                limit=config.history_message_limit,
                owner_user_id=owner_user_id,
            )

            def resolve_attachment(uri: str):
                if self._attachments is None:
                    raise AgentAttachmentInputUnavailableError("附件存储服务尚未配置。")
                return self._attachments.get_content(
                    conversation_id, uri, tenant_id=conversation_tenant_id or tenant_id,
                    owner_user_id=owner_user_id,
                ).path

            session_context = None
            if isinstance(runtime, SupervisorRuntime):
                session_states = None
                try:
                    conversation = self._repository.get_conversation(
                        conversation_id, conversation_tenant_id or tenant_id,
                        owner_user_id=owner_user_id,
                    )
                    session_states = deepcopy((conversation.detail or {}).get("runtimeSessions", {}))
                except AgentConversationNotFoundError:
                    raise
                except Exception:
                    _logger.warning(
                        "Could not load Supervisor session state conversation_id=%s message_id=%s",
                        conversation_id, assistant_id, exc_info=True,
                    )

                def load_session_history(after_sequence_no: int) -> list[dict]:
                    committed = self._repository.list_recent_completed_messages(
                        conversation_id, conversation_tenant_id or tenant_id,
                        before_sequence_no=initial.user_message.sequence_no,
                        after_sequence_no=after_sequence_no, limit=None,
                        owner_user_id=owner_user_id,
                    )
                    return [{
                        "role": item.role, "content": item.content or "",
                        "messageId": item.message_id, "sequenceNo": item.sequence_no,
                        "status": item.status,
                        **({"attachments": [dict(ref) for ref in item.attachments],
                            "imageUrls": [image_uri_to_data_url(ref["mediaUri"], resolve=resolve_attachment)
                                          for ref in item.attachments]}
                           if item.role == "USER" and item.attachments else {}),
                    } for item in committed]

                def save_session_state(key: str, state: dict) -> None:
                    if execution.cancellation.is_set():
                        raise AgentModelInvocationError("Agent execution cancelled")
                    try:
                        self._repository.save_runtime_session(
                            conversation_id, conversation_tenant_id or tenant_id,
                            message_id=assistant_id, key=key, state=state,
                            owner_user_id=owner_user_id,
                        )
                    except (AgentConflictError, AgentMessageNotFoundError, AgentConversationNotFoundError):
                        execution.cancellation.set()
                        raise

                if session_states is not None:
                    session_context = SupervisorSessionContext(
                        states=session_states,
                        message_sequence_no=initial.assistant_message.sequence_no,
                        load_history=load_session_history,
                        save_state=save_session_state,
                    )

            context_series_ids = list(initial.user_message.series_ids)
            if playback_series_id is not None and playback_series_id not in context_series_ids:
                context_series_ids.append(playback_series_id)
            series = [
                self._library.get_series(series_id, tenant_id=tenant_id)
                for series_id in context_series_ids
            ]
            series_context = deepcopy([{
                "seriesId": item.series_id, "name": item.name,
                "description": item.description,
                "transcriptLanguageHint": item.transcript_language_hint,
                "importantExtraData": item.important_extra_data,
                "characterBible": item.character_bible,
                "videos": [{
                    "episodeOrder": video.episode_order,
                    "originalFilename": video.original_filename,
                    "parseStatus": video.parse_status,
                } for video in item.videos],
            } for item in series])

            def load_video_context() -> list[dict]:
                return deepcopy([
                    item for item in series_context
                    if item["seriesId"] in initial.user_message.series_ids
                ])

            inputs = RunInputs(
                conversation_id=conversation_id, assistant_message_id=assistant_id,
                tenant_id=tenant_id, series_ids=list(initial.user_message.series_ids),
                user_message=initial.user_message.content or "",
                attachments=[dict(ref) for ref in initial.user_message.attachments],
                image_urls=[image_uri_to_data_url(ref["mediaUri"], resolve=resolve_attachment)
                            for ref in initial.user_message.attachments],
                requested_top_k=requested_top_k,
                history=[{
                    "role": item.role, "content": item.content or "",
                    "seriesIds": list(item.series_ids),
                    "messageId": item.message_id, "status": item.status,
                    "reference": item.reference,
                    "searchResult": _evidence_with_video_blocks(item),
                    **({"attachments": [dict(ref) for ref in item.attachments],
                        "imageUrls": [image_uri_to_data_url(ref["mediaUri"], resolve=resolve_attachment)
                                      for ref in item.attachments]} if item.attachments else {}),
                } for item in history],
                reference_context=execution.reference_context,
                extra_context=initial.user_message.extra_context,
                series_context=series_context,
            )
            if inputs["image_urls"] or any(item.get("imageUrls") for item in inputs["history"]):
                _logger.info(
                    "event=agent_image_inputs_prepared conversation_id=%s message_id=%s "
                    "current_image_count=%s history_image_count=%s",
                    conversation_id, assistant_id, len(inputs["image_urls"]),
                    sum(len(item.get("imageUrls", ())) for item in inputs["history"]),
                )
            try:
                run_kwargs = {
                    "emit": publish_runtime,
                    "cancellation": execution.cancellation,
                    "max_model_calls_per_turn": run_options.max_model_calls_per_turn,
                    "max_tool_calls_per_turn": run_options.max_tool_calls_per_turn,
                    "max_loop_turns": max_loop_turns,
                }
                context = {
                    "tenant_id": tenant_id,
                    "actor_user_id": owner_user_id,
                    "conversation_id": conversation_id,
                    "session_id": conversation_id,
                    "user_message_id": initial.user_message.message_id,
                    "assistant_message_id": assistant_id,
                    "run_id": assistant_id,
                    "series_ids": tuple(initial.user_message.series_ids),
                    "stage": "agent_chat",
                }
                tracing = (
                    self._tracing.scope(context)
                    if self._tracing is not None else nullcontext()
                )
                usage = self._parse_usage.capture_chat(context) if self._parse_usage is not None else nullcontext()
                # This scope belongs to the executor thread that invokes the models.
                with model_call_context(context), tracing, usage:
                    final = (
                        runtime.kick(
                            inputs, load_video_context=load_video_context,
                            session_context=session_context,
                            **run_kwargs,
                        )
                        if isinstance(runtime, SupervisorRuntime)
                        else runtime.kick(inputs, **run_kwargs)
                    )
            except AgentToolInvocationError as exc:
                if exc.__cause__ is None:
                    raise
                raise exc.__cause__ from None
            terminal_timing = timer.finish()
            if isinstance(runtime, SupervisorRuntime):
                content = final.content
                render_blocks = list(final.render_blocks) if final.render_blocks is not None else None
                video_result = final.video_result
                search_result = (
                    video_result.model_dump(mode="json", by_alias=True)
                    if video_result is not None else None
                )
            else:
                content = final.agent_reply.message
                render_blocks = None
                search_result = final.model_dump(mode="json", by_alias=True)
            if search_result is not None:
                legacy_blocks = search_result.pop("renderBlocks", None)
                if render_blocks is None:
                    render_blocks = legacy_blocks
            # Keep a new turn or deletion behind SQL and harness confirmation.
            with self._lock:
                if execution.cancellation.is_set():
                    raise AgentModelInvocationError("Agent execution cancelled")
                assistant = self._repository.complete_assistant_message(
                    assistant_id, content=content,
                    search_result=search_result, now=self._clock(),
                    timing=terminal_timing,
                    render_blocks=render_blocks,
                )
                committed_result = AgentMessageSendState(initial.user_message, assistant)
                if isinstance(runtime, SupervisorRuntime):
                    try:
                        runtime.confirm_turn(conversation_id, assistant_id, content)
                    except Exception:
                        _logger.warning(
                            "Could not confirm Supervisor turn conversation_id=%s message_id=%s",
                            conversation_id, assistant_id,
                            exc_info=True,
                        )
                        # SQL owns the completed answer; discard unconfirmed harness state.
                        try:
                            runtime.delete_thread(conversation_id)
                        except Exception:
                            _logger.warning(
                                "Could not clear Supervisor thread conversation_id=%s",
                                conversation_id,
                                exc_info=True,
                            )
            for event in pending_answer_events:
                if event["type"] == "answer.evidence":
                    event = {**event, "searchResult": {
                        key: value for key, value in event["searchResult"].items()
                        if key != "renderBlocks"
                    }}
                    hits = event["searchResult"].get("hits")
                    evidence_hit_count = len(hits) if isinstance(hits, list) else 0
                execution.publish(event)
            _logger.info(
                "agent_execution_completed conversation_id=%s message_id=%s "
                "phase=%s duration_ms=%s stage_count=%s output_chars=%s "
                "evidence_hits=%s answer_snapshots=%s max_model_calls_per_turn=%s "
                "max_tool_calls_per_turn=%s",
                conversation_id, assistant_id, last_phase,
                terminal_timing.total_duration_ms, len(terminal_timing.stages),
                len(content), evidence_hit_count, snapshot_count,
                run_options.max_model_calls_per_turn,
                run_options.max_tool_calls_per_turn,
            )
            execution.publish(_terminal_event(committed_result))
            return committed_result
        except Exception as exc:
            if committed_result is not None:
                _logger.exception(
                    "Could not publish completed Agent answer conversation_id=%s message_id=%s",
                    conversation_id, assistant_id,
                )
                return committed_result
            if isinstance(runtime, SupervisorRuntime):
                try:
                    runtime.discard_turn(conversation_id, assistant_id)
                except Exception:
                    _logger.warning(
                        "Could not discard Supervisor turn conversation_id=%s message_id=%s",
                        conversation_id, assistant_id,
                        exc_info=True,
                    )
            if isinstance(exc, AgentContextBudgetError):
                public_code = "AGENT_CONTEXT_BUDGET_EXCEEDED"
                public_message = (
                    "本次上下文较多，请缩小系列范围、减少候选、精简系列背景或新建对话后重试。"
                )
            elif isinstance(exc, AgentExecutionTimeoutError):
                public_code = "AGENT_EXECUTION_TIMEOUT"
                public_message = "本次处理超时，已停止生成，请缩小问题后重试。"
            elif execution.cancellation.is_set():
                public_code = "AGENT_CANCELLED"
                public_message = "已停止生成回答。"
            elif isinstance(exc, AgentModelInvocationError):
                public_code = "AGENT_MODEL_ERROR"
                public_message = "这次对话未能完成，请稍后重试。"
            else:
                public_code = "PROCESSING_FAILED"
                public_message = "这次对话未能完成，请稍后重试。"
            root_error = exc
            while root_error.__cause__ is not None:
                root_error = root_error.__cause__
            log_failure = (
                _logger.error
                if isinstance(exc, AgentModelInvocationError)
                else _logger.exception
            )
            terminal_timing = terminal_timing or timer.finish()
            log_failure(
                "agent_execution_failed conversation_id=%s message_id=%s code=%s "
                "phase=%s duration_ms=%s stage_count=%s partial_chars=%s "
                "evidence_hits=%s error_type=%s "
                "root_error_type=%s max_model_calls_per_turn=%s "
                "max_tool_calls_per_turn=%s",
                conversation_id, assistant_id, public_code, last_phase,
                terminal_timing.total_duration_ms, len(terminal_timing.stages),
                len(partial_content), evidence_hit_count,
                type(exc).__name__, type(root_error).__name__,
                run_options.max_model_calls_per_turn,
                run_options.max_tool_calls_per_turn,
            )
            failed = None
            try:
                failed = self._repository.fail_assistant_message(
                    assistant_id, error=f"{type(exc).__name__}: {str(exc).strip()}"[:8192],
                    now=self._clock(), timing=terminal_timing,
                    public_code=public_code,
                    public_message=public_message,
                )
            except Exception:
                _logger.exception("Could not persist Agent failure message_id=%s", assistant_id)
            if failed is not None:
                execution.publish(_terminal_event(AgentMessageSendState(initial.user_message, failed)))
            else:
                execution.publish({"type": "answer.interrupted", "error": {
                    "code": "STREAM_INTERRUPTED", "message": "回答状态暂时无法确认，请通过消息快照恢复。",
                }})
            raise
        finally:
            execution.finish()
            with self._lock:
                self._executions.pop(assistant_id, None)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            executions = list(self._executions.values())
        for execution in executions:
            execution.cancellation.set()
        try:
            self._executor.shutdown(wait=True)
        finally:
            for runtime in self._runtimes:
                runtime.close()


def _message_pair(result: AgentMessageSendState) -> dict:
    return {"userMessage": asdict(result.user_message), "assistantMessage": asdict(result.assistant_message)}


def _terminal_event(result: AgentMessageSendState) -> dict:
    if result.assistant_message.status == "COMPLETED":
        return {"type": "answer.completed", **_message_pair(result)}
    return {
        "type": "answer.failed", "error": asdict(result.assistant_message.last_error)
        if result.assistant_message.last_error is not None else {"code": "PROCESSING_FAILED", "message": "这次对话未能完成。"},
        **_message_pair(result),
    }


def _canonical_scope(series_ids: list[str]) -> list[str]:
    if not isinstance(series_ids, list):
        raise ValueError("series_ids must be a list")
    return sorted({canonical_uuid(item, "series_id") for item in series_ids})


def _bounded_run_limit(
    name: str,
    value: int | None,
    *,
    default: int,
    maximum: int,
) -> int:
    resolved = default if value is None else value
    if (
        isinstance(resolved, bool)
        or not isinstance(resolved, int)
        or not 1 <= resolved <= maximum
    ):
        raise ValueError(f"{name} must be between 1 and {maximum}")
    return resolved


def _canonical_reference(reference):
    if reference is None:
        return None
    if not isinstance(reference, dict) or set(reference) != {"messageId", "videoChunkId"}:
        raise AgentReferenceInvalidError("引用需要 messageId 和 videoChunkId。")
    try:
        return {name: canonical_uuid(value, name) for name, value in reference.items()}
    except ValueError as exc:
        raise AgentReferenceInvalidError("引用的消息或片段 ID 无效。") from exc


def _evidence_with_video_blocks(message: AgentMessageState) -> dict | None:
    if message.search_result is None or message.render_blocks is None:
        return message.search_result
    return {
        **message.search_result,
        "renderBlocks": [block for block in message.render_blocks if block.get("type") == "video"],
    }


def _validated_stored_video_range(
    search_result: dict,
    video_chunk_id: str,
    *,
    evidence_start_seconds: int,
    evidence_end_seconds: int,
    conversation_id: str | None = None,
    message_id: str | None = None,
) -> dict[str, int]:
    proposed_start_seconds = None
    proposed_end_seconds = None
    evidence_by_id = {
        hit["candidate"]["videoChunkId"]: hit["evidence"]
        for hit in search_result.get("hits") or []
        if isinstance(hit, dict) and isinstance(hit.get("candidate"), dict)
        and isinstance(hit["candidate"].get("videoChunkId"), str)
        and isinstance(hit.get("evidence"), dict)
    }
    evidence_by_id.setdefault(video_chunk_id, {
        "startSeconds": evidence_start_seconds, "endSeconds": evidence_end_seconds,
    })
    blocks = search_result.get("renderBlocks")
    if isinstance(blocks, list):
        for block in blocks:
            if (
                not isinstance(block, dict)
                or block.get("type") != "video"
                or block.get("videoChunkId") != video_chunk_id
            ):
                continue
            proposed_start_seconds = block.get("startSeconds")
            proposed_end_seconds = block.get("endSeconds")
            try:
                video_range_sources(
                    evidence_by_id, video_chunk_id, proposed_start_seconds, proposed_end_seconds,
                    source_video_chunk_ids=block.get("sourceVideoChunkIds"),
                )
            except ValueError:
                break
            return {
                "startSeconds": proposed_start_seconds,
                "endSeconds": proposed_end_seconds,
            }
    _logger.warning(
        "event=agent_invalid_stored_video_range conversation_id=%s message_id=%s "
        "video_chunk_id=%s proposed_start_seconds=%r proposed_end_seconds=%r "
        "evidence_start_seconds=%s evidence_end_seconds=%s "
        "action=reject_reference",
        conversation_id, message_id, video_chunk_id,
        proposed_start_seconds, proposed_end_seconds,
        evidence_start_seconds, evidence_end_seconds,
    )
    raise AgentReferenceInvalidError("引用片段缺少有效的已保存播放范围。")


def _page(items, total, offset, scope):
    next_offset = offset + len(items)
    has_more = next_offset < total
    return AgentPageState(items, total, encode_cursor(next_offset, scope=scope) if has_more else None, has_more)


def _positive_limit(value: int, *, maximum: int = 500) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 < value <= maximum
    ):
        raise ValueError(f"limit must be between 1 and {maximum}")
    return value
