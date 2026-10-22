from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, StringConstraints

from engine.rag.contracts import VideoChunkSearchHit

from ...common.errors import AgentModelInvocationError
from ...common.state import ExecutionControl
from ..context import retrieval_evidence_text

_OptionalCursor = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000),
] | None

class RemoveItemArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_chunk_id: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=36),
    ]

@dataclass(slots=True)
class _ToolCallBudget:
    maximum: int
    role: str
    used: int = 0
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if (
            isinstance(self.maximum, bool)
            or not isinstance(self.maximum, int)
            or self.maximum <= 0
        ):
            raise ValueError("maximum Tool calls must be a positive integer")

    def consume(self, tool_name: str) -> None:
        with self._lock:
            self.used += 1
            if self.used > self.maximum:
                raise AgentModelInvocationError(
                    f"{self.role} Agent Tool-call budget exceeded before {tool_name}"
                )


def _start_tool_call(
    control: ExecutionControl,
    budget: _ToolCallBudget | None,
    tool_name: str,
) -> None:
    control.check()
    if budget is not None:
        budget.consume(tool_name)


def _retrieval_card(hit: VideoChunkSearchHit, maximum_chars: int) -> dict[str, Any]:
    evidence = retrieval_evidence_text(hit)
    return {
        "video_chunk_id": hit.candidate.video_chunk_id,
        "video_id": hit.evidence.video_id,
        "episode_order": hit.evidence.episode_order,
        "start_seconds": hit.evidence.start_seconds,
        "end_seconds": hit.evidence.end_seconds,
        "description": _bounded_text(hit.selection_reason.summary, 500),
        "evidence": _bounded_text(evidence, maximum_chars),
        "evidence_truncated": len(evidence) > maximum_chars,
        "fused_rank": hit.candidate.fused_rank,
        "fused_score": hit.candidate.fused_score,
        "lexical_rank": hit.candidate.lexical_rank,
        "transcript_rank": hit.candidate.transcript_rank,
        "semantic_rank": hit.candidate.text_rank,
        "visual_rank": hit.candidate.vision_rank,
    }


def _bounded_text(value: str, maximum_chars: int) -> str:
    if len(value) <= maximum_chars:
        return value
    return value[: maximum_chars - 1].rstrip() + "…"
