from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from engine.common.usage import ParseModelCall as ParseModelCallEvent
from gateway.usage import TokenUsage

from ..model.identifiers import derive_video_chunk_id
from ..model.dto.jobDTO import (
    ChunkParseLease,
    IntervalParseLease,
    ParseJobLease,
    PrefixParseLease,
    VideoParseLease,
)
from ..model.dto.parseUsageDTO import ParseModelCallState, ParseUsageLedgerState
from ..model.job import SeriesParseJob
from ..model.parse_usage import (
    ParseModelCall,
    ParseUsageAttempt,
    VideoParseUsageAttempt,
)
from ..model.video import Series
from ..utils.time import unix_time_ms_to_datetime
from .job import ParseJobNotFoundError


class ParseUsageRepository:
    """Append consumed calls independently of the Job's current execution lease."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def start_attempt(self, lease: ParseJobLease, *, now: datetime) -> bool:
        identity = (lease.job_id, lease.attempt_count, lease.lease_version)
        try:
            with self._session_factory.begin() as session:
                record = session.get(ParseUsageAttempt, identity, with_for_update=True)
                previously_complete = record is None or record.recording_complete
                if record is None:
                    session.add(ParseUsageAttempt(
                        job_id=lease.job_id, attempt_count=lease.attempt_count,
                        lease_version=lease.lease_version, started_at=now, recording_complete=False,
                    ))
                else:
                    record.recording_complete = False
                return previously_complete
        except IntegrityError:
            with self._session_factory.begin() as session:
                record = session.get(ParseUsageAttempt, identity, with_for_update=True)
                if record is None:
                    raise
                record.recording_complete = False
                return False

    def finish_attempt(self, lease: ParseJobLease, *, recording_complete: bool) -> None:
        with self._session_factory.begin() as session:
            record = session.get(
                ParseUsageAttempt,
                (lease.job_id, lease.attempt_count, lease.lease_version),
                with_for_update=True,
            )
            if record is None:
                raise LookupError("Parse usage attempt was not started")
            record.recording_complete = recording_complete

    def record_call(self, lease: ParseJobLease, event: ParseModelCallEvent, *, estimated_cost_cny: Decimal | None, price_snapshot: dict | None) -> ParseModelCallState:
        call = event.call
        try:
            with self._session_factory.begin() as session:
                existing = session.get(ParseModelCall, call.call_id)
                if existing is not None:
                    return self._state(existing)
                record = ParseModelCall(
                    call_id=call.call_id, job_id=lease.job_id,
                    video_parse_task_id=None,
                    execution_kind=None,
                    execution_task_id=None,
                    attempt_count=lease.attempt_count,
                    lease_version=lease.lease_version, tenant_id=lease.tenant_id,
                    series_id=lease.series_id, video_id=lease.video_id, sequence_no=event.sequence_no,
                    video_chunk_id=derive_video_chunk_id(lease.video_id, event.sequence_no) if event.sequence_no is not None else None,
                    provider=call.provider, model=call.model, category=event.category, operation=call.operation,
                    started_at=unix_time_ms_to_datetime(call.started_at),
                    request_id=call.request_id,
                    status=call.status,
                    tokens=asdict(call.usage) if call.usage is not None else None,
                    estimated_cost_cny=format(estimated_cost_cny, "f") if estimated_cost_cny is not None else None,
                    price_snapshot=price_snapshot,
                )
                session.add(record)
                session.flush()
                return self._state(record)
        except IntegrityError:
            with self._session_factory() as session:
                existing = session.get(ParseModelCall, call.call_id)
                if existing is None:
                    raise
                return self._state(existing)

    def start_video_attempt(
        self,
        lease: VideoParseLease | ChunkParseLease | IntervalParseLease | PrefixParseLease,
        *,
        now: datetime,
    ) -> bool:
        (
            task_id,
            execution_kind,
            execution_task_id,
            attempt_count,
            lease_version,
        ) = _video_lease_identity(lease)
        attempt_id = _attempt_id(
            task_id,
            execution_kind,
            execution_task_id,
            attempt_count,
            lease_version,
        )
        try:
            with self._session_factory.begin() as session:
                record = session.get(
                    VideoParseUsageAttempt, attempt_id, with_for_update=True
                )
                previously_complete = record is None or record.recording_complete
                if record is None:
                    session.add(VideoParseUsageAttempt(
                        attempt_id=attempt_id,
                        video_parse_task_id=task_id,
                        execution_kind=execution_kind,
                        execution_task_id=execution_task_id,
                        attempt_count=attempt_count,
                        lease_version=lease_version,
                        started_at=now,
                        recording_complete=False,
                    ))
                else:
                    record.recording_complete = False
                return previously_complete
        except IntegrityError:
            with self._session_factory.begin() as session:
                record = session.get(
                    VideoParseUsageAttempt, attempt_id, with_for_update=True
                )
                if record is None:
                    raise
                record.recording_complete = False
                return False

    def finish_video_attempt(
        self,
        lease: VideoParseLease | ChunkParseLease | IntervalParseLease | PrefixParseLease,
        *,
        recording_complete: bool,
    ) -> None:
        (
            task_id,
            execution_kind,
            execution_task_id,
            attempt_count,
            lease_version,
        ) = _video_lease_identity(lease)
        with self._session_factory.begin() as session:
            record = session.get(
                VideoParseUsageAttempt,
                _attempt_id(
                    task_id,
                    execution_kind,
                    execution_task_id,
                    attempt_count,
                    lease_version,
                ),
                with_for_update=True,
            )
            if record is None:
                raise LookupError("Video parse usage attempt was not started")
            record.recording_complete = recording_complete

    def record_video_call(
        self,
        lease: VideoParseLease | ChunkParseLease | IntervalParseLease | PrefixParseLease,
        event: ParseModelCallEvent,
        *,
        tenant_id: str,
        series_id: str,
        video_id: str,
        estimated_cost_cny: Decimal | None,
        price_snapshot: dict | None,
    ) -> ParseModelCallState:
        task_id, execution_kind, execution_id, attempt_count, lease_version = (
            _video_lease_identity(lease)
        )
        call = event.call
        try:
            with self._session_factory.begin() as session:
                existing = session.get(ParseModelCall, call.call_id)
                if existing is not None:
                    return self._state(existing)
                record = ParseModelCall(
                    call_id=call.call_id,
                    job_id=None,
                    video_parse_task_id=task_id,
                    execution_kind=execution_kind,
                    execution_task_id=execution_id,
                    attempt_count=attempt_count,
                    lease_version=lease_version,
                    tenant_id=tenant_id,
                    series_id=series_id,
                    video_id=video_id,
                    sequence_no=event.sequence_no,
                    video_chunk_id=(
                        derive_video_chunk_id(video_id, event.sequence_no)
                        if event.sequence_no is not None else None
                    ),
                    provider=call.provider,
                    model=call.model,
                    category=event.category,
                    operation=call.operation,
                    started_at=unix_time_ms_to_datetime(call.started_at),
                    request_id=call.request_id,
                    status=call.status,
                    tokens=asdict(call.usage) if call.usage is not None else None,
                    estimated_cost_cny=(
                        format(estimated_cost_cny, "f")
                        if estimated_cost_cny is not None else None
                    ),
                    price_snapshot=price_snapshot,
                )
                session.add(record)
                session.flush()
                return self._state(record)
        except IntegrityError:
            with self._session_factory() as session:
                existing = session.get(ParseModelCall, call.call_id)
                if existing is None:
                    raise
                return self._state(existing)

    def list_video_calls(
        self,
        video_parse_task_id: str,
        *,
        tenant_id: str,
    ) -> list[ParseModelCallState]:
        with self._session_factory() as session:
            records = session.scalars(
                select(ParseModelCall)
                .where(
                    ParseModelCall.video_parse_task_id == video_parse_task_id,
                    ParseModelCall.tenant_id == tenant_id,
                )
                .order_by(ParseModelCall.started_at, ParseModelCall.call_id)
            ).all()
            return [self._state(record) for record in records]

    def list_calls(self, job_id: str, *, tenant_id: str) -> list[ParseModelCallState]:
        with self._session_factory() as session:
            records = session.scalars(select(ParseModelCall).where(
                ParseModelCall.job_id == job_id, ParseModelCall.tenant_id == tenant_id,
            ).order_by(ParseModelCall.started_at, ParseModelCall.call_id)).all()
            return [self._state(record) for record in records]

    def snapshot(self, job_id: str, *, tenant_id: str) -> ParseUsageLedgerState:
        # One statement keeps calls, completion markers and the current attempt
        # consistent even when the Worker finishes or claims another attempt.
        with self._session_factory() as session:
            rows = session.execute(
                select(SeriesParseJob.attempt_count, SeriesParseJob.status,
                       ParseModelCall, ParseUsageAttempt)
                .select_from(SeriesParseJob)
                .join(Series, Series.series_id == SeriesParseJob.series_id)
                .outerjoin(ParseModelCall, and_(
                    ParseModelCall.job_id == SeriesParseJob.job_id,
                    ParseModelCall.tenant_id == tenant_id,
                ))
                .outerjoin(ParseUsageAttempt, ParseUsageAttempt.job_id == SeriesParseJob.job_id)
                .where(SeriesParseJob.job_id == job_id, Series.tenant_id == tenant_id)
                .order_by(ParseModelCall.started_at, ParseModelCall.call_id)
            ).all()
            if not rows:
                raise ParseJobNotFoundError(f"jobId {job_id!r} does not exist")
            calls: dict[str, ParseModelCallState] = {}
            attempts: dict[int, bool] = {}
            for _attempt_count, _status, call, attempt in rows:
                if call is not None:
                    calls.setdefault(call.call_id, self._state(call))
                if attempt is not None:
                    attempts[attempt.attempt_count] = attempts.get(attempt.attempt_count, True) and attempt.recording_complete
            return ParseUsageLedgerState(rows[0][0], rows[0][1], tuple(calls.values()), attempts)

    @staticmethod
    def _state(record: ParseModelCall) -> ParseModelCallState:
        return ParseModelCallState(
            call_id=record.call_id, job_id=record.job_id,
            video_parse_task_id=record.video_parse_task_id,
            attempt_count=record.attempt_count,
            lease_version=record.lease_version, tenant_id=record.tenant_id,
            series_id=record.series_id, video_id=record.video_id, sequence_no=record.sequence_no,
            video_chunk_id=record.video_chunk_id, provider=record.provider, model=record.model,
            category=record.category, operation=record.operation, started_at=record.started_at,
            request_id=record.request_id, status=record.status,
            usage=TokenUsage(**record.tokens) if record.tokens is not None else None,
            estimated_cost_cny=Decimal(record.estimated_cost_cny) if record.estimated_cost_cny is not None else None,
            price_snapshot=record.price_snapshot,
            execution_kind=record.execution_kind,
            execution_task_id=record.execution_task_id,
        )


def _attempt_id(
    video_parse_task_id: str,
    execution_kind: str,
    execution_task_id: str,
    attempt_count: int,
    lease_version: int,
) -> str:
    return str(uuid5(
        NAMESPACE_URL,
        "anivag:parse-usage:v2:"
        f"{video_parse_task_id}:{execution_kind}:{execution_task_id}:"
        f"{attempt_count}:{lease_version}",
    ))


def _video_lease_identity(
    lease: VideoParseLease | ChunkParseLease | IntervalParseLease | PrefixParseLease,
) -> tuple[str, str, str, int, int]:
    if isinstance(lease, VideoParseLease):
        execution_kind = "video"
        execution_task_id = lease.video_parse_task_id
        attempt_count = lease.attempt_count
    elif isinstance(lease, ChunkParseLease):
        execution_kind = "chunk"
        execution_task_id = lease.chunk_parse_task_id
        attempt_count = lease.retry_count + 1
    elif isinstance(lease, IntervalParseLease):
        execution_kind = "interval"
        execution_task_id = lease.interval_parse_task_id
        attempt_count = lease.retry_count + 1
    else:
        # Subtasks persist retry_count rather than claim count. Adding one
        # identifies the initial execution as attempt 1 and every retry after it.
        execution_kind = "prefix"
        execution_task_id = lease.chunk_parse_task_id
        attempt_count = lease.retry_count + 1
    return (
        lease.video_parse_task_id,
        execution_kind,
        execution_task_id,
        attempt_count,
        lease.lease_version,
    )
