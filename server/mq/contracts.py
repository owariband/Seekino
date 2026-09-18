from __future__ import annotations

from datetime import timedelta
from typing import Protocol

from ..model.dto.jobDTO import ParseJobLease


class ParseJobLeaseLostError(RuntimeError):
    pass


class ParseJobQueue(Protocol):
    def claim_next(
        self,
        worker_id: str,
        *,
        lease_duration: timedelta,
        now: int | None = None,
    ) -> ParseJobLease | None: ...

    def heartbeat(
        self,
        lease: ParseJobLease,
        *,
        lease_duration: timedelta,
        now: int | None = None,
    ) -> bool: ...

    def acknowledge(
        self,
        lease: ParseJobLease,
        *,
        duration_ms: int,
        now: int | None = None,
    ) -> bool: ...

    def retry(
        self,
        lease: ParseJobLease,
        *,
        error: str,
        retry_at: int,
        duration_ms: int,
        now: int | None = None,
    ) -> bool: ...

    def reap_expired(
        self,
        *,
        retry_delay: timedelta,
        now: int | None = None,
        limit: int = 100,
    ) -> int: ...


class ParseJobHandler(Protocol):
    def process(self, lease: ParseJobLease) -> list[str]: ...
