from __future__ import annotations

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session, sessionmaker

from ..model.dto.jobDTO import (
    ParseJobLease,
    ParseJobState,
    ParseJobSummaryState,
    VideoChunkCheckpointState,
)
from ..model.job import (
    ParseJobStage,
    ParseJobStatus,
    SeriesParseJobRecord,
    VideoChunkCheckpointRecord,
    VideoChunkCheckpointStage,
)
from ..model.job_progress import calculate_checkpoint_progress
from ..model.upload import UploadSessionRecord, UploadSessionStatus
from ..model.video import SeriesRecord, VideoRecord
from ..mq.contracts import ParseJobLeaseLostError
from ..utils.time import now_unix_time_ms
from ..utils.validation import require_nonempty_text


class ParseJobNotFoundError(LookupError):
    pass


class ParseJobCheckpointConflictError(RuntimeError):
    pass


_CHECKPOINT_ORDER = {
    stage.value: order for order, stage in enumerate(VideoChunkCheckpointStage)
}
_JOB_STAGE_ORDER = {stage.value: order for order, stage in enumerate(ParseJobStage)}


def current_lease_predicate(
    lease: ParseJobLease,
    now: int,
) -> tuple:
    """Match writes owned by one still-current ParseJob lease."""

    return (
        SeriesParseJobRecord.job_id == lease.job_id,
        SeriesParseJobRecord.status == ParseJobStatus.RUNNING.value,
        SeriesParseJobRecord.lease_owner == lease.worker_id,
        SeriesParseJobRecord.lease_version == lease.lease_version,
        SeriesParseJobRecord.lease_expires_at > now,
    )


class ParseJobRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def get(self, job_id: str) -> ParseJobState:
        with self._session_factory() as session:
            upload_session_id = (
                select(UploadSessionRecord.upload_session_id)
                .where(UploadSessionRecord.video_id == SeriesParseJobRecord.video_id)
                .order_by(UploadSessionRecord.created_at)
                .limit(1)
                .correlate(SeriesParseJobRecord)
                .scalar_subquery()
            )
            is_primary_upload_job = exists(
                select(UploadSessionRecord.upload_session_id).where(
                    UploadSessionRecord.job_id == SeriesParseJobRecord.job_id,
                    UploadSessionRecord.status == UploadSessionStatus.COMPLETED.value,
                )
            ).correlate(SeriesParseJobRecord)
            row = session.execute(
                select(
                    SeriesParseJobRecord,
                    upload_session_id,
                    is_primary_upload_job,
                    SeriesRecord.workspace_id,
                    SeriesRecord.name,
                    VideoRecord.episode_order,
                    VideoRecord.original_filename,
                    VideoRecord.duration_seconds,
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
            (
                record,
                linked_upload_session_id,
                linked_as_primary_job,
                workspace_id,
                series_name,
                episode_order,
                original_filename,
                duration_seconds,
            ) = row
            return self._state(
                record,
                upload_session_id=linked_upload_session_id,
                is_primary_upload_job=linked_as_primary_job,
                workspace_id=workspace_id,
                series_name=series_name,
                episode_order=episode_order,
                original_filename=original_filename,
                duration_seconds=duration_seconds,
            )

    def list_for_workspace(
        self,
        workspace_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
        series_id: str | None = None,
        statuses: tuple[str, ...] | None = None,
    ) -> list[ParseJobState]:
        normalized_workspace = require_nonempty_text(workspace_id, "workspace_id")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        with self._session_factory() as session:
            upload_session_id = (
                select(UploadSessionRecord.upload_session_id)
                .where(UploadSessionRecord.video_id == SeriesParseJobRecord.video_id)
                .order_by(UploadSessionRecord.created_at)
                .limit(1)
                .correlate(SeriesParseJobRecord)
                .scalar_subquery()
            )
            is_primary_upload_job = exists(
                select(UploadSessionRecord.upload_session_id).where(
                    UploadSessionRecord.job_id == SeriesParseJobRecord.job_id,
                    UploadSessionRecord.status == UploadSessionStatus.COMPLETED.value,
                )
            ).correlate(SeriesParseJobRecord)
            rows = session.execute(
                select(
                    SeriesParseJobRecord,
                    upload_session_id,
                    is_primary_upload_job,
                    SeriesRecord.workspace_id,
                    SeriesRecord.name,
                    VideoRecord.episode_order,
                    VideoRecord.original_filename,
                    VideoRecord.duration_seconds,
                )
                .join(
                    SeriesRecord,
                    SeriesRecord.series_id == SeriesParseJobRecord.series_id,
                )
                .join(
                    VideoRecord,
                    VideoRecord.video_id == SeriesParseJobRecord.video_id,
                )
                .where(*self._workspace_filters(normalized_workspace, series_id=series_id, statuses=statuses))
                .order_by(
                    SeriesParseJobRecord.created_at.desc(),
                    SeriesParseJobRecord.job_id.desc(),
                )
                .limit(limit)
                .offset(offset)
            ).all()
            return [
                self._state(
                    record,
                    upload_session_id=linked_upload_session_id,
                    is_primary_upload_job=linked_as_primary_job,
                    workspace_id=workspace_id,
                    series_name=series_name,
                    episode_order=episode_order,
                    original_filename=original_filename,
                    duration_seconds=duration_seconds,
                )
                for (
                    record,
                    linked_upload_session_id,
                    linked_as_primary_job,
                    workspace_id,
                    series_name,
                    episode_order,
                    original_filename,
                    duration_seconds,
                ) in rows
            ]

    def count_for_workspace(
        self,
        workspace_id: str,
        *,
        series_id: str | None = None,
        statuses: tuple[str, ...] | None = None,
    ) -> int:
        with self._session_factory() as session:
            return int(session.scalar(
                select(func.count(SeriesParseJobRecord.job_id))
                .join(SeriesRecord)
                .where(*self._workspace_filters(workspace_id, series_id=series_id, statuses=statuses))
            ) or 0)

    def summary_for_workspace(self, workspace_id: str, *, series_id: str | None = None) -> ParseJobSummaryState:
        with self._session_factory() as session:
            rows = session.execute(
                select(
                    SeriesParseJobRecord.status,
                    func.count(SeriesParseJobRecord.job_id),
                    func.sum(SeriesParseJobRecord.version),
                    func.max(SeriesParseJobRecord.updated_at),
                )
                .join(SeriesRecord)
                .where(*self._workspace_filters(workspace_id, series_id=series_id, statuses=None))
                .group_by(SeriesParseJobRecord.status)
            ).all()
            checkpoint_count = session.scalar(
                select(func.count(VideoChunkCheckpointRecord.video_chunk_id))
                .join(SeriesParseJobRecord)
                .join(SeriesRecord)
                .where(*self._workspace_filters(workspace_id, series_id=series_id, statuses=None))
            )
            return ParseJobSummaryState(
                workspace_id=workspace_id, series_id=series_id,
                counts={status: int(count) for status, count, _version, _updated in rows},
                checkpoint_count=int(checkpoint_count or 0),
                version=sum(int(version) for _status, _count, version, _updated in rows),
                updated_at=max(
                    (updated for _status, _count, _version, updated in rows),
                    default=now_unix_time_ms(),
                ),
            )

    @staticmethod
    def _workspace_filters(workspace_id: str, *, series_id: str | None, statuses: tuple[str, ...] | None):
        filters = [SeriesRecord.workspace_id == workspace_id]
        if series_id is not None:
            filters.append(SeriesParseJobRecord.series_id == series_id)
        if statuses is not None:
            filters.append(SeriesParseJobRecord.status.in_(statuses))
        return filters

    def update_progress(
        self,
        lease: ParseJobLease,
        *,
        stage: ParseJobStage | str,
        progress: int | None = None,
        now: int | None = None,
    ) -> bool:
        """Advance the public stage without inventing a percentage.

        ``progress`` remains accepted for callers deployed with the previous
        contract, but the durable percentage is now derived exclusively from
        checkpoint time coverage in :meth:`save_checkpoint`.
        """

        stage_value = ParseJobStage(stage).value
        if progress is not None:
            if isinstance(progress, bool) or not isinstance(progress, int):
                raise ValueError("progress must be an integer")
            if not 0 <= progress <= 100:
                raise ValueError("progress must be between zero and 100")
        updated_at = now if now is not None else now_unix_time_ms()
        with self._session_factory.begin() as session:
            job = session.scalar(
                select(SeriesParseJobRecord)
                .where(*current_lease_predicate(lease, updated_at))
                .with_for_update()
            )
            if job is None:
                return False
            changed = False
            if _JOB_STAGE_ORDER[stage_value] > _JOB_STAGE_ORDER[job.stage]:
                job.stage = stage_value
                changed = True
            if changed:
                job.version += 1
            video = session.get(VideoRecord, lease.video_id)
            if video is not None and not lease.preserve_existing_result:
                video.parse_status = ParseJobStatus.RUNNING.value
            return True

    def save_checkpoint(
        self,
        lease: ParseJobLease,
        checkpoint: VideoChunkCheckpointState,
        *,
        processing_duration_ms: int | None = None,
        now: int | None = None,
    ) -> VideoChunkCheckpointState:
        if checkpoint.job_id != lease.job_id:
            raise ValueError("checkpoint job_id does not match the lease")
        if checkpoint.sequence_no < 0:
            raise ValueError("checkpoint sequence_no must be nonnegative")
        if processing_duration_ms is not None and (
            isinstance(processing_duration_ms, bool)
            or not isinstance(processing_duration_ms, int)
            or processing_duration_ms < 0
        ):
            raise ValueError("processing_duration_ms must be a nonnegative integer")
        _validate_checkpoint_bounds(checkpoint, lease.duration_seconds)
        stage = VideoChunkCheckpointStage(checkpoint.stage).value
        checked_at = now if now is not None else now_unix_time_ms()
        with self._session_factory.begin() as session:
            job = self._require_current_lease(session, lease, checked_at)
            current = session.get(
                VideoChunkCheckpointRecord,
                (checkpoint.job_id, checkpoint.sequence_no),
            )
            changed = current is None
            if current is None:
                current = VideoChunkCheckpointRecord(
                    job_id=checkpoint.job_id,
                    sequence_no=checkpoint.sequence_no,
                    video_chunk_id=checkpoint.video_chunk_id,
                    stage=stage,
                    chunk_start_seconds=checkpoint.chunk_start_seconds,
                    chunk_end_seconds=checkpoint.chunk_end_seconds,
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
                if current.stage != stage:
                    current.stage = stage
                    changed = True

                incoming_range = (
                    checkpoint.chunk_start_seconds,
                    checkpoint.chunk_end_seconds,
                )
                current_range = (current.chunk_start_seconds, current.chunk_end_seconds)
                if incoming_range != (None, None):
                    if current_range == (None, None):
                        current.chunk_start_seconds, current.chunk_end_seconds = incoming_range
                        changed = True
                    elif current_range != incoming_range:
                        raise ParseJobCheckpointConflictError(
                            "checkpoint canonical time range cannot change"
                        )

            for field_name in (
                "chunk_media_uri",
                "analysis_window_media_uri",
                "parse_artifact_uri",
                "text_embedding_artifact_uri",
                "vision_embedding_artifact_uri",
                "last_error",
            ):
                value = getattr(checkpoint, field_name)
                if value is not None and getattr(current, field_name) != value:
                    setattr(current, field_name, value)
                    changed = True
            session.flush()
            records = session.scalars(
                select(VideoChunkCheckpointRecord)
                .where(VideoChunkCheckpointRecord.job_id == checkpoint.job_id)
                .order_by(VideoChunkCheckpointRecord.sequence_no)
            ).all()
            _validate_checkpoint_topology(records, lease.duration_seconds)
            progress = calculate_checkpoint_progress(
                records,
                lease.duration_seconds,
            )
            derived_progress = (
                progress.overall_progress if progress is not None else None
            )
            if derived_progress is not None:
                video = session.get(VideoRecord, lease.video_id)
                if video is not None and not lease.preserve_existing_result:
                    video.parse_progress = derived_progress
                    video.parse_status = ParseJobStatus.RUNNING.value
            if derived_progress is not None and job.progress != derived_progress:
                job.progress = derived_progress
                changed = True
            if (
                processing_duration_ms is not None
                and processing_duration_ms > job.processing_duration_ms
            ):
                job.processing_duration_ms = processing_duration_ms
                changed = True
            if changed:
                job.version += 1
                job.updated_at = checked_at
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
        now: int | None = None,
    ) -> None:
        with self._session_factory() as session:
            self._require_current_lease(
                session,
                lease,
                now if now is not None else now_unix_time_ms(),
            )

    @classmethod
    def _require_current_lease(
        cls,
        session: Session,
        lease: ParseJobLease,
        now: int,
    ) -> SeriesParseJobRecord:
        job = session.scalar(
            select(SeriesParseJobRecord)
            .where(*current_lease_predicate(lease, now))
            .with_for_update()
        )
        if job is None:
            raise ParseJobLeaseLostError("parse job lease is no longer current")
        return job

    @staticmethod
    def _state(
        record: SeriesParseJobRecord,
        *,
        upload_session_id: str | None,
        is_primary_upload_job: bool,
        workspace_id: str,
        series_name: str,
        episode_order: int,
        original_filename: str,
        duration_seconds: int,
    ) -> ParseJobState:
        return ParseJobState(
            job_id=record.job_id,
            upload_session_id=upload_session_id,
            is_primary_upload_job=is_primary_upload_job,
            workspace_id=workspace_id,
            series_id=record.series_id,
            video_id=record.video_id,
            series_name=series_name,
            episode_order=episode_order,
            original_filename=original_filename,
            duration_seconds=duration_seconds,
            pipeline_version=record.pipeline_version,
            parse_config=record.parse_config,
            status=record.status,
            stage=record.stage,
            progress=record.progress,
            attempt_count=record.attempt_count,
            max_attempts=record.max_attempts,
            version=record.version,
            available_at=record.available_at,
            lease_owner=record.lease_owner,
            lease_version=record.lease_version,
            lease_expires_at=record.lease_expires_at,
            heartbeat_at=record.heartbeat_at,
            started_at=record.started_at,
            completed_at=record.completed_at,
            processing_duration_ms=record.processing_duration_ms,
            processing_timing_complete=record.processing_timing_complete,
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
            chunk_start_seconds=record.chunk_start_seconds,
            chunk_end_seconds=record.chunk_end_seconds,
            chunk_media_uri=record.chunk_media_uri,
            analysis_window_media_uri=record.analysis_window_media_uri,
            parse_artifact_uri=record.parse_artifact_uri,
            text_embedding_artifact_uri=record.text_embedding_artifact_uri,
            vision_embedding_artifact_uri=record.vision_embedding_artifact_uri,
            last_error=record.last_error,
        )

def _validate_checkpoint_bounds(
    checkpoint: VideoChunkCheckpointState,
    duration_seconds: int,
) -> None:
    start_seconds = checkpoint.chunk_start_seconds
    end_seconds = checkpoint.chunk_end_seconds
    if (start_seconds is None) != (end_seconds is None):
        raise ValueError(
            "checkpoint chunk_start_seconds and chunk_end_seconds must be provided together"
        )
    if start_seconds is None:
        return
    if (
        isinstance(start_seconds, bool)
        or isinstance(end_seconds, bool)
        or not isinstance(start_seconds, int)
        or not isinstance(end_seconds, int)
    ):
        raise ValueError("checkpoint canonical time range must use integer milliseconds")
    if start_seconds < 0 or end_seconds <= start_seconds:
        raise ValueError(
            "checkpoint chunk_end_seconds must be greater than nonnegative chunk_start_seconds"
        )
    if end_seconds > duration_seconds:
        raise ValueError("checkpoint canonical time range exceeds the Video duration")


def _validate_checkpoint_topology(
    records: list[VideoChunkCheckpointRecord],
    duration_seconds: int,
) -> None:
    ranged = [
        item
        for item in records
        if item.chunk_start_seconds is not None and item.chunk_end_seconds is not None
    ]
    if ranged and ranged[0].sequence_no == 0 and ranged[0].chunk_start_seconds != 0:
        raise ParseJobCheckpointConflictError(
            "the first checkpoint canonical range must start at zero"
        )
    for previous, current in zip(ranged, ranged[1:]):
        if current.sequence_no != previous.sequence_no + 1:
            continue
        if current.chunk_start_seconds != previous.chunk_end_seconds:
            raise ParseJobCheckpointConflictError(
                "adjacent checkpoint canonical ranges must be contiguous"
            )
    if any(item.chunk_end_seconds > duration_seconds for item in ranged):
        raise ParseJobCheckpointConflictError(
            "checkpoint canonical range exceeds the Video duration"
        )
