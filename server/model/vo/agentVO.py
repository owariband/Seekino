from __future__ import annotations

from typing import Literal

from pydantic import Field

from ...schema import ContractModel
from ...agent.state import AnswerPhase
from ..dto.agentDTO import EvidenceReference
from .commonVO import Failure, UnixTimeMs
from .searchVO import SearchResultView


class AgentConversationView(ContractModel):
    conversation_id: str
    workspace_id: str
    title: str
    message_count: int = Field(ge=0)
    last_message_preview: str | None
    last_message_at: UnixTimeMs | None
    created_at: UnixTimeMs
    updated_at: UnixTimeMs


class AgentMessageView(ContractModel):
    message_id: str
    conversation_id: str
    series_ids: list[str] = Field(min_length=1)
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
    search_result: SearchResultView | None
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


class AgentAnswerEvidenceView(AgentAnswerEventView):
    type: Literal["answer.evidence"] = "answer.evidence"
    search_result: SearchResultView


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
