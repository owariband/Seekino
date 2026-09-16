from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol

from .job import VideoChunkCheckpointStage


class CheckpointProgressSource(Protocol):
    sequence_no: int
    stage: str
    chunk_start_ms: int | None
    chunk_end_ms: int | None


@dataclass(frozen=True, slots=True)
class CurrentCheckpointRange:
    sequence_no: int
    start_ms: int
    end_ms: int


@dataclass(frozen=True, slots=True)
class CheckpointProgress:
    overall_progress: int
    completed_duration_ms: int
    completed_through_ms: int
    completed_chunk_count: int
    current: CurrentCheckpointRange | None


_CHECKPOINT_STAGE_RANK = {
    stage.value: rank for rank, stage in enumerate(VideoChunkCheckpointStage)
}


def calculate_checkpoint_progress(
    checkpoints: Iterable[CheckpointProgressSource],
    total_duration_ms: int,
    *,
    completed: bool = False,
) -> CheckpointProgress | None:
    ordered = tuple(sorted(checkpoints, key=lambda item: item.sequence_no))
    if completed:
        return CheckpointProgress(
            overall_progress=100,
            completed_duration_ms=total_duration_ms,
            completed_through_ms=total_duration_ms,
            completed_chunk_count=len(ordered),
            current=None,
        )
    if not ordered or total_duration_ms <= 0:
        return None

    expected_start_ms = 0
    for expected_sequence_no, checkpoint in enumerate(ordered):
        start_ms = checkpoint.chunk_start_ms
        end_ms = checkpoint.chunk_end_ms
        if (
            checkpoint.sequence_no != expected_sequence_no
            or start_ms is None
            or end_ms is None
            or start_ms != expected_start_ms
            or end_ms <= start_ms
            or checkpoint.stage not in _CHECKPOINT_STAGE_RANK
        ):
            return None
        expected_start_ms = end_ms
    if expected_start_ms != total_duration_ms:
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
    completed_duration_ms = sum(
        item.chunk_end_ms - item.chunk_start_ms for item in parsed
    )
    completed_through_ms = 0
    current: CurrentCheckpointRange | None = None
    for item in ordered:
        if _CHECKPOINT_STAGE_RANK[item.stage] < parsed_rank:
            current = CurrentCheckpointRange(
                sequence_no=item.sequence_no,
                start_ms=item.chunk_start_ms,
                end_ms=item.chunk_end_ms,
            )
            break
        completed_through_ms = item.chunk_end_ms

    return CheckpointProgress(
        overall_progress=(
            int(split_ready) + completed_duration_ms * 98 // total_duration_ms
        ),
        completed_duration_ms=completed_duration_ms,
        completed_through_ms=completed_through_ms,
        completed_chunk_count=len(parsed),
        current=current,
    )
