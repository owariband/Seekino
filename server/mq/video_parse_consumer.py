from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TypeVar

from agent.video_parse_v2.errors import RefineProviderRetryExhaustedError
from agent.video_parse_v2.state import ChunkTaskPhase

from ..model.dto.jobDTO import ChunkParseLease, IntervalParseLease, VideoParseLease
from ..utils.time import now_utc
from ..utils.retry import task_retry_delay
from .video_parse_queue import MySqlVideoParseQueue


@dataclass(frozen=True, slots=True)
class VideoParseConsumerHandlers:
    prepare: Callable[[VideoParseLease], Awaitable[None]]
    understand: Callable[[ChunkParseLease], Awaitable[None]]
    refine: Callable[[IntervalParseLease], Awaitable[None]]
    index: Callable[[ChunkParseLease], Awaitable[None]]
    prefix: Callable[[], Awaitable[bool]] | None = None
    maintenance: Callable[[], Awaitable[object]] | None = None
    summarize: Callable[[], Awaitable[bool]] | None = None


_Lease = TypeVar("_Lease", VideoParseLease, ChunkParseLease, IntervalParseLease)
_logger = logging.getLogger(__name__)


def _failure_log_text(value: object) -> str | int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if not isinstance(value, str):
        return None
    # Provider messages may reflect credentials. Never inspect bodies or headers.
    value = value[:8192]
    value = re.sub(
        r"(?is)[\"']?\b(?:authorization|proxy-authorization|cookie|set-cookie)[\"']?\s*[:=].*",
        "[redacted_header]", value,
    )
    value = re.sub(
        r"(?is)[\"']?\b(?:body|headers|payload|input|prompt|messages)[\"']?\s*[:=].*",
        "[redacted_payload]", value,
    )
    value = re.sub(
        r"(?i)([\"']?(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|token|"
        r"password|passwd|secret|signature|credential)[\"']?\s*[:=]\s*)"
        r"(?:\"[^\"]*\"|'[^']*'|[^\s,;&]+)",
        r"\1[redacted]", value,
    )
    value = re.sub(
        r"(?i)\b(?:bearer|basic)\s+\S+|\b(?:sk|ak)-[\w-]{8,}|\beyJ[\w-]+\.[\w-]+\.[\w-]+",
        "[redacted]", value,
    )
    value = re.sub(
        r"(?i)\bdata:[^,\s]*,[a-z0-9+/=]+|\b[a-z][a-z0-9+.-]*://[^\s<>\"']+",
        "[redacted_url]", value,
    )
    return value[:2048]


