from __future__ import annotations

from agent.utils.errors import (
    AgentToolFeedbackError, AgentToolInvocationError, AgentToolOutputError,
)


class AgentModelInvocationError(RuntimeError):
    """A bounded execution could not produce an approved, valid answer."""


class AgentContextBudgetError(AgentModelInvocationError):
    """Model context exceeded the configured explicit character budget."""


class UnknownToolItemError(AgentModelInvocationError):
    """A model-supplied Workspace item ID is unavailable to a Tool."""

    def __init__(self, message: str, retry_hint: str) -> None:
        self.retry_hint = retry_hint
        super().__init__(message)
