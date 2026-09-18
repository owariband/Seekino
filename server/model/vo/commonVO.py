from __future__ import annotations

from typing import Annotated, Generic, TypeVar

from pydantic import Field

from ...schema import ContractModel
from ...utils.time import MAX_UNIX_TIME_MS


UnixTimeMs = Annotated[
    int,
    Field(strict=True, ge=0, le=MAX_UNIX_TIME_MS),
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
