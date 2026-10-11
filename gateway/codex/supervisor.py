from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from threading import RLock
from typing import Any

from langchain_core.tools import BaseTool

from gateway.agent_model.contracts import AgentLoopModel


class CodexSupervisorModel:
    """Run a fresh native thread per turn, rebuilding history from committed SQL."""

    def __init__(self, *, agent_model: AgentLoopModel) -> None:
        self._agent_model = agent_model
        self.model_name = agent_model.model_name
        self.provider_name = agent_model.provider_name
        self.supports_vision_input = agent_model.supports_vision_input
        self._pending: dict[str, str] = {}
        self._lock = RLock()
        self._closed = False

    def run_agent(
        self, prompt: str, *, thread_id: str, message_id: str, user_message: str,
        history: Sequence[dict[str, Any]], tools: Sequence[BaseTool],
        max_model_calls: int | None, handle_text: Callable[[str], None],
        request_timeout_seconds: float | None = None,
        check_control: Callable[[], None] | None = None,
        max_input_chars: int | None = None,
    ) -> None:
        if not prompt.strip() or not thread_id or not message_id or not user_message.strip():
            raise ValueError("prompt, thread_id, message_id and user_message are required")
        with self._lock:
            if self._closed:
                raise RuntimeError("Codex Supervisor is closed")
            if thread_id in self._pending:
                raise RuntimeError("Supervisor previous turn has not been confirmed")
            self._pending[thread_id] = message_id
        try:
            messages: list[dict[str, str]] = []
            seen: set[str] = set()
            for index, item in enumerate(history):
                role = str(item.get("role", "")).lower()
                status = str(item.get("status", "COMPLETED")).upper()
                content = item.get("content")
                identity = str(item.get("messageId") or item.get("id") or f"history:{index}")
                if role not in {"user", "assistant"} or status != "COMPLETED" or not isinstance(content, str) or identity in seen:
                    continue
                seen.add(identity)
                messages.append({"role": role, "content": content})
            # Native state ends with this invocation; only SQL-confirmed history
            # supplied by the next call can carry an answer into another turn.
            self._agent_model.run_agent_loop(
                prompt + "\n\nCurrent user message:\n" + json.dumps(user_message, ensure_ascii=False),
                initial_messages=messages or None,
                tools=tools, max_model_calls=max_model_calls,
                handle_text=handle_text,
                request_timeout_seconds=request_timeout_seconds,
                check_control=check_control, max_input_chars=max_input_chars,
            )
        except BaseException:
            self.discard_turn(thread_id, message_id)
            raise

    def confirm_turn(self, thread_id: str, message_id: str, content: str) -> None:
        # The stored final answer can differ from the draft; no native history
        # survives to require rewriting it here.
        with self._lock:
            if self._pending.get(thread_id) == message_id:
                self._pending.pop(thread_id)

    def discard_turn(self, thread_id: str, message_id: str) -> None:
        with self._lock:
            if self._pending.get(thread_id) == message_id:
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
