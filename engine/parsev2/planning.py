"""Deterministic topology and Interval planning for Video Parse V2."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Sequence


MAX_INTERVAL_CHUNKS = 63


def _positive_integer(value: int, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class ChunkPlan:
    """One immutable source-video Chunk using half-open integer-second bounds."""

    sequence_no: int
    start_seconds: int
    end_seconds: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.sequence_no, bool)
            or not isinstance(self.sequence_no, int)
            or self.sequence_no < 0
        ):
            raise ValueError("sequence_no must be a nonnegative integer")
        if (
            isinstance(self.start_seconds, bool)
            or not isinstance(self.start_seconds, int)
            or self.start_seconds < 0
            or isinstance(self.end_seconds, bool)
            or not isinstance(self.end_seconds, int)
            or self.end_seconds <= self.start_seconds
        ):
            raise ValueError("Chunk bounds must be increasing nonnegative integers")


@dataclass(frozen=True, slots=True)
class IntervalPlan:
    """One complete or short sliding window over consecutive Chunk sequences."""

    start_sequence_no: int
    chunk_count: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.start_sequence_no, bool)
            or not isinstance(self.start_sequence_no, int)
            or self.start_sequence_no < 0
        ):
            raise ValueError("start_sequence_no must be a nonnegative integer")
        if (
            isinstance(self.chunk_count, bool)
            or not isinstance(self.chunk_count, int)
            or not 1 <= self.chunk_count <= MAX_INTERVAL_CHUNKS
        ):
            raise ValueError(
                f"chunk_count must be between 1 and {MAX_INTERVAL_CHUNKS}"
            )

    @property
    def end_sequence_no(self) -> int:
        return self.start_sequence_no + self.chunk_count

    @property
    def member_sequence_nos(self) -> tuple[int, ...]:
        return tuple(range(self.start_sequence_no, self.end_sequence_no))

    @property
    def full_ready_mask(self) -> int:
        return (1 << self.chunk_count) - 1

    def contains(self, sequence_no: int) -> bool:
        return self.start_sequence_no <= sequence_no < self.end_sequence_no


def plan_chunks(*, duration_seconds: int, chunk_duration: int) -> tuple[ChunkPlan, ...]:
    """Split a positive integer duration into stable half-open Chunk boundaries."""

    duration_seconds = _positive_integer(duration_seconds, name="duration_seconds")
    chunk_duration = _positive_integer(chunk_duration, name="chunk_duration")
    return tuple(
        ChunkPlan(
            sequence_no=sequence_no,
            start_seconds=sequence_no * chunk_duration,
            end_seconds=min((sequence_no + 1) * chunk_duration, duration_seconds),
        )
        for sequence_no in range(ceil(duration_seconds / chunk_duration))
    )


def interval_size(*, interval_duration: int, chunk_duration: int) -> int:
    """Return the number of Chunks needed to cover the configured Interval duration."""

    interval_duration = _positive_integer(
        interval_duration, name="interval_duration"
    )
    chunk_duration = _positive_integer(chunk_duration, name="chunk_duration")
    size = ceil(interval_duration / chunk_duration)
    if size > MAX_INTERVAL_CHUNKS:
        raise ValueError(
            f"interval requires more than {MAX_INTERVAL_CHUNKS} chunks"
        )
    return size


def plan_intervals(*, chunk_count: int, size: int) -> tuple[IntervalPlan, ...]:
    """Plan complete sliding windows, or the one required short-video window."""

    chunk_count = _positive_integer(chunk_count, name="chunk_count")
    size = _positive_integer(size, name="size")
    if size > MAX_INTERVAL_CHUNKS:
        raise ValueError(f"size must not exceed {MAX_INTERVAL_CHUNKS}")
    if chunk_count < size:
        return (IntervalPlan(start_sequence_no=0, chunk_count=chunk_count),)
    return tuple(
        IntervalPlan(start_sequence_no=start, chunk_count=size)
        for start in range(chunk_count - size + 1)
    )


def validate_chunk_topology(
    chunks: Sequence[ChunkPlan],
    *,
    duration_seconds: int,
    chunk_duration: int,
) -> tuple[ChunkPlan, ...]:
    """Reject an existing topology that differs from the fixed planning contract."""

    actual = tuple(chunks)
    expected = plan_chunks(
        duration_seconds=duration_seconds,
        chunk_duration=chunk_duration,
    )
    if actual != expected:
        raise ValueError("existing Chunk topology is incompatible with parse_config")
    return actual


def containing_intervals(
    sequence_no: int,
    intervals: Sequence[IntervalPlan],
) -> tuple[IntervalPlan, ...]:
    """Return every fixed Interval containing one Chunk sequence."""

    if (
        isinstance(sequence_no, bool)
        or not isinstance(sequence_no, int)
        or sequence_no < 0
    ):
        raise ValueError("sequence_no must be a nonnegative integer")
    return tuple(interval for interval in intervals if interval.contains(sequence_no))
