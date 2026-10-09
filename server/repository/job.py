from __future__ import annotations

from sqlalchemy import exists, func, select
from sqlalchemy.orm import aliased, Session, sessionmaker

from ..model.dto.jobDTO import (
    ParseJobLease,
    ParseJobState,
    ParseJobSummaryState,
    VideoParseTaskDetail,
    VideoParseTaskInputState,
    VideoParseTaskState,
    VideoChunkCheckpointState,
)
from ..model.job import (
    ChunkParseTask,
    IntervalParseTask,
    ParseJobStage,
    ParseJobStatus,
    SeriesParseJob,
    VideoParseTask,
    VideoChunkCheckpoint,
    VideoChunkCheckpointStage,
)
from ..model.job_progress import calculate_checkpoint_progress
from ..model.upload import UploadSession, UploadSessionStatus
from ..model.video import Series, Video
from ..mq.contracts import ParseJobLeaseLostError
from ..utils.time import now_unix_time_ms, unix_time_ms_to_datetime
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
        SeriesParseJob.job_id == lease.job_id,
        SeriesParseJob.status == ParseJobStatus.RUNNING.value,
        SeriesParseJob.lease_owner == lease.worker_id,
        SeriesParseJob.lease_version == lease.lease_version,
        SeriesParseJob.lease_expires_at > now,
    )


class ParseJobRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def get(self, job_id: str) -> ParseJobState:
        with self._session_factory() as session:
            upload_session_id = (
                select(UploadSession.upload_session_id)
                .where(UploadSession.video_id == SeriesParseJob.video_id)
                .order_by(UploadSession.created_at)
                .limit(1)
                .correlate(SeriesParseJob)
                .scalar_subquery()
            )
            is_primary_upload_job = exists(
                select(UploadSession.upload_session_id).where(
                    UploadSession.job_id == SeriesParseJob.job_id,
                    UploadSession.status == UploadSessionStatus.COMPLETED.value,
                )
            ).correlate(SeriesParseJob)
            row = session.execute(
                select(
                    SeriesParseJob,
                    upload_session_id,
                    is_primary_upload_job,
                    Series.tenant_id,
                    Series.name,
                    Video.episode_order,
                    Video.original_filename,
                    Video.duration_seconds,
                )
                .join(
                    Series,
                    Series.series_id == SeriesParseJob.series_id,
                )
                .join(
                    Video,
                    Video.video_id == SeriesParseJob.video_id,
                )
                .where(SeriesParseJob.job_id == job_id)
            ).one_or_none()
            if row is None:
                raise ParseJobNotFoundError(f"jobId {job_id!r} does not exist")
            (
                record,
                linked_upload_session_id,
                linked_as_primary_job,
                tenant_id,
                series_name,
                episode_order,
                original_filename,
                duration_seconds,
            ) = row
            return self._state(
                record,
                upload_session_id=linked_upload_session_id,
                is_primary_upload_job=linked_as_primary_job,
                tenant_id=tenant_id,
                series_name=series_name,
                episode_order=episode_order,
                original_filename=original_filename,
                duration_seconds=duration_seconds,
            )

    def get_video_parse_task(
        self,
        video_parse_task_id: str,
    ) -> VideoParseTaskState:
        """Read one V2 parse identity and its frozen execution snapshot."""

        with self._session_factory() as session:
            record = session.scalar(
                select(VideoParseTask).where(
                    VideoParseTask.id == video_parse_task_id,
                    VideoParseTask.is_deleted.is_(False),
                )
            )
            if record is None:
                raise ParseJobNotFoundError(
                    f"videoParseTaskId {video_parse_task_id!r} does not exist"
                )
            return self._video_parse_task_state(record)

    def get_video_parse_input(
        self,
        video_parse_task_id: str,
    ) -> VideoParseTaskInputState:
        """Load the immutable task snapshot together with its source Video."""

        with self._session_factory() as session:
            row = session.execute(
                select(VideoParseTask, Video)
                .join(
                    Video,
                    Video.video_id == VideoParseTask.video_id,
                )
                .where(
                    VideoParseTask.id == video_parse_task_id,
                    VideoParseTask.is_deleted.is_(False),
                )
            ).one_or_none()
            if row is None:
                raise ParseJobNotFoundError(
                    f"videoParseTaskId {video_parse_task_id!r} does not exist"
                )
            task, video = row
            return VideoParseTaskInputState(
                task=self._video_parse_task_state(task),
                series_id=video.series_id,
                episode_order=video.episode_order,
                original_filename=video.original_filename,
                original_source_uri=video.original_source_uri,
                inference_proxy_uri=video.inference_proxy_uri,
                content_hash=video.content_hash,
                total_bytes=video.total_bytes,
                duration_seconds=video.duration_seconds,
            )

    def latest_video_parse_task(
        self,
        video_id: str,
    ) -> VideoParseTaskState:
        """Return the newest non-deleted parse attempt for one Video."""

        with self._session_factory() as session:
            record = session.scalar(
                select(VideoParseTask)
                .where(
                    VideoParseTask.video_id == video_id,
                    VideoParseTask.is_deleted.is_(False),
                )
                .order_by(
                    VideoParseTask.version.desc(),
                    VideoParseTask.id.desc(),
                )
                .limit(1)
            )
            if record is None:
                raise ParseJobNotFoundError(
                    f"videoId {video_id!r} has no Video Parse V2 task"
                )
            return self._video_parse_task_state(record)

    def list_video_parse_tasks(
        self,
        video_id: str,
    ) -> list[VideoParseTaskState]:
        """List retained parse attempts in stable parse-version order."""

        with self._session_factory() as session:
            records = session.scalars(
                select(VideoParseTask)
                .where(
                    VideoParseTask.video_id == video_id,
                    VideoParseTask.is_deleted.is_(False),
                )
                .order_by(
                    VideoParseTask.version,
                    VideoParseTask.id,
                )
            ).all()
            return [self._video_parse_task_state(record) for record in records]

    def get_video_parse_task_detail(
        self,
        video_parse_task_id: str,
        *,
        tenant_id: str,
    ) -> VideoParseTaskDetail:
        """Read one V2 task with the public library context used by job views."""

        normalized_workspace = require_nonempty_text(tenant_id, "tenant_id")
        with self._session_factory() as session:
            upload_session_id = self._video_parse_upload_session_id()
            is_latest = self._video_parse_task_is_latest()
            row = session.execute(
                select(
                    VideoParseTask,
                    Series.tenant_id,
                    Series.series_id,
                    Series.name,
                    Video.episode_order,
                    Video.original_filename,
                    Video.duration_seconds,
                    upload_session_id,
                    is_latest,
                )
                .join(
                    Video,
                    Video.video_id == VideoParseTask.video_id,
                )
                .join(Series, Series.series_id == Video.series_id)
                .where(
                    VideoParseTask.id == video_parse_task_id,
                    VideoParseTask.is_deleted.is_(False),
                    Series.tenant_id == normalized_workspace,
                )
            ).one_or_none()
            if row is None:
                raise ParseJobNotFoundError(
                    f"videoParseTaskId {video_parse_task_id!r} does not exist"
                )
            observed_at_ms = now_unix_time_ms()
            active_prefix_max = self._active_prefix_max_episode_by_series(
                session,
                (row[2],),
                observed_at_ms=observed_at_ms,
            )
            active_execution_task_ids = self._active_execution_task_ids(
                session,
                (row[0].id,),
                observed_at_ms=observed_at_ms,
            )
            deletion_fenced_video_ids = self._deletion_fenced_video_ids(
                session,
                (row[0].video_id,),
                observed_at_ms=observed_at_ms,
            )
            return self._video_parse_task_detail(
                row,
                active_prefix_max_episode=active_prefix_max.get(row[2]),
                has_active_execution_lease=row[0].id in active_execution_task_ids,
                has_active_deletion_lease=(
                    row[0].video_id in deletion_fenced_video_ids
                ),
                observed_at_ms=observed_at_ms,
            )

    def list_video_parse_tasks_for_workspace(
        self,
        tenant_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
        series_id: str | None = None,
        statuses: tuple[str, ...] | None = None,
    ) -> list[VideoParseTaskDetail]:
        normalized_workspace = require_nonempty_text(tenant_id, "tenant_id")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a nonnegative integer")
        with self._session_factory() as session:
            upload_session_id = self._video_parse_upload_session_id()
            is_latest = self._video_parse_task_is_latest()
            rows = session.execute(
                select(
                    VideoParseTask,
                    Series.tenant_id,
                    Series.series_id,
                    Series.name,
                    Video.episode_order,
                    Video.original_filename,
                    Video.duration_seconds,
                    upload_session_id,
                    is_latest,
                )
                .join(
                    Video,
                    Video.video_id == VideoParseTask.video_id,
                )
                .join(Series, Series.series_id == Video.series_id)
                .where(
                    *self._video_parse_task_workspace_filters(
                        normalized_workspace,
                        series_id=series_id,
                        statuses=statuses,
                    )
                )
                .order_by(
                    VideoParseTask.create_time.desc(),
                    VideoParseTask.id.desc(),
                )
                .limit(limit)
                .offset(offset)
            ).all()
            observed_at_ms = now_unix_time_ms()
            active_prefix_max = self._active_prefix_max_episode_by_series(
                session,
                tuple({row[2] for row in rows}),
                observed_at_ms=observed_at_ms,
            )
            active_execution_task_ids = self._active_execution_task_ids(
                session,
                tuple(row[0].id for row in rows),
                observed_at_ms=observed_at_ms,
            )
            deletion_fenced_video_ids = self._deletion_fenced_video_ids(
                session,
                tuple({row[0].video_id for row in rows}),
                observed_at_ms=observed_at_ms,
            )
            return [
                self._video_parse_task_detail(
                    row,
                    active_prefix_max_episode=active_prefix_max.get(row[2]),
                    has_active_execution_lease=(
                        row[0].id in active_execution_task_ids
                    ),
                    has_active_deletion_lease=(
                        row[0].video_id in deletion_fenced_video_ids
                    ),
                    observed_at_ms=observed_at_ms,
                )
                for row in rows
            ]

    def count_video_parse_tasks_for_workspace(
        self,
        tenant_id: str,
        *,
        series_id: str | None = None,
        statuses: tuple[str, ...] | None = None,
    ) -> int:
        normalized_workspace = require_nonempty_text(tenant_id, "tenant_id")
        with self._session_factory() as session:
            return int(
                session.scalar(
                    select(func.count(VideoParseTask.id))
                    .join(
                        Video,
                        Video.video_id == VideoParseTask.video_id,
                    )
                    .join(
                        Series,
                        Series.series_id == Video.series_id,
                    )
                    .where(
                        *self._video_parse_task_workspace_filters(
                            normalized_workspace,
                            series_id=series_id,
                            statuses=statuses,
                        )
                    )
                )
                or 0
            )

    def list_for_workspace(
        self,
        tenant_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
        series_id: str | None = None,
        statuses: tuple[str, ...] | None = None,
    ) -> list[ParseJobState]:
        normalized_workspace = require_nonempty_text(tenant_id, "tenant_id")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        with self._session_factory() as session:
            upload_session_id = (
                select(UploadSession.upload_session_id)
                .where(UploadSession.video_id == SeriesParseJob.video_id)
                .order_by(UploadSession.created_at)
                .limit(1)
                .correlate(SeriesParseJob)
                .scalar_subquery()
            )
            is_primary_upload_job = exists(
                select(UploadSession.upload_session_id).where(
                    UploadSession.job_id == SeriesParseJob.job_id,
                    UploadSession.status == UploadSessionStatus.COMPLETED.value,
                )
            ).correlate(SeriesParseJob)
            rows = session.execute(
                select(
                    SeriesParseJob,
                    upload_session_id,
                    is_primary_upload_job,
                    Series.tenant_id,
                    Series.name,
                    Video.episode_order,
                    Video.original_filename,
                    Video.duration_seconds,
                )
                .join(
                    Series,
                    Series.series_id == SeriesParseJob.series_id,
                )
                .join(
                    Video,
                    Video.video_id == SeriesParseJob.video_id,
                )
                .where(*self._workspace_filters(normalized_workspace, series_id=series_id, statuses=statuses))
                .order_by(
                    SeriesParseJob.created_at.desc(),
                    SeriesParseJob.job_id.desc(),
                )
                .limit(limit)
                .offset(offset)
            ).all()
            return [
                self._state(
                    record,
                    upload_session_id=linked_upload_session_id,
                    is_primary_upload_job=linked_as_primary_job,
                    tenant_id=tenant_id,
                    series_name=series_name,
                    episode_order=episode_order,
                    original_filename=original_filename,
                    duration_seconds=duration_seconds,
                )
                for (
                    record,
                    linked_upload_session_id,
                    linked_as_primary_job,
                    tenant_id,
                    series_name,
                    episode_order,
                    original_filename,
                    duration_seconds,
                ) in rows
            ]

    def count_for_workspace(
        self,
        tenant_id: str,
        *,
        series_id: str | None = None,
        statuses: tuple[str, ...] | None = None,
    ) -> int:
        with self._session_factory() as session:
            return int(session.scalar(
                select(func.count(SeriesParseJob.job_id))
                .join(Series)
                .where(*self._workspace_filters(tenant_id, series_id=series_id, statuses=statuses))
            ) or 0)

    def summary_for_workspace(self, tenant_id: str, *, series_id: str | None = None) -> ParseJobSummaryState:
        with self._session_factory() as session:
            rows = session.execute(
                select(
                    SeriesParseJob.status,
                    func.count(SeriesParseJob.job_id),
                    func.sum(SeriesParseJob.version),
                    func.max(SeriesParseJob.updated_at),
                )
                .join(Series)
                .where(*self._workspace_filters(tenant_id, series_id=series_id, statuses=None))
                .group_by(SeriesParseJob.status)
            ).all()
            checkpoint_count = session.scalar(
                select(func.count(VideoChunkCheckpoint.video_chunk_id))
                .join(SeriesParseJob)
                .join(Series)
                .where(*self._workspace_filters(tenant_id, series_id=series_id, statuses=None))
            )
            return ParseJobSummaryState(
                tenant_id=tenant_id, series_id=series_id,
                counts={status: int(count) for status, count, _version, _updated in rows},
                checkpoint_count=int(checkpoint_count or 0),
                version=sum(int(version) for _status, _count, version, _updated in rows),
                updated_at=max(
                    (updated for _status, _count, _version, updated in rows),
                    default=now_unix_time_ms(),
                ),
            )

    @staticmethod
    def _workspace_filters(tenant_id: str, *, series_id: str | None, statuses: tuple[str, ...] | None):
        filters = [Series.tenant_id == tenant_id]
        if series_id is not None:
            filters.append(SeriesParseJob.series_id == series_id)
        if statuses is not None:
            filters.append(SeriesParseJob.status.in_(statuses))
        return filters

    @staticmethod
    def _video_parse_task_workspace_filters(
        tenant_id: str,
        *,
        series_id: str | None,
        statuses: tuple[str, ...] | None,
    ) -> list:
        filters = [
            Series.tenant_id == tenant_id,
            VideoParseTask.is_deleted.is_(False),
        ]
        if series_id is not None:
            filters.append(Video.series_id == series_id)
        if statuses is not None:
            filters.append(VideoParseTask.status.in_(statuses))
        return filters

    @staticmethod
    def _video_parse_upload_session_id():
        return (
            select(UploadSession.upload_session_id)
            .where(
                UploadSession.video_id == VideoParseTask.video_id,
                UploadSession.status == UploadSessionStatus.COMPLETED.value,
            )
            .order_by(
                UploadSession.created_at,
                UploadSession.upload_session_id,
            )
            .limit(1)
            .correlate(VideoParseTask)
            .scalar_subquery()
        )

    @staticmethod
    def _video_parse_task_is_latest():
        newer_task = aliased(VideoParseTask)
        return ~exists(
            select(newer_task.id).where(
                newer_task.video_id == VideoParseTask.video_id,
                newer_task.is_deleted.is_(False),
                newer_task.version > VideoParseTask.version,
            )
        ).correlate(VideoParseTask)

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
                select(SeriesParseJob)
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
            video = session.get(Video, lease.video_id)
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
                VideoChunkCheckpoint,
                (checkpoint.job_id, checkpoint.sequence_no),
            )
            changed = current is None
            if current is None:
                current = VideoChunkCheckpoint(
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
                select(VideoChunkCheckpoint)
                .where(VideoChunkCheckpoint.job_id == checkpoint.job_id)
                .order_by(VideoChunkCheckpoint.sequence_no)
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
                video = session.get(Video, lease.video_id)
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
            if session.get(SeriesParseJob, job_id) is None:
                raise ParseJobNotFoundError(f"jobId {job_id!r} does not exist")
            records = session.scalars(
                select(VideoChunkCheckpoint)
                .where(VideoChunkCheckpoint.job_id == job_id)
                .order_by(VideoChunkCheckpoint.sequence_no)
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
                select(VideoChunkCheckpoint)
                .where(VideoChunkCheckpoint.job_id.in_(ordered_ids))
                .order_by(
                    VideoChunkCheckpoint.job_id,
                    VideoChunkCheckpoint.sequence_no,
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
    ) -> SeriesParseJob:
        job = session.scalar(
            select(SeriesParseJob)
            .where(*current_lease_predicate(lease, now))
            .with_for_update()
        )
        if job is None:
            raise ParseJobLeaseLostError("parse job lease is no longer current")
        return job

    @staticmethod
    def _state(
        record: SeriesParseJob,
        *,
        upload_session_id: str | None,
        is_primary_upload_job: bool,
        tenant_id: str,
        series_name: str,
        episode_order: int,
        original_filename: str,
        duration_seconds: int,
    ) -> ParseJobState:
        return ParseJobState(
            job_id=record.job_id,
        created_by_user_id=record.created_by_user_id,
            upload_session_id=upload_session_id,
            is_primary_upload_job=is_primary_upload_job,
            tenant_id=tenant_id,
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
    def _video_parse_task_state(
        record: VideoParseTask,
    ) -> VideoParseTaskState:
        return VideoParseTaskState(
            video_parse_task_id=record.id,
            video_id=record.video_id,
            pipeline_version=record.pipeline_version,
            status=record.status,
            parse_config=record.parse_config,
            chunk_count=record.chunk_count,
            version=record.version,
            next_execute_time=record.next_execute_time,
            attempt_count=record.attempt_count,
            max_attempts=record.max_attempts,
            lease_owner=record.lease_owner,
            lease_version=record.lease_version,
            lease_expire_time=record.lease_expire_time,
            heartbeat_time=record.heartbeat_time,
            start_time=record.start_time,
            finish_time=record.finish_time,
            processing_duration_ms=record.processing_duration_ms,
            processing_timing_complete=record.processing_timing_complete,
            last_error=record.last_error,
            is_test=record.is_test,
            detail=record.detail,
            create_time=record.create_time,
            update_time=record.update_time,
        )

    @staticmethod
    def _active_prefix_max_episode_by_series(
        session: Session,
        series_ids: tuple[str, ...],
        *,
        observed_at_ms: int,
    ) -> dict[str, int]:
        if not series_ids:
            return {}
        rows = session.execute(
            select(
                VideoParseTask,
                Video.series_id,
                Video.episode_order,
            )
            .join(
                Video,
                Video.video_id == VideoParseTask.video_id,
            )
            .where(
                Video.series_id.in_(series_ids),
                VideoParseTask.is_deleted.is_(False),
            )
            .order_by(
                VideoParseTask.video_id,
                VideoParseTask.version.desc(),
                VideoParseTask.id.desc(),
            )
        ).all()
        latest_by_video: dict[str, tuple[VideoParseTask, str, int]] = {}
        for task, series_id, episode_order in rows:
            latest_by_video.setdefault(
                task.video_id,
                (task, series_id, episode_order),
            )
        result: dict[str, int] = {}
        for task, series_id, episode_order in latest_by_video.values():
            if not _has_active_prefix_lease(task, now_ms=observed_at_ms):
                continue
            result[series_id] = max(result.get(series_id, 0), episode_order)
        return result

    @staticmethod
    def _active_execution_task_ids(
        session: Session,
        task_ids: tuple[str, ...],
        *,
        observed_at_ms: int,
    ) -> set[str]:
        if not task_ids:
            return set()
        observed_at = unix_time_ms_to_datetime(observed_at_ms)
        active_chunks = session.scalars(
            select(ChunkParseTask.video_parse_task_id).where(
                ChunkParseTask.video_parse_task_id.in_(task_ids),
                ChunkParseTask.lease_owner.is_not(None),
                ChunkParseTask.lease_expire_time > observed_at,
            )
        )
        active_intervals = session.scalars(
            select(IntervalParseTask.video_parse_task_id).where(
                IntervalParseTask.video_parse_task_id.in_(task_ids),
                IntervalParseTask.lease_owner.is_not(None),
                IntervalParseTask.lease_expire_time > observed_at,
            )
        )
        return {*active_chunks, *active_intervals}

    @classmethod
    def _deletion_fenced_video_ids(
        cls,
        session: Session,
        video_ids: tuple[str, ...],
        *,
        observed_at_ms: int,
    ) -> set[str]:
        if not video_ids:
            return set()
        result = set(session.scalars(
            select(SeriesParseJob.video_id).where(
                SeriesParseJob.video_id.in_(video_ids),
                SeriesParseJob.status == ParseJobStatus.RUNNING.value,
            )
        ))
        tasks = list(session.scalars(
            select(VideoParseTask).where(
                VideoParseTask.video_id.in_(video_ids),
            )
        ))
        observed_at = unix_time_ms_to_datetime(observed_at_ms)
        for task in tasks:
            if (
                task.lease_owner is not None
                and task.lease_expire_time is not None
                and task.lease_expire_time > observed_at
            ) or _has_active_prefix_lease(task, now_ms=observed_at_ms):
                result.add(task.video_id)
        video_id_by_task_id = {task.id: task.video_id for task in tasks}
        for task_id in cls._active_execution_task_ids(
            session,
            tuple(video_id_by_task_id),
            observed_at_ms=observed_at_ms,
        ):
            result.add(video_id_by_task_id[task_id])
        return result

    @classmethod
    def _video_parse_task_detail(
        cls,
        row,
        *,
        active_prefix_max_episode: int | None = None,
        has_active_execution_lease: bool = False,
        has_active_deletion_lease: bool = False,
        observed_at_ms: int | None = None,
    ) -> VideoParseTaskDetail:
        (
            record,
            tenant_id,
            series_id,
            series_name,
            episode_order,
            original_filename,
            duration_seconds,
            upload_session_id,
            is_latest,
        ) = row
        return VideoParseTaskDetail(
            task=cls._video_parse_task_state(record),
            tenant_id=tenant_id,
            series_id=series_id,
            series_name=series_name,
            episode_order=episode_order,
            original_filename=original_filename,
            duration_seconds=duration_seconds,
            upload_session_id=upload_session_id,
            is_latest=bool(is_latest),
            has_active_prefix_lease=_has_active_prefix_lease(
                record,
                now_ms=observed_at_ms,
            ),
            has_active_prefix_suffix_lease=(
                active_prefix_max_episode is not None
                and active_prefix_max_episode >= episode_order
            ),
            has_active_execution_lease=has_active_execution_lease,
            has_active_deletion_lease=has_active_deletion_lease,
        )

    @staticmethod
    def _checkpoint_state(
        record: VideoChunkCheckpoint,
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


def _has_active_prefix_lease(
    record: VideoParseTask,
    *,
    now_ms: int | None = None,
) -> bool:
    detail = record.detail
    runtime = detail.get("_worker_runtime_v2") if isinstance(detail, dict) else None
    prefix = runtime.get("prefix") if isinstance(runtime, dict) else None
    if not isinstance(prefix, dict) or prefix.get("lease_owner") is None:
        return False
    expires_at_ms = prefix.get("lease_expire_time_ms")
    return (
        isinstance(expires_at_ms, int)
        and not isinstance(expires_at_ms, bool)
        and expires_at_ms > (now_unix_time_ms() if now_ms is None else now_ms)
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
        raise ValueError("checkpoint canonical time range must use integer seconds")
    if start_seconds < 0 or end_seconds <= start_seconds:
        raise ValueError(
            "checkpoint chunk_end_seconds must be greater than nonnegative chunk_start_seconds"
        )
    if end_seconds > duration_seconds:
        raise ValueError("checkpoint canonical time range exceeds the Video duration")


def _validate_checkpoint_topology(
    records: list[VideoChunkCheckpoint],
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
