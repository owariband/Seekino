from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from threading import RLock
from typing import Any

from langchain_core.tools import BaseTool

from gateway.agent_model.contracts import SupervisorSessionContext
from gateway.media import prepare_input_messages, require_vision_input

from .agent import CodexAgentModel, SessionLike


_logger = logging.getLogger(__name__)


@dataclass
class _PendingTurn:
    message_id: str
    confirm: Callable[[str], None] | None = None


def _completed_messages(history: Sequence[dict[str, Any]], *, include_ids: bool = False) -> list[dict[str, Any]]:
    messages = []
    seen: set[str] = set()
    for index, item in enumerate(history):
        role = str(item.get("role", "")).lower()
        status = str(item.get("status", "COMPLETED")).upper()
        content = item.get("content")
        identity = str(item.get("messageId") or item.get("id") or f"history:{index}")
        if role not in {"user", "assistant"} or status != "COMPLETED" or not isinstance(content, (str, list)) or identity in seen:
            continue
        seen.add(identity)
        message: dict[str, Any] = {"role": role, "content": content}
        if include_ids:
            message["messageId"] = identity
        if role == "user" and item.get("imageUrls"):
            message["imageUrls"] = item["imageUrls"]
        messages.append(message)
    return prepare_input_messages("", initial_messages=messages) if messages else []