def _log_retry_failure(
    lease: _Lease, *, phase: str, error: Exception,
    recorded: bool, retry_at: datetime | None, prepare_failure_count: int | None = None,
) -> None:
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen and len(chain) < 32:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    root = chain[-1]
    error_chain = [{
        "error_type": type(item).__name__,
        "message": _failure_log_text(getattr(item, "message", None) or str(item)),
    } for item in chain]
    fields = {
        "video_parse_task_id": lease.video_parse_task_id,
        "chunk_parse_task_id": getattr(lease, "chunk_parse_task_id", None),
        "interval_parse_task_id": getattr(lease, "interval_parse_task_id", None),
        "phase": phase,
        "attempt": None if isinstance(lease, VideoParseLease) else lease.retry_count + 1,
        "max_retry_count": getattr(lease, "max_retry_count", None),
        "lease_version": lease.lease_version,
        "retry_recorded": bool(recorded),
        "retry_at": retry_at.isoformat() if retry_at is not None else None,
        "error_type": type(error).__name__,
        "reason": _failure_log_text(getattr(error, "reason", None)),
        "root_error_type": type(root).__name__,
        "root_error_message": error_chain[-1]["message"],
        "error_chain": error_chain,
        "error_chain_truncated": current is not None and id(current) not in seen,
    }
    if isinstance(lease, VideoParseLease):
        fields["coordinator_attempt"] = lease.attempt_count
        fields["prepare_failure_count"] = prepare_failure_count
    if isinstance(error, RefineProviderRetryExhaustedError):
        fields["provider_retries"] = error.provider_retries
        fields["retry_budget"] = "provider"
    for field, attribute in (
        ("root_error_code", "code"), ("root_status_code", "status_code"),
        ("model_request_id", "request_id"), ("response_id", "response_id"),
        ("partial_output_chars", "partial_output_chars"),
    ):
        fields[field] = next((
            value for item in reversed(chain)
            if (value := _failure_log_text(getattr(item, attribute, None))) is not None
        ), None)
    details = " ".join(
        f"{key}={json.dumps(value, ensure_ascii=False)}"
        for key, value in fields.items()
    )
    _logger.warning("event=video_parse_v2_retry %s", details)
    if recorded and isinstance(error, RefineProviderRetryExhaustedError):
        _logger.error(
            'event=video_parse_v2_terminal_error terminal_reason="provider_retry_budget_exhausted" %s',
            details,
        )
    elif (recorded and not isinstance(lease, VideoParseLease)
          and lease.retry_count + 1 > lease.max_retry_count):
        _logger.error(
            'event=video_parse_v2_terminal_error terminal_reason="child_retry_budget_exhausted" %s',
            details,
        )


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
        lease_duration: timedelta,
        heartbeat_interval: timedelta,
        poll_interval: timedelta,
        maintenance_interval: timedelta,
        video_concurrency: int,
        summarize_concurrency: int = 1,
    ) -> None:
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        if not timedelta(0) < heartbeat_interval < lease_duration:
            raise ValueError("heartbeat_interval must be positive and below lease_duration")
        if poll_interval <= timedelta(0):
            raise ValueError("poll_interval must be positive")
        if maintenance_interval <= timedelta(0):
            raise ValueError("maintenance_interval must be positive")
        if type(video_concurrency) is not int or video_concurrency <= 0:
            raise ValueError("video_concurrency must be a positive integer")
        if type(summarize_concurrency) is not int or summarize_concurrency <= 0:
            raise ValueError("summarize_concurrency must be a positive integer")
        self._queue = queue
        self._handlers = handlers
        self._worker_id = worker_id
        self._lease_duration = lease_duration
        self._heartbeat_interval = heartbeat_interval
        self._poll_interval = poll_interval
        self._maintenance_interval = maintenance_interval
        self._next_maintenance_at = 0.0
        self._video_concurrency = video_concurrency
        self._phase_cursor = 0
        self._video_tasks: set[asyncio.Task[None]] = set()
        self._work_tasks: set[asyncio.Task[None]] = set()
        self._prefix_task: asyncio.Task[None] | None = None
        self._summarize_concurrency = summarize_concurrency
        self._summary_tasks: set[asyncio.Task[None]] = set()
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
        await asyncio.to_thread(self._queue.reap_expired)
        await self._run_maintenance_if_due()
        self._video_tasks = {task for task in self._video_tasks if not task.done()}
        self._work_tasks = {task for task in self._work_tasks if not task.done()}
        if len(self._video_tasks) < self._video_concurrency:
            video = await asyncio.to_thread(
                self._queue.claim_video, self._worker_id, lease_duration=self._lease_duration,
            )
            if video is not None:
                self._track(asyncio.create_task(self._run_video(video)), coordinator=True)
                return True

        # These cursors are only fairness hints. Every execution permit is granted by MySQL.
        stages = (
            (self._queue.claim_chunk, self._run_chunk),
            (self._queue.claim_interval_refine, lambda lease: self._run_interval(lease, self._handlers.refine)),
            (self._queue.claim_chunk_index, self._run_chunk),
        )
        for offset in range(5):
            stage = (self._phase_cursor + offset) % 5
            if stage == 4:
                if self._handlers.summarize is None or len(self._summary_tasks) >= self._summarize_concurrency:
                    continue
                task = asyncio.create_task(self._run_summary())
                self._summary_tasks.add(task)
                task.add_done_callback(self._summary_tasks.discard)
                self._track(task)
                self._phase_cursor = 0
                return True
            if stage == 3:
                if self._handlers.prefix is None or self._prefix_task is not None and not self._prefix_task.done():
                    continue
                self._prefix_task = asyncio.create_task(self._run_prefix())
                self._track(self._prefix_task)
                self._phase_cursor = 4
                return True
            claim, run = stages[stage]
            lease = await asyncio.to_thread(claim, self._worker_id, lease_duration=self._lease_duration)
            if lease is not None:
                self._track(asyncio.create_task(run(lease)))
                self._phase_cursor = (stage + 1) % 5
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
            self._summary_tasks.clear()

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
        except asyncio.CancelledError as error:
            await self._release_cancelled(lease, error)
            raise
        except Exception as exc:
            result = await asyncio.to_thread(
                self._queue.retry_video,
                lease,
                error=str(exc),
            )
            _log_retry_failure(
                lease, phase="prepare", error=exc,
                recorded=result is not None,
                retry_at=result.next_execute_time if result is not None else None,
                prepare_failure_count=result.prepare_failure_count if result is not None else None,
            )

    async def _run_chunk(self, lease: ChunkParseLease) -> None:
        try:
            await self._with_heartbeat(
                lease,
                self._handlers.understand if lease.phase == ChunkTaskPhase.PARSE else self._handlers.index,
                self._queue.heartbeat_chunk,
            )
        except asyncio.CancelledError as error:
            await self._release_cancelled(lease, error)
            raise
        except Exception as exc:
            retry_at = now_utc() + task_retry_delay(
                lease.retry_initial_delay_seconds,
                lease.retry_max_delay_seconds,
                lease.retry_count + 1,
            )
            recorded = await asyncio.to_thread(
                self._queue.retry_chunk,
                lease,
                error=str(exc),
                retry_at=retry_at,
            )
            _log_retry_failure(
                lease, phase="understand" if lease.phase == ChunkTaskPhase.PARSE else "index", error=exc,
                recorded=recorded, retry_at=retry_at,
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
        except asyncio.CancelledError as error:
            await self._release_cancelled(lease, error)
            raise
        except Exception as exc:
            delay = task_retry_delay(
                lease.retry_initial_delay_seconds, lease.retry_max_delay_seconds, lease.retry_count + 1,
            )
            retry_at = now_utc() + delay
            retry_options = (
                {"provider_retry_exhausted": True}
                if isinstance(exc, RefineProviderRetryExhaustedError) else {}
            )
            recorded = await asyncio.to_thread(
                self._queue.retry_interval,
                lease,
                error=str(exc),
                retry_at=retry_at,
                **retry_options,
            )
            _log_retry_failure(
                lease, phase="refine",
                error=exc, recorded=recorded, retry_at=None if retry_options else retry_at,
            )

    async def _run_prefix(self) -> None:
        assert self._handlers.prefix is not None
        worked = await self._handlers.prefix()
        if not worked:
            await asyncio.sleep(self._poll_interval.total_seconds())

    async def _run_summary(self) -> None:
        assert self._handlers.summarize is not None
        try:
            worked = await self._handlers.summarize()
        except Exception as error:
            # Metadata failures never stop parsing or indexing. The durable lease
            # permits recovery if even recording a retry failed.
            _logger.warning("Video summary execution failed: %s", _failure_log_text(str(error)))
            worked = False
        if not worked:
            await asyncio.sleep(self._poll_interval.total_seconds())

    async def _release_cancelled(self, lease: _Lease, error: asyncio.CancelledError) -> None:
        if getattr(error, "execution_pending", False):
            # The transport still owns its resources. Release only after its real thread exits.
            completion = getattr(error, "completion", None)
            if completion is not None:
                def release_after_exit(_future) -> None:
                    try:
                        self._queue.release_execution(lease)
                    except Exception:
                        _logger.exception("Deferred Parse lease release failed; expiry will recover it")
                completion.add_done_callback(release_after_exit)
            return
        await asyncio.to_thread(self._queue.release_execution, lease)

    async def _with_heartbeat(
        self, lease: _Lease, handler: Callable[[_Lease], Awaitable[None]],
        heartbeat: Callable[..., bool],
    ) -> None:
        result = handler(lease)
        if not inspect.isawaitable(result):
            raise TypeError("VideoParseConsumer handlers must be async")
        work = asyncio.create_task(result)
        interval = self._heartbeat_interval.total_seconds()
        deadline = getattr(lease, "execution_deadline", None)
        try:
            while True:
                remaining = (deadline - now_utc()).total_seconds() if deadline is not None else None
                if remaining is not None and remaining <= 0:
                    if await asyncio.to_thread(self._queue.is_execution_lease_committed, lease):
                        await work
                        return
                    work.cancel()
                    await asyncio.gather(work, return_exceptions=True)
                    raise TimeoutError("Video Parse stage execution deadline exceeded")
                done, _pending = await asyncio.wait(
                    {work}, timeout=min(interval, remaining) if remaining is not None else interval,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if work in done:
                    await work
                    return
                current = await asyncio.to_thread(heartbeat, lease, lease_duration=self._lease_duration)
                if not current:
                    if await asyncio.to_thread(self._queue.is_execution_lease_committed, lease):
                        await work
                        return
                    work.cancel()
                    await asyncio.gather(work, return_exceptions=True)
                    raise RuntimeError("Video Parse V2 worker lease was lost")
        except asyncio.CancelledError:
            work.cancel()
            try:
                await work
            except asyncio.CancelledError as stopped:
                if getattr(stopped, "execution_pending", False):
                    raise stopped
            raise
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
