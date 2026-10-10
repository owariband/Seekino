from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Protocol, TypeVar

from langchain_core.tools import BaseTool
from pydantic import BaseModel

from .context import ContextCheckpoint

_ResultModel = TypeVar("_ResultModel", bound=BaseModel)


class StructuredToolAgentModel(Protocol):
    provider_name: str
    model_name: str

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
    ) -> None: ...


__all__ = ["AgentLoopModel", "StructuredToolAgentModel"]
