from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from ...schema import ContractModel
from ...agent.common.state import AnswerPhase
from ...agent.video_search.state import RetrievalTraceEntry
from ..dto.agentDTO import EvidenceReference
from .commonVO import Failure, UnixTimeMs
from .searchVO import SearchEvidenceView


class AgentIntegerOptionView(ContractModel):
    default_value: int = Field(gt=0)
    minimum: int = Field(gt=0)
    maximum: int = Field(gt=0)


class AgentMessageOptionsView(ContractModel):
    top_k: AgentIntegerOptionView
    max_model_calls_per_turn: AgentIntegerOptionView
    max_tool_calls_per_turn: AgentIntegerOptionView
    max_loop_turns: AgentIntegerOptionView


class AgentGlobalSettingsView(ContractModel):
    """Effective global Agent settings; value keys match config.yaml exactly."""

    revision: str = Field(min_length=1)
    values: dict[str, Any]
    restart_required_fields: list[str]
    active_values: dict[str, Any]


class AgentMarkdownRenderBlockView(ContractModel):
    type: Literal["markdown"] = "markdown"
    markdown: str = Field(min_length=1, max_length=20_000)


class AgentVideoRenderBlockView(ContractModel):
    type: Literal["video"] = "video"
    video_chunk_id: str = Field(min_length=1, max_length=36)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_time_range(self) -> AgentVideoRenderBlockView:
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms must be greater than start_ms")
        return self


AgentRenderBlockView = Annotated[
    AgentMarkdownRenderBlockView | AgentVideoRenderBlockView,
    Field(discriminator="type"),
]


class AgentStageTimingView(ContractModel):
    phase: AnswerPhase
    duration_ms: int = Field(ge=0)


class AgentMessageTimingView(ContractModel):
    started_at: UnixTimeMs
    total_duration_ms: int | None = Field(ge=0)
    stages: list[AgentStageTimingView]


class AgentConversationView(ContractModel):
    conversation_id: str
    workspace_id: str
    title: str
    message_count: int = Field(ge=0)
    last_message_preview: str | None
    last_message_at: UnixTimeMs | None
    created_at: UnixTimeMs
    updated_at: UnixTimeMs


class AgentSearchEvidenceView(SearchEvidenceView):
    retrieval_trace: list[RetrievalTraceEntry]


class AgentMessageView(ContractModel):
    message_id: str
    conversation_id: str
    series_ids: list[str]
    sequence_no: int = Field(ge=0)
    role: Literal["USER", "ASSISTANT"]
    status: Literal["PROCESSING", "COMPLETED", "FAILED"]
    phase: AnswerPhase | None
    version: int = Field(gt=0)
    reference: EvidenceReference | None
    content: str | None
    model_name: str | None
    reply_to_message_id: str | None
    retrieval_query: str | None
    search_result: AgentSearchEvidenceView | None
    render_blocks: list[AgentRenderBlockView] = Field(default_factory=list)
    timing: AgentMessageTimingView | None
    last_error: Failure | None
    created_at: UnixTimeMs
    updated_at: UnixTimeMs


class AgentMessageSendView(ContractModel):
    user_message: AgentMessageView
    assistant_message: AgentMessageView


class AgentMessageEventView(ContractModel):
    type: Literal["messages"] = "messages"
    conversation_id: str
    messages: list[AgentMessageView]
    occurred_at: UnixTimeMs


class AgentAnswerEventView(ContractModel):
    conversation_id: str
    message_id: str
    sequence: int = Field(gt=0)
    occurred_at: UnixTimeMs


class AgentAnswerStartedView(AgentAnswerEventView, AgentMessageSendView):
    type: Literal["answer.started"] = "answer.started"


class AgentAnswerProgressView(AgentAnswerEventView):
    type: Literal["answer.progress"] = "answer.progress"
    phase: AnswerPhase
    timing: AgentMessageTimingView | None = None


class AgentAnswerEvidenceView(AgentAnswerEventView):
    type: Literal["answer.evidence"] = "answer.evidence"
    search_result: AgentSearchEvidenceView


class AgentAnswerBlockView(AgentAnswerEventView):
    type: Literal["answer.block"] = "answer.block"
    block: AgentRenderBlockView


class AgentAnswerDeltaView(AgentAnswerEventView):
    type: Literal["answer.delta"] = "answer.delta"
    delta: str


class AgentAnswerCompletedView(AgentAnswerEventView, AgentMessageSendView):
    type: Literal["answer.completed"] = "answer.completed"


class AgentAnswerFailedView(AgentAnswerEventView, AgentMessageSendView):
    type: Literal["answer.failed"] = "answer.failed"
    error: Failure


class AgentAnswerInterruptedView(AgentAnswerEventView):
    type: Literal["answer.interrupted"] = "answer.interrupted"
    error: Failure
