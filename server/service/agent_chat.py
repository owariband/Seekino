from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, replace
from queue import Empty, Full, Queue
from threading import Event, Lock
from time import monotonic_ns
from uuid import uuid4

from ..agent.errors import (
    AgentContextBudgetError, AgentModelInvocationError, AgentToolInvocationError,
)
from ..agent.runtime import AgentRuntime
from ..agent.state import AnswerPhase, RunInputs
from ..config.agent import AgentServiceConfig
from ..identity import canonical_uuid
from ..model.dto.agentDTO import (
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
    AgentMessageInProgressError,
    AgentRepository, AgentReferenceInvalidError, AgentMessageNotFoundError,
)
from ..repository.video import SeriesNotReadyError, VideoChunkNotFoundError
from ..utils.time import now_unix_time_ms
from ..utils.validation import require_nonempty_text
from .library import LibraryService
from .pagination import decode_cursor, encode_cursor

_logger = logging.getLogger(__name__)


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
        runtime: AgentRuntime,
        *,
        config: AgentServiceConfig | None = None,
        config_loader: Callable[[], tuple[AgentServiceConfig, str]] | None = None,
        runtime_factory: Callable[[AgentServiceConfig], AgentRuntime] | None = None,
        config_revision: str | None = None,
        clock: Callable[[], int] = now_unix_time_ms,
        monotonic_clock: Callable[[], int] = monotonic_ns,
    ) -> None:
        self._repository = repository
        self._library = library
        self._runtime = runtime
        self._config = config or AgentServiceConfig()
        self._config_loader = config_loader
        self._runtime_factory = runtime_factory
        self._config_revision = config_revision
        self._runtimes = [runtime]
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

    def _current_config(self) -> tuple[AgentRuntime, AgentServiceConfig, ThreadPoolExecutor]:
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
                            self._runtime = self._runtime_factory(config)
                            self._runtimes.append(self._runtime)
                        self._config = config
                        _logger.info(
                            "agent_global_settings_reloaded revision=%s "
                            "restart_required=%s",
                            revision[:12],
                            config.maximum_concurrent_kicks != self._active_maximum_concurrent_kicks,
                        )
                    self._config_revision = revision
            return self._runtime, self._config, self._executor

    def create_conversation(
        self,
        *,
        workspace_id: str,
        title: str | None = None,
        idempotency_key: str,
    ) -> AgentConversationState:
        normalized_workspace = require_nonempty_text(
            workspace_id,
            "workspace_id",
            maximum=64,
        )
        return self._repository.create_conversation(
            conversation_id=str(uuid4()),
            workspace_id=normalized_workspace,
            title="新对话" if title is None else require_nonempty_text(title, "title", maximum=255),
            title_is_auto=title is None,
            idempotency_key=require_nonempty_text(
                idempotency_key,
                "idempotency_key",
                maximum=128,
            ),
        )

    def list_conversations(
        self,
        *,
        workspace_id: str,
        limit: int = 100,
    ) -> list[AgentConversationState]:
        return self._repository.list_conversations(
            workspace_id=require_nonempty_text(workspace_id, "workspace_id", maximum=64),
            limit=_positive_limit(limit),
        )

    def get_conversation(
        self,
        conversation_id: str,
        *,
        workspace_id: str,
    ) -> AgentConversationState:
        return self._repository.get_conversation(
            canonical_uuid(conversation_id, "conversation_id"),
            require_nonempty_text(workspace_id, "workspace_id", maximum=64),
        )

    def delete_conversation(
        self,
        conversation_id: str,
        *,
        workspace_id: str,
    ) -> AgentConversationState:
        normalized_conversation_id = canonical_uuid(
            conversation_id,
            "conversation_id",
        )
        normalized_workspace = require_nonempty_text(
            workspace_id,
            "workspace_id",
            maximum=64,
        )
        with self._lock:
            return self._repository.delete_conversation(
                normalized_conversation_id,
                normalized_workspace,
            )

    def page_conversations(
        self, *, workspace_id: str, cursor: str | None = None, limit: int = 50,
        series_id: str | None = None, query: str | None = None,
    ) -> AgentPageState[AgentConversationState]:
        workspace_id = require_nonempty_text(workspace_id, "workspace_id", maximum=64)
        series_id = canonical_uuid(series_id, "series_id") if series_id is not None else None
        query = query.strip() if query is not None else None
        scope = {"workspaceId": workspace_id, "seriesId": series_id, "query": query,
                 "resource": "conversations", "sort": "updatedAt,conversationId:desc"}
        offset = decode_cursor(cursor, scope=scope)
        items, total = self._repository.list_conversation_page(
            workspace_id, offset=offset, limit=_positive_limit(limit, maximum=100),
            series_id=series_id, query=query,
        )
        return _page(items, total, offset, scope)

    def page_messages(
        self, conversation_id: str, *, workspace_id: str, cursor: str | None = None,
        limit: int = 50, after_sequence_no: int = -1,
    ) -> AgentPageState[AgentMessageState]:
        conversation_id = canonical_uuid(conversation_id, "conversation_id")
        workspace_id = require_nonempty_text(workspace_id, "workspace_id", maximum=64)
        if isinstance(after_sequence_no, bool) or not isinstance(after_sequence_no, int) or after_sequence_no < -1:
            raise ValueError("after_sequence_no must be an integer at least -1")
        scope = {"workspaceId": workspace_id, "conversationId": conversation_id,
                 "afterSequenceNo": after_sequence_no, "resource": "messages", "sort": "sequenceNo:asc"}
        offset = decode_cursor(cursor, scope=scope)
        items, total = self._repository.list_message_page(
            conversation_id, workspace_id, offset=offset,
            limit=_positive_limit(limit, maximum=100), after_sequence_no=after_sequence_no,
        )
        return _page(items, total, offset, scope)

    def list_messages(
        self,
        conversation_id: str,
        *,
        workspace_id: str,
        after_sequence_no: int = -1,
        limit: int = 200,
    ) -> list[AgentMessageState]:
        if (
            isinstance(after_sequence_no, bool)
            or not isinstance(after_sequence_no, int)
            or after_sequence_no < -1
        ):
            raise ValueError("after_sequence_no must be an integer at least -1")
        return self._repository.list_messages(
            canonical_uuid(conversation_id, "conversation_id"),
            require_nonempty_text(workspace_id, "workspace_id", maximum=64),
            after_sequence_no=after_sequence_no,
            limit=_positive_limit(limit, maximum=500),
        )

    def list_all_messages(
        self,
        conversation_id: str,
        *,
        workspace_id: str,
    ) -> list[AgentMessageState]:
        """Read a complete persisted snapshot, including in-place reply updates."""
        return self._repository.list_messages(
            canonical_uuid(conversation_id, "conversation_id"),
            require_nonempty_text(workspace_id, "workspace_id", maximum=64),
            after_sequence_no=-1,
            limit=None,
        )

    def send_message(
        self, conversation_id: str, *, workspace_id: str, content: str,
        idempotency_key: str, series_ids: list[str] | None = None, top_k: int | None = None,
        max_model_calls_per_turn: int | None = None,
        max_tool_calls_per_turn: int | None = None,
        max_loop_turns: int | None = None,
        reference: dict[str, str] | None = None,
    ) -> AgentMessageSendState:
        return self.start_message(
            conversation_id, workspace_id=workspace_id, content=content,
            idempotency_key=idempotency_key, series_ids=series_ids, top_k=top_k, reference=reference,
            max_model_calls_per_turn=max_model_calls_per_turn,
            max_tool_calls_per_turn=max_tool_calls_per_turn,
            max_loop_turns=max_loop_turns,
        ).result()

    def start_message(
        self, conversation_id: str, *, workspace_id: str, content: str,
        idempotency_key: str, series_ids: list[str] | None = None,
        top_k: int | None = None, stream: bool = False,
        max_model_calls_per_turn: int | None = None,
        max_tool_calls_per_turn: int | None = None,
        max_loop_turns: int | None = None,
        reference: dict[str, str] | None = None,
    ) -> AgentMessageExecution:
        runtime, config, executor = self._current_config()
        normalized_workspace = require_nonempty_text(workspace_id, "workspace_id", maximum=64)
        normalized_conversation_id = canonical_uuid(conversation_id, "conversation_id")
        normalized_content = require_nonempty_text(content, "content", maximum=20_000)
        normalized_reference = _canonical_reference(reference)
        requested_scope = _canonical_scope([] if series_ids is None else series_ids)
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
                workspace_id=normalized_workspace, content=normalized_content,
                requested_top_k=requested_top_k, idempotency_key=normalized_key,
                requested_series_ids=requested_scope,
                requested_run_options=requested_run_options,
                reference=normalized_reference,
            )
            if started is None:
                scope = self._resolve_scope(requested_scope, normalized_workspace)
                reference_context = self._load_reference(
                    normalized_reference, normalized_conversation_id, normalized_workspace, scope,
                )
                timing_started_at = self._clock()
                timing_started_ns = self._monotonic_clock()
                started = self._repository.start_message(
                    conversation_id=normalized_conversation_id,
                    workspace_id=normalized_workspace, content=normalized_content,
                    model_name=require_nonempty_text(runtime.model_name, "chat_model.model_name", maximum=255),
                    requested_top_k=requested_top_k, idempotency_key=normalized_key,
                    series_ids=scope, requested_series_ids=requested_scope,
                    requested_run_options=requested_run_options,
                    user_message_id=str(uuid4()), assistant_message_id=str(uuid4()),
                    now=timing_started_at,
                    reference=normalized_reference,
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
                    self._execute, execution, normalized_workspace, requested_top_k,
                    run_options, max_loop_turns, runtime, config,
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
        self, conversation_id: str, *, workspace_id: str,
        message_id: str | None = None, idempotency_key: str | None = None,
    ) -> AgentMessageSendState:
        if (message_id is None) == (idempotency_key is None):
            raise ValueError("provide exactly one of messageId or idempotencyKey")
        pair = self._repository.get_message_pair(
            canonical_uuid(conversation_id, "conversation_id"),
            require_nonempty_text(workspace_id, "workspace_id", maximum=64),
            message_id=canonical_uuid(message_id, "message_id") if message_id is not None else None,
            idempotency_key=require_nonempty_text(idempotency_key, "idempotency_key", maximum=128)
            if idempotency_key is not None else None,
        )
        return AgentMessageSendState(pair.user_message, pair.assistant_message)

    def _load_reference(self, reference, conversation_id, workspace_id, scope):
        if reference is None:
            return None
        try:
            pair = self._repository.get_message_pair(
                conversation_id, workspace_id, message_id=reference["messageId"],
            )
            source = pair.assistant_message
            if source.message_id != reference["messageId"] or not source.search_result:
                raise AgentReferenceInvalidError("引用必须来自当前对话的真实候选。")
            hit = next((hit for hit in source.search_result["hits"]
                        if hit["candidate"]["videoChunkId"] == reference["videoChunkId"]), None)
            if hit is None:
                raise AgentReferenceInvalidError("引用的片段不在该回答的候选中。")
            evidence = self._repository.load_reference_evidence(reference["videoChunkId"])
            if evidence.workspace_id != workspace_id or evidence.series_id not in scope:
                raise AgentReferenceInvalidError("引用的片段不属于本次检索范围。")
            selected_range = _validated_stored_video_range(
                source.search_result,
                reference["videoChunkId"],
                evidence_start_ms=evidence.start_ms,
                evidence_end_ms=evidence.end_ms,
                conversation_id=source.conversation_id,
                message_id=source.message_id,
            )
            return {"reference": reference, "sourceStatus": source.status,
                    "selectionReason": hit["selectionReason"],
                    "selectedRange": selected_range,
                    "evidence": evidence.model_dump(mode="json", by_alias=True)}
        except (AgentMessageNotFoundError, VideoChunkNotFoundError) as exc:
            raise AgentReferenceInvalidError("引用的消息或片段已失效。") from exc

    def _resolve_scope(self, requested: list[str], workspace_id: str) -> list[str]:
        if requested:
            series = [
                self._library.get_series(series_id, workspace_id=workspace_id)
                for series_id in requested
            ]
            if any(item.status != "READY" for item in series):
                raise SeriesNotReadyError("selected Series must be READY before retrieval")
        else:
            series = [
                item for item in self._library.list_series(workspace_id=workspace_id)
                if item.status == "READY"
            ]
        if not series:
            raise SeriesNotReadyError("this workspace has no READY Series to retrieve")
        return sorted(item.series_id for item in series)

    def _execute(
        self, execution: AgentMessageExecution, workspace_id: str, requested_top_k: int,
        run_options: AgentRunOptionsState, max_loop_turns: int | None,
        runtime: AgentRuntime, config: AgentServiceConfig,
    ) -> AgentMessageSendState:
        initial = execution.initial
        assistant_id = initial.assistant_message.message_id
        conversation_id = initial.assistant_message.conversation_id
        pending_answer_events: list[dict] = []
        last_phase = initial.assistant_message.phase
        evidence_hit_count = 0
        timer = execution.timing
        assert timer is not None
        terminal_timing: AgentMessageTimingState | None = None

        def publish_runtime(event):
            nonlocal last_phase
            event_type = event["type"]
            if event_type == "answer.progress":
                last_phase = event["phase"]
                timing = timer.transition(AnswerPhase(event["phase"]))
                self._repository.checkpoint_assistant_message(
                    assistant_id, phase=event["phase"], now=self._clock(),
                    timing=timing,
                )
                event = {**event, "timing": asdict(timing)}
            elif event_type in {"answer.evidence", "answer.block", "answer.delta"}:
                pending_answer_events.append(event)
                return
            execution.publish(event)
        try:
            history = self._repository.list_recent_completed_messages(
                conversation_id, workspace_id,
                before_sequence_no=initial.user_message.sequence_no,
                limit=config.history_message_limit,
            )
            series = [
                self._library.get_series(series_id, workspace_id=workspace_id)
                for series_id in initial.user_message.series_ids
            ]
            inputs = RunInputs(
                conversation_id=conversation_id, assistant_message_id=assistant_id,
                workspace_id=workspace_id, series_ids=list(initial.user_message.series_ids),
                user_message=initial.user_message.content or "",
                requested_top_k=requested_top_k,
                history=[{
                    "role": item.role, "content": item.content or "",
                    "seriesIds": list(item.series_ids),
                    "messageId": item.message_id, "status": item.status,
                    "reference": item.reference,
                    "searchResult": item.search_result,
                } for item in history],
                reference_context=execution.reference_context,
                series_context=[{
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
                } for item in series],
            )
            try:
                final = runtime.kick(
                    inputs, emit=publish_runtime, cancellation=execution.cancellation,
                    max_model_calls_per_turn=run_options.max_model_calls_per_turn,
                    max_tool_calls_per_turn=run_options.max_tool_calls_per_turn,
                    max_loop_turns=max_loop_turns,
                )
            except AgentToolInvocationError as exc:
                if exc.__cause__ is None:
                    raise
                raise exc.__cause__ from None
            terminal_timing = timer.finish()
            assistant = self._repository.complete_assistant_message(
                assistant_id, content=final.agent_reply.message, retrieval_query=final.query,
                search_result=final.model_dump(mode="json", by_alias=True), now=self._clock(),
                timing=terminal_timing,
            )
            for event in pending_answer_events:
                if event["type"] == "answer.evidence":
                    hits = event["searchResult"].get("hits")
                    evidence_hit_count = len(hits) if isinstance(hits, list) else 0
                execution.publish(event)
            _logger.info(
                "agent_execution_completed conversation_id=%s message_id=%s "
                "phase=%s duration_ms=%s stage_count=%s output_chars=%s "
                "evidence_hits=%s max_model_calls_per_turn=%s "
                "max_tool_calls_per_turn=%s",
                conversation_id, assistant_id, last_phase,
                terminal_timing.total_duration_ms, len(terminal_timing.stages),
                len(final.agent_reply.message), evidence_hit_count,
                run_options.max_model_calls_per_turn,
                run_options.max_tool_calls_per_turn,
            )
            result = AgentMessageSendState(initial.user_message, assistant)
            execution.publish(_terminal_event(result))
            return result
        except Exception as exc:
            if isinstance(exc, AgentContextBudgetError):
                public_code = "AGENT_CONTEXT_BUDGET_EXCEEDED"
                public_message = (
                    "本次上下文较多，请缩小系列范围、减少候选、精简系列背景或新建对话后重试。"
                )
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
                0, evidence_hit_count,
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


