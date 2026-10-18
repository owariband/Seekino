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
    ) -> None:
        self.code = code
        self.phase = phase
        self.retryable = retryable
        super().__init__(code.value)


class VideoRetrievalError(AgentToolInvocationError):
    """The video retrieval dependency failed before producing a valid result."""


class InvalidDraftRangeError(ValueError):
    """A model-submitted Draft range is outside its canonical Evidence."""

    def __init__(
        self,
        *,
        video_chunk_id: str,
        start_seconds: int,
        end_seconds: int,
        evidence_start_seconds: int,
        evidence_end_seconds: int,
    ) -> None:
        self.video_chunk_id = video_chunk_id
        self.start_seconds = start_seconds
        self.end_seconds = end_seconds
        self.evidence_start_seconds = evidence_start_seconds
        self.evidence_end_seconds = evidence_end_seconds
        super().__init__(
            "Draft Item range must satisfy canonical Evidence bounds and "
            "start_seconds < end_seconds"
        )
