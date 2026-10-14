from __future__ import annotations

from collections.abc import Sequence
from math import ceil

from .errors import ParseError
from .state import (
    ChunkScheduleSnapshot,
    ChunkTaskStatus,
    ChunkTaskPhase,
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
            task.understanding_committed for task in snapshot.tasks
        )


def claimable_chunk_sequences(
    tasks: Sequence[tuple[int, int, str, bool]], batch_max_size: int,
) -> frozenset[int]:
    """Apply the batch policy to a fresh snapshot; the host claims under lock."""

    ordered = sorted(tasks)
    if [sequence for sequence, _phase, _status, _committed in ordered] != list(range(len(ordered))):
        raise ParseError(
            "Chunk schedule does not match the fixed consecutive topology",
            retryable=False,
        )
    opened_end = min(batch_max_size, len(ordered))
    previous_start = 0
    while opened_end < len(ordered):
        previous = ordered[previous_start:opened_end]
        ready = sum(committed for _sequence, _phase, _status, committed in previous)
        if ready < ceil(len(previous) * 0.9):
            break
        previous_start = opened_end
        opened_end = min(opened_end + batch_max_size, len(ordered))
    return frozenset(
        sequence for sequence, phase, status, committed in ordered[:opened_end]
        if phase == ChunkTaskPhase.PARSE and status == ChunkTaskStatus.QUEUED and not committed
    )


def chunk_index_ready(
    understanding_committed: bool, covering_refine_committed: Sequence[bool],
) -> bool:
    """Every covering window must have submitted Refine before indexing."""
    return understanding_committed and bool(covering_refine_committed) and all(covering_refine_committed)


def summarize_video_task(
    *, topology_complete: bool, understanding_complete: bool,
    refinement_complete: bool, indexing_complete: bool,
    parsing_failed: bool, indexing_failed: bool,
) -> str:
    """Return the durable work outcome; the host drains executions before failure."""
    if parsing_failed:
        return "PARSING_FAILED"
    if indexing_failed:
        return "INDEXING_FAILED"
    if not (topology_complete and understanding_complete and refinement_complete):
        return "PARSING"
    return "COMPLETE" if indexing_complete else "INDEXING"
