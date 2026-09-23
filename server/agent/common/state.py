from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from threading import Event
from time import monotonic
from typing import NotRequired, TypedDict

from .errors import AgentModelInvocationError


class AnswerPhase(StrEnum):
    QUEUED = "QUEUED"
    RETRIEVING = "RETRIEVING"
    EVALUATING = "EVALUATING"
    RESPONDING = "RESPONDING"


class RunInputs(TypedDict):
    conversation_id: str
    assistant_message_id: str
    workspace_id: str
    series_ids: list[str]
    series_context: list[dict]
    user_message: str
    history: list[dict]
    requested_top_k: int
    reference_context: NotRequired[dict | None]
    extra_context: NotRequired[dict | None]


@dataclass(slots=True)
class ExecutionControl:
    deadline: float
    cancellation: Event | None = None
    emit: Callable[[dict], None] | None = None

    def check(self) -> None:
        if self.cancellation is not None and self.cancellation.is_set():
            raise AgentModelInvocationError("Agent execution cancelled")
        if monotonic() >= self.deadline:
            raise AgentModelInvocationError("Agent execution deadline exceeded")

    def remaining_seconds(self) -> float:
        """Return the current Provider budget without extending the Kick deadline."""

        if self.cancellation is not None and self.cancellation.is_set():
            raise AgentModelInvocationError("Agent execution cancelled")
        remaining = self.deadline - monotonic()
        if remaining <= 0:
            raise AgentModelInvocationError("Agent execution deadline exceeded")
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
