from __future__ import annotations

import asyncio
from math import ceil

from .errors import ParseError
from .runtime import (
    ChunkScheduleEntry,
    ChunkScheduleSnapshot,
    ChunkTaskStatus,
    VideoParseRuntime,
)
from .state import RunState


class ChunkScheduler:
    """Open ordered batches and ask the host to dispatch due Chunk work."""

    def __init__(self, *, runtime: VideoParseRuntime) -> None:
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
            await self._dispatch_ready_chunks(state, snapshot)
            await self._runtime.wait_for_video_change(
                task_id,
                timeout_seconds=snapshot.wait_timeout_seconds,
            )

    @staticmethod
    def _all_chunks_complete(snapshot: ChunkScheduleSnapshot) -> bool:
        return bool(snapshot.tasks) and all(
            task.status == ChunkTaskStatus.COMPLETE for task in snapshot.tasks
        )

    async def _dispatch_ready_chunks(
        self,
        state: RunState,
        snapshot: ChunkScheduleSnapshot,
    ) -> None:
        config = state["inputs"].parse_config
        ordered = sorted(snapshot.tasks, key=lambda task: task.sequence_no)
        if [task.sequence_no for task in ordered] != list(range(len(ordered))):
            raise ParseError(
                "Chunk schedule does not match the fixed consecutive topology",
                retryable=False,
            )

        opened_end = self._opened_sequence_end(ordered, config.batch_max_size)
        active_count = sum(
            task.status == ChunkTaskStatus.UNDERSTANDING for task in ordered
        )
        capacity = max(0, config.batch_max_size - active_count)
        candidates = [
            task
            for task in ordered[:opened_end]
            if task.status == ChunkTaskStatus.QUEUED and task.dispatchable
        ][:capacity]

        accepted = 0
        for candidate in candidates:
            if accepted and config.dispatch_interval_seconds:
                await asyncio.sleep(config.dispatch_interval_seconds)
            if await self._runtime.dispatch_chunk(
                candidate.chunk_parse_task_id,
                state["lease"],
            ):
                accepted += 1

    @staticmethod
    def _opened_sequence_end(
        tasks: list[ChunkScheduleEntry],
        batch_max_size: int,
    ) -> int:
        opened_end = min(batch_max_size, len(tasks))
        previous_start = 0
        while opened_end < len(tasks):
            previous = tasks[previous_start:opened_end]
            threshold = ceil(len(previous) * 0.9)
            ready = sum(task.status == ChunkTaskStatus.COMPLETE for task in previous)
            if ready < threshold:
                break
            previous_start = opened_end
            opened_end = min(opened_end + batch_max_size, len(tasks))
        return opened_end
