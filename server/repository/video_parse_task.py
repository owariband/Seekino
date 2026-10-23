"""SQL access to parse parent tasks and their source records."""

from __future__ import annotations

from copy import deepcopy

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from ..model.dto.jobDTO import VideoParseTaskData, VideoData, SeriesData
from ..model.job import VideoParseTask
from ..model.video import Video, Series
from ..constants.parse import RUNTIME_DETAIL_KEY


def list_due_summary_task_ids(session: Session, now_ms: int, *, limit: int = 64) -> list[str]:
    """Bounded SQL scan of explicitly queued summaries, including expired claims."""
    summary = VideoParseTask.detail[RUNTIME_DETAIL_KEY]["summarize"]
    status = summary["status"].as_string()
    return list(session.scalars(select(VideoParseTask.id).where(
        VideoParseTask.is_deleted.is_(False),
        or_(
            and_(status.in_(["PENDING", "RETRY_WAIT"]),
                 summary["next_execute_time_ms"].as_integer() <= now_ms),
            and_(status == "RUNNING", summary["lease_expire_time_ms"].as_integer() <= now_ms),
        ),
    ).order_by(VideoParseTask.update_time, VideoParseTask.id).limit(limit)))


def task_data(row: VideoParseTask) -> VideoParseTaskData:
    return VideoParseTaskData(
        id=row.id,
        creator_id=row.creator_id,
        video_id=row.video_id,
        pipeline_version=row.pipeline_version,
        status=row.status,
        parse_config=deepcopy(row.parse_config),
        chunk_count=row.chunk_count,
        version=row.version,
        next_execute_time=row.next_execute_time,
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        lease_owner=row.lease_owner,
        lease_version=row.lease_version,
        lease_expire_time=row.lease_expire_time,
        heartbeat_time=row.heartbeat_time,
        start_time=row.start_time,
        finish_time=row.finish_time,
        processing_duration_ms=row.processing_duration_ms,
        processing_timing_complete=row.processing_timing_complete,
        last_error=deepcopy(row.last_error),
        is_test=row.is_test,
        is_deleted=row.is_deleted,
        detail=deepcopy(row.detail),
        create_time=row.create_time,
        create_date=row.create_date,
        update_time=row.update_time,
        update_date=row.update_date,
    )


def get_task(
    session: Session, record_id: str, *, for_update: bool = False,
    skip_locked: bool = False, include_deleted: bool = True,
) -> VideoParseTaskData | None:
    statement = select(VideoParseTask).where(VideoParseTask.id == record_id)
    if not include_deleted:
        statement = statement.where(VideoParseTask.is_deleted.is_(False))
    if for_update:
        statement = statement.with_for_update(skip_locked=skip_locked)
    row = session.scalar(statement)
    return task_data(row) if row is not None else None


def save_task(session: Session, value: VideoParseTaskData) -> None:
    row = session.get(VideoParseTask, value.id)
    if row is None:
        raise LookupError("video_parse_task row no longer exists")
    row.creator_id = value.creator_id
    row.video_id = value.video_id
    row.pipeline_version = value.pipeline_version
    row.status = value.status
    row.parse_config = deepcopy(value.parse_config)
    row.chunk_count = value.chunk_count
    row.version = value.version
    row.next_execute_time = value.next_execute_time
    row.attempt_count = value.attempt_count
    row.max_attempts = value.max_attempts
    row.lease_owner = value.lease_owner
    row.lease_version = value.lease_version
    row.lease_expire_time = value.lease_expire_time
    row.heartbeat_time = value.heartbeat_time
    row.start_time = value.start_time
    row.finish_time = value.finish_time
    row.processing_duration_ms = value.processing_duration_ms
    row.processing_timing_complete = value.processing_timing_complete
    row.last_error = deepcopy(value.last_error)
    row.is_test = value.is_test
    row.is_deleted = value.is_deleted
    row.detail = deepcopy(value.detail)
    row.create_time = value.create_time
    row.update_time = value.update_time


def insert_task(session: Session, value: VideoParseTaskData) -> None:
    session.add(VideoParseTask(
        id=value.id,
        creator_id=value.creator_id,
        video_id=value.video_id,
        pipeline_version=value.pipeline_version,
        status=value.status,
        parse_config=deepcopy(value.parse_config),
        chunk_count=value.chunk_count,
        version=value.version,
        next_execute_time=value.next_execute_time,
        attempt_count=value.attempt_count,
        max_attempts=value.max_attempts,
        lease_owner=value.lease_owner,
        lease_version=value.lease_version,
        lease_expire_time=value.lease_expire_time,
        heartbeat_time=value.heartbeat_time,
        start_time=value.start_time,
        finish_time=value.finish_time,
        processing_duration_ms=value.processing_duration_ms,
        processing_timing_complete=value.processing_timing_complete,
        last_error=deepcopy(value.last_error),
        is_test=value.is_test,
        is_deleted=value.is_deleted,
        detail=deepcopy(value.detail),
        create_time=value.create_time,
        update_time=value.update_time,
    ))


