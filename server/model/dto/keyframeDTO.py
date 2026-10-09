from __future__ import annotations

from dataclasses import dataclass

from pydantic import Field

from ...schema import ContractModel


@dataclass(frozen=True, slots=True)
class KeyframeBackfillCandidate:
    tenant_id: str
    series_id: str
    video_id: str
    episode_order: int
    duration_seconds: int
    video_chunk_id: str
    sequence_no: int
    start_seconds: int
    end_seconds: int
    chunk_media_ref: str
    keyframe_snapshot: tuple[object, ...]


class KeyframeBackfillIssue(ContractModel):
    video_chunk_id: str
    code: str


class KeyframeBackfillReport(ContractModel):
    dry_run: bool
    limit: int = Field(gt=0)
    candidate_count: int = Field(ge=0)
    ready_count: int = Field(ge=0)
    backfilled_count: int = Field(ge=0)
    missing_media_count: int = Field(ge=0)
    concurrent_skip_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    issues: list[KeyframeBackfillIssue]
