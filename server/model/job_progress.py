from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol

from .job import VideoChunkCheckpointStage


class CheckpointProgressSource(Protocol):
    sequence_no: int
    stage: str
    chunk_start_seconds: int | None
    chunk_end_seconds: int | None


@dataclass(frozen=True, slots=True)
class CurrentCheckpointRange:
    sequence_no: int
    start_seconds: int
    end_seconds: int


@dataclass(frozen=True, slots=True)
class CheckpointProgress:
    overall_progress: int
    completed_duration_seconds: int
    completed_through_seconds: int
    completed_chunk_count: int
    current: CurrentCheckpointRange | None


_CHECKPOINT_STAGE_RANK = {
    stage.value: rank for rank, stage in enumerate(VideoChunkCheckpointStage)
}


def calculate_video_parse_progress(
    *, completed_parse_units: int, total_parse_units: int,
    indexed_chunks: int, total_chunks: int,
) -> float | None:
    if total_parse_units <= 0:
        return None
    if total_chunks == 0:
        return completed_parse_units * 100 / total_parse_units
    # Understanding and Refine retain equal unit weights within the first 95%.
    return (completed_parse_units * 95 / total_parse_units
            + indexed_chunks * 5 / total_chunks)


def calculate_checkpoint_progress(
    checkpoints: Iterable[CheckpointProgressSource],
    total_duration_seconds: int,
    *,
    completed: bool = False,
) -> CheckpointProgress | None:
    ordered = tuple(sorted(checkpoints, key=lambda item: item.sequence_no))
    if completed:
        return CheckpointProgress(
            overall_progress=100,
            completed_duration_seconds=total_duration_seconds,
            completed_through_seconds=total_duration_seconds,
            completed_chunk_count=len(ordered),
            current=None,
        )
    if not ordered or total_duration_seconds <= 0:
        return None

    expected_start_seconds = 0
    for expected_sequence_no, checkpoint in enumerate(ordered):
        start_seconds = checkpoint.chunk_start_seconds
        end_seconds = checkpoint.chunk_end_seconds
        if (
            checkpoint.sequence_no != expected_sequence_no
            or start_seconds is None
            or end_seconds is None
            or start_seconds != expected_start_seconds
            or end_seconds <= start_seconds
            or checkpoint.stage not in _CHECKPOINT_STAGE_RANK
        ):
            return None
        expected_start_seconds = end_seconds
    if expected_start_seconds != total_duration_seconds:
        return None

    split_ready_rank = _CHECKPOINT_STAGE_RANK[
        VideoChunkCheckpointStage.SPLIT_READY.value
    ]
    parsed_rank = _CHECKPOINT_STAGE_RANK[VideoChunkCheckpointStage.PARSED.value]
    split_ready = all(
        _CHECKPOINT_STAGE_RANK[item.stage] >= split_ready_rank for item in ordered
    )
    parsed = [
        item
        for item in ordered
        if _CHECKPOINT_STAGE_RANK[item.stage] >= parsed_rank
    ]
    completed_duration_seconds = sum(
        item.chunk_end_seconds - item.chunk_start_seconds for item in parsed
    )
    completed_through_seconds = 0
    current: CurrentCheckpointRange | None = None
    for item in ordered:
        if _CHECKPOINT_STAGE_RANK[item.stage] < parsed_rank:
            current = CurrentCheckpointRange(
                sequence_no=item.sequence_no,
                start_seconds=item.chunk_start_seconds,
                end_seconds=item.chunk_end_seconds,
            )
            break
        completed_through_seconds = item.chunk_end_seconds

    return CheckpointProgress(
        overall_progress=(
            int(split_ready) + completed_duration_seconds * 98 // total_duration_seconds
        ),
        completed_duration_seconds=completed_duration_seconds,
        completed_through_seconds=completed_through_seconds,
        completed_chunk_count=len(parsed),
        current=current,
    )
