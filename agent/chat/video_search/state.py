from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Annotated, Literal, TypedDict

from pydantic import (
    BaseModel, ConfigDict, Field, StringConstraints, ValidationInfo,
    field_validator, model_serializer, model_validator,
)

from engine.rag.contracts import VideoChunkSearchEvidence, VideoChunkSearchResult
from engine.rag.retrieval.fusion import AGENT_RETRIEVAL_STRATEGY_VERSION

from ..common.state import AnswerPhase, ExecutionControl, RunInputs, context_json
from ..common.tool import ToolCallStatus
from .frame_evidence import FrameInspectionSession

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


SupervisorToolName = Literal["search_video_graph", "search_web", "understand_current_video"]


class ToolRenderBlock(_AgentContractModel):
    """Public lifecycle of a real Supervisor tool call; never its result."""

    type: Literal["tool"] = "tool"
    call_id: str = Field(min_length=1, max_length=128)
    tool: SupervisorToolName
    status: ToolCallStatus
    query: str | None = Field(default=None, min_length=1, max_length=500)


class VideoRenderBlock(_AgentContractModel):
    type: Literal["video"] = "video"
    video_chunk_id: _VideoChunkId
    start_seconds: int = Field(strict=True, ge=0)
    end_seconds: int = Field(strict=True, gt=0)
    # Persisted provenance; the Server projects only the existing public fields.
    source_video_chunk_ids: list[_VideoChunkId] | None = Field(
        default=None, min_length=1, max_length=2,
    )

    @model_serializer(mode="wrap")
    def serialize(self, handler):
        data = handler(self)
        if self.source_video_chunk_ids is None:
            data.pop("source_video_chunk_ids", None)
            data.pop("sourceVideoChunkIds", None)
        return data

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
    def validate_blocks(self, info: ValidationInfo) -> ChatRenderDraft:
        answer_markdown = self.answer_markdown
        if not answer_markdown.strip() and not (info.context or {}).get("partial"):
            raise ValueError("Chat render requires at least one nonempty Markdown block")
        if len(answer_markdown) > 20_000:
            raise ValueError("Chat Markdown exceeds 20000 characters")
        ids = [item.video_chunk_id for item in self.video_items]
        if len(ids) != len(set(ids)):
            raise ValueError("video_items must not contain duplicate video_chunk_id values")
        return self

    @classmethod
    def validate_prefix(cls, blocks: list[ChatRenderBlock]) -> None:
        # An in-progress answer can be empty or start with a card; all other
        # render limits remain the same as for the completed answer.
        if blocks:
            cls.model_validate({"blocks": blocks}, context={"partial": True})

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
    top_k: int = Field(ge=1)
    strategy_version: str = Field(min_length=1, max_length=255)
    requested_weights: RetrievalChannelWeights | LexicalRetrievalChannelWeights
    effective_weights: RetrievalChannelWeights | LexicalRetrievalChannelWeights
    result_count: int = Field(ge=0)
    video_chunk_ids: list[_VideoChunkId]
    granularity: Literal["chunk", "event"] | None = None
    effective_filters: dict[str, Any] | None = None
    event_trace: dict[str, Any] | None = None
    role: Literal["query", "evaluator", "supervisor"] | None = None
    status: Literal["success", "error"] | None = None
    error: dict[str, Any] | None = None

    @model_serializer(mode="wrap")
    def serialize_trace(self, handler):
        data = handler(self)
        for name in ("granularity", "effective_filters", "event_trace", "role", "status", "error"):
            if getattr(self, name) is None:
                data.pop(name, None)
                data.pop(_to_camel(name), None)
        return data

    @model_validator(mode="after")
    def require_matching_weight_channels(self) -> "RetrievalTraceEntry":
        if type(self.requested_weights) is not type(self.effective_weights):
            raise ValueError("requested and effective weights must use the same channels")
        return self


class AgentFinalOutput(VideoChunkSearchEvidence):
    render_blocks: list[FinalRenderBlock] = Field(min_length=1, max_length=200)
    retrieval_trace: list[RetrievalTraceEntry] = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class RunLimits:
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
    granularity: Literal["chunk", "event"] | None = None
    effective_filters: dict[str, Any] | None = None
    event_trace: dict[str, Any] | None = None
    role: Literal["query", "evaluator", "supervisor"] | None = None
    status: Literal["success", "error"] | None = None
    error: dict[str, Any] | None = None

    def context(self) -> dict:
        summary = {
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
        if self.granularity is not None:
            summary["granularity"] = self.granularity
        if self.effective_filters is not None:
            summary["effective_filters"] = self.effective_filters
        if self.role is not None:
            summary["role"] = self.role
        if self.status is not None:
            summary["status"] = self.status
        if self.error is not None:
            summary["error"] = {key: value[:500] if isinstance(value, str) else value
                                for key, value in self.error.items()}
        # Candidate scores, raw Event fields and hydration details stay in the
        # business trace, never copied wholesale into each model context.
        return summary


@dataclass(frozen=True, slots=True)
class QueryReport:
    """A delegated investigation; the Supervisor owns the final user answer."""

    completion: QueryCompletion
    query_draft: QueryDraft
    retrieval_result: VideoChunkSearchResult
    retrieval_trace: list[RetrievalTraceEntry]
    retrieval_calls: tuple[RetrievalCall, ...]
    canvas: Any


class RunState(TypedDict):
    inputs: RunInputs
    # Any avoids the Canvas/state import cycle when LangGraph resolves hints.
    canvas: Any
    frame_inspections: FrameInspectionSession | None
    calls: list[RetrievalCall]
    control: ExecutionControl
    limits: RunLimits
    tool_budget: Any
    completion: QueryCompletion
    query_draft: QueryDraft
    retrieval_result: VideoChunkSearchResult
    report: QueryReport
