"""Shared deterministic validation and canonical fingerprints."""

from __future__ import annotations

import hashlib
import json
import math

def _sha256_json(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_bounds(start_seconds: int, end_seconds: int) -> None:
    if (
        isinstance(start_seconds, bool)
        or not isinstance(start_seconds, int)
        or start_seconds < 0
        or isinstance(end_seconds, bool)
        or not isinstance(end_seconds, int)
        or end_seconds <= start_seconds
    ):
        raise ValueError("Chunk bounds must be increasing nonnegative integer milliseconds")


def _nonblank(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _positive_finite(value: object) -> bool:
    return _nonnegative_finite(value) and value > 0  # type: ignore[operator]


def _nonnegative_finite(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value >= 0
    )


def is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )
