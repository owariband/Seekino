"""Validated snapshots of one Parse model call's visible text."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Literal

DraftStatus = Literal["streaming", "completed", "failed", "cancelled"]
DRAFT_STATUSES = frozenset({"streaming", "completed", "failed", "cancelled"})
DRAFT_METADATA_FIELDS = frozenset({
    "provider", "model", "operation",
    "run_id", "video_id", "series_id", "stage", "execution_kind",
    "execution_task_id", "chunk_parse_task_id", "interval_parse_task_id",
    "video_chunk_id", "sequence_no", "lease_version", "attempt_count",
    "retry_count", "index_generation", "refine_generation", "category",
})


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("draft metadata keys must be strings")
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    raise ValueError("draft metadata must contain JSON values")


@dataclass(frozen=True, slots=True)
class ParseOutputDraftState:
    call_id: str
    tenant_id: str
    video_task_id: str
    text: str
    text_bytes: int
    status: DraftStatus
    metadata: Mapping[str, Any]
    started_at: datetime | None = None
    actor_user_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text:
            raise ValueError("draft text must be nonempty")
        expected_bytes = len(self.text.encode("utf-8"))
        if type(self.text_bytes) is not int or self.text_bytes != expected_bytes:
            raise ValueError("text_bytes must match the UTF-8 text length")
        if self.status not in DRAFT_STATUSES:
            raise ValueError("unsupported draft status")
        if not isinstance(self.metadata, Mapping):
            raise ValueError("draft metadata must be a JSON object")
        unknown = set(self.metadata) - DRAFT_METADATA_FIELDS
        if unknown:
            raise ValueError(f"unsupported draft metadata fields: {sorted(unknown)}")
        object.__setattr__(self, "metadata", _freeze_json(self.metadata))
