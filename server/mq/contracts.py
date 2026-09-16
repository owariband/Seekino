from __future__ import annotations

from datetime import datetime, timedelta
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
        now: datetime | None = None,
    ) -> ParseJobLease | None: ...

    def heartbeat(
        self,
        lease: ParseJobLease,
        *,
        lease_duration: timedelta,
        now: datetime | None = None,
    ) -> bool: ...

    def acknowledge(
        self,
        lease: ParseJobLease,
        *,
        now: datetime | None = None,
    ) -> bool: ...

    def retry(
        self,
        lease: ParseJobLease,
        *,
        error: str,
        retry_at: datetime,
        now: datetime | None = None,
    ) -> bool: ...

    def reap_expired(
        self,
        *,
        retry_delay: timedelta,
        now: datetime | None = None,
        limit: int = 100,
    ) -> int: ...


class ParseJobHandler(Protocol):
    def process(self, lease: ParseJobLease) -> list[str]: ...
