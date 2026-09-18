from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from ..model.dto.jobDTO import ParseJobLease
from ..model.dto.jobTimingDTO import (
    ParseJobAttemptTimingState,
    VideoChunkStageTimingState,
)
from ..model.job_timing import (
    ParseJobAttemptRecord,
    ParseJobAttemptOutcome,
    VideoChunkStageTimingRecord,
    VideoChunkTimingStage,
    VideoChunkTimingStatus,
)
from ..model.job import SeriesParseJobRecord


class ParseJobTimingRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def start_chunk_stage(
        self,
        lease: ParseJobLease,
        *,
        sequence_no: int,
        video_chunk_id: str,
        stage: VideoChunkTimingStage | str,
        started_at: int,
    ) -> VideoChunkStageTimingState:
        if isinstance(sequence_no, bool) or not isinstance(sequence_no, int):
            raise ValueError("sequence_no must be an integer")
        if sequence_no < 0:
            raise ValueError("sequence_no must be nonnegative")
        if not video_chunk_id:
            raise ValueError("video_chunk_id must not be empty")
        _require_nonnegative_int(started_at, "started_at")
        stage_value = VideoChunkTimingStage(stage).value
        identity = (
            lease.job_id,
            lease.attempt_count,
            lease.lease_version,
        )
        with self._session_factory.begin() as session:
            attempt = session.get(
                ParseJobAttemptRecord,
                identity,
                with_for_update=True,
            )
            if attempt is None:
                raise LookupError("ParseJob attempt timing was not started")
            if attempt.outcome != ParseJobAttemptOutcome.RUNNING.value:
                raise RuntimeError("ParseJob attempt timing is already complete")
            record_identity = (*identity, sequence_no, stage_value)
            if session.get(VideoChunkStageTimingRecord, record_identity) is not None:
                raise RuntimeError("VideoChunk stage timing was already started")
            record = VideoChunkStageTimingRecord(
                job_id=lease.job_id,
                attempt_count=lease.attempt_count,
                lease_version=lease.lease_version,
                sequence_no=sequence_no,
                video_chunk_id=video_chunk_id,
                stage=stage_value,
                started_at=started_at,
                status=VideoChunkTimingStatus.RUNNING.value,
            )
            session.add(record)
            session.flush()
            return self._chunk_stage_state(record)

    def finish_chunk_stage(
        self,
        lease: ParseJobLease,
        *,
        sequence_no: int,
        stage: VideoChunkTimingStage | str,
        completed_at: int,
        duration_ms: int,
        status: VideoChunkTimingStatus | str,
    ) -> VideoChunkStageTimingState:
        _require_nonnegative_int(completed_at, "completed_at")
        _require_nonnegative_int(duration_ms, "duration_ms")
        stage_value = VideoChunkTimingStage(stage).value
        status_value = VideoChunkTimingStatus(status).value
        if status_value not in {
            VideoChunkTimingStatus.SUCCEEDED.value,
            VideoChunkTimingStatus.FAILED.value,
        }:
            raise ValueError(
                "a measured VideoChunk stage must finish as SUCCEEDED or FAILED"
            )
        identity = (
            lease.job_id,
            lease.attempt_count,
            lease.lease_version,
            sequence_no,
            stage_value,
        )
        with self._session_factory.begin() as session:
            attempt = session.get(
                ParseJobAttemptRecord,
                identity[:3],
                with_for_update=True,
            )
            if attempt is None:
                raise LookupError("ParseJob attempt timing was not started")
            if attempt.outcome != ParseJobAttemptOutcome.RUNNING.value:
                raise RuntimeError("ParseJob attempt timing is no longer running")
            record = session.get(
                VideoChunkStageTimingRecord,
                identity,
                with_for_update=True,
            )
            if record is None:
                raise LookupError("VideoChunk stage timing was not started")
            if record.status != VideoChunkTimingStatus.RUNNING.value:
                raise RuntimeError("VideoChunk stage timing is already complete")
            record.completed_at = completed_at
            record.duration_ms = duration_ms
            record.status = status_value
            session.flush()
            return self._chunk_stage_state(record)

    def list_attempts(self, job_id: str) -> list[ParseJobAttemptTimingState]:
        with self._session_factory() as session:
            records = session.scalars(
                select(ParseJobAttemptRecord)
                .where(ParseJobAttemptRecord.job_id == job_id)
                .order_by(
                    ParseJobAttemptRecord.attempt_count,
                    ParseJobAttemptRecord.lease_version,
                )
            ).all()
            return [self._attempt_state(record) for record in records]

    def list_chunk_stages(
        self,
        job_id: str,
    ) -> list[VideoChunkStageTimingState]:
        with self._session_factory() as session:
            records = session.scalars(
                select(VideoChunkStageTimingRecord)
                .where(VideoChunkStageTimingRecord.job_id == job_id)
                .order_by(
                    VideoChunkStageTimingRecord.attempt_count,
                    VideoChunkStageTimingRecord.lease_version,
                    VideoChunkStageTimingRecord.sequence_no,
                    VideoChunkStageTimingRecord.started_at,
                )
            ).all()
            return [self._chunk_stage_state(record) for record in records]

    def totals(self, job_id: str) -> tuple[int, bool]:
        with self._session_factory() as session:
            return attempt_totals_in_session(session, job_id)

    def totals_for_jobs(self, job_ids: list[str]) -> dict[str, tuple[int, bool]]:
        if not job_ids:
            return {}
        with self._session_factory() as session:
            records = session.scalars(
                select(ParseJobAttemptRecord).where(
                    ParseJobAttemptRecord.job_id.in_(job_ids)
                )
            ).all()
            attempt_counts = dict(session.execute(
                select(
                    SeriesParseJobRecord.job_id,
                    SeriesParseJobRecord.attempt_count,
                ).where(SeriesParseJobRecord.job_id.in_(job_ids))
            ).all())
        grouped: dict[str, list[ParseJobAttemptRecord]] = {
            job_id: [] for job_id in job_ids
        }
        for record in records:
            grouped[record.job_id].append(record)
        return {
            job_id: (
                sum(record.duration_ms or 0 for record in attempts),
                _attempts_are_complete(
                    attempts, attempt_counts.get(job_id, 0),
                ),
            )
            for job_id, attempts in grouped.items()
        }

    @staticmethod
    def _attempt_state(
        record: ParseJobAttemptRecord,
    ) -> ParseJobAttemptTimingState:
        return ParseJobAttemptTimingState(
            job_id=record.job_id,
            attempt_count=record.attempt_count,
            lease_version=record.lease_version,
            started_at=record.started_at,
            completed_at=record.completed_at,
            duration_ms=record.duration_ms,
            outcome=record.outcome,
            timing_complete=record.timing_complete,
        )

    @staticmethod
    def _chunk_stage_state(
        record: VideoChunkStageTimingRecord,
    ) -> VideoChunkStageTimingState:
        return VideoChunkStageTimingState(
            job_id=record.job_id,
            attempt_count=record.attempt_count,
            lease_version=record.lease_version,
            sequence_no=record.sequence_no,
            video_chunk_id=record.video_chunk_id,
            stage=record.stage,
            started_at=record.started_at,
            completed_at=record.completed_at,
            duration_ms=record.duration_ms,
            status=record.status,
        )


