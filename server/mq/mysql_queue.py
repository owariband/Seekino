from __future__ import annotations

from datetime import timedelta

from sqlalchemy import case, exists, select, update
from sqlalchemy.orm import aliased, Session, sessionmaker

from ..model.dto.jobDTO import ParseJobLease
from ..model.job import ParseJobStage, ParseJobStatus, SeriesParseJobRecord
from ..model.upload import UploadSessionRecord, UploadSessionStatus
from ..model.video import SeriesRecord, VideoRecord
from ..repository.index_sync import refresh_series_statuses
from ..repository.job import current_lease_predicate
from ..utils.time import now_unix_time_ms, timedelta_to_ms
from ..utils.validation import require_nonempty_text


class MySqlParseJobQueue:
    """Persist ParseJob delivery, lease, acknowledgement, and retry state."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def claim_next(
        self,
        worker_id: str,
        *,
        lease_duration: timedelta,
        now: int | None = None,
    ) -> ParseJobLease | None:
        normalized_worker_id = require_nonempty_text(worker_id, "worker_id")
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        claimed_at = now if now is not None else now_unix_time_ms()
        with self._session_factory.begin() as session:
            active_job = aliased(SeriesParseJobRecord)
            earlier_video = aliased(VideoRecord)
            earlier_upload = aliased(UploadSessionRecord)
            row = session.execute(
                select(SeriesParseJobRecord, VideoRecord, SeriesRecord)
                .join(
                    VideoRecord,
                    VideoRecord.video_id == SeriesParseJobRecord.video_id,
                )
                .join(
                    SeriesRecord,
                    SeriesRecord.series_id == SeriesParseJobRecord.series_id,
                )
                .where(
                    SeriesParseJobRecord.status.in_(
                        [
                            ParseJobStatus.QUEUED.value,
                            ParseJobStatus.RETRY_WAIT.value,
                        ]
                    ),
                    SeriesParseJobRecord.available_at <= claimed_at,
                    SeriesParseJobRecord.attempt_count
                    < SeriesParseJobRecord.max_attempts,
                    ~exists(
                        select(active_job.job_id).where(
                            active_job.series_id
                            == SeriesParseJobRecord.series_id,
                            active_job.status == ParseJobStatus.RUNNING.value,
                            active_job.lease_expires_at > claimed_at,
                        )
                    ),
                    # Completion order is not execution order. Wait for every
                    # earlier video in the Series to finish parsing.
                    ~exists(
                        select(earlier_video.video_id).where(
                            earlier_video.series_id
                            == SeriesParseJobRecord.series_id,
                            earlier_video.upload_sequence
                            < VideoRecord.upload_sequence,
                            earlier_video.parse_status.not_in(
                                [ParseJobStatus.SUCCEEDED.value, "COMPLETED"]
                            ),
                        )
                    ),
                    # An earlier upload has no ParseJob until completion.
                    # Expired sessions without a Video no longer block work.
                    ~exists(
                        select(earlier_upload.upload_session_id).where(
                            earlier_upload.series_id
                            == SeriesParseJobRecord.series_id,
                            earlier_upload.upload_sequence
                            < VideoRecord.upload_sequence,
                            earlier_upload.status.in_(
                                [
                                    UploadSessionStatus.UPLOADING.value,
                                    UploadSessionStatus.COMPLETING.value,
                                ]
                            ),
                            earlier_upload.expires_at > claimed_at,
                        )
                    ),
                )
                .order_by(
                    VideoRecord.upload_sequence,
                    SeriesParseJobRecord.available_at,
                    SeriesParseJobRecord.created_at,
                    SeriesParseJobRecord.job_id,
                )
                .limit(1)
                .with_for_update(skip_locked=True)
            ).one_or_none()
            if row is None:
                return None

            job, video, series = row
            preserve_existing_result = video.parse_status in {
                ParseJobStatus.SUCCEEDED.value,
                "COMPLETED",
            }
            expires_at = claimed_at + timedelta_to_ms(lease_duration)
            job.status = ParseJobStatus.RUNNING.value
            if job.stage == ParseJobStage.QUEUED.value:
                job.stage = ParseJobStage.PREPARING.value
            job.attempt_count += 1
            job.lease_owner = normalized_worker_id
            job.lease_version += 1
            job.lease_expires_at = expires_at
            job.heartbeat_at = claimed_at
            if job.started_at is None:
                job.started_at = claimed_at
            job.last_error = None
            job.version += 1
            if not preserve_existing_result:
                video.parse_status = ParseJobStatus.RUNNING.value
                series.status = "INDEXING"
            lease = ParseJobLease(
                job_id=job.job_id,
                series_id=job.series_id,
                video_id=job.video_id,
                pipeline_version=job.pipeline_version,
                worker_id=normalized_worker_id,
                lease_version=job.lease_version,
                lease_expires_at=expires_at,
                attempt_count=job.attempt_count,
                max_attempts=job.max_attempts,
                processing_duration_base_ms=job.processing_duration_ms,
                episode_order=video.episode_order,
                upload_sequence=video.upload_sequence,
                original_filename=video.original_filename,
                original_source_uri=video.original_source_uri,
                content_hash=video.content_hash,
                total_bytes=video.total_bytes,
                duration_ms=video.duration_ms,
                workspace_id=series.workspace_id,
                series_name=series.name,
                series_description=series.description,
                important_extra_data=series.important_extra_data,
                character_bible=series.character_bible,
                preserve_existing_result=preserve_existing_result,
            )
            session.flush()
            return lease

    def heartbeat(
        self,
        lease: ParseJobLease,
        *,
        lease_duration: timedelta,
        now: int | None = None,
    ) -> bool:
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        heartbeat_at = now if now is not None else now_unix_time_ms()
        with self._session_factory.begin() as session:
            result = session.execute(
                update(SeriesParseJobRecord)
                .where(*current_lease_predicate(lease, heartbeat_at))
                .values(
                    heartbeat_at=heartbeat_at,
                    lease_expires_at=heartbeat_at + timedelta_to_ms(lease_duration),
                )
            )
            return result.rowcount == 1

    def acknowledge(
        self,
        lease: ParseJobLease,
        *,
        duration_ms: int,
        now: int | None = None,
    ) -> bool:
        _require_duration(duration_ms)
        processing_duration_ms = lease.processing_duration_base_ms + duration_ms
        completed_at = now if now is not None else now_unix_time_ms()
        with self._session_factory.begin() as session:
            result = session.execute(
                update(SeriesParseJobRecord)
                .where(*current_lease_predicate(lease, completed_at))
                .values(
                    status=ParseJobStatus.SUCCEEDED.value,
                    stage=ParseJobStage.COMPLETED.value,
                    progress=100,
                    lease_owner=None,
                    lease_expires_at=None,
                    heartbeat_at=None,
                    completed_at=completed_at,
                    processing_duration_ms=case(
                        (
                            SeriesParseJobRecord.processing_duration_ms
                            < processing_duration_ms,
                            processing_duration_ms,
                        ),
                        else_=SeriesParseJobRecord.processing_duration_ms,
                    ),
                    last_error=None,
                    version=SeriesParseJobRecord.version + 1,
                )
            )
            if result.rowcount != 1:
                return False
            session.execute(
                update(VideoRecord)
                .where(VideoRecord.video_id == lease.video_id)
                .values(
                    parse_status=ParseJobStatus.SUCCEEDED.value,
                    parse_progress=100,
                )
            )
            refresh_series_statuses(session, {lease.series_id})
            return True

    def retry(
        self,
        lease: ParseJobLease,
        *,
        error: str,
        retry_at: int,
        duration_ms: int,
        now: int | None = None,
    ) -> bool:
        _require_duration(duration_ms)
        processing_duration_ms = lease.processing_duration_base_ms + duration_ms
        failed_at = now if now is not None else now_unix_time_ms()
        with self._session_factory.begin() as session:
            job = session.scalar(
                select(SeriesParseJobRecord)
                .where(*current_lease_predicate(lease, failed_at))
                .with_for_update()
            )
            if job is None:
                return False
            exhausted = job.attempt_count >= job.max_attempts
            job.status = (
                ParseJobStatus.FAILED.value
                if exhausted
                else ParseJobStatus.RETRY_WAIT.value
            )
            job.available_at = retry_at
            job.lease_owner = None
            job.lease_expires_at = None
            job.heartbeat_at = None
            job.last_error = _error_summary(error)
            job.version += 1
            job.completed_at = failed_at if exhausted else None
            job.processing_duration_ms = max(
                job.processing_duration_ms,
                processing_duration_ms,
            )
            video = session.get(VideoRecord, lease.video_id)
            if video is not None and not lease.preserve_existing_result:
                video.parse_status = job.status
            return True

    def reap_expired(
        self,
        *,
        retry_delay: timedelta,
        now: int | None = None,
        limit: int = 100,
    ) -> int:
        if retry_delay < timedelta(0):
            raise ValueError("retry_delay cannot be negative")
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        reaped_at = now if now is not None else now_unix_time_ms()
        with self._session_factory.begin() as session:
            jobs = session.scalars(
                select(SeriesParseJobRecord)
                .where(
                    SeriesParseJobRecord.status == ParseJobStatus.RUNNING.value,
                    SeriesParseJobRecord.lease_expires_at <= reaped_at,
                )
                .order_by(SeriesParseJobRecord.lease_expires_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            ).all()
            for job in jobs:
                exhausted = job.attempt_count >= job.max_attempts
                job.status = (
                    ParseJobStatus.FAILED.value
                    if exhausted
                    else ParseJobStatus.RETRY_WAIT.value
                )
                job.available_at = reaped_at + timedelta_to_ms(retry_delay)
                job.lease_owner = None
                job.lease_expires_at = None
                job.heartbeat_at = None
                job.last_error = "worker lease expired"
                job.version += 1
                job.completed_at = reaped_at if exhausted else None
                job.processing_timing_complete = False
                video = session.get(VideoRecord, job.video_id)
                if video is not None and video.parse_status not in {
                    ParseJobStatus.SUCCEEDED.value,
                    "COMPLETED",
                }:
                    video.parse_status = job.status
            return len(jobs)


def _error_summary(error: str) -> str:
    normalized = error.strip() or "parse job failed"
    return normalized[:8192]


def _require_duration(duration_ms: int) -> None:
    if (
        isinstance(duration_ms, bool)
        or not isinstance(duration_ms, int)
        or duration_ms < 0
    ):
        raise ValueError("duration_ms must be a nonnegative integer")
