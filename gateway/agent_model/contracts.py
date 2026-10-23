from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from langchain_core.tools import BaseTool
from pydantic import BaseModel

from .context import ContextCheckpoint

_ResultModel = TypeVar("_ResultModel", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class AgentModelProfile:
    """An authorized, immutable model configuration resolved by the caller."""

    id: str
    revision: str
    provider: str
    model: str
    api_key: str = field(repr=False)
    base_url: str
    supports_vision_input: bool = False
    max_output_tokens: int = 8192
    context_window_tokens: int = 128_000
    reasoning_effort: str = "none"

    def __post_init__(self) -> None:
        if not all(value.strip() for value in (
            self.id, self.revision, self.provider, self.model, self.api_key, self.base_url,
        )):
            raise ValueError("model profile identity, endpoint and credentials are required")
        if self.max_output_tokens <= 0 or self.context_window_tokens <= self.max_output_tokens:
            raise ValueError("model profile output budget must fit its context window")


@dataclass(frozen=True, slots=True)
class SupervisorSessionContext:
    """Host-owned session bindings and committed history, separate from model input."""

    states: dict[str, dict[str, Any]]
    message_sequence_no: int
    load_history: Callable[[int], Sequence[dict[str, Any]]]
    save_state: Callable[[str, dict[str, Any]], None]
    model_profile: AgentModelProfile | None = None


class StructuredToolAgentModel(Protocol):
    provider_name: str
    model_name: str
    supports_vision_input: bool

    def run_structured(
        self,
        prompt: str,
        *,
        tools: Sequence[BaseTool],
        result_type: type[_ResultModel],
        max_model_calls: int | None,
        request_timeout_seconds: float | None = None,
        check_control: Callable[[], None] | None = None,
        max_input_chars: int | None = None,
        context_checkpoint: ContextCheckpoint | None = None,
        initial_messages: Sequence[dict[str, Any]] | None = None,
        image_urls: Sequence[str] = (),
    ) -> _ResultModel: ...


class AgentLoopModel(Protocol):
    provider_name: str
    model_name: str
    supports_vision_input: bool

    def run_agent_loop(
        self,
        prompt: str,
        *,
        tools: Sequence[BaseTool],
        max_model_calls: int | None,
        handle_text: Callable[[str], None],
        require_tool_call: bool = False,
        stop_feedback: Callable[[], str | None] | None = None,
        request_timeout_seconds: float | None = None,
        check_control: Callable[[], None] | None = None,
        max_input_chars: int | None = None,
        context_checkpoint: ContextCheckpoint | None = None,
        initial_messages: Sequence[dict[str, Any]] | None = None,
        image_urls: Sequence[str] = (),
    ) -> None: ...


class SupervisorAgentModel(Protocol):
    provider_name: str
    model_name: str
    supports_vision_input: bool

    def run_agent(
        self,
        prompt: str,
        *,
        thread_id: str,
        message_id: str,
        user_message: str,
        history: Sequence[dict[str, Any]],
        tools: Sequence[BaseTool],
        max_model_calls: int | None,
        handle_text: Callable[[str], None],
        request_timeout_seconds: float | None = None,
        check_control: Callable[[], None] | None = None,
        max_input_chars: int | None = None,
        image_urls: Sequence[str] = (),
        session_context: SupervisorSessionContext | None = None,
    ) -> None: ...

    def confirm_turn(self, thread_id: str, message_id: str, content: str) -> None: ...

    def discard_turn(self, thread_id: str, message_id: str) -> None: ...

    def delete_thread(self, thread_id: str) -> None: ...


__all__ = ["AgentLoopModel", "AgentModelProfile", "StructuredToolAgentModel", "SupervisorAgentModel", "SupervisorSessionContext"]
