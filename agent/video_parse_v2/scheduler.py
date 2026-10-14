from __future__ import annotations

from collections.abc import Sequence
from math import ceil

from .errors import ParseError
from .state import (
    ChunkScheduleSnapshot,
    ChunkTaskStatus,
)
from .state import RunState


class ChunkScheduler:
    """Observe durable Chunk progress while Workers claim eligible work."""

    def __init__(self, *, runtime) -> None:
        self._runtime = runtime

    async def run(self, state: RunState) -> RunState:
        task_id = state["video_parse_task_id"]
        while True:
            snapshot = await self._runtime.load_chunk_schedule(task_id)
            if snapshot.terminal_error is not None:
                raise ParseError(snapshot.terminal_error, retryable=False)
            if not snapshot.tasks:
                raise ParseError(
                    "Prepared Video task has no Chunk tasks",
                    retryable=False,
                )
            if self._all_chunks_complete(snapshot):
                return state
            await self._runtime.wait_for_video_change(
                task_id,
                timeout_seconds=snapshot.wait_timeout_seconds,
            )

    @staticmethod
    def _all_chunks_complete(snapshot: ChunkScheduleSnapshot) -> bool:
        return bool(snapshot.tasks) and all(
            task.status == ChunkTaskStatus.COMPLETE for task in snapshot.tasks
        )


def claimable_chunk_sequences(
    tasks: Sequence[tuple[int, str]], batch_max_size: int,
) -> frozenset[int]:
    """Apply the batch policy to a fresh snapshot; the host claims under lock."""

    ordered = sorted(tasks)
    if [sequence for sequence, _status in ordered] != list(range(len(ordered))):
        raise ParseError(
            "Chunk schedule does not match the fixed consecutive topology",
            retryable=False,
        )
    active = sum(status == ChunkTaskStatus.UNDERSTANDING for _sequence, status in ordered)
    if active >= batch_max_size:
        return frozenset()
    opened_end = min(batch_max_size, len(ordered))
    previous_start = 0
    while opened_end < len(ordered):
        previous = ordered[previous_start:opened_end]
        ready = sum(status == ChunkTaskStatus.COMPLETE for _sequence, status in previous)
        if ready < ceil(len(previous) * 0.9):
            break
        previous_start = opened_end
        opened_end = min(opened_end + batch_max_size, len(ordered))
    return frozenset(
        sequence for sequence, status in ordered[:opened_end]
        if status == ChunkTaskStatus.QUEUED
    )
