from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from threading import Event
from time import monotonic
from typing import NotRequired, TypedDict

from .errors import AgentExecutionTimeoutError, AgentModelInvocationError


class AnswerPhase(StrEnum):
    QUEUED = "QUEUED"
    RETRIEVING = "RETRIEVING"
    EVALUATING = "EVALUATING"
    RESPONDING = "RESPONDING"


class RunInputs(TypedDict):
    conversation_id: str
    assistant_message_id: str
    tenant_id: str
    series_ids: list[str]
    series_context: list[dict]
    user_message: str
    image_urls: NotRequired[list[str]]
    history: list[dict]
    requested_top_k: int
    reference_context: NotRequired[dict | None]
    extra_context: NotRequired[dict | None]


@dataclass(slots=True)
class ExecutionControl:
    deadline: float | None
    cancellation: Event | None = None
    emit: Callable[[dict], None] | None = None

    def check(self) -> None:
        if self.cancellation is not None and self.cancellation.is_set():
            raise AgentModelInvocationError("Agent execution cancelled")
        if self.deadline is not None and monotonic() >= self.deadline:
            raise AgentExecutionTimeoutError("Agent execution deadline exceeded")

    def remaining_seconds(self) -> float | None:
        """Return the remaining budget, or None when no deadline is configured."""

        if self.cancellation is not None and self.cancellation.is_set():
            raise AgentModelInvocationError("Agent execution cancelled")
        if self.deadline is None:
            return None
        remaining = self.deadline - monotonic()
        if remaining <= 0:
            raise AgentExecutionTimeoutError("Agent execution deadline exceeded")
        return remaining

    def publish(self, inputs: RunInputs, event: dict) -> None:
        self.check()
        if self.emit is not None:
            self.emit({
                "conversationId": inputs["conversation_id"],
                "messageId": inputs["assistant_message_id"],
                **event,
            })


def context_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
