from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import TypeAlias

UnixTimeMs: TypeAlias = int
DurationMs: TypeAlias = int
MAX_UNIX_TIME_MS = 9_223_372_036_854_775_807


def now_unix_time_ms() -> UnixTimeMs:
    return time.time_ns() // 1_000_000


def now_utc() -> datetime:
    """Return an aware UTC datetime at the persistence contract's ms precision."""

    instant = datetime.now(UTC)
    return instant.replace(microsecond=(instant.microsecond // 1_000) * 1_000)


def require_utc_datetime(
    value: datetime,
    name: str = "timestamp",
) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def normalize_database_utc_datetime(
    value: datetime,
    name: str = "timestamp",
) -> datetime:
    """Normalize a DB datetime, treating a naive value as UTC by contract."""

    if not isinstance(value, datetime):
        raise ValueError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def require_unix_time_ms(
    value: int,
    name: str = "timestamp",
) -> UnixTimeMs:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > MAX_UNIX_TIME_MS
    ):
        raise ValueError(f"{name} must be a nonnegative signed INT64 Unix timestamp in milliseconds")
    return value


def datetime_to_unix_time_ms(value: datetime) -> UnixTimeMs:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include an explicit timezone")
    instant = value.astimezone(UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    elapsed = instant - epoch
    return require_unix_time_ms(
        elapsed.days * 86_400_000
        + elapsed.seconds * 1_000
        + elapsed.microseconds // 1_000
    )


def unix_time_ms_to_datetime(value: UnixTimeMs) -> datetime:
    return datetime.fromtimestamp(
        require_unix_time_ms(value) / 1_000,
        tz=UTC,
    )


def timedelta_to_ms(value: timedelta) -> DurationMs:
    return (
        value.days * 86_400_000
        + value.seconds * 1_000
        + value.microseconds // 1_000
    )
