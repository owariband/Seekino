from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from engine.retrieval.contracts import (
    AgentReply,
    MatchedEvidence,
    SelectionReason,
    TranscriptCueView,
    VideoChunkCandidate,
    VideoChunkEvidence,
    VideoChunkKeyframeReference,
    VideoChunkSearchHit,
    VideoChunkSearchResult,
)


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class ContractModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        from_attributes=True,
        extra="forbid",
    )


__all__ = [
    "AgentReply",
    "ContractModel",
    "MatchedEvidence",
    "SelectionReason",
    "TranscriptCueView",
    "VideoChunkCandidate",
    "VideoChunkEvidence",
    "VideoChunkKeyframeReference",
    "VideoChunkSearchHit",
    "VideoChunkSearchResult",
]
