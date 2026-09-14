from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class ApiModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class ParseImportReport(ApiModel):
    series_id: str
    video_id: str
    video_chunk_ids: list[str]
    chunk_count: int = Field(gt=0)
    vectors_imported: bool


class IndexSyncRunReport(ApiModel):
    materialized_count: int = Field(ge=0)
    selected_count: int = Field(ge=0)
    synced_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    stale_result_count: int = Field(ge=0)


class IndexReconcileReport(ApiModel):
    materialized_count: int = Field(ge=0)
    inspected_count: int = Field(ge=0)
    consistent_count: int = Field(ge=0)
    repair_attempted_count: int = Field(ge=0)
    synced_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    stale_result_count: int = Field(ge=0)


class VideoChunkEvidence(ApiModel):
    series_id: str
    workspace_id: str
    series_name: str
    series_description: str | None
    series_status: str
    important_extra_data: dict[str, Any] | None
    character_bible: list[dict[str, Any]]

    video_id: str
    episode_order: int
    original_filename: str
    original_source_uri: str
    inference_proxy_uri: str | None
    content_hash: str
    duration_ms: int
    upload_status: str
    parse_status: str

    video_chunk_id: str
    sequence_no: int
    start_ms: int
    end_ms: int
    analysis_window_start_ms: int
    analysis_window_end_ms: int
    chunk_media_ref: str
    analysis_window_media_ref: str
    transcript: list[dict[str, Any]]
    baseline_visual_caption: str | None
    keyframe_uris: list[str]

    all_time_events: list[dict[str, Any]]
    all_time_event_text: str
    video_compact: str
    context_before_compact: str
    context_after_compact: str
    window_summary: str
    model_provider: str | None
    model_name: str | None
    pipeline_version: str
    is_fallback: bool
    parse_provenance: dict[str, Any] | None
    parsed_at: datetime