def video_data(row: Video) -> VideoData:
    return VideoData(
        id=row.id,
        series_id=row.series_id,
        episode_order=row.episode_order,
        upload_sequence=row.upload_sequence,
        filename=row.filename,
        source_uri=row.source_uri,
        proxy_uri=row.proxy_uri,
        hash=row.hash,
        size_bytes=row.size_bytes,
        duration_ms=row.duration_ms,
        upload_status=row.upload_status,
        parse_status=row.parse_status,
        upload_progress=row.upload_progress,
        parse_progress=row.parse_progress,
        create_time=row.create_time,
        create_date=row.create_date,
        update_time=row.update_time,
        update_date=row.update_date,
    )


def get_video(
    session: Session, record_id: str, *, for_update: bool = False,
    skip_locked: bool = False,
) -> VideoData | None:
    statement = select(Video).where(Video.id == record_id)
    if for_update:
        statement = statement.with_for_update(skip_locked=skip_locked)
    row = session.scalar(statement)
    return video_data(row) if row is not None else None


def update_video(
    session: Session,
    record_id: str,
    *,
    parse_status: str,
    parse_progress: int | None = None,
) -> None:
    row = session.get(Video, record_id)
    if row is None:
        raise LookupError("video row no longer exists")
    row.parse_status = parse_status
    if parse_progress is not None:
        row.parse_progress = parse_progress


def series_data(row: Series) -> SeriesData:
    return SeriesData(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        display_name=row.display_name,
        description=row.description,
        transcript_language_hint=row.transcript_language_hint,
        status=row.status,
        visibility=row.visibility,
        important_extra_data=deepcopy(row.important_extra_data),
        character_bible=deepcopy(row.character_bible),
        create_time=row.create_time,
        create_date=row.create_date,
        update_time=row.update_time,
        update_date=row.update_date,
    )


def get_series(
    session: Session, record_id: str, *, for_update: bool = False,
    skip_locked: bool = False,
) -> SeriesData | None:
    statement = select(Series).where(Series.id == record_id)
    if for_update:
        statement = statement.with_for_update(skip_locked=skip_locked)
    row = session.scalar(statement)
    return series_data(row) if row is not None else None


def latest_video_task_id(session: Session, video_id: str) -> str | None:
    return session.scalar(select(VideoParseTask.id).where(
        VideoParseTask.video_id == video_id, VideoParseTask.is_deleted.is_(False),
    ).order_by(VideoParseTask.version.desc(), VideoParseTask.id).limit(1))


def list_series_tasks(
    session: Session, series_id: str, *, from_episode: int | None = None,
    from_video_id: str | None = None, task_id_desc: bool = False,
) -> list[tuple[VideoParseTaskData, VideoData]]:
    statement = select(VideoParseTask, Video).join(
        Video, Video.id == VideoParseTask.video_id,
    ).where(
        Video.series_id == series_id, VideoParseTask.is_deleted.is_(False),
    )
    if from_episode is not None:
        statement = statement.where(or_(
            Video.episode_order > from_episode,
            and_(Video.episode_order == from_episode, Video.id >= from_video_id),
        ))
    statement = statement.order_by(
        Video.episode_order, Video.id, VideoParseTask.version.desc(),
        VideoParseTask.id.desc() if task_id_desc else VideoParseTask.id,
    )
    return [(task_data(task), video_data(source)) for task, source in session.execute(statement)]


def get_task_input_context(
    session: Session, task_id: str,
) -> tuple[VideoParseTaskData, VideoData, SeriesData] | None:
    row = session.execute(
        select(VideoParseTask, Video, Series)
        .join(Video, Video.id == VideoParseTask.video_id)
        .join(Series, Series.id == Video.series_id)
        .where(VideoParseTask.id == task_id, VideoParseTask.is_deleted.is_(False))
    ).one_or_none()
    return None if row is None else (task_data(row[0]), video_data(row[1]), series_data(row[2]))


def list_series_ids(session: Session) -> list[str]:
    return list(session.scalars(
        select(Video.series_id)
        .join(VideoParseTask, VideoParseTask.video_id == Video.id)
        .where(VideoParseTask.is_deleted.is_(False))
        .distinct().order_by(Video.series_id)
    ))
