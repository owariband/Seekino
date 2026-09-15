from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Generic, TypeVar

from pydantic import BeforeValidator, Field

from ...schema import ContractModel


def unix_time_ms(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    elapsed = value.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return elapsed.days * 86_400_000 + elapsed.seconds * 1000 + elapsed.microseconds // 1000


def _absolute_time(value: object) -> object:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return unix_time_ms(value) if isinstance(value, datetime) else value


UnixTimeMs = Annotated[
    int,
    BeforeValidator(_absolute_time),
    Field(strict=True, ge=0, le=9_007_199_254_740_991),
]


class Failure(ContractModel):
    code: str
    message: str


class FieldError(ContractModel):
    field: str
    message: str


class ApiError(ContractModel):
    code: str
    message: str
    request_id: str
    field_errors: list[FieldError] = Field(default_factory=list)
    retry_at: UnixTimeMs | None = None


T = TypeVar("T")


class Page(ContractModel, Generic[T]):
    items: list[T]
    total: int = Field(ge=0)
    next_cursor: str | None
    has_more: bool


class ImageAsset(ContractModel):
    image_id: str
    url: str
    expires_at: UnixTimeMs | None = None
