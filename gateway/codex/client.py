from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from contextvars import copy_context
from math import isfinite
from queue import Empty, Queue
from threading import Lock, Thread as WorkerThread
from typing import Any, Protocol
from uuid import uuid4

from langchain_core.tools import BaseTool
from openai_codex import ApprovalMode, CodexConfig, RunInput, Sandbox, Thread, TurnResult
from openai_codex.client import CodexClient
from openai_codex.errors import CodexError
from openai_codex.generated.v2_all import (
    AgentMessageThreadItem, ErrorNotification, ItemCompletedNotification, ThreadResumeResponse,
    ReasoningEffort as CodexReasoningEffort,
)
from openai_codex.models import Notification
from pydantic import BaseModel

from gateway.media import prepare_input_messages

from .protocol import CodexToolBridge, dynamic_tool_specs


_logger = logging.getLogger(__name__)


_BASE_INSTRUCTIONS = """\
You are a bounded AniVAG reasoning component. Use only the dynamic tools supplied
by the host for external actions. Do not use shell commands, file access, MCP,
apps, plugins, browser tools, web search, user-input tools, or permission tools.
Return the requested answer or structured result directly.
"""

_DISABLED_FEATURE_OVERRIDES = (
    "mcp_servers={}",
    'tools.disabled_tools=["apply_patch","exec","exec_command",'
    '"image_generation","request_user_input","shell","spawn_agent",'
    '"view_image","web_search","write_stdin"]',
    "features.apps=false",
    "features.browser_use=false",
    "features.browser_use_external=false",
    "features.computer_use=false",
    "features.goals=false",
    "features.image_generation=false",
    "features.in_app_browser=false",
    "features.multi_agent=false",
    "features.plugins=false",
    "features.shell_tool=false",
    "features.skill_search=false",
    "features.sleep_tool=false",
    "features.unified_exec=false",
    "features.unbounded_connection_retries=false",
    "features.view_image=false",
)


class CodexClientLike(Protocol):
    def start(self) -> None: ...

    def initialize(self) -> object: ...

    def thread_start(self, params: dict[str, Any]) -> object: ...

    def thread_resume(self, thread_id: str, params: dict[str, Any] | None = None) -> object: ...

    def request(
        self, method: str, params: dict[str, Any], *, response_model: type[BaseModel],
    ) -> BaseModel: ...

    def request_history(
        self, method: str, params: dict[str, Any], *,
        response_model: type[BaseModel], timeout_seconds: float,
    ) -> BaseModel: ...

    def close(self) -> None: ...


ClientFactory = Callable[
    [CodexConfig, Callable[[str, dict[str, Any] | None], dict[str, Any]]],
    CodexClientLike,
]


class CodexModelCallLimitError(RuntimeError):
    """Continuing a Codex session would exceed its model-call budget."""


class _EmptyResponse(BaseModel):
    """Accept an RPC acknowledgement whose payload is not consumed."""


class CodexModelCallBudget:
    """Conservatively reserve every observable Codex sampling round.

    App-server has no notification that can gate each internal inference before
    it starts. One outer turn therefore reserves its initial inference, and each
    dynamic tool request reserves a possible continuation before receiving a
    tool result. Dynamic tool ``turnId`` identifies the whole outer turn and is
    reused by sequential sampling rounds, so it cannot safely distinguish a
    parallel tool batch. Parallel calls may therefore overcount; this is safer
    than allowing the configured model-call ceiling to be exceeded.
    """

    def __init__(self, limit: int | None) -> None:
        if limit is not None and (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit <= 0
        ):
            raise ValueError("model call limit must be a positive integer")
        self._limit = limit
        self._used = 0
        self._lock = Lock()

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    def reserve(self) -> None:
        with self._lock:
            if self._limit is not None and self._used >= self._limit:
                raise CodexModelCallLimitError(
                    "Codex exhausted max_model_calls before completion"
                )
            self._used += 1


def codex_reasoning_effort(value: str) -> CodexReasoningEffort:
    """Use an explicit app-server effort, including the real `none` value."""
    try:
        return CodexReasoningEffort(value)
    except ValueError as exc:
        raise ValueError(f"unsupported Codex reasoning effort: {value!r}") from exc


