from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator

from ..utils.time import datetime_to_unix_time_ms, unix_time_ms_to_datetime


def _decimal_rate(value):
    if isinstance(value, float):
        raise ValueError("price rates must be quoted decimal strings, not YAML floats")
    return value


Rate = Annotated[Decimal, BeforeValidator(_decimal_rate), Field(ge=0, allow_inf_nan=False)]


class PriceTier(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_input_tokens: int = Field(default=0, ge=0)
    max_input_tokens: int | None = Field(default=None, gt=0)
    input_cny_per_unit: Rate | None = None
    output_cny_per_unit: Rate | None = None
    cached_input_cny_per_unit: Rate | None = None
    text_input_cny_per_unit: Rate | None = None
    image_input_cny_per_unit: Rate | None = None
    audio_input_cny_per_unit: Rate | None = None
    cached_audio_input_cny_per_unit: Rate | None = None

    @model_validator(mode="after")
    def valid_range(self):
        if self.max_input_tokens is not None and self.max_input_tokens <= self.min_input_tokens:
            raise ValueError("price tier upper bound must exceed its lower bound")
        return self


class PriceTimeRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    end: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")

    @model_validator(mode="after")
    def ordered(self):
        if self.start >= self.end:
            raise ValueError("price time range must start before end")
        return self


def _ordered_tiers(tiers: list[PriceTier]) -> None:
    for previous, current in zip(tiers, tiers[1:]):
        if previous.max_input_tokens is None or previous.max_input_tokens > current.min_input_tokens:
            raise ValueError("price tiers must be ordered and non-overlapping")


class PriceTimeBand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    weekdays: list[int] = Field(min_length=1)
    ranges: list[PriceTimeRange] = Field(min_length=1)
    tiers: list[PriceTier] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_band(self):
        if any(day not in range(1, 8) for day in self.weekdays):
            raise ValueError("price weekdays must be ISO weekdays 1 through 7")
        _ordered_tiers(self.tiers)
        return self


class ModelPrice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    category: Literal["parse", "embedding"]
    source_url: str = Field(pattern=r"^https://")
    checked_at: datetime
    effective_from: datetime
    effective_until: datetime | None = None
    tiers: list[PriceTier] = Field(min_length=1)
    time_bands: list[PriceTimeBand] = Field(default_factory=list)

    @field_validator("checked_at", "effective_from", "effective_until")
    @classmethod
    def utc_time(cls, value):
        return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value

    @model_validator(mode="after")
    def valid_model_price(self):
        _ordered_tiers(self.tiers)
        if self.effective_until is not None and self.effective_until <= self.effective_from:
            raise ValueError("price effective_until must exceed effective_from")
        return self


class ModelPricingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    currency: Literal["CNY"] = "CNY"
    unit: Literal[1000000] = 1000000
    models: list[ModelPrice] = Field(default_factory=list)

    def snapshot(self, *, provider: str, model: str, category: str, input_tokens: int, started_at: int) -> dict | None:
        instant = unix_time_ms_to_datetime(started_at)
        candidates = [price for price in self.models
                      if price.provider == provider and price.model == model and price.category == category
                      and price.effective_from <= instant
                      and (price.effective_until is None or instant < price.effective_until)]
        if not candidates:
            return None
        price = max(candidates, key=lambda candidate: candidate.effective_from)
        local = instant.astimezone(ZoneInfo("Asia/Shanghai"))
        clock = local.strftime("%H:%M")
        band = next((band for band in price.time_bands
                     if local.isoweekday() in band.weekdays
                     and any(period.start <= clock < period.end for period in band.ranges)), None)
        tiers = band.tiers if band else price.tiers
        tier = next((tier for tier in tiers if tier.min_input_tokens <= input_tokens
                     and (tier.max_input_tokens is None or input_tokens < tier.max_input_tokens)), None)
        if tier is None:
            return None
        model_price = price.model_dump(mode="json")
        model_price["checked_at"] = datetime_to_unix_time_ms(price.checked_at)
        model_price["effective_from"] = datetime_to_unix_time_ms(price.effective_from)
        model_price["effective_until"] = (
            datetime_to_unix_time_ms(price.effective_until)
            if price.effective_until is not None
            else None
        )
        return {"version": self.version, "currency": self.currency, "unit": self.unit,
                "model_price": model_price, "time_band": band.name if band else None,
                "tier": tier.model_dump(mode="json")}


def load_model_pricing(path: str | Path | None = None) -> ModelPricingConfig:
    source = Path(path) if path is not None else Path(__file__).resolve().parents[2] / "config" / "model-pricing.yaml"
    if not source.is_file() and path is None:
        return ModelPricingConfig()
    payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    return ModelPricingConfig.model_validate(payload)
