from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from threading import Event
from time import monotonic
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from ..schema import VideoChunkSearchResult

_Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20_000)]


class AgentModelInvocationError(RuntimeError):
    """A bounded execution could not produce an approved, valid answer."""


class AgentToolInvocationError(RuntimeError):
    """An injected tool failed; the application owns its cause's error mapping."""


class QueryDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_context: _Text
    message: _Text
    selected_video_chunk_ids: list[
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=36)]
    ] = Field(max_length=100)


class EvalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["complete", "continue"]
    advice: str = Field(max_length=2_000)

    @model_validator(mode="after")
    def require_revision_advice(self) -> EvalDecision:
        if self.action == "continue" and not self.advice.strip():
            raise ValueError("continue requires actionable advice")
        return self


class RunInputs(TypedDict):
    conversation_id: str
    assistant_message_id: str
    workspace_id: str
    series_ids: list[str]
    series_context: list[dict]
    user_message: str
    history: list[dict[str, str]]
    requested_top_k: int


@dataclass(frozen=True, slots=True)
class RetrievalCall:
    query: str
    top_k: int
    result: VideoChunkSearchResult

    def context(self) -> dict:
        return {
            "query": self.query,
            "top_k": self.top_k,
            "result": self.result.model_dump(mode="json", by_alias=True),
        }


@dataclass(frozen=True, slots=True)
class LoopTurn:
    number: int
    plan_context: str
    retrieval_calls: tuple[RetrievalCall, ...]
    retrieval_result: VideoChunkSearchResult
    evidence_source_turns: dict[str, int]
    query_draft: QueryDraft
    draft_output: VideoChunkSearchResult
    review: EvalDecision
    status: Literal["complete", "continue"]
    rendered_text: str = ""

    def context(self) -> dict:
        return {
            "number": self.number,
            "status": self.status,
            "plan_context": self.plan_context,
            "retrieval_calls": [call.context() for call in self.retrieval_calls],
            "retrieval_result": self.retrieval_result.model_dump(mode="json", by_alias=True),
            "evidence_source_turns": self.evidence_source_turns,
            "query_draft": self.query_draft.model_dump(mode="json"),
            "draft_output": self.draft_output.model_dump(mode="json", by_alias=True),
            "review": self.review.model_dump(mode="json"),
        }


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

    def publish(self, inputs: RunInputs, event: dict) -> None:
        self.check()
        if self.emit is not None:
            self.emit({
                "conversationId": inputs["conversation_id"],
                "messageId": inputs["assistant_message_id"],
                **event,
            })


class RunState(TypedDict):
    inputs: RunInputs
    turns: list[LoopTurn]
    control: ExecutionControl
    final_output: VideoChunkSearchResult


def context_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