class CodexSupervisorModel:
    """Resume the native thread and append missing committed conversation messages."""

    def __init__(self, *, agent_model: CodexAgentModel) -> None:
        self._agent_model = agent_model
        self.model_name = agent_model.model_name
        self.provider_name = agent_model.provider_name
        self.supports_vision_input = agent_model.supports_vision_input
        self._pending: dict[str, _PendingTurn] = {}
        self._lock = RLock()
        self._closed = False

    def run_agent(
        self, prompt: str, *, thread_id: str, message_id: str, user_message: str,
        history: Sequence[dict[str, Any]], tools: Sequence[BaseTool],
        max_model_calls: int | None, handle_text: Callable[[str], None],
        request_timeout_seconds: float | None = None,
        check_control: Callable[[], None] | None = None,
        max_input_chars: int | None = None,
        image_urls: Sequence[str] = (),
        session_context: SupervisorSessionContext | None = None,
    ) -> None:
        if not prompt.strip() or not thread_id or not message_id or not (user_message.strip() or image_urls):
            raise ValueError("prompt, thread_id, message_id and user_message are required")
        with self._lock:
            if self._closed:
                raise RuntimeError("Codex Supervisor is closed")
            if thread_id in self._pending:
                raise RuntimeError("Supervisor previous turn has not been confirmed")
            pending = _PendingTurn(message_id)
            self._pending[thread_id] = pending
        try:
            messages = _completed_messages(history)
            if image_urls and session_context is None:
                messages.append({"role": "user", "content": user_message})
            require_vision_input(
                prepare_input_messages(prompt, initial_messages=messages or None, image_urls=image_urls),
                supports_vision_input=self.supports_vision_input, model_name=self.model_name,
            )
            if session_context is not None:
                messages = []
            native_options = {}
            native_text: list[str] = []

            def accept_text(text: str) -> None:
                native_text.append(text)
                handle_text(text)

            if session_context is not None:
                key = self._agent_model.session_key
                state = deepcopy(session_context.states.get(key, {}))

                def save_state(snapshot: dict[str, Any]) -> None:
                    try:
                        session_context.save_state(key, deepcopy(snapshot))
                    except Exception:
                        _logger.warning(
                            "event=codex_session_save_failed conversation_id=%s message_id=%s",
                            thread_id, message_id, exc_info=True,
                        )

                def prepare(session: SessionLike) -> None:
                    def check_session() -> None:
                        if check_control is not None:
                            check_control()
                        # A model deadline closes the session independently of host cancellation.
                        session.open()

                    def load_history(after: int) -> list[dict[str, Any]] | None:
                        try:
                            result = list(session_context.load_history(after))
                        except Exception:
                            check_session()
                            _logger.warning(
                                "event=codex_history_load_failed conversation_id=%s message_id=%s",
                                thread_id, message_id, exc_info=True,
                            )
                            return None
                        check_session()
                        return result

                    check_session()
                    if not self.supports_vision_input and state.get("threadId"):
                        committed_history = load_history(-1)
                        if committed_history is None:
                            raise ValueError(f"Cannot verify committed history image input for model {self.model_name!r} (supports_vision_input=False)")
                        require_vision_input(_completed_messages(committed_history), supports_vision_input=False, model_name=self.model_name)
                    state["threadId"] = session.native_thread_id
                    # Older versions blocked on an uncertain batch. Retry from the confirmed cursor.
                    state.pop("pendingSync", None)
                    # Save a new binding before inference can fail or be cancelled.
                    save_state(state)
                    check_session()
                    cursor = state.get("syncedThroughSequence", -1)
                    missing = load_history(cursor)
                    history_synced = missing is not None
                    can_reconcile = True
                    previous = state.pop("pendingTurn", None)
                    if previous is not None and missing is not None:
                        committed = next((item for item in missing if item.get("messageId") == previous["messageId"]), None)
                        has_native_turn = session.native_last_turn_id != previous["previousTurnId"]
                        if committed is None and has_native_turn:
                            # Remove an uncommitted native turn when reconciliation is available.
                            try:
                                session.rollback_last_turn()
                            except Exception:
                                check_session()
                                _logger.warning(
                                    "event=codex_history_reconcile_failed conversation_id=%s message_id=%s",
                                    thread_id, message_id, exc_info=True,
                                )
                                history_synced = False
                                can_reconcile = False
                            check_session()
                    additions = _completed_messages(missing or [], include_ids=True)
                    if additions:
                        require_vision_input(additions, supports_vision_input=self.supports_vision_input, model_name=self.model_name)
                        target = max(item["sequenceNo"] for item in missing or [])
                        check_session()
                        try:
                            session.inject_messages(additions)
                        except Exception:
                            check_session()
                            _logger.warning(
                                "event=codex_history_inject_failed conversation_id=%s message_id=%s count=%s",
                                thread_id, message_id, len(additions), exc_info=True,
                            )
                            history_synced = False
                        else:
                            cursor = target
                        check_session()
                    state["syncedThroughSequence"] = cursor
                    # An unconfirmed revert leaves the cached last-turn ID unreliable.
                    if can_reconcile:
                        state["pendingTurn"] = {
                            "messageId": message_id,
                            "previousTurnId": session.native_last_turn_id,
                        }
                    check_session()
                    save_state(state)
                    check_session()

                    def confirm(content: str) -> None:
                        draft = "\n\n".join(text.strip() for text in native_text if text.strip())
                        confirmed = dict(state)
                        if history_synced:
                            cursor = session_context.message_sequence_no
                            if draft != content.strip():
                                cursor -= 1  # Append the canonical SQL assistant message on resume.
                            confirmed["syncedThroughSequence"] = cursor
                        confirmed.pop("pendingTurn", None)
                        save_state(confirmed)

                    pending.confirm = confirm

                native_options = {"native_thread_id": state.get("threadId"), "on_session_ready": prepare}
            turn_prompt = prompt + "\n\nCurrent user message:\n" + json.dumps(user_message, ensure_ascii=False)
            self._agent_model.run_agent_loop(
                turn_prompt,
                initial_messages=messages or None,
                image_urls=image_urls,
                tools=tools, max_model_calls=max_model_calls,
                handle_text=accept_text if session_context is not None else handle_text,
                request_timeout_seconds=request_timeout_seconds,
                check_control=check_control, max_input_chars=max_input_chars,
                **native_options,
            )
        except BaseException:
            self.discard_turn(thread_id, message_id)
            raise

    def confirm_turn(self, thread_id: str, message_id: str, content: str) -> None:
        with self._lock:
            pending = self._pending.get(thread_id)
            if pending is not None and pending.message_id == message_id:
                if pending.confirm is not None:
                    pending.confirm(content)
                self._pending.pop(thread_id)

    def discard_turn(self, thread_id: str, message_id: str) -> None:
        with self._lock:
            pending = self._pending.get(thread_id)
            if pending is not None and pending.message_id == message_id:
                self._pending.pop(thread_id)

    def delete_thread(self, thread_id: str) -> None:
        with self._lock:
            self._pending.pop(thread_id, None)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._pending.clear()
        close = getattr(self._agent_model, "close", None)
        if close is not None:
            close()
