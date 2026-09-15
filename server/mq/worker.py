from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol

from ..model.dto.jobDTO import ParseJobLease
from ..repository.job import ParseJobLeaseLostError, ParseJobRepository
from ..utils.errors import exception_summary
from ..utils.time import utc_now_naive


class ParseJobProcessor(Protocol):
    def process(self, lease: ParseJobLease) -> list[str]: ...


class ParseWorker:
    def __init__(
        self,
        jobs: ParseJobRepository,
        processor: ParseJobProcessor,
        *,
        worker_id: str,
        lease_duration: timedelta,
        heartbeat_interval: timedelta,
        retry_base: timedelta,
        retry_max: timedelta,
        poll_interval_seconds: float = 2.0,
        clock: Callable[[], datetime] | None = None,
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id must not be empty")
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        if not timedelta(0) < heartbeat_interval < lease_duration:
            raise ValueError("heartbeat_interval must be positive and below the lease")
        if retry_base <= timedelta(0) or retry_max < retry_base:
            raise ValueError("retry delays are invalid")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be greater than zero")
        self._jobs = jobs
        self._processor = processor
        self._worker_id = worker_id.strip()
        self._lease_duration = lease_duration
        self._heartbeat_interval = heartbeat_interval
        self._retry_base = retry_base
        self._retry_max = retry_max
        self._poll_interval_seconds = poll_interval_seconds
        self._clock = clock or utc_now_naive
        self._on_log = on_log or (lambda _message: None)

    def run_once(self) -> bool:
        self._jobs.reap_expired(
            retry_delay=self._retry_base,
            now=self._clock(),
        )
        lease = self._jobs.claim_next(
            self._worker_id,
            lease_duration=self._lease_duration,
            now=self._clock(),
        )
        if lease is None:
            return False
        self._on_log(
            f"claimed job={lease.job_id} attempt={lease.attempt_count} "
            f"leaseVersion={lease.lease_version}"
        )
        try:
            with _LeaseHeartbeat(
                self._jobs,
                lease,
                lease_duration=self._lease_duration,
                interval=self._heartbeat_interval,
            ) as heartbeat:
                chunk_ids = self._processor.process(lease)
                heartbeat.require_current()
                if not self._jobs.mark_succeeded(lease, now=self._clock()):
                    raise ParseJobLeaseLostError(
                        "parse job lease was lost before completion"
                    )
            self._on_log(
                f"completed job={lease.job_id} videoChunks={len(chunk_ids)}"
            )
        except ParseJobLeaseLostError as exc:
            self._on_log(f"abandoned stale job={lease.job_id}: {exc}")
        except Exception as exc:
            retry_at = self._clock() + self._retry_delay(lease.attempt_count)
            recorded = self._jobs.mark_failed(
                lease,
                error=exception_summary(exc),
                retry_at=retry_at,
                now=self._clock(),
            )
            disposition = "recorded" if recorded else "lease already lost"
            self._on_log(
                f"failed job={lease.job_id}; {disposition}; "
                f"error={exception_summary(exc)}"
            )
        return True

    def run_forever(self, stop: threading.Event | None = None) -> None:
        stop_event = stop or threading.Event()
        while not stop_event.is_set():
            worked = self.run_once()
            if not worked:
                stop_event.wait(self._poll_interval_seconds)

    def _retry_delay(self, attempt_count: int) -> timedelta:
        exponent = min(max(attempt_count - 1, 0), 30)
        return min(self._retry_base * (2**exponent), self._retry_max)


class _LeaseHeartbeat:
    def __init__(
        self,
        jobs: ParseJobRepository,
        lease: ParseJobLease,
        *,
        lease_duration: timedelta,
        interval: timedelta,
    ) -> None:
        self._jobs = jobs
        self._lease = lease
        self._lease_duration = lease_duration
        self._interval_seconds = interval.total_seconds()
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._error: Exception | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"parse-job-heartbeat-{lease.job_id}",
            daemon=True,
        )

    def __enter__(self) -> "_LeaseHeartbeat":
        self._thread.start()
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self._stop.set()
        self._thread.join(timeout=max(self._interval_seconds * 2, 1))

    def require_current(self) -> None:
        if self._lost.is_set():
            if self._error is not None:
                raise ParseJobLeaseLostError(
                    f"parse job heartbeat failed: {self._error}"
                ) from self._error
            raise ParseJobLeaseLostError("parse job lease is no longer current")

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            try:
                current = self._jobs.heartbeat(
                    self._lease,
                    lease_duration=self._lease_duration,
                )
            except Exception as exc:
                self._error = exc
                self._lost.set()
                return
            if not current:
                self._lost.set()
                return