def _require_nonnegative_int(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def start_attempt_in_session(
    session: Session,
    lease: ParseJobLease,
    *,
    started_at: int,
) -> None:
    """Create the attempt alongside the queue claim that owns it."""

    _require_nonnegative_int(started_at, "started_at")
    identity = (lease.job_id, lease.attempt_count, lease.lease_version)
    if session.get(ParseJobAttemptRecord, identity) is not None:
        raise RuntimeError("ParseJob attempt timing already exists")
    session.add(ParseJobAttemptRecord(
        job_id=lease.job_id,
        attempt_count=lease.attempt_count,
        lease_version=lease.lease_version,
        started_at=started_at,
        outcome=ParseJobAttemptOutcome.RUNNING.value,
        timing_complete=False,
    ))


def finish_attempt_in_session(
    session: Session,
    lease: ParseJobLease,
    *,
    completed_at: int,
    outcome: ParseJobAttemptOutcome,
    duration_ms: int | None,
) -> None:
    """Close the exact lease attempt while the queue state is transitioned."""

    _require_nonnegative_int(completed_at, "completed_at")
    if duration_ms is not None:
        _require_nonnegative_int(duration_ms, "duration_ms")
    if outcome is ParseJobAttemptOutcome.RUNNING:
        raise ValueError("an attempt cannot be finished as RUNNING")
    if outcome is ParseJobAttemptOutcome.LEASE_EXPIRED and duration_ms is not None:
        raise ValueError("expired leases must not invent a duration")
    if outcome is not ParseJobAttemptOutcome.LEASE_EXPIRED and duration_ms is None:
        raise ValueError("completed attempts require a measured duration")
    record = session.get(
        ParseJobAttemptRecord,
        (lease.job_id, lease.attempt_count, lease.lease_version),
        with_for_update=True,
    )
    if record is None:
        raise LookupError("ParseJob attempt timing was not started")
    if record.outcome != ParseJobAttemptOutcome.RUNNING.value:
        raise RuntimeError("ParseJob attempt timing is already complete")
    record.completed_at = completed_at
    record.duration_ms = duration_ms
    record.outcome = outcome.value
    record.timing_complete = duration_ms is not None


def expire_attempt_in_session(
    session: Session,
    *,
    job_id: str,
    attempt_count: int,
    lease_version: int,
    completed_at: int,
) -> bool:
    """Record a lease expiry without guessing how long a dead worker ran."""

    _require_nonnegative_int(completed_at, "completed_at")
    record = session.get(
        ParseJobAttemptRecord,
        (job_id, attempt_count, lease_version),
        with_for_update=True,
    )
    if record is None:
        # A job claimed before timing was deployed has no safe per-attempt
        # start instant to backfill. Reap it without inventing timing data;
        # aggregate completeness will remain false because the row is absent.
        return False
    if record.outcome != ParseJobAttemptOutcome.RUNNING.value:
        return False
    session.execute(
        update(VideoChunkStageTimingRecord)
        .where(
            VideoChunkStageTimingRecord.job_id == job_id,
            VideoChunkStageTimingRecord.attempt_count == attempt_count,
            VideoChunkStageTimingRecord.lease_version == lease_version,
            VideoChunkStageTimingRecord.status
            == VideoChunkTimingStatus.RUNNING.value,
        )
        .values(
            completed_at=completed_at,
            duration_ms=None,
            status=VideoChunkTimingStatus.LEASE_EXPIRED.value,
        )
    )
    record.completed_at = completed_at
    record.duration_ms = None
    record.outcome = ParseJobAttemptOutcome.LEASE_EXPIRED.value
    record.timing_complete = False
    return True


def attempt_totals_in_session(
    session: Session,
    job_id: str,
) -> tuple[int, bool]:
    """Return the sum of known durations and whether no timing is missing."""

    records = session.scalars(
        select(ParseJobAttemptRecord).where(ParseJobAttemptRecord.job_id == job_id)
    ).all()
    attempt_count = session.scalar(
        select(SeriesParseJobRecord.attempt_count).where(
            SeriesParseJobRecord.job_id == job_id
        )
    )
    return (
        sum(record.duration_ms or 0 for record in records),
        _attempts_are_complete(records, int(attempt_count or 0)),
    )


def _attempts_are_complete(
    records: list[ParseJobAttemptRecord],
    attempt_count: int,
) -> bool:
    """Old jobs may predate timing rows; never mistake their gaps for zero."""

    if attempt_count <= 0 or len(records) != attempt_count:
        return False
    if {record.attempt_count for record in records} != set(range(1, attempt_count + 1)):
        return False
    return all(record.timing_complete for record in records)
