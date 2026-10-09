from __future__ import annotations


class AgentToolInvocationError(RuntimeError):
    """An injected tool failed; the application owns its cause's error mapping."""


class AgentToolOutputError(AgentToolInvocationError):
    """A Tool could not satisfy its bounded output contract."""


class AgentToolFeedbackError(ValueError):
    """A Tool call the Agent can correct without ending the current turn."""

    def __init__(
        self, code: str, message: str, retry_hint: str,
        *, suggested_end: int | None = None,
    ) -> None:
        self.code = code
        self.retry_hint = retry_hint
        self.suggested_end = suggested_end
        super().__init__(message)