def _validated_stored_video_range(
    search_result: dict,
    video_chunk_id: str,
    *,
    evidence_start_ms: int,
    evidence_end_ms: int,
    conversation_id: str | None = None,
    message_id: str | None = None,
) -> dict[str, int]:
    proposed_start_ms = None
    proposed_end_ms = None
    blocks = search_result.get("renderBlocks")
    if isinstance(blocks, list):
        for block in blocks:
            if (
                not isinstance(block, dict)
                or block.get("type") != "video"
                or block.get("videoChunkId") != video_chunk_id
            ):
                continue
            proposed_start_ms = block.get("startMs")
            proposed_end_ms = block.get("endMs")
            if (
                isinstance(proposed_start_ms, int)
                and not isinstance(proposed_start_ms, bool)
                and isinstance(proposed_end_ms, int)
                and not isinstance(proposed_end_ms, bool)
                and evidence_start_ms <= proposed_start_ms
                and proposed_start_ms < proposed_end_ms
                and proposed_end_ms <= evidence_end_ms
            ):
                return {
                    "startMs": proposed_start_ms,
                    "endMs": proposed_end_ms,
                }
            break
    _logger.warning(
        "event=agent_invalid_stored_video_range conversation_id=%s message_id=%s "
        "video_chunk_id=%s proposed_start_ms=%r proposed_end_ms=%r "
        "evidence_start_ms=%s evidence_end_ms=%s "
        "action=reject_reference",
        conversation_id, message_id, video_chunk_id,
        proposed_start_ms, proposed_end_ms,
        evidence_start_ms, evidence_end_ms,
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
