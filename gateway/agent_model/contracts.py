from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from langchain_core.tools import BaseTool
from pydantic import BaseModel

from .context import ContextCheckpoint

_ResultModel = TypeVar("_ResultModel", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class SupervisorSessionContext:
    """Host-owned session bindings and committed history, separate from model input."""

    states: dict[str, dict[str, Any]]
    message_sequence_no: int
    load_history: Callable[[int], Sequence[dict[str, Any]]]
    save_state: Callable[[str, dict[str, Any]], None]


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


__all__ = ["AgentLoopModel", "StructuredToolAgentModel", "SupervisorAgentModel", "SupervisorSessionContext"]