class _ObservedCodexClient(CodexClient):
    # SDK 0.156.1 has no public notification callback. Observe on its sole
    # reader before routing, so notifications and tool requests share an order.
    notification_handler: Callable[[Notification], None] | None = None

    def _coerce_notification(self, method: str, params: object) -> Notification:
        notification = super()._coerce_notification(method, params)
        if self.notification_handler is not None:
            self.notification_handler(notification)
        return notification

    def request_history(
        self, method: str, params: dict[str, Any], *,
        response_model: type[BaseModel], timeout_seconds: float,
    ) -> BaseModel:
        # SDK 0.156.1 request() waits indefinitely and exposes no timeout.
        # Reuse its router/transport only for optional history delivery.
        if method not in {"thread/inject_items", "thread/revert", "thread/resume"}:
            raise ValueError("Only history maintenance supports a bounded response wait")
        request_id = str(uuid4())
        waiter = self._router.create_response_waiter(request_id)
        try:
            self._write_message({"id": request_id, "method": method, "params": params})
            try:
                result = waiter.get(timeout=timeout_seconds)
            except Empty as exc:
                raise TimeoutError(f"Codex history request timed out: {method}") from exc
        finally:
            # SDK ignores late responses once their waiter is removed.
            self._router.discard_response_waiter(request_id)
        if isinstance(result, BaseException):
            raise result
        if not isinstance(result, dict):
            raise CodexError(f"{method} response must be a JSON object")
        return response_model.model_validate(result)


def _default_client_factory(
    config: CodexConfig,
    request_handler: Callable[[str, dict[str, Any] | None], dict[str, Any]],
) -> CodexClientLike:
    return _ObservedCodexClient(config=config, approval_handler=request_handler)


def _thread_id(response: object) -> str:
    thread = (
        response.get("thread")
        if isinstance(response, dict)
        else getattr(response, "thread", None)
    )
    value = (
        thread.get("id")
        if isinstance(thread, dict)
        else getattr(thread, "id", None)
    )
    if not isinstance(value, str) or not value:
        raise RuntimeError("Codex thread/start returned no thread id")
    return value


