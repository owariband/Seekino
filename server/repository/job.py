from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, select, update
from sqlalchemy.orm import Session, aliased, sessionmaker

from ..model.job import (
    ParseJobStage,
    ParseJobStatus,
    SeriesParseJobRecord,
    VideoChunkCheckpointRecord,
    VideoChunkCheckpointStage,
)
from ..model.video import SeriesRecord, VideoRecord
from .index_sync import refresh_series_statuses


class ParseJobNotFoundError(LookupError):
    pass


class ParseJobLeaseLostError(RuntimeError):
    pass


class ParseJobCheckpointConflictError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ParseJobState:
    job_id: str
    series_id: str
    video_id: str
    series_name: str
    episode_order: int
    original_filename: str
    pipeline_version: str
    status: str
    stage: str
    progress: int
    attempt_count: int
    max_attempts: int
    available_at: datetime
    lease_owner: str | None
    lease_version: int
    lease_expires_at: datetime | None
    heartbeat_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ParseJobLease:
    job_id: str
    series_id: str
    video_id: str
    pipeline_version: str
    worker_id: str
    lease_version: int
    lease_expires_at: datetime
    attempt_count: int
    max_attempts: int
    episode_order: int
    upload_sequence: int
    original_filename: str
    original_source_uri: str
    content_hash: str
    total_bytes: int
    duration_ms: int
    workspace_id: str
    series_name: str
    series_description: str | None
    important_extra_data: dict | None
    character_bible: list[dict]


@dataclass(frozen=True, slots=True)
class VideoChunkCheckpointState:
    job_id: str
    sequence_no: int
    video_chunk_id: str
    stage: str
    chunk_media_uri: str | None = None
    analysis_window_media_uri: str | None = None
    parse_artifact_uri: str | None = None
    text_embedding_artifact_uri: str | None = None
    vision_embedding_artifact_uri: str | None = None
    last_error: str | None = None


_CHECKPOINT_ORDER = {
    stage.value: order for order, stage in enumerate(VideoChunkCheckpointStage)
}
_JOB_STAGE_ORDER = {stage.value: order for order, stage in enumerate(ParseJobStage)}


class ParseJobRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def get(self, job_id: str) -> ParseJobState:
        with self._session_factory() as session:
            row = session.execute(
                select(
                    SeriesParseJobRecord,
                    SeriesRecord.name,
                    VideoRecord.episode_order,
                    VideoRecord.original_filename,
                )
                .join(
                    SeriesRecord,
                    SeriesRecord.series_id == SeriesParseJobRecord.series_id,
                )
                .join(
                    VideoRecord,
                    VideoRecord.video_id == SeriesParseJobRecord.video_id,
                )
                .where(SeriesParseJobRecord.job_id == job_id)
            ).one_or_none()
            if row is None:
                raise ParseJobNotFoundError(f"jobId {job_id!r} does not exist")
            record, series_name, episode_order, original_filename = row
            return self._state(
                record,
                series_name=series_name,
                episode_order=episode_order,
                original_filename=original_filename,
            )

    def list_for_workspace(
        self,
        workspace_id: str,
        *,
        limit: int = 100,
    ) -> list[ParseJobState]:
        normalized_workspace = _required_text(workspace_id, "workspace_id")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        with self._session_factory() as session:
            rows = session.execute(
                select(
                    SeriesParseJobRecord,
                    SeriesRecord.name,
                    VideoRecord.episode_order,
                    VideoRecord.original_filename,
                )
                .join(
                    SeriesRecord,
                    SeriesRecord.series_id == SeriesParseJobRecord.series_id,
                )
                .join(
                    VideoRecord,
                    VideoRecord.video_id == SeriesParseJobRecord.video_id,
                )
                .where(SeriesRecord.workspace_id == normalized_workspace)
                .order_by(
                    SeriesParseJobRecord.created_at.desc(),
                    SeriesParseJobRecord.job_id.desc(),
                )
                .limit(limit)
            ).all()
            return [
                self._state(
                    record,
                    series_name=series_name,
                    episode_order=episode_order,
                    original_filename=original_filename,
                )
                for record, series_name, episode_order, original_filename in rows
            ]

    def claim_next(
        self,
        worker_id: str,
        *,
        lease_duration: timedelta,
        now: datetime | None = None,
    ) -> ParseJobLease | None:
        normalized_worker_id = _required_text(worker_id, "worker_id")
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        claimed_at = now or _utc_now()
        with self._session_factory.begin() as session:
            active_job = aliased(SeriesParseJobRecord)
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
                )
                .order_by(
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
            expires_at = claimed_at + lease_duration
            job.status = ParseJobStatus.RUNNING.value
            if job.stage == ParseJobStage.QUEUED.value:
                job.stage = ParseJobStage.PREPARING.value
            job.attempt_count += 1
            job.lease_owner = normalized_worker_id
            job.lease_version += 1
            job.lease_expires_at = expires_at
            job.heartbeat_at = claimed_at
            job.started_at = job.started_at or claimed_at
            job.last_error = None
            video.parse_status = ParseJobStatus.RUNNING.value
            series.status = "INDEXING"
            session.flush()
            return ParseJobLease(
                job_id=job.job_id,
                series_id=job.series_id,
                video_id=job.video_id,
                pipeline_version=job.pipeline_version,
                worker_id=normalized_worker_id,
                lease_version=job.lease_version,
                lease_expires_at=expires_at,
                attempt_count=job.attempt_count,
                max_attempts=job.max_attempts,
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
            )

    def heartbeat(
        self,
        lease: ParseJobLease,
        *,
        lease_duration: timedelta,
        now: datetime | None = None,
    ) -> bool:
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        heartbeat_at = now or _utc_now()
        with self._session_factory.begin() as session:
            result = session.execute(
                update(SeriesParseJobRecord)
                .where(*self._lease_predicate(lease, heartbeat_at))
                .values(
                    heartbeat_at=heartbeat_at,
                    lease_expires_at=heartbeat_at + lease_duration,
                )
            )
            return result.rowcount == 1

    def update_progress(
        self,
        lease: ParseJobLease,
        *,
        stage: ParseJobStage | str,
        progress: int,
        now: datetime | None = None,
    ) -> bool:
        stage_value = ParseJobStage(stage).value
        if isinstance(progress, bool) or not isinstance(progress, int):
            raise ValueError("progress must be an integer")
        if not 0 <= progress <= 100:
            raise ValueError("progress must be between zero and 100")
        updated_at = now or _utc_now()
        with self._session_factory.begin() as session:
            job = session.scalar(
                select(SeriesParseJobRecord)
                .where(*self._lease_predicate(lease, updated_at))
                .with_for_update()
            )
            if job is None:
                return False
            if progress > job.progress:
                job.progress = progress
            if _JOB_STAGE_ORDER[stage_value] > _JOB_STAGE_ORDER[job.stage]:
                job.stage = stage_value
            video = session.get(VideoRecord, lease.video_id)
            if video is not None:
                video.parse_status = ParseJobStatus.RUNNING.value
                video.parse_progress = max(video.parse_progress, progress)
            return True

    def save_checkpoint(
        self,
        lease: ParseJobLease,
        checkpoint: VideoChunkCheckpointState,
        *,
        now: datetime | None = None,
    ) -> VideoChunkCheckpointState:
        if checkpoint.job_id != lease.job_id:
            raise ValueError("checkpoint job_id does not match the lease")
        if checkpoint.sequence_no < 0:
            raise ValueError("checkpoint sequence_no must be nonnegative")
        stage = VideoChunkCheckpointStage(checkpoint.stage).value
        checked_at = now or _utc_now()
        with self._session_factory.begin() as session:
            self._require_current_lease(session, lease, checked_at)
            current = session.get(
                VideoChunkCheckpointRecord,
                (checkpoint.job_id, checkpoint.sequence_no),
            )
            if current is None:
                current = VideoChunkCheckpointRecord(
                    job_id=checkpoint.job_id,
                    sequence_no=checkpoint.sequence_no,
                    video_chunk_id=checkpoint.video_chunk_id,
                    stage=stage,
                )
                session.add(current)
            else:
                if current.video_chunk_id != checkpoint.video_chunk_id:
                    raise ParseJobCheckpointConflictError(
                        "checkpoint sequence already identifies another VideoChunk"
                    )
                if _CHECKPOINT_ORDER[stage] < _CHECKPOINT_ORDER[current.stage]:
                    raise ParseJobCheckpointConflictError(
                        "checkpoint stage cannot move backwards"
                    )
                current.stage = stage

            for field_name in (
                "chunk_media_uri",
                "analysis_window_media_uri",
                "parse_artifact_uri",
                "text_embedding_artifact_uri",
                "vision_embedding_artifact_uri",
                "last_error",
            ):
                value = getattr(checkpoint, field_name)
                if value is not None:
                    setattr(current, field_name, value)
            session.flush()
            return self._checkpoint_state(current)

    def list_checkpoints(self, job_id: str) -> list[VideoChunkCheckpointState]:
        with self._session_factory() as session:
            if session.get(SeriesParseJobRecord, job_id) is None:
                raise ParseJobNotFoundError(f"jobId {job_id!r} does not exist")
            records = session.scalars(
                select(VideoChunkCheckpointRecord)
                .where(VideoChunkCheckpointRecord.job_id == job_id)
                .order_by(VideoChunkCheckpointRecord.sequence_no)
            ).all()
            return [self._checkpoint_state(item) for item in records]

    def list_checkpoints_for_jobs(
        self,
        job_ids: list[str],
    ) -> dict[str, list[VideoChunkCheckpointState]]:
        ordered_ids = list(dict.fromkeys(job_ids))
        grouped = {job_id: [] for job_id in ordered_ids}
        if not ordered_ids:
            return grouped
        with self._session_factory() as session:
            records = session.scalars(
                select(VideoChunkCheckpointRecord)
                .where(VideoChunkCheckpointRecord.job_id.in_(ordered_ids))
                .order_by(
                    VideoChunkCheckpointRecord.job_id,
                    VideoChunkCheckpointRecord.sequence_no,
                )
            ).all()
        for record in records:
            grouped[record.job_id].append(self._checkpoint_state(record))
        return grouped

    def assert_current_lease(
        self,
        lease: ParseJobLease,
        *,
        now: datetime | None = None,
    ) -> None:
        with self._session_factory() as session:
            self._require_current_lease(session, lease, now or _utc_now())

    def mark_succeeded(
        self,
        lease: ParseJobLease,
        *,
        now: datetime | None = None,
    ) -> bool:
        completed_at = now or _utc_now()
        with self._session_factory.begin() as session:
            result = session.execute(
                update(SeriesParseJobRecord)
                .where(*self._lease_predicate(lease, completed_at))
                .values(
                    status=ParseJobStatus.SUCCEEDED.value,
                    stage=ParseJobStage.COMPLETED.value,
                    progress=100,
                    lease_owner=None,
                    lease_expires_at=None,
                    heartbeat_at=None,
                    completed_at=completed_at,
                    last_error=None,
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

    def mark_failed(
        self,
        lease: ParseJobLease,
        *,
        error: str,
        retry_at: datetime,
        now: datetime | None = None,
    ) -> bool:
        failed_at = now or _utc_now()
        summary = _error_summary(error)
        with self._session_factory.begin() as session:
            job = session.scalar(
                select(SeriesParseJobRecord)
                .where(*self._lease_predicate(lease, failed_at))
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
            job.last_error = summary
            video = session.get(VideoRecord, lease.video_id)
            if video is not None:
                video.parse_status = job.status
            return True

    def reap_expired(
        self,
        *,
        retry_delay: timedelta,
        now: datetime | None = None,
        limit: int = 100,
    ) -> int:
        if retry_delay < timedelta(0):
            raise ValueError("retry_delay cannot be negative")
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        reaped_at = now or _utc_now()
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
                job.available_at = reaped_at + retry_delay
                job.lease_owner = None
                job.lease_expires_at = None
                job.heartbeat_at = None
                job.last_error = "worker lease expired"
                video = session.get(VideoRecord, job.video_id)
                if video is not None:
                    video.parse_status = job.status
            return len(jobs)

    @staticmethod
    def _lease_predicate(
        lease: ParseJobLease,
        now: datetime,
    ) -> tuple:
        return (
            SeriesParseJobRecord.job_id == lease.job_id,
            SeriesParseJobRecord.status == ParseJobStatus.RUNNING.value,
            SeriesParseJobRecord.lease_owner == lease.worker_id,
            SeriesParseJobRecord.lease_version == lease.lease_version,
            SeriesParseJobRecord.lease_expires_at > now,
        )

    @classmethod
    def _require_current_lease(
        cls,
        session: Session,
        lease: ParseJobLease,
        now: datetime,
    ) -> SeriesParseJobRecord:
        job = session.scalar(
            select(SeriesParseJobRecord).where(*cls._lease_predicate(lease, now))
        )
        if job is None:
            raise ParseJobLeaseLostError("parse job lease is no longer current")
        return job

    @staticmethod
    def _state(
        record: SeriesParseJobRecord,
        *,
        series_name: str,
        episode_order: int,
        original_filename: str,
    ) -> ParseJobState:
        return ParseJobState(
            job_id=record.job_id,
            series_id=record.series_id,
            video_id=record.video_id,
            series_name=series_name,
            episode_order=episode_order,
            original_filename=original_filename,
            pipeline_version=record.pipeline_version,
            status=record.status,
            stage=record.stage,
            progress=record.progress,
            attempt_count=record.attempt_count,
            max_attempts=record.max_attempts,
            available_at=record.available_at,
            lease_owner=record.lease_owner,
            lease_version=record.lease_version,
            lease_expires_at=record.lease_expires_at,
            heartbeat_at=record.heartbeat_at,
            started_at=record.started_at,
            completed_at=record.completed_at,
            last_error=record.last_error,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    @staticmethod
    def _checkpoint_state(
        record: VideoChunkCheckpointRecord,
    ) -> VideoChunkCheckpointState:
        return VideoChunkCheckpointState(
            job_id=record.job_id,
            sequence_no=record.sequence_no,
            video_chunk_id=record.video_chunk_id,
            stage=record.stage,
            chunk_media_uri=record.chunk_media_uri,
            analysis_window_media_uri=record.analysis_window_media_uri,
            parse_artifact_uri=record.parse_artifact_uri,
            text_embedding_artifact_uri=record.text_embedding_artifact_uri,
            vision_embedding_artifact_uri=record.vision_embedding_artifact_uri,
            last_error=record.last_error,
        )


def _required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _error_summary(error: str) -> str:
    normalized = error.strip() or "parse job failed"
    return normalized[:8192]


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
