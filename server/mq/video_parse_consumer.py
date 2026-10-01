from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import TypeVar

from ..model.dto.jobDTO import ChunkParseLease, IntervalParseLease, VideoParseLease
from ..utils.time import now_utc
from .video_parse_queue import MySqlVideoParseQueue


@dataclass(frozen=True, slots=True)
class VideoParseConsumerHandlers:
    prepare: Callable[[VideoParseLease], Awaitable[None]]
    understand: Callable[[ChunkParseLease], Awaitable[None]]
    refine: Callable[[IntervalParseLease], Awaitable[None]]
    index: Callable[[IntervalParseLease], Awaitable[None]]
    prefix: Callable[[], Awaitable[bool]] | None = None
    maintenance: Callable[[], Awaitable[object]] | None = None


_Lease = TypeVar("_Lease", VideoParseLease, ChunkParseLease, IntervalParseLease)
_logger = logging.getLogger(__name__)


class VideoParseConsumer:
    """Small Worker loop over the durable V2 queue.

    Handlers own successful transactional commits. The consumer owns only
    claiming, heartbeats, retry scheduling, and expired-lease recovery.
    """

    def __init__(
        self,
        queue: MySqlVideoParseQueue,
        handlers: VideoParseConsumerHandlers,
        *,
        worker_id: str,
        lease_duration: timedelta = timedelta(minutes=5),
        heartbeat_interval: timedelta | None = None,
        retry_initial_delay: timedelta = timedelta(seconds=5),
        retry_max_delay: timedelta = timedelta(minutes=5),
        poll_interval: timedelta = timedelta(seconds=2),
        maintenance_interval: timedelta | None = None,
        max_concurrent_handlers: int = 8,
    ) -> None:
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        effective_heartbeat_interval = (
            lease_duration / 3
            if heartbeat_interval is None
            else heartbeat_interval
        )
        if not timedelta(0) < effective_heartbeat_interval < lease_duration:
            raise ValueError("heartbeat_interval must be positive and below lease_duration")
        if retry_initial_delay <= timedelta(0):
            raise ValueError("retry_initial_delay must be positive")
        if retry_max_delay < retry_initial_delay:
            raise ValueError("retry_max_delay cannot be shorter than initial delay")
        if poll_interval <= timedelta(0):
            raise ValueError("poll_interval must be positive")
        effective_maintenance_interval = (
            lease_duration
            if maintenance_interval is None
            else maintenance_interval
        )
        if effective_maintenance_interval <= timedelta(0):
            raise ValueError("maintenance_interval must be positive")
        if max_concurrent_handlers < 2:
            raise ValueError(
                "max_concurrent_handlers must leave one coordinator and one work slot"
            )
        self._queue = queue
        self._handlers = handlers
        self._worker_id = worker_id
        self._lease_duration = lease_duration
        self._heartbeat_interval = effective_heartbeat_interval
        self._retry_initial_delay = retry_initial_delay
        self._retry_max_delay = retry_max_delay
        self._poll_interval = poll_interval
        self._maintenance_interval = effective_maintenance_interval
        self._next_maintenance_at = 0.0
        self._max_concurrent_handlers = max_concurrent_handlers
        self._video_tasks: set[asyncio.Task[None]] = set()
        self._work_tasks: set[asyncio.Task[None]] = set()
        self._prefix_task: asyncio.Task[None] | None = None
        self._lifecycle_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_async: asyncio.Event | None = None
        self._stop_requested = threading.Event()
        self._failure: Exception | None = None

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def failure(self) -> Exception | None:
        return self._failure

    def start(self) -> None:
        with self._lifecycle_lock:
            if self.running:
                return
            self._stop_requested.clear()
            self._failure = None
            self._next_maintenance_at = 0.0
            self._thread = threading.Thread(
                target=self._thread_main,
                name="video-parse-v2-consumer",
                daemon=True,
            )
            self._thread.start()

    def close(self, timeout: float | None = None) -> None:
        self._stop_requested.set()
        loop = self._loop
        stop = self._stop_async
        if loop is not None and stop is not None:
            loop.call_soon_threadsafe(stop.set)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def run_once_sync(self) -> bool:
        """Run one initial claim and its dependent local work to completion."""

        with self._lifecycle_lock:
            if self.running:
                raise RuntimeError("cannot run once while the consumer thread is running")
            if self._video_tasks or self._work_tasks:
                raise RuntimeError("consumer already has work bound to another event loop")
        return asyncio.run(self._run_one_claim_to_completion())

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._thread_run())
        except Exception as exc:
            self._failure = exc

    async def _thread_run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_async = asyncio.Event()
        if self._stop_requested.is_set():
            self._stop_async.set()
        try:
            await self.run_forever(self._stop_async)
        finally:
            self._loop = None
            self._stop_async = None

    async def run_once(self) -> bool:
        await asyncio.to_thread(
            self._queue.reap_expired,
            retry_delay=self._retry_initial_delay,
        )
        await self._run_maintenance_if_due()
        self._video_tasks = {task for task in self._video_tasks if not task.done()}
        self._work_tasks = {task for task in self._work_tasks if not task.done()}
        if not self._video_tasks:
            video = await asyncio.to_thread(
                self._queue.claim_video,
                self._worker_id,
                lease_duration=self._lease_duration,
            )
            if video is not None:
                self._track(asyncio.create_task(self._run_video(video)), coordinator=True)
                return True

        # The coordinator may spend most of its life waiting for durable child
        # changes, so child phases run in separate bounded slots.
        if len(self._work_tasks) >= self._max_concurrent_handlers - 1:
            return False

        chunk = await asyncio.to_thread(
            self._queue.claim_chunk,
            self._worker_id,
            lease_duration=self._lease_duration,
        )
        if chunk is not None:
            self._track(asyncio.create_task(self._run_chunk(chunk)))
            return True

        refine = await asyncio.to_thread(
            self._queue.claim_interval_refine,
            self._worker_id,
            lease_duration=self._lease_duration,
        )
        if refine is not None:
            self._track(asyncio.create_task(
                self._run_interval(refine, self._handlers.refine)
            ))
            return True

        indexing = await asyncio.to_thread(
            self._queue.claim_interval_index,
            self._worker_id,
            lease_duration=self._lease_duration,
        )
        if indexing is not None:
            self._track(asyncio.create_task(
                self._run_interval(indexing, self._handlers.index)
            ))
            return True
        if (
            self._handlers.prefix is not None
            and (self._prefix_task is None or self._prefix_task.done())
        ):
            self._prefix_task = asyncio.create_task(self._run_prefix())
            self._track(self._prefix_task)
            return True
        return False

    async def run_forever(self, stop: asyncio.Event) -> None:
        try:
            while not stop.is_set():
                worked = await self.run_once()
                if worked:
                    continue
                try:
                    await asyncio.wait_for(
                        stop.wait(), timeout=self._poll_interval.total_seconds()
                    )
                except TimeoutError:
                    pass
        finally:
            pending = tuple(self._video_tasks | self._work_tasks)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            self._video_tasks.clear()
            self._work_tasks.clear()

    async def _run_one_claim_to_completion(self) -> bool:
        worked = await self.run_once()
        if not worked:
            return False
        initial_video_tasks = tuple(self._video_tasks)
        while True:
            await asyncio.sleep(0)
            pending = tuple(
                task
                for task in self._video_tasks | self._work_tasks
                if not task.done()
            )
            if not pending:
                if self._failure is not None:
                    raise self._failure
                return True
            coordinator_running = any(
                not task.done() for task in initial_video_tasks
            )
            if coordinator_running:
                scheduled = await self.run_once()
                if scheduled:
                    continue
            await asyncio.wait(
                pending,
                timeout=self._poll_interval.total_seconds(),
                return_when=asyncio.FIRST_COMPLETED,
            )

    def _track(
        self,
        task: asyncio.Task[None],
        *,
        coordinator: bool = False,
    ) -> None:
        target = self._video_tasks if coordinator else self._work_tasks
        target.add(task)
        task.add_done_callback(
            lambda completed: self._task_done(completed, target)
        )

    def _task_done(
        self,
        task: asyncio.Task[None],
        target: set[asyncio.Task[None]],
    ) -> None:
        target.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return
        if self._failure is None:
            self._failure = error
        _logger.error(
            "Video Parse V2 consumer task failed",
            exc_info=(type(error), error, error.__traceback__),
        )
        if self._stop_async is not None:
            self._stop_async.set()

    async def _run_video(self, lease: VideoParseLease) -> None:
        try:
            await self._with_heartbeat(
                lease,
                self._handlers.prepare,
                self._queue.heartbeat_video,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            retry_at = now_utc() + self._retry_delay(lease.attempt_count)
            await asyncio.to_thread(
                self._queue.retry_video,
                lease,
                error=str(exc),
                retry_at=retry_at,
            )

    async def _run_chunk(self, lease: ChunkParseLease) -> None:
        try:
            await self._with_heartbeat(
                lease,
                self._handlers.understand,
                self._queue.heartbeat_chunk,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            retry_at = now_utc() + self._retry_delay(lease.retry_count + 1)
            await asyncio.to_thread(
                self._queue.retry_chunk,
                lease,
                error=str(exc),
                retry_at=retry_at,
            )

    async def _run_interval(
        self,
        lease: IntervalParseLease,
        handler: Callable[[IntervalParseLease], Awaitable[None]],
    ) -> None:
        try:
            await self._with_heartbeat(
                lease,
                handler,
                self._queue.heartbeat_interval,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            retry_at = now_utc() + self._retry_delay(lease.retry_count + 1)
            await asyncio.to_thread(
                self._queue.retry_interval,
                lease,
                error=str(exc),
                retry_at=retry_at,
            )

    async def _run_prefix(self) -> None:
        assert self._handlers.prefix is not None
        worked = await self._handlers.prefix()
        if not worked:
            await asyncio.sleep(self._poll_interval.total_seconds())

    async def _with_heartbeat(
        self,
        lease: _Lease,
        handler: Callable[[_Lease], Awaitable[None]],
        heartbeat: Callable[..., bool],
    ) -> None:
        result = handler(lease)
        if not inspect.isawaitable(result):
            raise TypeError("VideoParseConsumer handlers must be async")
        work = asyncio.create_task(result)
        interval = self._heartbeat_interval.total_seconds()
        try:
            while True:
                done, _pending = await asyncio.wait(
                    {work}, timeout=interval, return_when=asyncio.FIRST_COMPLETED
                )
                if work in done:
                    await work
                    return
                current = await asyncio.to_thread(
                    heartbeat,
                    lease,
                    lease_duration=self._lease_duration,
                )
                if not current:
                    committed = await asyncio.to_thread(
                        self._queue.is_execution_lease_committed,
                        lease,
                    )
                    if committed:
                        await work
                        return
                    work.cancel()
                    await asyncio.gather(work, return_exceptions=True)
                    raise RuntimeError("Video Parse V2 worker lease was lost")
        finally:
            if not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)

    async def _run_maintenance_if_due(self) -> None:
        handler = self._handlers.maintenance
        if handler is None:
            return
        current = time.monotonic()
        if current < self._next_maintenance_at:
            return
        self._next_maintenance_at = (
            current + self._maintenance_interval.total_seconds()
        )
        try:
            result = handler()
            if not inspect.isawaitable(result):
                raise TypeError("VideoParseConsumer maintenance must be async")
            await result
        except Exception:
            # Cleanup is best effort and its active-lease predicate fails
            # closed. A transient scan failure must not stop parse work.
            _logger.exception("Video Parse V2 maintenance failed")

    def _retry_delay(self, failure_count: int) -> timedelta:
        multiplier = 2 ** max(0, failure_count - 1)
        seconds = min(
            self._retry_initial_delay.total_seconds() * multiplier,
            self._retry_max_delay.total_seconds(),
        )
        return timedelta(seconds=seconds)
