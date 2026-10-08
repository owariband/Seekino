from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from gateway.usage import TokenUsage

from .jobDTO import ParseJobState


@dataclass(frozen=True, slots=True)
class ParseModelCallState:
    call_id: str
    job_id: str | None
    video_parse_task_id: str | None
    attempt_count: int
    lease_version: int
    workspace_id: str
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
class UsageTotals:
    call_count: int | None
    input_tokens: int | None
    output_tokens: int | None
    cached_input_tokens: int | None
    cache_write_input_tokens: int | None
    reasoning_tokens: int | None
    total_tokens: int | None
    known_tokens: int
    estimated_cost_usd: Decimal | None
    known_cost_usd: Decimal
    missing_usage_count: int
    unpriced_call_count: int


@dataclass(frozen=True, slots=True)
class UsageModelRow(UsageTotals):
    provider: str | None
    model: str | None
    category: str | None
    operation: str | None


@dataclass(frozen=True, slots=True)
class UsageRunRow(UsageTotals):
    run_id: str
    kind: Literal["parse", "chat", "other"]
    series_id: str | None
    video_id: str | None


@dataclass(frozen=True, slots=True)
class UsageOverviewState:
    status: Literal["available", "disabled", "not_configured", "unavailable"]
    message: str | None
    from_start_time: int
    to_start_time: int
    fetched_at: int
    truncated: bool
    totals: UsageTotals
    models: tuple[UsageModelRow, ...]
    runs: tuple[UsageRunRow, ...]
