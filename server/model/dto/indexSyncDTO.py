from __future__ import annotations

from dataclasses import dataclass

from pydantic import Field

from ...schema import ContractModel
from ..index_sync import IndexSyncOperation, IndexSyncStatus


class IndexSyncRunReport(ContractModel):
    materialized_count: int = Field(ge=0)
    selected_count: int = Field(ge=0)
    synced_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    stale_result_count: int = Field(ge=0)


class IndexReconcileReport(ContractModel):
    materialized_count: int = Field(ge=0)
    inspected_count: int = Field(ge=0)
    consistent_count: int = Field(ge=0)
    repair_attempted_count: int = Field(ge=0)
    synced_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    stale_result_count: int = Field(ge=0)


@dataclass(frozen=True)
class IndexSyncItem:
    video_chunk_id: str
    series_id: str
    video_id: str
    operation: IndexSyncOperation
    source_revision: str
    generation: int
    status: IndexSyncStatus
    attempt_count: int
    available_at: int
    last_error: str | None
    synced_at: int | None
    is_fallback: bool
    text_vector: list[float] | None
    vision_vector: list[float] | None

    @property
    def payload(self) -> dict[str, str | bool]:
        return {
            "seriesId": self.series_id,
            "videoId": self.video_id,
            "videoChunkId": self.video_chunk_id,
            "sourceRevision": self.source_revision,
            "isFallback": self.is_fallback,
        }
