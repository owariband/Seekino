from __future__ import annotations

from typing import Literal

from pydantic import Field

from ...schema import ContractModel


class ParseUsageRowVO(ContractModel):
    provider: str
    model: str
    category: Literal["parse", "embedding"]
    operation: str
    call_count: int = Field(ge=0)
    input_tokens: int | None = Field(ge=0)
    output_tokens: int | None = Field(ge=0)
    cached_input_tokens: int | None = Field(ge=0)
    reasoning_tokens: int | None = Field(ge=0)
    total_tokens: int | None = Field(ge=0)
    estimated_cost_cny: str | None
    missing_usage_count: int = Field(ge=0)
    unpriced_call_count: int = Field(ge=0)


class ParseUsageVO(ContractModel):
    job_id: str
    video_id: str
    series_id: str
    series_name: str
    episode_order: int = Field(gt=0)
    original_filename: str
    currency: Literal["CNY"] = "CNY"
    coverage: Literal["not_started", "recorded", "partial", "unknown"]
    total_tokens: int | None = Field(ge=0)
    estimated_cost_cny: str | None
    call_count: int = Field(ge=0)
    missing_usage_count: int = Field(ge=0)
    unpriced_call_count: int = Field(ge=0)
    items: list[ParseUsageRowVO]
