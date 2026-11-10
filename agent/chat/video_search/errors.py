"""Errors owned by the video-search domain."""

from __future__ import annotations

from enum import StrEnum

from ..common.errors import AgentToolInvocationError


class VideoSearchFailureCode(StrEnum):
    """Stable, model-safe reasons why the video graph could not complete."""

    OUTPUT_TOKEN_LIMIT = "VIDEO_OUTPUT_TOKEN_LIMIT"
    MODEL_TIMEOUT = "VIDEO_MODEL_TIMEOUT"
    REQUIRED_TOOL_MISSING = "VIDEO_REQUIRED_TOOL_MISSING"
    INVALID_MODEL_RESPONSE = "VIDEO_INVALID_MODEL_RESPONSE"
    CONTEXT_BUDGET_EXCEEDED = "VIDEO_CONTEXT_BUDGET_EXCEEDED"
    LOOP_LIMIT_EXCEEDED = "VIDEO_LOOP_LIMIT_EXCEEDED"
    RETRIEVAL_FAILED = "VIDEO_RETRIEVAL_FAILED"
    SCOPE_NOT_READY = "SERIES_NOT_READY"
    EPISODE_NOT_FOUND = "EPISODE_NOT_FOUND"
    EVENT_INDEX_NOT_READY = "EVENT_INDEX_NOT_READY"
    SERVICE_UNAVAILABLE = "RETRIEVAL_UNAVAILABLE"


class VideoSearchFailurePhase(StrEnum):
    """Video-search stage in which a recoverable failure occurred."""

    QUERY = "query"
    EVALUATOR = "evaluator"
    CHAT = "chat"
    RESULT = "result"
    RETRIEVAL = "retrieval"


class VideoSearchFailure(AgentToolInvocationError):
    """A classified failure safe for the Supervisor to reason about."""

    def __init__(
        self,
        code: VideoSearchFailureCode,
        phase: VideoSearchFailurePhase,
        *,
        retryable: bool,
        public_message: str | None = None,
    ) -> None:
        self.code = code
        self.phase = phase
        self.retryable = retryable
        self.public_message = public_message
        super().__init__(code.value)


class VideoRetrievalError(AgentToolInvocationError):
    """The video retrieval dependency failed before producing a valid result."""
