from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from threading import Event
from time import monotonic
from typing import Any, Annotated, Literal, NotRequired, TypedDict

from pydantic import (
    BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator,
)

from ..constants.retrieval import AGENT_RETRIEVAL_STRATEGY_VERSION
from ..schema import ContractModel, VideoChunkSearchResult
from .errors import AgentContextBudgetError, AgentModelInvocationError

_Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20_000)]
_EvidenceClaim = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000),
]
_VideoChunkId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=36),
]
_Markdown = Annotated[str, StringConstraints(min_length=1, max_length=20_000)]


class AnswerPhase(StrEnum):
    QUEUED = "QUEUED"
    RETRIEVING = "RETRIEVING"
    EVALUATING = "EVALUATING"
    RESPONDING = "RESPONDING"


class SelectedVideoRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_chunk_id: _VideoChunkId
    start_ms: int = Field(strict=True, ge=0)
    end_ms: int = Field(strict=True, gt=0)

    @model_validator(mode="after")
    def validate_time_range(self) -> SelectedVideoRange:
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms must be greater than start_ms")
        return self


class QueryDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_context: _Text
    message: _Text
    selected_video_ranges: list[SelectedVideoRange]


class QueryCompletion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_context: _Text
    message: _Text


class DraftItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_chunk_id: _VideoChunkId
    start_ms: int = Field(strict=True, ge=0)
    end_ms: int = Field(strict=True, gt=0)
    claim: _EvidenceClaim

    @model_validator(mode="after")
    def validate_time_range(self) -> DraftItem:
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms must be greater than start_ms")
        return self


