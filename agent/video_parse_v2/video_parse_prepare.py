from __future__ import annotations

from engine.parsev2.planning import (
    interval_size,
    plan_chunks,
    plan_intervals,
    validate_chunk_topology,
)

from .chunk_interval_listener import ChunkIntervalListener
from .runtime import VideoParseRuntime, VideoTaskStatus
from .state import RunState


class VideoParsePrepare:
    """Fix topology and idempotently initialize one persisted parse task."""

    def __init__(
        self,
        *,
        runtime: VideoParseRuntime,
        listener: ChunkIntervalListener,
    ) -> None:
        self._runtime = runtime
        self._listener = listener

    async def run(self, state: RunState) -> RunState:
        task_id = state["video_parse_task_id"]
        inputs = state["inputs"]
        plans = plan_chunks(
            duration_seconds=state["duration_seconds"],
            chunk_duration=inputs.parse_config.chunk_duration,
        )
        lease = state["lease"]
        chunks = await self._runtime.ensure_video_chunks(task_id, lease, plans)
        validate_chunk_topology(
            tuple(chunk.plan for chunk in chunks),
            duration_seconds=state["duration_seconds"],
            chunk_duration=inputs.parse_config.chunk_duration,
        )

        size = interval_size(
            interval_duration=inputs.parse_config.interval_duration,
            chunk_duration=inputs.parse_config.chunk_duration,
        )
        intervals = plan_intervals(chunk_count=len(chunks), size=size)
        await self._runtime.ensure_parse_tasks(task_id, lease, chunks, intervals)
        await self._runtime.activate_video_task(task_id, lease)
        return {
            **state,
            "status": VideoTaskStatus.PARSING,
            "video_chunks": chunks,
        }
