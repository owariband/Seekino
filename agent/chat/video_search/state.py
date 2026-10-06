from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Annotated, Literal, TypedDict

from pydantic import (
    BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator,
)

from engine.rag.contracts import VideoChunkSearchEvidence, VideoChunkSearchResult
from engine.rag.retrieval.fusion import AGENT_RETRIEVAL_STRATEGY_VERSION

from ..common.errors import AgentContextBudgetError
from ..common.state import AnswerPhase, ExecutionControl, RunInputs, context_json

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
RetrievalChannelContract = Literal["transcript", "lexical"]


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class _AgentContractModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        from_attributes=True,
        extra="forbid",
    )


class SelectedVideoRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_chunk_id: _VideoChunkId
    start_seconds: int = Field(strict=True, ge=0)
    end_seconds: int = Field(strict=True, gt=0)

    @model_validator(mode="after")
    def validate_time_range(self) -> SelectedVideoRange:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must be greater than start_seconds")
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
    start_seconds: int = Field(strict=True, ge=0)
    end_seconds: int = Field(strict=True, gt=0)
    claim: _EvidenceClaim

    @model_validator(mode="after")
    def validate_time_range(self) -> DraftItem:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must be greater than start_seconds")
        return self


class RetrievalChannelWeights(BaseModel):
    """ASR/Semantic/Visual weights retained for live V1 calls and old traces."""

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


class LexicalRetrievalChannelWeights(BaseModel):
    """Lexical/Semantic/Visual weights for the current retrieval contract."""

    model_config = ConfigDict(extra="forbid")

    lexical: float = Field(ge=0, le=1, allow_inf_nan=False)
    semantic: float = Field(ge=0, le=1, allow_inf_nan=False)
    visual: float = Field(ge=0, le=1, allow_inf_nan=False)

    @field_validator("lexical", "semantic", "visual", mode="before")
    @classmethod
    def reject_boolean_weights(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("channel weights must be numbers, not booleans")
        return value

    @model_validator(mode="after")
    def require_enabled_channel(self) -> "LexicalRetrievalChannelWeights":
        if self.lexical == 0 and self.semantic == 0 and self.visual == 0:
            raise ValueError("at least one channel weight must be greater than zero")
        return self


class MarkdownRenderBlock(_AgentContractModel):
    type: Literal["markdown"] = "markdown"
    markdown: _Markdown


class VideoRenderBlock(_AgentContractModel):
    type: Literal["video"] = "video"
    video_chunk_id: _VideoChunkId
    start_seconds: int = Field(strict=True, ge=0)
    end_seconds: int = Field(strict=True, gt=0)

    @model_validator(mode="after")
    def validate_time_range(self) -> VideoRenderBlock:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must be greater than start_seconds")
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


class RetrievalTraceEntry(_AgentContractModel):
    query: _Text
    top_k: int = Field(ge=1, le=100)
    strategy_version: str = Field(min_length=1, max_length=255)
    requested_weights: RetrievalChannelWeights | LexicalRetrievalChannelWeights
    effective_weights: RetrievalChannelWeights | LexicalRetrievalChannelWeights
    result_count: int = Field(ge=0)
    video_chunk_ids: list[_VideoChunkId] = Field(max_length=100)

    @model_validator(mode="after")
    def require_matching_weight_channels(self) -> "RetrievalTraceEntry":
        if type(self.requested_weights) is not type(self.effective_weights):
            raise ValueError("requested and effective weights must use the same channels")
        return self


class AgentFinalOutput(VideoChunkSearchEvidence):
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


@dataclass(frozen=True, slots=True)
class RunLimits:
    max_loop_turns: int | None
    max_model_calls_per_turn: int | None
    max_tool_calls_per_turn: int | None


@dataclass(frozen=True, slots=True)
class RetrievalCall:
    query: str
    top_k: int
    requested_weights: RetrievalChannelWeights | LexicalRetrievalChannelWeights
    effective_weights: RetrievalChannelWeights | LexicalRetrievalChannelWeights
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
