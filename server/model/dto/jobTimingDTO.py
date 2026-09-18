from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ParseJobAttemptTimingState:
    job_id: str
    attempt_count: int
    lease_version: int
    started_at: int
    completed_at: int | None
    duration_ms: int | None
    outcome: str
    timing_complete: bool


@dataclass(frozen=True, slots=True)
class VideoChunkStageTimingState:
    job_id: str
    attempt_count: int
    lease_version: int
    sequence_no: int
    video_chunk_id: str
    stage: str
    started_at: int
    completed_at: int | None
    duration_ms: int | None
    status: str
