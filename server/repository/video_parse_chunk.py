"""SQL access and DTO mapping for Chunk task, content and source rows.

Transaction ownership and workflow decisions belong to the calling service.
"""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..model.dto.jobDTO import ChunkParseTaskData, ChunkParseContentData, VideoChunkData
from ..model.job import ChunkParseTask, ChunkParseContent
from ..model.video import VideoChunk


def task_data(row: ChunkParseTask) -> ChunkParseTaskData:
    return ChunkParseTaskData(
        id=row.id,
        video_task_id=row.video_task_id,
        chunk_id=row.chunk_id,
        phase=row.phase,
        indexed_content_version=row.indexed_content_version,
        status=row.status,
        next_execute_time=row.next_execute_time,
        retry_count=row.retry_count,
        lease_owner=row.lease_owner,
        lease_version=row.lease_version,
        lease_expire_time=row.lease_expire_time,
        start_time=row.start_time,
        finish_time=row.finish_time,
        last_error=deepcopy(row.last_error),
        detail=deepcopy(row.detail),
        create_time=row.create_time,
        create_date=row.create_date,
        update_time=row.update_time,
        update_date=row.update_date,
    )


def content_data(row: ChunkParseContent) -> ChunkParseContentData:
    return ChunkParseContentData(
        id=row.id,
        chunk_task_id=row.chunk_task_id,
        content=deepcopy(row.content),
        version=row.version,
        detail=deepcopy(row.detail),
        create_time=row.create_time,
        create_date=row.create_date,
        update_time=row.update_time,
        update_date=row.update_date,
    )


def video_chunk_data(row: VideoChunk) -> VideoChunkData:
    return VideoChunkData(
        id=row.id,
        video_id=row.video_id,
        sequence_no=row.sequence_no,
        start_ms=row.start_ms,
        end_ms=row.end_ms,
        window_start_ms=row.window_start_ms,
        window_end_ms=row.window_end_ms,
        chunk_media_ref=row.chunk_media_ref,
        analysis_window_media_ref=row.analysis_window_media_ref,
        transcript=deepcopy(row.transcript),
        baseline_visual_caption=row.baseline_visual_caption,
        keyframe_uris=deepcopy(row.keyframe_uris),
        create_time=row.create_time,
        create_date=row.create_date,
        update_time=row.update_time,
        update_date=row.update_date,
    )


def get_task(
    session: Session, task_id: str, *, video_task_id: str | None = None,
    for_update: bool = False,
) -> ChunkParseTaskData | None:
    statement = select(ChunkParseTask).where(ChunkParseTask.id == task_id)
    if video_task_id is not None:
        statement = statement.where(ChunkParseTask.video_task_id == video_task_id)
    if for_update:
        statement = statement.with_for_update()
    row = session.scalar(statement)
    return task_data(row) if row is not None else None


def get_content(
    session: Session, chunk_task_id: str, *, for_update: bool = False,
) -> ChunkParseContentData | None:
    statement = select(ChunkParseContent).where(ChunkParseContent.chunk_task_id == chunk_task_id)
    if for_update:
        statement = statement.with_for_update()
    row = session.scalar(statement)
    return content_data(row) if row is not None else None


def get_video_chunk(
    session: Session, chunk_id: str, *, for_update: bool = False,
) -> VideoChunkData | None:
    statement = select(VideoChunk).where(VideoChunk.id == chunk_id)
    if for_update:
        statement = statement.with_for_update()
    row = session.scalar(statement)
    return video_chunk_data(row) if row is not None else None


def list_tasks(
    session: Session, video_task_id: str, *, for_update: bool = False,
) -> list[ChunkParseTaskData]:
    statement = select(ChunkParseTask).where(
        ChunkParseTask.video_task_id == video_task_id,
    ).order_by(ChunkParseTask.id)
    if for_update:
        statement = statement.with_for_update()
    return [task_data(row) for row in session.scalars(statement)]


def list_contents(session: Session, chunk_task_ids: Sequence[str]) -> list[ChunkParseContentData]:
    return [content_data(row) for row in session.scalars(select(ChunkParseContent).where(
        ChunkParseContent.chunk_task_id.in_(chunk_task_ids),
    ))]


def list_video_chunks(
    session: Session, *, video_id: str | None = None,
    chunk_ids: Sequence[str] | None = None, for_update: bool = False,
) -> list[VideoChunkData]:
    statement = select(VideoChunk).order_by(VideoChunk.sequence_no)
    if video_id is not None:
        statement = statement.where(VideoChunk.video_id == video_id)
    if chunk_ids is not None:
        statement = statement.where(VideoChunk.id.in_(chunk_ids))
    if for_update:
        statement = statement.with_for_update()
    return [video_chunk_data(row) for row in session.scalars(statement)]


