from __future__ import annotations

import logging
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC
from queue import Empty, Full, Queue
from threading import Event, Lock
from time import monotonic
from uuid import uuid4

from ..agent.runtime import AgentRuntime
from ..agent.state import (
    AgentContextBudgetError, AgentModelInvocationError, AgentToolInvocationError, RunInputs,
)
from ..config.agent import AgentServiceConfig
from ..identity import canonical_uuid
from ..model.dto.agentDTO import (
    AgentConversationState,
    AgentMessageSendState,
    AgentMessageState,
    AgentPageState,
)
from ..repository.agent import (
    AgentMessageInProgressError,
    AgentRepository, AgentReferenceInvalidError, AgentMessageNotFoundError,
)
from ..repository.video import SeriesNotReadyError, VideoChunkNotFoundError
from ..utils.time import utc_now_naive
from ..utils.validation import require_nonempty_text
from .library import LibraryService
from .pagination import decode_cursor, encode_cursor

_logger = logging.getLogger(__name__)


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
        self.reference_context: dict | None = None

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
                "occurredAt": int(utc_now_naive().replace(tzinfo=UTC).timestamp() * 1000),
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

    @property
    def maximum_top_k(self) -> int:
        return self._config.maximum_top_k

    @property
    def default_top_k(self) -> int:
        return self._config.default_top_k

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
        reference: dict[str, str] | None = None,
    ) -> AgentMessageSendState:
        return self.start_message(
            conversation_id, workspace_id=workspace_id, content=content,
            idempotency_key=idempotency_key, series_ids=series_ids, top_k=top_k, reference=reference,
        ).result()

    def start_message(
        self, conversation_id: str, *, workspace_id: str, content: str,
        idempotency_key: str, series_ids: list[str] | None = None,
        top_k: int | None = None, stream: bool = False,
        reference: dict[str, str] | None = None,
    ) -> AgentMessageExecution:
        normalized_workspace = require_nonempty_text(workspace_id, "workspace_id", maximum=64)
        normalized_conversation_id = canonical_uuid(conversation_id, "conversation_id")
        normalized_content = require_nonempty_text(content, "content", maximum=20_000)
        normalized_reference = _canonical_reference(reference)
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
                reference=normalized_reference,
            )
            if started is None:
                scope = self._resolve_scope(requested_scope, normalized_workspace)
                reference_context = self._load_reference(
                    normalized_reference, normalized_conversation_id, normalized_workspace, scope,
                )
                started = self._repository.start_message(
                    conversation_id=normalized_conversation_id,
                    workspace_id=normalized_workspace, content=normalized_content,
                    model_name=require_nonempty_text(self.model_name, "chat_model.model_name", maximum=255),
                    requested_top_k=requested_top_k, idempotency_key=normalized_key,
                    series_ids=scope, requested_series_ids=requested_scope,
                    user_message_id=str(uuid4()), assistant_message_id=str(uuid4()),
                    now=utc_now_naive(),
                    reference=normalized_reference,
                )
            if started.reused and started.assistant_message.status == "PROCESSING":
                raise AgentMessageInProgressError("the idempotent message is still being processed")
            initial = AgentMessageSendState(started.user_message, started.assistant_message)
            execution = AgentMessageExecution(initial, stream=stream)
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
            return {"reference": reference, "sourceStatus": source.status,
                    "selectionReason": hit["selectionReason"],
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
    ) -> AgentMessageSendState:
        initial = execution.initial
        assistant_id = initial.assistant_message.message_id
        conversation_id = initial.assistant_message.conversation_id
        chunks: list[str] = []
        last_checkpoint = monotonic()

        def publish_runtime(event):
            nonlocal last_checkpoint
            event_type = event["type"]
            checkpoint = {}
            if event_type == "answer.progress":
                checkpoint["phase"] = event["phase"]
            elif event_type == "answer.evidence":
                checkpoint["search_result"] = event["searchResult"]
            elif event_type == "answer.delta":
                chunks.append(event["delta"])
                if monotonic() - last_checkpoint >= 1:
                    checkpoint["content"] = "".join(chunks)
            if checkpoint:
                self._repository.checkpoint_assistant_message(assistant_id, now=utc_now_naive(), **checkpoint)
                last_checkpoint = monotonic()
            execution.publish(event)
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
                    "messageId": item.message_id, "status": item.status,
                    "reference": item.reference,
                    "searchResult": item.search_result,
                } for item in history],
                reference_context=execution.reference_context,
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
                    inputs, emit=publish_runtime, cancellation=execution.cancellation,
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
            _logger.exception(
                "Agent execution failed conversation_id=%s message_id=%s code=%s",
                conversation_id, assistant_id, public_code,
            )
            failed = None
            try:
                if chunks:
                    self._repository.checkpoint_assistant_message(
                        assistant_id, content="".join(chunks), now=utc_now_naive(),
                    )
                failed = self._repository.fail_assistant_message(
                    assistant_id, error=f"{type(exc).__name__}: {str(exc).strip()}"[:8192],
                    now=utc_now_naive(),
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
        self._executor.shutdown(wait=True)


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


def _canonical_reference(reference):
    if reference is None:
        return None
    if not isinstance(reference, dict) or set(reference) != {"messageId", "videoChunkId"}:
        raise AgentReferenceInvalidError("引用需要 messageId 和 videoChunkId。")
    try:
        return {name: canonical_uuid(value, name) for name, value in reference.items()}
    except ValueError as exc:
        raise AgentReferenceInvalidError("引用的消息或片段 ID 无效。") from exc


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
