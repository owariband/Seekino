from __future__ import annotations

from .runtime import VideoParseRuntime


class ChunkIntervalListener:
    """Stateless callbacks backed by durable host checks and queue notifications."""

    def __init__(self, *, runtime: VideoParseRuntime) -> None:
        self.runtime = runtime

    def update_interval_mask(self, chunk_parse_task_id: str) -> None:
        """OR member bits inside the caller's Chunk-result transaction."""

        self.runtime.update_interval_mask(chunk_parse_task_id)

    async def run_interval_refine_if_ready(
        self, interval_parse_task_id: str
    ) -> None:
        """Notify the queue only after a durable readiness recheck."""

        if await self.runtime.interval_is_ready(interval_parse_task_id):
            await self.runtime.enqueue_interval_refine(interval_parse_task_id)

    async def run_prefix_context_if_ready(self, series_id: str) -> None:
        """Keep the persisted prefix cursor at the leftmost unresolved gap."""

        chunk_task_id = await self.runtime.next_prefix_chunk(series_id)
        if chunk_task_id is not None:
            await self.runtime.enqueue_prefix_context(chunk_task_id)
