"""Distributed Parse V2 model-call admission wired into Gateway call tracking."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta
from threading import Event, Lock
from typing import Protocol

from gateway.usage import (
    ModelCallCancelledError,
    ModelCallControlError,
    ModelCallTracker,
    ModelCallUsage,
    capture_model_usage,
    control_model_calls,
)

from ..model.dto.jobDTO import (
    ChunkParseLease,
    IntervalParseLease,
    ModelCallReservation,
    PrefixParseLease,
    VideoParseLease,
)
from ..repository.video_parse import VideoParseRepositoryError


ExecutionLease = (
    VideoParseLease | ChunkParseLease | IntervalParseLease | PrefixParseLease
)


class ModelCallReservationRepository(Protocol):
    def reserve_model_call(
        self,
        execution_lease: ExecutionLease,
        call_id: str,
        *,
        lease_duration: timedelta,
        now: datetime | None = None,
    ) -> ModelCallReservation | None: ...

    def renew_model_call(
        self,
        execution_lease: ExecutionLease,
        call_id: str,
        *,
        lease_duration: timedelta,
        now: datetime | None = None,
    ) -> bool: ...

    def release_model_call(
        self,
        video_parse_task_id: str,
        call_id: str,
        worker_id: str,
        *,
        now: datetime | None = None,
    ) -> bool: ...


class ModelCallAdmissionError(ModelCallControlError):
    """The persistent Video budget or execution lease denied a new request."""


class VideoParseModelCallManager:
    """Reserve every observable provider request under one Video task budget.

    This manager controls calls that enter ``gateway.usage.track_model_call``.
    It cannot count or cancel hidden network rounds performed inside an SDK.
    """

    def __init__(
        self,
        repository: ModelCallReservationRepository,
        *,
        execution_lease: ExecutionLease,
        lease_duration: timedelta,
        usage_observer: Callable[[ModelCallUsage], None] | None = None,
        check_active: Callable[[], None] | None = None,
    ) -> None:
        if (
            not execution_lease.video_parse_task_id.strip()
            or not execution_lease.worker_id.strip()
        ):
            raise ValueError("execution lease identities must not be blank")
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        self._repository = repository
        self.execution_lease = execution_lease
        self.video_parse_task_id = execution_lease.video_parse_task_id
        self.worker_id = execution_lease.worker_id
        self.lease_duration = lease_duration
        self._usage_observer = usage_observer
        self._check_active = check_active or (lambda: None)
        self._cancelled = Event()
        self._lock = Lock()
        self._active: dict[str, ModelCallTracker] = {}

    @contextmanager
    def capture(self) -> Iterator[None]:
        """Apply admission and, when supplied, the real Gateway usage observer."""

        with ExitStack() as stack:
            stack.enter_context(control_model_calls(self))
            if self._usage_observer is not None:
                stack.enter_context(capture_model_usage(self._usage_observer))
            yield

    def acquire(self, call: ModelCallTracker) -> None:
        self.check_active()
        try:
            reservation = self._repository.reserve_model_call(
                self.execution_lease,
                call.call_id,
                lease_duration=self.lease_duration,
            )
        except VideoParseRepositoryError as error:
            raise ModelCallAdmissionError(
                "model-call reservation was rejected"
            ) from error
        if reservation is None:
            raise ModelCallAdmissionError(
                "model-call budget or execution lease is unavailable for "
                f"VideoParseTask {self.video_parse_task_id}"
            )
        with self._lock:
            if self._cancelled.is_set():
                cancelled = True
            else:
                self._active[call.call_id] = call
                cancelled = False
        if cancelled:
            call.cancel()
            self._repository.release_model_call(
                self.video_parse_task_id, call.call_id, self.worker_id
            )
            raise ModelCallCancelledError("Parse V2 execution was cancelled")

    def release(self, call: ModelCallTracker) -> None:
        with self._lock:
            self._active.pop(call.call_id, None)
            released = self._repository.release_model_call(
                self.video_parse_task_id, call.call_id, self.worker_id
            )
        if not released:
            raise RuntimeError(
                f"model-call reservation {call.call_id} was not owned by this Worker"
            )

    def heartbeat(self) -> bool:
        """Extend active provider slots alongside the owning Worker heartbeat."""

        lost_reservation = False
        try:
            self.check_active()
            with self._lock:
                active = tuple(self._active.values())
                for call in active:
                    renewed = self._repository.renew_model_call(
                        self.execution_lease,
                        call.call_id,
                        lease_duration=self.lease_duration,
                    )
                    if not renewed:
                        lost_reservation = True
                        break
        except BaseException:
            self.cancel()
            raise
        if lost_reservation:
            self.cancel()
            return False
        return True

    def cancel(self) -> None:
        """Prevent new requests and best-effort close this process's transports."""

        with self._lock:
            self._cancelled.set()
            active = tuple(self._active.values())
        for call in active:
            call.cancel()

    def check_active(self) -> None:
        """Fence local cancellation and the durable execution lease."""

        self._raise_if_cancelled()
        try:
            self._check_active()
        except Exception as error:
            self.cancel()
            raise ModelCallCancelledError(
                "Parse V2 execution lease was lost"
            ) from error

    def _raise_if_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise ModelCallCancelledError("Parse V2 execution was cancelled")


__all__ = [
    "ExecutionLease",
    "ModelCallAdmissionError",
    "ModelCallReservationRepository",
    "VideoParseModelCallManager",
]
