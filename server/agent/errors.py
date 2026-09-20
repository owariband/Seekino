from __future__ import annotations


class AgentModelInvocationError(RuntimeError):
    """A bounded execution could not produce an approved, valid answer."""


class AgentContextBudgetError(AgentModelInvocationError):
    """Model context exceeded the Server's explicit character budget."""


class AgentToolInvocationError(RuntimeError):
    """An injected tool failed; the application owns its cause's error mapping."""


class AgentToolOutputError(AgentToolInvocationError):
    """A Tool could not satisfy its bounded output contract."""


class InvalidDraftRangeError(ValueError):
    """A model-submitted Draft range is outside its canonical Evidence."""

    def __init__(
        self,
        *,
        video_chunk_id: str,
        start_ms: int,
        end_ms: int,
        evidence_start_ms: int,
        evidence_end_ms: int,
    ) -> None:
        self.video_chunk_id = video_chunk_id
        self.start_ms = start_ms
        self.end_ms = end_ms
        self.evidence_start_ms = evidence_start_ms
        self.evidence_end_ms = evidence_end_ms
        super().__init__(
            "Draft Item range must satisfy canonical Evidence bounds and "
            "start_ms < end_ms"
        )
