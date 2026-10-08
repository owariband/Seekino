from __future__ import annotations

from typing import Literal

from pydantic import Field

from ...schema import ContractModel
from .commonVO import UnixTimeMs


class UsageTotalsVO(ContractModel):
    call_count: int | None = Field(ge=0)
    input_tokens: int | None = Field(ge=0)
    output_tokens: int | None = Field(ge=0)
    cached_input_tokens: int | None = Field(ge=0)
    cache_write_input_tokens: int | None = Field(ge=0)
    reasoning_tokens: int | None = Field(ge=0)
    total_tokens: int | None = Field(ge=0)
    known_tokens: int = Field(ge=0)
    estimated_cost_usd: str | None
    known_cost_usd: str
    missing_usage_count: int = Field(ge=0)
    unpriced_call_count: int = Field(ge=0)


class UsageModelRowVO(UsageTotalsVO):
    provider: str | None
    model: str | None
    category: str | None
    operation: str | None


class UsageRunRowVO(UsageTotalsVO):
    run_id: str
    kind: Literal["parse", "chat", "other"]
    series_id: str | None
    video_id: str | None


class UsageOverviewVO(ContractModel):
    source: Literal["langfuse"] = "langfuse"
    currency: Literal["USD"] = "USD"
    status: Literal["available", "disabled", "not_configured", "unavailable"]
    message: str | None
    from_start_time: UnixTimeMs
    to_start_time: UnixTimeMs
    fetched_at: UnixTimeMs
    truncated: bool
    totals: UsageTotalsVO
    models: list[UsageModelRowVO]
    runs: list[UsageRunRowVO]