class CodexSession:
    """Own one app-server process and an ephemeral or resumed Codex thread."""

    def __init__(
        self,
        *,
        model: str,
        tools: Sequence[BaseTool],
        reasoning_effort: str,
        codex_bin: str | None,
        cwd: str | None,
        max_tool_calls: int | None,
        check_control: Callable[[], None] | None,
        max_model_calls: int | None = None,
        client_factory: ClientFactory = _default_client_factory,
        handle_text: Callable[[str], None] | None = None,
        native_thread_id: str | None = None,
        persistent: bool = False,
        history_request_timeout_seconds: float = 2.0,
    ) -> None:
        if not isfinite(history_request_timeout_seconds) or history_request_timeout_seconds <= 0:
            raise ValueError("history_request_timeout_seconds must be finite and positive")
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._cwd = cwd
        self._persistent = persistent
        self._history_request_timeout_seconds = history_request_timeout_seconds
        self._native_thread_id = native_thread_id
        self._native_last_turn_id: str | None = None
        self._tool_specs = dynamic_tool_specs(tools)
        self._model_call_budget = CodexModelCallBudget(max_model_calls)
        self._tool_bridge = CodexToolBridge(
            tools,
            check_control=check_control,
            max_tool_calls=max_tool_calls,
            before_tool_call=self._model_call_budget.reserve,
        )
        self._tool_context = copy_context()
        self._config = CodexConfig(
            codex_bin=codex_bin,
            config_overrides=_DISABLED_FEATURE_OVERRIDES,
            cwd=cwd,
            experimental_api=True,
        )
        self._client_factory = client_factory
        self._client: CodexClientLike | None = None
        self._thread: Thread | None = None
        self._active_turn: object | None = None
        self.last_turn: TurnResult | None = None
        self._closed = False
        self._lock = Lock()
        self._handle_text = handle_text
        self._callbacks: Queue[tuple[Future, Callable, tuple] | None] = Queue()
        self._pending_callbacks: set[Future] = set()
        self._callback_error: BaseException | None = None
        self._turn_errors: dict[str, RuntimeError] = {}
        self._dispatcher: WorkerThread | None = None
        self._visible_messages: set[tuple[str, str]] = set()

    @property
    def tool_call_count(self) -> int:
        return self._tool_bridge.tool_call_count

    @property
    def successful_tool_call_count(self) -> int:
        return self._tool_bridge.successful_tool_call_count

    @property
    def native_thread_id(self) -> str | None:
        return self._native_thread_id

    @property
    def native_last_turn_id(self) -> str | None:
        return self._native_last_turn_id

    def __enter__(self) -> CodexSession:
        self.open()
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def open(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("Codex session is closed")
            if self._client is not None:
                return
            client = self._client_factory(
                self._config,
                self._handle_request,
            )
            self._client = client
            client.notification_handler = self._handle_notification  # type: ignore[attr-defined]
            if self._handle_text is not None:
                # Start before turn/start: the reader can receive tools before
                # its response. Never borrow the model's bounded executor here.
                self._dispatcher = WorkerThread(
                    target=self._run_callbacks, name="anivag-codex-events", daemon=True,
                )
                self._dispatcher.start()
        try:
            client.start()
            client.initialize()
            if self._persistent and self._native_thread_id is not None:
                started = client.thread_resume(self._native_thread_id, self._thread_options())
            else:
                started = client.thread_start({
                    **self._thread_options(),
                    "dynamicTools": self._tool_specs,
                    "ephemeral": not self._persistent,
                })
            self._load_thread(client, started)
        except BaseException:
            self.close()
            raise

    def _thread_options(self) -> dict[str, Any]:
        return {
            "approvalPolicy": "never",
            "baseInstructions": _BASE_INSTRUCTIONS,
            "cwd": self._cwd,
            "developerInstructions": _BASE_INSTRUCTIONS,
            "model": self._model,
            "sandbox": "read-only",
        }

    def _load_thread(self, client: CodexClientLike, response: object) -> None:
        thread_id = _thread_id(response)
        native_thread = response.get("thread") if isinstance(response, dict) else getattr(response, "thread", None)
        turns = native_thread.get("turns", []) if isinstance(native_thread, dict) else getattr(native_thread, "turns", [])
        last_turn = turns[-1] if turns else None
        last_id = last_turn.get("id") if isinstance(last_turn, dict) else getattr(last_turn, "id", None)
        with self._lock:
            if self._closed:
                raise RuntimeError("Codex session was closed while starting")
            self._thread = Thread(client, thread_id)  # type: ignore[arg-type]
            self._native_thread_id = thread_id
            self._native_last_turn_id = last_id

    def inject_messages(self, messages: Sequence[dict[str, Any]]) -> None:
        """Append committed host messages without replaying a model turn."""
        if not messages:
            return
        items = []
        for message in prepare_input_messages("", initial_messages=messages):
            role = str(message["role"]).lower()
            if role not in {"user", "assistant"}:
                raise ValueError("Codex history injection accepts only user and assistant messages")
            blocks = message["content"]
            if isinstance(blocks, str):
                blocks = [{"type": "text", "text": blocks}]
            content = []
            for block in blocks:
                if block["type"] in {"text", "input_text", "output_text"}:
                    content.append({"type": "input_text" if role == "user" else "output_text", "text": block["text"]})
                elif role == "user" and block["type"] in {"image_url", "input_image"}:
                    image = block["image_url"]
                    content.append({"type": "input_image", "image_url": image["url"] if isinstance(image, dict) else image})
                else:
                    raise ValueError(f"Unsupported Codex history block: {block['type']}")
            item = {"type": "message", "role": role, "content": content}
            if message.get("messageId"):
                item["id"] = message["messageId"]
            items.append(item)
        self.open()
        with self._lock:
            if self._closed or self._client is None or self._thread is None or self._active_turn is not None:
                raise RuntimeError("Codex session is unavailable for history injection")
            client = self._client
            thread_id = self._thread.id
        # The pinned SDK exposes request(), but does not generate inject_items.
        client.request_history("thread/inject_items", {
            "threadId": thread_id, "items": items,
        }, response_model=_EmptyResponse, timeout_seconds=self._history_request_timeout_seconds)

    def rollback_last_turn(self) -> None:
        """Discard an uncommitted native turn, then reload the same thread."""
        self.open()
        with self._lock:
            if self._closed or self._client is None or self._thread is None or self._active_turn is not None:
                raise RuntimeError("Codex session is unavailable for turn rollback")
            client = self._client
            thread_id, turn_id = self._thread.id, self._native_last_turn_id
        if turn_id is None:
            return
        # CLI 0.156.1 calls this revert; beforeTurnId excludes that turn.
        client.request_history("thread/revert", {
            "threadId": thread_id, "beforeTurnId": turn_id,
        }, response_model=_EmptyResponse, timeout_seconds=self._history_request_timeout_seconds)
        resumed = client.request_history("thread/resume", {
            **self._thread_options(), "threadId": thread_id,
        }, response_model=ThreadResumeResponse, timeout_seconds=self._history_request_timeout_seconds)
        self._load_thread(client, resumed)

    def _handle_request(
        self, method: str, params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if self._handle_text is not None:
            return self._dispatch(self._tool_bridge.handle, method, params).result()
        # SDK reader threads do not inherit the host's execution scope.
        return self._tool_context.copy().run(self._tool_bridge.handle, method, params)

    def _handle_notification(self, event: Notification) -> None:
        payload = event.payload
        if isinstance(payload, ErrorNotification):
            info = payload.error.codex_error_info
            details = info.model_dump(mode="json", by_alias=True) if info is not None else None
            http_status = None
            if isinstance(details, dict):
                for detail in details.values():
                    if isinstance(detail, dict):
                        http_status = detail.get("httpStatusCode")
            fatal = not payload.will_retry or http_status in (401, 403)
            with self._lock:
                if self._closed or self._thread is None or payload.thread_id != self._thread.id:
                    return
                if fatal:
                    self._turn_errors.setdefault(payload.turn_id, RuntimeError(payload.error.message))
            _logger.warning(
                "codex_turn_error thread_id=%s turn_id=%s will_retry=%s http_status=%s fatal=%s",
                payload.thread_id, payload.turn_id, payload.will_retry, http_status, fatal,
            )
            return
        if self._handle_text is None or not isinstance(payload, ItemCompletedNotification):
            return
        item = payload.item.root
        key = (payload.turn_id, item.id)
        if (
            isinstance(item, AgentMessageThreadItem) and item.text.strip()
            and key not in self._visible_messages
        ):
            self._visible_messages.add(key)
            self._dispatch(self._handle_text, item.text)

    def raise_if_failed(self) -> None:
        # Notifications can precede the turn/start response. Match only after
        # its ID is known, so an early error is retained and an old turn ignored.
        with self._lock:
            error = self._turn_errors.get(getattr(self._active_turn, "id", None))
        if error is not None:
            raise error

    def _dispatch(self, callback: Callable, *args: Any) -> Future:
        future: Future = Future()
        with self._lock:
            if self._callback_error is not None:
                raise self._callback_error
            if self._closed:
                raise RuntimeError("Codex session is closed")
            self._pending_callbacks.add(future)
            self._callbacks.put_nowait((future, callback, args))
        return future

    def _run_callbacks(self) -> None:
        while (work := self._callbacks.get()) is not None:
            future, callback, args = work
            if future.done():
                continue
            try:
                result = self._tool_context.copy().run(callback, *args)
            except BaseException as exc:
                with self._lock:
                    self._callback_error = exc
                    if not future.done():
                        future.set_exception(exc)
                self.close()
                return
            else:
                with self._lock:
                    if not future.done():
                        future.set_result(result)
            finally:
                with self._lock:
                    self._pending_callbacks.discard(future)

    def run_turn(
        self,
        prompt: RunInput,
        *,
        output_schema: dict[str, Any] | None = None,
    ) -> TurnResult:
        # Starting an outer turn always consumes one sampling round. Each tool
        # call reserves a later continuation conservatively in CodexToolBridge.
        self._model_call_budget.reserve()
        self.open()
        with self._lock:
            if self._closed or self._thread is None:
                raise RuntimeError("Codex session is unavailable")
            thread = self._thread
        turn = None
        try:
            turn = thread.turn(
                prompt,
                approval_mode=ApprovalMode.deny_all,
                effort=codex_reasoning_effort(self._reasoning_effort),
                model=self._model,
                output_schema=output_schema,
                sandbox=Sandbox.read_only,
            )
            with self._lock:
                if self._closed:
                    raise RuntimeError("Codex session closed while starting a turn")
                self._active_turn = turn
                self._native_last_turn_id = getattr(turn, "id", self._native_last_turn_id)
            self.raise_if_failed()
            result = turn.run()
            self.raise_if_failed()
            self.last_turn = result
            if self._handle_text is not None:
                self._dispatch(lambda: None).result()
            return result
        except BaseException:
            if self._callback_error is not None:
                raise self._callback_error
            raise
        finally:
            with self._lock:
                if self._active_turn is turn:
                    self._active_turn = None

    def interrupt(self) -> None:
        """Hard-cancel without waiting for the app-server reader thread.

        The SDK dispatches dynamic tools on its sole JSON-RPC reader thread.
        Asking an active turn to interrupt can therefore wait forever when that
        reader is blocked inside a tool. Closing the client terminates the
        transport first and is the only safe cancellation primitive here.
        """
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            client = self._client
            self._client = None
            self._thread = None
            for future in self._pending_callbacks:
                if not future.done():
                    future.set_exception(RuntimeError("Codex session is closed"))
            self._pending_callbacks.clear()
            if self._dispatcher is not None:
                self._callbacks.put_nowait(None)
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