class RetrievalChannelWeights(BaseModel):
    """Agent-selected relative weights for the currently available channels."""

    model_config = ConfigDict(extra="forbid")

    transcript: float = Field(
        default=0.0,
        ge=0,
        le=1,
        allow_inf_nan=False,
        description=(
            "Relative weight of literal ASR phrase ranking. Raise it for exact "
            "quotes, distinctive wording, names and short spoken phrases."
        ),
    )
    semantic: float = Field(
        ge=0,
        le=1,
        allow_inf_nan=False,
        description=(
            "Relative weight of the Pro Semantic text-vector ranking. "
            "Raise it for plot, events, relationships and dialogue meaning."
        ),
    )
    visual: float = Field(
        ge=0,
        le=1,
        allow_inf_nan=False,
        description=(
            "Relative weight of the Video Vision ranking. Raise it for visible "
            "objects, actions, colors, locations and shot composition."
        ),
    )

    @field_validator("transcript", "semantic", "visual", mode="before")
    @classmethod
    def reject_boolean_weights(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("channel weights must be numbers, not booleans")
        return value

    @model_validator(mode="after")
    def require_enabled_channel(self) -> "RetrievalChannelWeights":
        if self.transcript == 0 and self.semantic == 0 and self.visual == 0:
            raise ValueError("at least one channel weight must be greater than zero")
        return self


class MarkdownRenderBlock(ContractModel):
    type: Literal["markdown"] = "markdown"
    markdown: _Markdown


class VideoRenderBlock(ContractModel):
    type: Literal["video"] = "video"
    video_chunk_id: _VideoChunkId
    start_ms: int = Field(strict=True, ge=0)
    end_ms: int = Field(strict=True, gt=0)

    @model_validator(mode="after")
    def validate_time_range(self) -> VideoRenderBlock:
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms must be greater than start_ms")
        return self


class ChatVideoBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["video"] = "video"
    video_chunk_id: _VideoChunkId
    claim: _EvidenceClaim


ChatRenderBlock = Annotated[
    MarkdownRenderBlock | ChatVideoBlock,
    Field(discriminator="type"),
]
FinalRenderBlock = Annotated[
    MarkdownRenderBlock | VideoRenderBlock,
    Field(discriminator="type"),
]


class ChatRenderDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    blocks: list[ChatRenderBlock] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_blocks(self) -> ChatRenderDraft:
        answer_markdown = self.answer_markdown
        if not answer_markdown.strip():
            raise ValueError("Chat render requires at least one nonempty Markdown block")
        if len(answer_markdown) > 20_000:
            raise ValueError("Chat Markdown exceeds 20000 characters")
        ids = [item.video_chunk_id for item in self.video_items]
        if len(ids) != len(set(ids)):
            raise ValueError("video_items must not contain duplicate video_chunk_id values")
        return self

    @property
    def answer_markdown(self) -> str:
        return "\n\n".join(
            block.markdown
            for block in self.blocks
            if isinstance(block, MarkdownRenderBlock)
        )

    @property
    def video_items(self) -> list[ChatVideoBlock]:
        return [
            block for block in self.blocks
            if isinstance(block, ChatVideoBlock)
        ]


class RetrievalTraceEntry(ContractModel):
    query: _Text
    top_k: int = Field(ge=1, le=100)
    strategy_version: str = Field(min_length=1, max_length=255)
    requested_weights: RetrievalChannelWeights
    effective_weights: RetrievalChannelWeights
    result_count: int = Field(ge=0)
    video_chunk_ids: list[_VideoChunkId] = Field(max_length=100)


class AgentFinalOutput(VideoChunkSearchResult):
    render_blocks: list[FinalRenderBlock] = Field(min_length=1, max_length=200)
    retrieval_trace: list[RetrievalTraceEntry] = Field(min_length=1)


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
    history: list[dict]
    requested_top_k: int
    reference_context: NotRequired[dict | None]


@dataclass(frozen=True, slots=True)
class RunLimits:
    max_loop_turns: int | None
    max_model_calls_per_turn: int | None
    max_tool_calls_per_turn: int | None


@dataclass(frozen=True, slots=True)
class RetrievalCall:
    query: str
    top_k: int
    requested_weights: RetrievalChannelWeights
    effective_weights: RetrievalChannelWeights
    result: VideoChunkSearchResult
    strategy_version: str = AGENT_RETRIEVAL_STRATEGY_VERSION

    def context(self) -> dict:
        return {
            "query": self.query,
            "top_k": self.top_k,
            "retrieval_strategy": {
                "version": self.strategy_version,
                "requested_weights": self.requested_weights.model_dump(mode="json"),
                "effective_weights": self.effective_weights.model_dump(mode="json"),
            },
            "video_chunk_ids": [
                hit.candidate.video_chunk_id for hit in self.result.hits
            ],
            "partial": self.result.partial,
            "warnings": list(self.result.warnings),
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
    rendered_answer: ChatRenderDraft | None = None

    def context(self) -> dict:
        return {
            "number": self.number,
            "status": self.status,
            "plan_context": self.plan_context,
            "retrieval_calls": [call.context() for call in self.retrieval_calls],
            "evidence_source_turns": self.evidence_source_turns,
            "draft": {
                "message": self.query_draft.message,
                "video_chunk_ids": [
                    item.video_chunk_id
                    for item in self.query_draft.selected_video_ranges
                ],
            },
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


@dataclass(slots=True)
class ModelContextBudget:
    maximum_chars: int
    consumed_chars: int = 0

    def __post_init__(self) -> None:
        if (
            isinstance(self.maximum_chars, bool)
            or not isinstance(self.maximum_chars, int)
            or self.maximum_chars <= 0
        ):
            raise ValueError("maximum context characters must be a positive integer")

    def consume(self, label: str, value: str) -> None:
        next_total = self.consumed_chars + len(value)
        if next_total > self.maximum_chars:
            raise AgentContextBudgetError(
                f"Agent context character budget exceeded while adding {label}: "
                f"{next_total} > {self.maximum_chars}"
            )
        self.consumed_chars = next_total


class RunState(TypedDict):
    inputs: RunInputs
    # AgentWorkspace is intentionally typed as Any here: LangGraph resolves all
    # TypedDict hints at runtime, while workspace.py imports this module.
    workspace: Any
    turns: list[LoopTurn]
    control: ExecutionControl
    limits: RunLimits
    final_output: AgentFinalOutput


def context_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
