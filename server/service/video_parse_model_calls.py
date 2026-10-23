"""Distributed Parse V2 model-call admission wired into Gateway call tracking."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
from threading import Event, Lock
from typing import Any, Protocol

from sqlalchemy.orm import Session

from gateway.usage import (
    ModelCallCancelledError,
    ModelCallControlError,
    ModelCallTracker,
    ModelCallUsage,
    capture_model_usage,
    control_model_calls,
)

from ..constants.parse import RUNTIME_DETAIL_KEY
from ..model.dto.jobDTO import (
    ChunkParseLease,
    IntervalParseLease,
    ModelCallReservation,
    PrefixParseLease,
    VideoSummarizeLease,
    VideoParseLease,
    VideoParseTaskData,
)
from ..model.job import ChunkTaskPhase
from ..repository import video_parse_task as task_store
from ..repository import video_parse_chunk as chunk_store
from ..repository import video_parse_interval as interval_store
from ..utils.time import datetime_to_unix_time_ms
from .video_parse_errors import (
    VideoParseRepositoryError,
    VideoParseLeaseLostError,
    ModelCallLeaseConflictError,
)
from .video_parse_task import (
    _locked_parent_task,
    _ensure_runtime,
    _latest_video_task_id,
    _execution_config,
    _positive_int,
)
from .video_parse_content import _matching_chunk_lease, _matching_interval_lease
from .video_parse_prefix import _current_prefix_lease
from .video_parse_summarize import current_summary_lease


ExecutionLease = (
    VideoParseLease | ChunkParseLease | IntervalParseLease | PrefixParseLease | VideoSummarizeLease
)


class ModelCallReservations(Protocol):
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
        reservations: ModelCallReservations,
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
        self._reservations = reservations
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
            reservation = self._reservations.reserve_model_call(
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
            self._reservations.release_model_call(
                self.video_parse_task_id, call.call_id, self.worker_id
            )
            raise ModelCallCancelledError("Parse V2 execution was cancelled")

    def release(self, call: ModelCallTracker) -> None:
        with self._lock:
            self._active.pop(call.call_id, None)
            released = self._reservations.release_model_call(
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
                    renewed = self._reservations.renew_model_call(
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

        self.raise_if_cancelled()
        try:
            self._check_active()
        except Exception as error:
            self.cancel()
            raise ModelCallCancelledError(
                "Parse V2 execution lease was lost"
            ) from error

    def raise_if_cancelled(self) -> None:
        """Check local cancellation without acquiring a database lease lock."""
        if self._cancelled.is_set():
            raise ModelCallCancelledError("Parse V2 execution was cancelled")


__all__ = [
    "ExecutionLease",
    "ModelCallAdmissionError",
    "ModelCallReservations",
    "VideoParseModelCallManager",
]


def _model_execution_identity(
    lease: VideoParseLease | ChunkParseLease | IntervalParseLease | PrefixParseLease | VideoSummarizeLease,
) -> tuple[str, str, int]:
    if isinstance(lease, VideoParseLease):
        return "video", lease.video_parse_task_id, lease.lease_version
    if isinstance(lease, ChunkParseLease):
        return "chunk", lease.chunk_parse_task_id, lease.lease_version
    if isinstance(lease, IntervalParseLease):
        return "interval", lease.interval_parse_task_id, lease.lease_version
    if isinstance(lease, VideoSummarizeLease):
        return "summarize", lease.video_parse_task_id, lease.lease_version
    return "prefix", lease.chunk_parse_task_id, lease.lease_version


def _stored_model_execution_identity(
    slot: Mapping[str, Any],
) -> tuple[str, str, int] | None:
    kind = slot.get("execution_kind")
    task_id = slot.get("execution_task_id")
    version = slot.get("execution_lease_version")
    if (
        not isinstance(kind, str)
        or not isinstance(task_id, str)
        or isinstance(version, bool)
        or not isinstance(version, int)
    ):
        return None
    return kind, task_id, version


def _lock_model_execution_lease(
    session: Session,
    lease: VideoParseLease | ChunkParseLease | IntervalParseLease | PrefixParseLease | VideoSummarizeLease,
    now: datetime,
) -> VideoParseTaskData:
    """Serialize the task budget and then fence the actual execution lease."""
    parent = _locked_parent_task(session, lease.video_parse_task_id)

    if isinstance(lease, ChunkParseLease):
        task = chunk_store.get_task(session, lease.chunk_parse_task_id,
            video_task_id=lease.video_parse_task_id, for_update=True)
        if not _matching_chunk_lease(task, lease, now):
            raise VideoParseLeaseLostError("ChunkParseTask lease is no longer current")
    elif isinstance(lease, IntervalParseLease):
        interval = interval_store.get_task(session, lease.interval_parse_task_id,
            video_task_id=lease.video_parse_task_id, for_update=True)
        if (
            not _matching_interval_lease(interval, lease, now)
            or interval.status != lease.phase
        ):
            raise VideoParseLeaseLostError("IntervalParseTask lease is no longer current")
    if isinstance(lease, VideoParseLease):
        if (
            parent.lease_owner != lease.worker_id
            or parent.lease_version != lease.lease_version
            or parent.lease_expire_time is None
            or parent.lease_expire_time <= now
        ):
            raise VideoParseLeaseLostError("VideoParseTask lease is no longer current")
    elif isinstance(lease, VideoSummarizeLease):
        if (current_summary_lease(parent, lease, now) is None
                or _latest_video_task_id(session, parent.video_id) != parent.id):
            raise VideoParseLeaseLostError("Video summary lease is no longer current")
    elif isinstance(lease, PrefixParseLease):
        if (
            _current_prefix_lease(parent, lease, now) is None
            or _latest_video_task_id(session, parent.video_id) != parent.id
        ):
            raise VideoParseLeaseLostError("Prefix lease is no longer current")
    return parent


def record_execution_timing(
    session: Session,
    *,
    lease: ChunkParseLease | IntervalParseLease,
    started_at_ms: int,
    elapsed_ms: int,
    finished: bool,
    outcome: str,
    sampled_at: datetime,
) -> bool:
    if isinstance(lease, ChunkParseLease):
        phase = "understand" if lease.phase == ChunkTaskPhase.PARSE else "index"
        kind, task_id = "chunk", lease.chunk_parse_task_id
    else:
        phase = "refine"
        kind, task_id = "interval", lease.interval_parse_task_id
    identity = {
        "phase": phase, "execution_kind": kind, "execution_task_id": task_id,
        "lease_version": lease.lease_version, "worker_id": lease.worker_id,
        "started_at_ms": started_at_ms,
    }
    key = f"{phase}:{task_id}:{lease.lease_version}"
    parent = _locked_parent_task(session, lease.video_parse_task_id)
    child = (chunk_store.get_task(session, task_id, for_update=True)
             if isinstance(lease, ChunkParseLease)
             else interval_store.get_task(session, task_id, for_update=True))
    if child is None or child.video_task_id != lease.video_parse_task_id:
        return False
    if isinstance(lease, ChunkParseLease):
        active = _matching_chunk_lease(child, lease, sampled_at)
        result = chunk_store.get_content(session, child.id)
        receipt = result.detail if result is not None and isinstance(result.detail, dict) else {}
        committed = child.chunk_id == lease.video_chunk_id and (
            receipt.get("parse_committed_lease_version") == lease.lease_version
            if lease.phase == ChunkTaskPhase.PARSE else lease.lease_version in receipt.get("index_committed_lease_versions", [])
        )
    else:
        active = _matching_interval_lease(child, lease, sampled_at)
        content = interval_store.get_content(session, task_id) if finished else None
        receipt = content.detail if content is not None and isinstance(content.detail, dict) else {}
        committed_version = receipt.get("refinement_committed_lease_version") == lease.lease_version
        committed = bool(
            committed_version
            and tuple(child.chunk_task_ids) == lease.chunk_parse_task_ids
            and child.start_sequence_no == lease.start_sequence_no
            and child.chunk_count == lease.chunk_count
        )
    if not active and not (finished and committed):
        return False
    detail = deepcopy(dict(parent.detail or {}))
    runtime = detail.get(RUNTIME_DETAIL_KEY, {})
    if not isinstance(runtime, dict):
        return False
    timing = runtime.get("execution_timing", {"version": 1, "entries": {}})
    if not isinstance(timing, dict) or timing.get("version") != 1:
        return False
    entries = timing.get("entries")
    if not isinstance(entries, dict):
        return False
    entry = entries.get(key)
    if entry is None:
        # A completion receipt may close an existing sample, never
        # invent a start for work that was not observed while active.
        if not active:
            return False
        entry = {**identity, "elapsed_ms": 0, "finished": False, "outcome": "running"}
    elif not isinstance(entry, dict) or any(
        entry.get(name) != value for name, value in identity.items()
    ):
        return False
    if entry.get("finished") is True:
        return True
    if type(entry.get("elapsed_ms")) is not int or entry["elapsed_ms"] < 0:
        return False
    entries[key] = {
        **entry, "elapsed_ms": max(entry["elapsed_ms"], elapsed_ms),
        "finished": finished, "outcome": outcome,
    }
    timing["entries"] = entries
    runtime["execution_timing"] = timing
    detail[RUNTIME_DETAIL_KEY] = runtime
    parent.detail = detail
    parent.update_time = datetime_to_unix_time_ms(sampled_at)
    task_store.save_task(session, parent)
    return True


def reserve_model_call(
    session: Session,
    *,
    lease: VideoParseLease | ChunkParseLease | IntervalParseLease | PrefixParseLease | VideoSummarizeLease,
    call_id: str,
    expires_at: datetime,
    now_ms: int,
    reserved_at: datetime,
) -> ModelCallReservation:
    identity = _model_execution_identity(lease)
    task = _lock_model_execution_lease(session, lease, reserved_at)
    execution = _execution_config(task.parse_config)
    # Keep the legacy snapshot value for reporting; cumulative calls do not limit admission.
    max_calls = _positive_int(execution.get("max_model_calls"), "max_model_calls")
    detail = _ensure_runtime(task.detail)
    runtime = deepcopy(detail[RUNTIME_DETAIL_KEY])
    model_calls = deepcopy(runtime["model_calls"])
    active = {
        key: value for key, value in model_calls["active"].items()
        if isinstance(value, dict) and value.get("lease_expire_time_ms", 0) > now_ms
    }
    used = list(model_calls["used_call_ids"])
    existing = active.get(call_id)
    if existing is not None:
        if (
            existing.get("worker_id") != lease.worker_id
            or _stored_model_execution_identity(existing) != identity
        ):
            raise ModelCallLeaseConflictError(
                "call_id is reserved by another execution lease"
            )
        first = False
    else:
        first = call_id not in used
        if first:
            used.append(call_id)
    active[call_id] = {
        "worker_id": lease.worker_id,
        "execution_kind": identity[0],
        "execution_task_id": identity[1],
        "execution_lease_version": identity[2],
        "lease_expire_time_ms": datetime_to_unix_time_ms(expires_at),
    }
    model_calls["used_call_ids"] = used
    model_calls["active"] = active
    runtime["model_calls"] = model_calls
    detail[RUNTIME_DETAIL_KEY] = runtime
    task.detail = detail
    task.update_time = datetime_to_unix_time_ms(reserved_at)
    task_store.save_task(session, task)
    return ModelCallReservation(
        video_parse_task_id=task.id,
        call_id=call_id,
        worker_id=lease.worker_id,
        first_reservation=first,
        used_calls=len(used),
        max_calls=max_calls,
        active_calls=len(active),
        lease_expire_time=expires_at,
    )


def renew_model_call(
    session: Session,
    *,
    lease: VideoParseLease | ChunkParseLease | IntervalParseLease | PrefixParseLease | VideoSummarizeLease,
    call_id: str,
    lease_duration: timedelta,
    renewed_at: datetime,
    renewed_at_ms: int,
) -> bool:
    identity = _model_execution_identity(lease)
    task = _lock_model_execution_lease(session, lease, renewed_at)
    detail = _ensure_runtime(task.detail)
    runtime = deepcopy(detail[RUNTIME_DETAIL_KEY])
    model_calls = deepcopy(runtime["model_calls"])
    active = deepcopy(model_calls["active"])
    slot = active.get(call_id)
    expires_at_ms = (
        slot.get("lease_expire_time_ms") if isinstance(slot, dict) else None
    )
    if (
        not isinstance(slot, dict)
        or slot.get("worker_id") != lease.worker_id
        or _stored_model_execution_identity(slot) != identity
        or not isinstance(expires_at_ms, int)
        or expires_at_ms <= renewed_at_ms
    ):
        return False
    expires_at = renewed_at + lease_duration
    slot["lease_expire_time_ms"] = datetime_to_unix_time_ms(expires_at)
    active[call_id] = slot
    model_calls["active"] = active
    runtime["model_calls"] = model_calls
    detail[RUNTIME_DETAIL_KEY] = runtime
    task.detail = detail
    task.update_time = datetime_to_unix_time_ms(renewed_at)
    task_store.save_task(session, task)
    return True


def release_model_call(
    session: Session,
    *,
    video_parse_task_id: str,
    call_id: str,
    worker_id: str,
    released_at: datetime,
) -> bool:
    task = task_store.get_task(session, video_parse_task_id, for_update=True)
    if task is None:
        return False
    detail = _ensure_runtime(task.detail)
    runtime = deepcopy(detail[RUNTIME_DETAIL_KEY])
    model_calls = deepcopy(runtime["model_calls"])
    active = deepcopy(model_calls["active"])
    slot = active.get(call_id)
    if slot is None or slot.get("worker_id") != worker_id:
        return False
    del active[call_id]
    model_calls["active"] = active
    runtime["model_calls"] = model_calls
    detail[RUNTIME_DETAIL_KEY] = runtime
    task.detail = detail
    task.update_time = datetime_to_unix_time_ms(released_at)
    task_store.save_task(session, task)
    return True
