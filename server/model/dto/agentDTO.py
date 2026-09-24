from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from pydantic import Field, field_validator

from agent.chat.common.state import AnswerPhase

from ...schema import ContractModel

_Item = TypeVar("_Item")
_MAX_EXTRA_CONTEXT_BYTES = 16_384


class EvidenceReference(ContractModel):
    message_id: str = Field(min_length=1)
    video_chunk_id: str = Field(min_length=1)


class AgentConversationCreateRequest(ContractModel):
    workspace_id: str = Field(min_length=1, max_length=64)
    title: str | None = Field(default=None, min_length=1, max_length=255)


class AgentMessageSendRequest(ContractModel):
    workspace_id: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=20_000)
    series_ids: list[str] = Field(default_factory=list)
    top_k: int | None = Field(default=None, gt=0, le=100)
    max_model_calls_per_turn: int | None = Field(default=None, gt=0)
    max_tool_calls_per_turn: int | None = Field(default=None, gt=0)
    max_loop_turns: int | None = Field(default=None, ge=0, le=100)
    reference: EvidenceReference | None = None
    extra_context: dict[str, Any] | None = None

    @field_validator("extra_context")
    @classmethod
    def validate_extra_context_size(
        cls,
        value: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        try:
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("extra_context must contain valid JSON values") from exc
        if len(encoded) > _MAX_EXTRA_CONTEXT_BYTES:
            raise ValueError(
                f"extra_context must not exceed {_MAX_EXTRA_CONTEXT_BYTES} bytes"
            )
        return value


class AgentGlobalSettingsPatchRequest(ContractModel):
    """Patch global Agent defaults against the YAML revision last read by the UI."""

    revision: str = Field(min_length=1)
    values: dict[str, Any] = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class AgentRunOptionsState:
    max_model_calls_per_turn: int
    max_tool_calls_per_turn: int


@dataclass(frozen=True, slots=True)
class AgentIntegerOptionState:
    default_value: int
    minimum: int
    maximum: int


@dataclass(frozen=True, slots=True)
class AgentMessageOptionsState:
    top_k: AgentIntegerOptionState
    max_model_calls_per_turn: AgentIntegerOptionState
    max_tool_calls_per_turn: AgentIntegerOptionState
    max_loop_turns: AgentIntegerOptionState


@dataclass(frozen=True, slots=True)
class AgentFailure:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class AgentStageTimingState:
    phase: AnswerPhase
    duration_ms: int


@dataclass(frozen=True, slots=True)
class AgentMessageTimingState:
    started_at: int
    total_duration_ms: int | None
    stages: tuple[AgentStageTimingState, ...]


@dataclass(frozen=True, slots=True)
class AgentConversationState:
    conversation_id: str
    workspace_id: str
    title: str
    message_count: int
    last_message_at: int | None
    created_at: int
    updated_at: int
    last_message_preview: str | None = None


@dataclass(frozen=True, slots=True)
class AgentMessageState:
    message_id: str
    conversation_id: str
    sequence_no: int
    role: str
    status: str
    content: str | None
    series_ids: tuple[str, ...]
    model_name: str | None
    reply_to_message_id: str | None
    retrieval_query: str | None
    search_result: dict | None
    last_error: AgentFailure | None
    created_at: int
    updated_at: int
    phase: AnswerPhase | None = None
    version: int = 1
    reference: dict[str, str] | None = None
    timing: AgentMessageTimingState | None = None
    extra_context: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class StartedAgentMessageState:
    user_message: AgentMessageState
    assistant_message: AgentMessageState
    reused: bool


@dataclass(frozen=True, slots=True)
class AgentPageState(Generic[_Item]):
    items: list[_Item]
    total: int
    next_cursor: str | None
    has_more: bool


@dataclass(frozen=True, slots=True)
class AgentMessageSendState:
    user_message: AgentMessageState
    assistant_message: AgentMessageState
