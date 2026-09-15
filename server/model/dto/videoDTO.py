from __future__ import annotations

from dataclasses import dataclass

from ...schema import VideoChunkEvidence


@dataclass(frozen=True, slots=True)
class HydratedVideoChunkState:
    evidence: VideoChunkEvidence
    source_revision: str | None
    index_operation: str | None
    index_status: str | None
