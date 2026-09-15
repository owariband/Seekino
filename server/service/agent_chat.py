from __future__ import annotations

import logging
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from queue import Empty, Full, Queue
from threading import Event, Lock
from uuid import uuid4

from ..agent.runtime import AgentRuntime
from ..agent.state import AgentModelInvocationError, AgentToolInvocationError, RunInputs
from ..config.agent import AgentServiceConfig
from ..identity import canonical_uuid
from ..repository.agent import (
    AgentConversationState, AgentMessageInProgressError, AgentMessageState,
    AgentRepository,
)
from ..repository.video import SeriesNotReadyError
from ..utils.time import utc_now_naive
from ..utils.validation import require_nonempty_text
from .library import LibraryService

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AgentMessageSendState:
    user_message: AgentMessageState
    assistant_message: AgentMessageState


class AgentMessageExecution:
    """A Service-owned task with an optional, detachable HTTP event consumer."""

    def __init__(self, initial: AgentMessageSendState, *, stream: bool) -> None:
        self.initial = initial
        self.cancellation = Event()
        self.future: Future[AgentMessageSendState] | None = None
        self._events: Queue[dict | None] = Queue(maxsize=256)
        self._lock = Lock()
        self._attached = stream
        self._sequence = 0

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
            }
            try:
                self._events.put_nowait(payload)
            except Full:
                # Stop buffering for a slow consumer; the execution still commits.
                self._attached = False
                self._drain()
                self._events.put_nowait({
                    "type": "answer.failed",
                    "conversationId": payload["conversationId"],
                    "messageId": payload["messageId"],
                    "sequence": self._sequence,
                    "code": "STREAM_INTERRUPTED",
                    "message": "连接未能及时接收回答，请通过会话快照恢复。",
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
    ) -> None:
        self._repository = repository
        self._library = library
        self._runtime = runtime
        self._config = config or AgentServiceConfig()
        self._executor = ThreadPoolExecutor(
            max_workers=self._config.maximum_concurrent_kicks,
            thread_name_prefix="anivag-agent",
        )
        self._lock = Lock()
        self._executions: dict[str, AgentMessageExecution] = {}
        self._closed = False

    @property
    def model_name(self) -> str:
        return self._runtime.model_name

    def create_conversation(
        self,
        *,
        workspace_id: str,
        title: str,
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
            title=require_nonempty_text(title, "title", maximum=255),
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
    ) -> AgentMessageSendState:
        return self.start_message(
            conversation_id, workspace_id=workspace_id, content=content,
            idempotency_key=idempotency_key, series_ids=series_ids, top_k=top_k,
        ).result()

    def start_message(
        self, conversation_id: str, *, workspace_id: str, content: str,
        idempotency_key: str, series_ids: list[str] | None = None,
        top_k: int | None = None, stream: bool = False,
    ) -> AgentMessageExecution:
        normalized_workspace = require_nonempty_text(workspace_id, "workspace_id", maximum=64)
        normalized_conversation_id = canonical_uuid(conversation_id, "conversation_id")
        normalized_content = require_nonempty_text(content, "content", maximum=20_000)
        requested_scope = _canonical_scope([] if series_ids is None else series_ids)
        requested_top_k = self._config.default_top_k if top_k is None else top_k
        if (
            isinstance(requested_top_k, bool) or not isinstance(requested_top_k, int)
            or not 0 < requested_top_k <= self._config.maximum_top_k
        ):
            raise ValueError(f"top_k must be between 1 and {self._config.maximum_top_k}")
        normalized_key = require_nonempty_text(idempotency_key, "idempotency_key", maximum=128)
        with self._lock:
            if self._closed:
                raise AgentMessageInProgressError("the Agent service is shutting down")
            started = self._repository.find_idempotent_message(
                conversation_id=normalized_conversation_id,
                workspace_id=normalized_workspace, content=normalized_content,
                requested_top_k=requested_top_k, idempotency_key=normalized_key,
                requested_series_ids=requested_scope,
            )
            if started is None:
                scope = self._resolve_scope(requested_scope, normalized_workspace)
                started = self._repository.start_message(
                    conversation_id=normalized_conversation_id,
                    workspace_id=normalized_workspace, content=normalized_content,
                    model_name=require_nonempty_text(self.model_name, "chat_model.model_name", maximum=255),
                    requested_top_k=requested_top_k, idempotency_key=normalized_key,
                    series_ids=scope, requested_series_ids=requested_scope,
                    user_message_id=str(uuid4()), assistant_message_id=str(uuid4()),
                    now=utc_now_naive(),
                )
            if started.reused and started.assistant_message.status == "PROCESSING":
                raise AgentMessageInProgressError("the idempotent message is still being processed")
            initial = AgentMessageSendState(started.user_message, started.assistant_message)
            execution = AgentMessageExecution(initial, stream=stream)
            execution.publish({"type": "answer.started", **_message_pair(initial)})
            if started.reused:
                execution.future = Future()
                execution.future.set_result(initial)
                execution.publish(_terminal_event(initial))
                execution.finish()
                return execution
            self._executions[started.assistant_message.message_id] = execution
            try:
                execution.future = self._executor.submit(
                    self._execute, execution, normalized_workspace, requested_top_k,
                )
            except Exception:
                self._executions.pop(started.assistant_message.message_id, None)
                self._repository.fail_assistant_message(
                    started.assistant_message.message_id, error="Agent task could not start",
                    now=utc_now_naive(),
                )
                raise
            return execution

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
    ) -> AgentMessageSendState:
        initial = execution.initial
        assistant_id = initial.assistant_message.message_id
        conversation_id = initial.assistant_message.conversation_id
        try:
            history = self._repository.list_recent_completed_messages(
                conversation_id, workspace_id,
                before_sequence_no=initial.user_message.sequence_no,
                limit=self._config.history_message_limit,
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
                } for item in history],
                series_context=[{
                    "seriesId": item.series_id, "name": item.name,
                    "description": item.description,
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
                final = self._runtime.kick(
                    inputs, emit=execution.publish, cancellation=execution.cancellation,
                )
            except AgentToolInvocationError as exc:
                if exc.__cause__ is None:
                    raise
                raise exc.__cause__ from None
            assistant = self._repository.complete_assistant_message(
                assistant_id, content=final.agent_reply.message, retrieval_query=final.query,
                search_result=final.model_dump(mode="json", by_alias=True), now=utc_now_naive(),
            )
            result = AgentMessageSendState(initial.user_message, assistant)
            execution.publish(_terminal_event(result))
            return result
        except Exception as exc:
            failed = None
            try:
                failed = self._repository.fail_assistant_message(
                    assistant_id, error=f"{type(exc).__name__}: {str(exc).strip()}"[:8192],
                    now=utc_now_naive(),
                )
            except Exception:
                _logger.exception("Could not persist Agent failure", extra={"message_id": assistant_id})
            execution.publish({
                "type": "answer.failed",
                "code": "AGENT_MODEL_ERROR" if isinstance(exc, AgentModelInvocationError) else "AGENT_EXECUTION_ERROR",
                "message": str(exc) if isinstance(exc, AgentModelInvocationError) else "这次对话未能完成，请稍后重试。",
                "userMessage": asdict(initial.user_message),
                "assistantMessage": asdict(failed) if failed is not None else None,
            })
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
        self._executor.shutdown(wait=True)


def _message_pair(result: AgentMessageSendState) -> dict:
    return {"userMessage": asdict(result.user_message), "assistantMessage": asdict(result.assistant_message)}


def _terminal_event(result: AgentMessageSendState) -> dict:
    if result.assistant_message.status == "COMPLETED":
        return {"type": "answer.completed", **_message_pair(result)}
    return {
        "type": "answer.failed", "code": "AGENT_EXECUTION_ERROR",
        "message": result.assistant_message.content or "这次对话未能完成。",
        **_message_pair(result),
    }


def _canonical_scope(series_ids: list[str]) -> list[str]:
    if not isinstance(series_ids, list):
        raise ValueError("series_ids must be a list")
    return sorted({canonical_uuid(item, "series_id") for item in series_ids})


def _positive_limit(value: int, *, maximum: int = 500) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 < value <= maximum
    ):
        raise ValueError(f"limit must be between 1 and {maximum}")
    return value
