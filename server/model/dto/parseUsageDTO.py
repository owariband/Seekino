from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Literal

from gateway.usage import TokenUsage

from .jobDTO import ParseJobState


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    raise ValueError("usage and metadata must contain JSON values")


@dataclass(frozen=True, slots=True)
class ModelUsageState:
    call_id: str
    tenant_id: str
    task_type: Literal["video_parse", "chat_retrieval"]
    run_id: str
    started_at: datetime
    usage: Mapping[str, Any]
    metadata: Mapping[str, Any]
    created_at: datetime
    actor_user_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("usage", "metadata"):
            value = getattr(self, name)
            if not isinstance(value, Mapping):
                raise ValueError(f"{name} must be a JSON object")
            object.__setattr__(self, name, _freeze_json(value))


@dataclass(frozen=True, slots=True)
class ModelCallUsageBatch:
    records: tuple[ModelUsageState, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class ParseModelCallState:
    call_id: str
    job_id: str | None
    video_parse_task_id: str | None
    attempt_count: int
    lease_version: int
    tenant_id: str
    series_id: str
    video_id: str
    sequence_no: int | None
    video_chunk_id: str | None
    provider: str
    model: str
    category: Literal["parse", "embedding"]
    operation: str
    started_at: datetime
    request_id: str | None
    status: Literal["completed", "failed"]
    usage: TokenUsage | None
    estimated_cost_cny: Decimal | None
    price_snapshot: dict | None
    execution_kind: Literal["video", "chunk", "interval", "prefix"] | None = None
    execution_task_id: str | None = None


@dataclass(frozen=True, slots=True)
class ParseUsageLedgerState:
    attempt_count: int
    job_status: str
    calls: tuple[ParseModelCallState, ...]
    attempts: dict[int, bool]


@dataclass(frozen=True, slots=True)
class ParseUsageRowState:
    provider: str
    model: str
    category: Literal["parse", "embedding"]
    operation: str
    call_count: int
    input_tokens: int | None
    output_tokens: int | None
    cached_input_tokens: int | None
    reasoning_tokens: int | None
    total_tokens: int | None
    estimated_cost_cny: Decimal | None
    missing_usage_count: int
    unpriced_call_count: int


@dataclass(frozen=True, slots=True)
class ParseUsageState:
    job: ParseJobState
    coverage: Literal["not_started", "recorded", "partial", "unknown"]
    total_tokens: int | None
    estimated_cost_cny: Decimal | None
    call_count: int
    missing_usage_count: int
    unpriced_call_count: int
    items: tuple[ParseUsageRowState, ...]


@dataclass(frozen=True, slots=True)
class UsageObservationBatch:
    observations: tuple[dict, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class UsageCostSummary:
    currency: Literal["CNY"] = "CNY"
    estimated_amount: str | None = None
    known_amount: str | None = None
    priced_call_count: int = 0
    unpriced_call_count: int = 0
    excluded_call_count: int = 0


@dataclass(frozen=True, slots=True)
class UsageTotals:
    call_count: int | None
    input_tokens: int | None
    output_tokens: int | None
    cached_input_tokens: int | None
    cache_write_input_tokens: int | None
    reasoning_tokens: int | None
    total_tokens: int | None
    known_tokens: int
    missing_usage_count: int
    cost: UsageCostSummary | None = field(default=None, kw_only=True)


@dataclass(frozen=True, slots=True)
class UsageModelRow(UsageTotals):
    provider: str | None
    model: str | None
    category: str | None
    operation: str | None
    src: str | None = field(default=None, kw_only=True)


@dataclass(frozen=True, slots=True)
class UsageRunRow(UsageTotals):
    run_id: str
    task_type: Literal["video_parse", "chat_retrieval"]
    series_id: str | None
    video_id: str | None


@dataclass(frozen=True, slots=True)
class UsageOverviewState:
    status: Literal["available", "unavailable"]
    message: str | None
    from_start_time: int
    to_start_time: int
    fetched_at: int
    truncated: bool
    totals: UsageTotals
    models: tuple[UsageModelRow, ...]
    runs: tuple[UsageRunRow, ...]
