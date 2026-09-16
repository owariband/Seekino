from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta

from ..model.dto.jobDTO import ParseJobLease
from ..utils.errors import exception_summary
from ..utils.time import utc_now_naive
from .contracts import ParseJobHandler, ParseJobLeaseLostError, ParseJobQueue

_logger = logging.getLogger(__name__)


class ParseJobConsumer:
    def __init__(
        self,
        queue: ParseJobQueue,
        processor: ParseJobHandler,
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
            raise ValueError(
                "heartbeat_interval must be positive and below the lease"
            )
        if retry_base <= timedelta(0) or retry_max < retry_base:
            raise ValueError("retry delays are invalid")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be greater than zero")
        self._queue = queue
        self._processor = processor
        self._worker_id = worker_id.strip()
        self._lease_duration = lease_duration
        self._heartbeat_interval = heartbeat_interval
        self._retry_base = retry_base
        self._retry_max = retry_max
        self._poll_interval_seconds = poll_interval_seconds
        self._clock = clock or utc_now_naive
        self._on_log = on_log
        self._stop = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._thread: threading.Thread | None = None
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
            self._stop.clear()
            self._failure = None
            self._thread = threading.Thread(
                target=self._run,
                name="parse-job-consumer",
                daemon=True,
            )
            self._thread.start()

    def close(self, timeout: float | None = None) -> None:
        self._stop.set()
        self.join(timeout)

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def run_once(self) -> bool:
        self._queue.reap_expired(
            retry_delay=self._retry_base,
            now=self._clock(),
        )
        lease = self._queue.claim_next(
            self._worker_id,
            lease_duration=self._lease_duration,
            now=self._clock(),
        )
        if lease is None:
            return False
        self._log(
            f"claimed job={lease.job_id} attempt={lease.attempt_count} "
            f"leaseVersion={lease.lease_version}"
        )
        try:
            with _LeaseHeartbeat(
                self._queue,
                lease,
                lease_duration=self._lease_duration,
                interval=self._heartbeat_interval,
            ) as heartbeat:
                chunk_ids = self._processor.process(lease)
                heartbeat.require_current()
                if not self._queue.acknowledge(lease, now=self._clock()):
                    raise ParseJobLeaseLostError(
                        "parse job lease was lost before completion"
                    )
            self._log(
                f"completed job={lease.job_id} videoChunks={len(chunk_ids)}"
            )
        except ParseJobLeaseLostError as exc:
            self._log(f"abandoned stale job={lease.job_id}: {exc}")
        except Exception as exc:
            retry_at = self._clock() + self._retry_delay(lease.attempt_count)
            recorded = self._queue.retry(
                lease,
                error=exception_summary(exc),
                retry_at=retry_at,
                now=self._clock(),
            )
            disposition = "recorded" if recorded else "lease already lost"
            self._log(
                f"failed job={lease.job_id}; {disposition}; "
                f"error={exception_summary(exc)}"
            )
        return True

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                worked = self.run_once()
                if not worked:
                    self._stop.wait(self._poll_interval_seconds)
        except Exception as exc:
            self._failure = exc
            message = (
                "parse job consumer stopped after fatal error: "
                f"{exception_summary(exc)}"
            )
            _logger.exception(message)
            if self._on_log is not None:
                self._on_log(message)

    def _log(self, message: str) -> None:
        if self._on_log is None:
            _logger.info(message)
        else:
            self._on_log(message)

    def _retry_delay(self, attempt_count: int) -> timedelta:
        exponent = min(max(attempt_count - 1, 0), 30)
        return min(self._retry_base * (2**exponent), self._retry_max)


class _LeaseHeartbeat:
    def __init__(
        self,
        queue: ParseJobQueue,
        lease: ParseJobLease,
        *,
        lease_duration: timedelta,
        interval: timedelta,
    ) -> None:
        self._queue = queue
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

    def __enter__(self) -> _LeaseHeartbeat:
        self._thread.start()
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self._stop.set()
        self._thread.join()

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
                current = self._queue.heartbeat(
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
