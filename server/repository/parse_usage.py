from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from decimal import Decimal

from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from engine.parse.usage import ParseModelCall
from gateway.usage import TokenUsage

from ..identity import derive_video_chunk_id
from ..model.dto.jobDTO import ParseJobLease
from ..model.dto.parseUsageDTO import ParseModelCallState, ParseUsageLedgerState
from ..model.job import SeriesParseJobRecord
from ..model.parse_usage import ParseModelCallRecord, ParseUsageAttemptRecord
from ..model.video import SeriesRecord
from .job import ParseJobNotFoundError


class ParseUsageRepository:
    """Append consumed calls independently of the Job's current execution lease."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def start_attempt(self, lease: ParseJobLease, *, now: datetime) -> bool:
        identity = (lease.job_id, lease.attempt_count, lease.lease_version)
        try:
            with self._session_factory.begin() as session:
                record = session.get(ParseUsageAttemptRecord, identity, with_for_update=True)
                previously_complete = record is None or record.recording_complete
                if record is None:
                    session.add(ParseUsageAttemptRecord(
                        job_id=lease.job_id, attempt_count=lease.attempt_count,
                        lease_version=lease.lease_version, started_at=now, recording_complete=False,
                    ))
                else:
                    record.recording_complete = False
                return previously_complete
        except IntegrityError:
            with self._session_factory.begin() as session:
                record = session.get(ParseUsageAttemptRecord, identity, with_for_update=True)
                if record is None:
                    raise
                record.recording_complete = False
                return False

    def finish_attempt(self, lease: ParseJobLease, *, recording_complete: bool) -> None:
        with self._session_factory.begin() as session:
            record = session.get(ParseUsageAttemptRecord, (lease.job_id, lease.attempt_count, lease.lease_version), with_for_update=True)
            if record is None:
                raise LookupError("Parse usage attempt was not started")
            record.recording_complete = recording_complete

    def record_call(self, lease: ParseJobLease, event: ParseModelCall, *, estimated_cost_cny: Decimal | None, price_snapshot: dict | None) -> ParseModelCallState:
        call = event.call
        try:
            with self._session_factory.begin() as session:
                existing = session.get(ParseModelCallRecord, call.call_id)
                if existing is not None:
                    return self._state(existing)
                record = ParseModelCallRecord(
                    call_id=call.call_id, job_id=lease.job_id, attempt_count=lease.attempt_count,
                    lease_version=lease.lease_version, workspace_id=lease.workspace_id,
                    series_id=lease.series_id, video_id=lease.video_id, sequence_no=event.sequence_no,
                    video_chunk_id=derive_video_chunk_id(lease.video_id, event.sequence_no) if event.sequence_no is not None else None,
                    provider=call.provider, model=call.model, category=event.category, operation=call.operation,
                    started_at=call.started_at, request_id=call.request_id, status=call.status,
                    tokens=asdict(call.usage) if call.usage is not None else None,
                    estimated_cost_cny=format(estimated_cost_cny, "f") if estimated_cost_cny is not None else None,
                    price_snapshot=price_snapshot,
                )
                session.add(record)
                session.flush()
                return self._state(record)
        except IntegrityError:
            with self._session_factory() as session:
                existing = session.get(ParseModelCallRecord, call.call_id)
                if existing is None:
                    raise
                return self._state(existing)

    def list_calls(self, job_id: str, *, workspace_id: str) -> list[ParseModelCallState]:
        with self._session_factory() as session:
            records = session.scalars(select(ParseModelCallRecord).where(
                ParseModelCallRecord.job_id == job_id, ParseModelCallRecord.workspace_id == workspace_id,
            ).order_by(ParseModelCallRecord.started_at, ParseModelCallRecord.call_id)).all()
            return [self._state(record) for record in records]

    def snapshot(self, job_id: str, *, workspace_id: str) -> ParseUsageLedgerState:
        # One statement keeps calls, completion markers and the current attempt
        # consistent even when the Worker finishes or claims another attempt.
        with self._session_factory() as session:
            rows = session.execute(
                select(SeriesParseJobRecord.attempt_count, SeriesParseJobRecord.status,
                       ParseModelCallRecord, ParseUsageAttemptRecord)
                .select_from(SeriesParseJobRecord)
                .join(SeriesRecord, SeriesRecord.series_id == SeriesParseJobRecord.series_id)
                .outerjoin(ParseModelCallRecord, and_(
                    ParseModelCallRecord.job_id == SeriesParseJobRecord.job_id,
                    ParseModelCallRecord.workspace_id == workspace_id,
                ))
                .outerjoin(ParseUsageAttemptRecord, ParseUsageAttemptRecord.job_id == SeriesParseJobRecord.job_id)
                .where(SeriesParseJobRecord.job_id == job_id, SeriesRecord.workspace_id == workspace_id)
                .order_by(ParseModelCallRecord.started_at, ParseModelCallRecord.call_id)
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
    def _state(record: ParseModelCallRecord) -> ParseModelCallState:
        return ParseModelCallState(
            call_id=record.call_id, job_id=record.job_id, attempt_count=record.attempt_count,
            lease_version=record.lease_version, workspace_id=record.workspace_id,
            series_id=record.series_id, video_id=record.video_id, sequence_no=record.sequence_no,
            video_chunk_id=record.video_chunk_id, provider=record.provider, model=record.model,
            category=record.category, operation=record.operation, started_at=record.started_at,
            request_id=record.request_id, status=record.status,
            usage=TokenUsage(**record.tokens) if record.tokens is not None else None,
            estimated_cost_cny=Decimal(record.estimated_cost_cny) if record.estimated_cost_cny is not None else None,
            price_snapshot=record.price_snapshot,
        )