def list_task_chunk_content_rows(
    session: Session, video_task_id: str | None = None, *,
    video_id: str | None = None, chunk_ids: Sequence[str] | None = None,
    chunk_task_ids: Sequence[str] | None = None, sequence_no: int | None = None,
    include_missing_content: bool = False, content_not_null: bool = False,
    for_update: bool = False, order_by: str = "sequence",
) -> list[tuple[ChunkParseTaskData, VideoChunkData, ChunkParseContentData | None]]:
    statement = select(ChunkParseTask, VideoChunk, ChunkParseContent).join(
        VideoChunk, VideoChunk.id == ChunkParseTask.chunk_id,
    )
    if include_missing_content:
        statement = statement.outerjoin(ChunkParseContent, ChunkParseContent.chunk_task_id == ChunkParseTask.id)
    else:
        statement = statement.join(ChunkParseContent, ChunkParseContent.chunk_task_id == ChunkParseTask.id)
    if video_task_id is not None:
        statement = statement.where(ChunkParseTask.video_task_id == video_task_id)
    if video_id is not None:
        statement = statement.where(VideoChunk.video_id == video_id)
    if chunk_ids is not None:
        statement = statement.where(VideoChunk.id.in_(chunk_ids))
    if chunk_task_ids is not None:
        statement = statement.where(ChunkParseTask.id.in_(chunk_task_ids))
    if sequence_no is not None:
        statement = statement.where(VideoChunk.sequence_no == sequence_no)
    if content_not_null:
        statement = statement.where(ChunkParseContent.content.is_not(None))
    if order_by == "sequence":
        statement = statement.order_by(VideoChunk.sequence_no)
    elif order_by == "chunk_id":
        statement = statement.order_by(VideoChunk.id)
    else:
        raise ValueError("unsupported Chunk row ordering")
    if for_update:
        statement = statement.with_for_update()
    return [(task_data(task), video_chunk_data(chunk), content_data(content) if content is not None else None)
            for task, chunk, content in session.execute(statement)]


def list_task_content_rows(
    session: Session, video_task_id: str, *, task_ids: Sequence[str],
    for_update: bool = False,
) -> list[tuple[ChunkParseTaskData, ChunkParseContentData]]:
    statement = select(ChunkParseTask, ChunkParseContent).join(
        ChunkParseContent, ChunkParseContent.chunk_task_id == ChunkParseTask.id,
    ).where(
        ChunkParseTask.video_task_id == video_task_id,
        ChunkParseTask.id.in_(task_ids),
    ).order_by(ChunkParseTask.id)
    if for_update:
        statement = statement.with_for_update()
    return [(task_data(task), content_data(content)) for task, content in session.execute(statement)]


def list_owned_chunk_ids(
    session: Session, video_task_id: str, video_id: str, chunk_ids: Sequence[str],
) -> set[str]:
    return set(session.scalars(select(VideoChunk.id).join(
        ChunkParseTask, ChunkParseTask.chunk_id == VideoChunk.id,
    ).where(
        ChunkParseTask.video_task_id == video_task_id,
        VideoChunk.video_id == video_id, VideoChunk.id.in_(chunk_ids),
    )))


def has_leased_task(session: Session, video_task_id: str) -> bool:
    return session.scalar(select(ChunkParseTask.id).where(
        ChunkParseTask.video_task_id == video_task_id,
        ChunkParseTask.lease_owner.is_not(None),
    ).limit(1)) is not None


def insert_video_chunk(session: Session, data: VideoChunkData) -> None:
    session.add(VideoChunk(
        id=data.id,
        video_id=data.video_id,
        sequence_no=data.sequence_no,
        start_ms=data.start_ms,
        end_ms=data.end_ms,
        window_start_ms=data.window_start_ms,
        window_end_ms=data.window_end_ms,
        chunk_media_ref=data.chunk_media_ref,
        analysis_window_media_ref=data.analysis_window_media_ref,
        transcript=deepcopy(data.transcript),
        baseline_visual_caption=data.baseline_visual_caption,
        keyframe_uris=deepcopy(data.keyframe_uris),
        create_time=data.create_time,
        update_time=data.update_time,
    ))


def insert_task(session: Session, data: ChunkParseTaskData) -> None:
    session.add(ChunkParseTask(
        id=data.id,
        video_task_id=data.video_task_id,
        chunk_id=data.chunk_id,
        phase=data.phase,
        indexed_content_version=data.indexed_content_version,
        status=data.status,
        next_execute_time=data.next_execute_time,
        retry_count=data.retry_count,
        lease_owner=data.lease_owner,
        lease_version=data.lease_version,
        lease_expire_time=data.lease_expire_time,
        start_time=data.start_time,
        finish_time=data.finish_time,
        last_error=deepcopy(data.last_error),
        detail=deepcopy(data.detail),
        create_time=data.create_time,
        update_time=data.update_time,
    ))


def save_task(session: Session, data: ChunkParseTaskData) -> None:
    row = session.get(ChunkParseTask, data.id)
    if row is None:
        raise LookupError("ChunkParseTask row no longer exists")
    row.video_task_id = data.video_task_id
    row.chunk_id = data.chunk_id
    row.phase = data.phase
    row.indexed_content_version = data.indexed_content_version
    row.status = data.status
    row.next_execute_time = data.next_execute_time
    row.retry_count = data.retry_count
    row.lease_owner = data.lease_owner
    row.lease_version = data.lease_version
    row.lease_expire_time = data.lease_expire_time
    row.start_time = data.start_time
    row.finish_time = data.finish_time
    row.last_error = deepcopy(data.last_error)
    row.detail = deepcopy(data.detail)
    row.update_time = data.update_time


def insert_content(session: Session, data: ChunkParseContentData) -> None:
    session.add(ChunkParseContent(
        id=data.id,
        chunk_task_id=data.chunk_task_id,
        content=deepcopy(data.content),
        version=data.version,
        detail=deepcopy(data.detail),
        create_time=data.create_time,
        update_time=data.update_time,
    ))


def save_content(session: Session, data: ChunkParseContentData) -> None:
    row = session.get(ChunkParseContent, data.id)
    if row is None:
        raise LookupError("ChunkParseContent row no longer exists")
    row.chunk_task_id = data.chunk_task_id
    row.content = deepcopy(data.content)
    row.version = data.version
    row.detail = deepcopy(data.detail)
    row.update_time = data.update_time
