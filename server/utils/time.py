from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_now_naive() -> datetime:
    """UTC without tzinfo, matching the persisted MySQL DateTime convention."""
    return utc_now().replace(tzinfo=None)


def as_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)
