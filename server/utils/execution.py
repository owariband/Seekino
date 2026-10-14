"""Run admitted blocking work independently of asyncio's control executor."""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import Future
from contextvars import ContextVar, copy_context
from functools import partial
from threading import Lock, Thread
from typing import Callable, TypeVar


_T = TypeVar("_T")
_logger = logging.getLogger(__name__)


class BlockingCallCancelled(asyncio.CancelledError):
    """The waiter was cancelled; the blocking operation has not exited yet."""

    execution_pending = True

    def __init__(self, completion: Future) -> None:
        super().__init__("blocking execution cancellation requested; operation is still running")
        self.completion = completion


class ExecutionCalls:
    """Keep ownership of resources until all blocking calls really exit.

    Task/lease admission owns capacity. A thread starts only for admitted work;
    a second fixed pool must not silently impose a lower concurrency limit.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._pending: set[Future] = set()
        self._cleanup: list[Callable[[], object]] = []
        self._drained: Future[None] = Future()

    @property
    def pending(self) -> bool:
        with self._lock:
            return bool(self._pending)

    def defer_cleanup(self, cleanup: Callable[[], object]) -> bool:
        with self._lock:
            if not self._pending:
                return False
            self._cleanup.append(cleanup)
            return True

    def _finished(self, future: Future) -> None:
        with self._lock:
            self._pending.discard(future)
            cleanup = self._cleanup if not self._pending else []
            drained = self._drained if not self._pending else None
            if cleanup:
                self._cleanup = []
        for operation in cleanup:
            try:
                operation()
            except BaseException:
                _logger.exception("deferred execution resource cleanup failed")
        if drained is not None:
            drained.set_result(None)

    async def run(self, operation: Callable[..., _T], /, *args, **kwargs) -> _T:
        future: Future[_T] = Future()
        caller_context = copy_context()
        with self._lock:
            if not self._pending:
                self._drained = Future()
            self._pending.add(future)
            drained = self._drained
        future.add_done_callback(self._finished)

        def invoke() -> None:
            try:
                result = caller_context.run(partial(operation, *args, **kwargs))
            except BaseException as error:
                future.set_exception(error)
            else:
                future.set_result(result)

        # A provider that ignores cancellation must not block interpreter exit.
        # Its handle remains live, and its resources remain owned until it exits.
        try:
            Thread(target=invoke, name="execution-call", daemon=True).start()
        except BaseException as error:
            future.set_exception(error)
        waiter = asyncio.wrap_future(future)
        try:
            return await asyncio.shield(waiter)
        except asyncio.CancelledError:
            # Consume a later exception without claiming the underlying call
            # stopped; the concurrent Future is retained for the owner to drain.
            waiter.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
            if not drained.done():
                raise BlockingCallCancelled(drained) from None
            raise


execution_calls: ContextVar[ExecutionCalls | None] = ContextVar("execution_calls", default=None)


async def run_blocking(operation: Callable[..., _T], /, *args, **kwargs) -> _T:
    calls = execution_calls.get() or ExecutionCalls()
    return await calls.run(operation, *args, **kwargs)


async def cleanup_execution(operation: Callable[..., object], /, *args, **kwargs) -> None:
    calls = execution_calls.get()
    cleanup = partial(operation, *args, **kwargs)
    if calls is not None and calls.defer_cleanup(cleanup):
        return
    await run_blocking(cleanup)
