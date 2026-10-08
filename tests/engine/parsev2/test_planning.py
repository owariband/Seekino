from __future__ import annotations

import pytest

from engine.parsev2.planning import (
    ChunkPlan,
    containing_intervals,
    interval_size,
    plan_chunks,
    plan_intervals,
    validate_chunk_topology,
)


def test_chunk_plan_uses_fixed_half_open_bounds_and_short_final_chunk() -> None:
    assert plan_chunks(duration_seconds=61000, chunk_duration=20) == (
        ChunkPlan(0, 0, 20000),
        ChunkPlan(1, 20000, 40000),
        ChunkPlan(2, 40000, 60000),
        ChunkPlan(3, 60000, 61000),
    )
    assert plan_chunks(duration_seconds=40000, chunk_duration=20)[-1].end_seconds == 40000


def test_chunk_plan_retains_fractional_final_duration_with_second_configuration() -> None:
    assert plan_chunks(duration_seconds=61008, chunk_duration=20)[-1] == ChunkPlan(3, 60000, 61008)
    assert plan_chunks(duration_seconds=250, chunk_duration=20) == (ChunkPlan(0, 0, 250),)


def test_interval_plan_uses_ceil_and_only_complete_windows_for_long_video() -> None:
    size = interval_size(interval_duration=61, chunk_duration=20)
    assert size == 4
    intervals = plan_intervals(chunk_count=6, size=size)

    assert [interval.member_sequence_nos for interval in intervals] == [
        (0, 1, 2, 3),
        (1, 2, 3, 4),
        (2, 3, 4, 5),
    ]
    assert all(interval.full_ready_mask == 0b1111 for interval in intervals)


def test_short_video_has_one_short_interval_instead_of_zero_intervals() -> None:
    intervals = plan_intervals(chunk_count=2, size=3)
    assert len(intervals) == 1
    assert intervals[0].member_sequence_nos == (0, 1)
    assert intervals[0].full_ready_mask == 0b11


def test_containing_intervals_matches_edge_and_overlap_release_example() -> None:
    intervals = plan_intervals(chunk_count=4, size=3)
    assert [item.start_sequence_no for item in containing_intervals(0, intervals)] == [0]
    assert [item.start_sequence_no for item in containing_intervals(1, intervals)] == [0, 1]
    assert [item.start_sequence_no for item in containing_intervals(2, intervals)] == [0, 1]
    assert [item.start_sequence_no for item in containing_intervals(3, intervals)] == [1]


def test_existing_topology_must_match_the_fixed_config() -> None:
    chunks = plan_chunks(duration_seconds=41000, chunk_duration=20)
    assert validate_chunk_topology(
        chunks,
        duration_seconds=41000,
        chunk_duration=20,
    ) == chunks

    incompatible = (*chunks[:-1], ChunkPlan(2, 40000, 42000))
    with pytest.raises(ValueError, match="incompatible"):
        validate_chunk_topology(
            incompatible,
            duration_seconds=41000,
            chunk_duration=20,
        )


@pytest.mark.parametrize(
    ("chunk_count", "size"),
    [(0, 3), (3, 0), (3, 64), (True, 3)],
)
def test_interval_planning_rejects_invalid_or_unrepresentable_counts(
    chunk_count: int,
    size: int,
) -> None:
    with pytest.raises(ValueError):
        plan_intervals(chunk_count=chunk_count, size=size)
