from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import BigInteger, DateTime
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.engine import Dialect
from sqlalchemy.engine.default import DefaultExecutionContext
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from ..database import Base
from ..utils.time import (
    normalize_database_utc_datetime,
    now_unix_time_ms,
    require_utc_datetime,
    require_unix_time_ms,
)


class UtcDateTime(TypeDecorator[datetime]):
    """UTC dates with millisecond precision, stored as MySQL DATETIME(3)."""

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect):  # type: ignore[no-untyped-def]
        if dialect.name == "mysql":
            return dialect.type_descriptor(DATETIME(fsp=3))
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        instant = require_utc_datetime(value)
        return instant.replace(microsecond=instant.microsecond // 1_000 * 1_000, tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return None if value is None else normalize_database_utc_datetime(value)


def _create_date(context: DefaultExecutionContext) -> datetime | None:
    value = context.get_current_parameters()["create_time"]
    if value is None:
        return None
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=require_unix_time_ms(value))


def _initial_update_time(context: DefaultExecutionContext) -> int | None:
    return context.get_current_parameters()["create_time"]


def _update_date(context: DefaultExecutionContext) -> datetime | None:
    value = context.get_current_parameters()["update_time"]
    if value is None:
        return None
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=require_unix_time_ms(value))


class BaseModel(Base):
    __abstract__ = True

    # The integer columns are written first; each date derives from that value.
    # Column defaults also apply to SQLAlchemy Core/bulk INSERT and UPDATE.
    create_time: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True, default=now_unix_time_ms,
    )
    create_date: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True, index=True, default=_create_date,
    )
    update_time: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True,
        default=_initial_update_time, onupdate=now_unix_time_ms,
    )
    update_date: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True, index=True,
        default=_update_date, onupdate=_update_date,
    )
