"""Read parse rows and map database records to detached data objects."""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import Session, aliased, load_only

from ..model.dto.jobDTO import (
    ChunkParseContentData,
    ChunkParseTaskData,
    SeriesData,
    VideoChunkData,
    VideoData,
    VideoParseTaskData,
)
from ..model.job import ChunkParseContent, ChunkParseTask, VideoParseTask
from ..model.video import Series, Video, VideoChunk
from . import video_parse_chunk as chunk_store
from . import video_parse_task as task_store


def list_series_ids(
    session: Session, *, series_ids: Sequence[str], workspace: str,
) -> tuple[str, ...]:
    return tuple(session.scalars(select(Series.id).where(
        Series.id.in_(series_ids), Series.tenant_id == workspace,
    )))


def list_series_videos(session: Session, *, series_ids: Sequence[str]) -> list[VideoData]:
    return [task_store.video_data(row) for row in session.scalars(select(Video).where(
        Video.series_id.in_(series_ids),
    ).order_by(Video.episode_order, Video.id))]


def list_video_tasks(session: Session, *, video_ids: Sequence[str]) -> list[VideoParseTaskData]:
    return [task_store.task_data(row) for row in session.scalars(select(VideoParseTask).where(
        VideoParseTask.video_id.in_(video_ids), VideoParseTask.is_deleted.is_(False),
    ).order_by(VideoParseTask.video_id, VideoParseTask.version.desc(), VideoParseTask.id.desc()))]


def list_tasks(session: Session, *, task_ids: Sequence[str]) -> list[VideoParseTaskData]:
    return [task_store.task_data(row) for row in session.scalars(select(VideoParseTask).where(
        VideoParseTask.id.in_(task_ids), VideoParseTask.is_deleted.is_(False),
    ))]


def list_indexed_chunk_rows(
    session: Session, *, task_ids: Sequence[str], phase: int, status: str,
) -> list[tuple[ChunkParseTaskData, VideoChunkData, ChunkParseContentData]]:
    rows = session.execute(select(ChunkParseTask, VideoChunk, ChunkParseContent).join(
        VideoChunk, VideoChunk.id == ChunkParseTask.chunk_id,
    ).join(ChunkParseContent, ChunkParseContent.chunk_task_id == ChunkParseTask.id).where(
        ChunkParseTask.video_task_id.in_(task_ids), ChunkParseTask.phase == phase,
        ChunkParseTask.status == status,
        ChunkParseTask.indexed_content_version == ChunkParseContent.version,
    ).order_by(ChunkParseTask.video_task_id, VideoChunk.sequence_no))
    return [(chunk_store.task_data(child), chunk_store.video_chunk_data(chunk),
             chunk_store.content_data(content)) for child, chunk, content in rows]


def load_task_chunk_rows(
    session: Session, *, task_id: str,
) -> list[tuple[ChunkParseTaskData, ChunkParseContentData | None, VideoChunkData | None]]:
    rows = session.execute(select(ChunkParseTask, ChunkParseContent, VideoChunk).outerjoin(
        ChunkParseContent, ChunkParseContent.chunk_task_id == ChunkParseTask.id,
    ).outerjoin(VideoChunk, VideoChunk.id == ChunkParseTask.chunk_id).where(
        ChunkParseTask.video_task_id == task_id,
    ).order_by(ChunkParseTask.chunk_id))
    return [(chunk_store.task_data(child),
             chunk_store.content_data(content) if content is not None else None,
             chunk_store.video_chunk_data(chunk) if chunk is not None else None)
            for child, content, chunk in rows]


def _candidate_query(*, task_ids: Sequence[str], task_status: str, phase: int, status: str):
    return select(Series, Video, VideoChunk, VideoParseTask, ChunkParseTask, ChunkParseContent).join(
        Video, Video.series_id == Series.id,
    ).join(VideoParseTask, VideoParseTask.video_id == Video.id).join(
        ChunkParseTask, ChunkParseTask.video_task_id == VideoParseTask.id,
    ).join(VideoChunk, VideoChunk.id == ChunkParseTask.chunk_id).join(
        ChunkParseContent, ChunkParseContent.chunk_task_id == ChunkParseTask.id,
    ).where(
        VideoParseTask.id.in_(task_ids), VideoParseTask.status == task_status,
        VideoParseTask.is_deleted.is_(False), ChunkParseTask.phase == phase,
        ChunkParseTask.status == status,
        ChunkParseTask.indexed_content_version == ChunkParseContent.version,
    )


def load_candidate_rows(
    session: Session, *, task_ids: Sequence[str], chunk_ids: set[str],
    task_status: str, phase: int, status: str,
) -> list[tuple[SeriesData, VideoData, VideoChunkData, VideoParseTaskData,
                ChunkParseTaskData, ChunkParseContentData]]:
    rows = session.execute(_candidate_query(
        task_ids=task_ids, task_status=task_status, phase=phase, status=status,
    ).where(VideoChunk.id.in_(chunk_ids)))
    return [(task_store.series_data(series), task_store.video_data(video),
             chunk_store.video_chunk_data(chunk), task_store.task_data(task),
             chunk_store.task_data(child), chunk_store.content_data(content))
            for series, video, chunk, task, child, content in rows]


def load_neighbor_rows(
    session: Session, *, task_ids: Sequence[str], neighbors: Sequence[tuple[str, tuple[int, int]]],
    task_status: str, phase: int, status: str,
) -> list[tuple[SeriesData, VideoData, VideoChunkData, VideoParseTaskData,
                ChunkParseTaskData, ChunkParseContentData]]:
    conditions = [and_(VideoParseTask.id == task_id, VideoChunk.sequence_no.in_(sequences))
                  for task_id, sequences in neighbors]
    rows = session.execute(_candidate_query(
        task_ids=task_ids, task_status=task_status, phase=phase, status=status,
    ).where(or_(*conditions)))
    return [(task_store.series_data(series), task_store.video_data(video),
             chunk_store.video_chunk_data(chunk), task_store.task_data(task),
             chunk_store.task_data(child), chunk_store.content_data(content))
            for series, video, chunk, task, child, content in rows]


def load_chunk_evidence_video(
    session: Session, *, chunk_id: str, workspace: str,
) -> tuple[SeriesData, VideoData] | None:
    row = session.execute(select(Series, Video).join(
        Video, Video.series_id == Series.id,
    ).join(VideoChunk, VideoChunk.video_id == Video.id).where(
        VideoChunk.id == chunk_id, Series.tenant_id == workspace,
    )).one_or_none()
    if row is None:
        return None
    series, video = row
    return task_store.series_data(series), task_store.video_data(video)


def load_indexed_chunk_content(
    session: Session, *, task_id: str, chunk_id: str, phase: int, status: str,
) -> ChunkParseContentData | None:
    row = session.scalar(select(ChunkParseContent).join(
        ChunkParseTask, ChunkParseTask.id == ChunkParseContent.chunk_task_id,
    ).where(
        ChunkParseTask.video_task_id == task_id, ChunkParseTask.chunk_id == chunk_id,
        ChunkParseTask.phase == phase, ChunkParseTask.status == status,
        ChunkParseTask.indexed_content_version == ChunkParseContent.version,
    ))
    return chunk_store.content_data(row) if row is not None else None


def load_event_source_rows(
    session: Session, *, task_ids: Sequence[str],
) -> list[tuple[ChunkParseTaskData, ChunkParseContentData, VideoChunkData]]:
    rows = session.execute(select(ChunkParseTask, ChunkParseContent, VideoChunk).join(
        ChunkParseContent, ChunkParseContent.chunk_task_id == ChunkParseTask.id,
    ).join(VideoChunk, VideoChunk.id == ChunkParseTask.chunk_id).where(
        ChunkParseTask.video_task_id.in_(task_ids),
    ))
    return [(chunk_store.task_data(child), chunk_store.content_data(content),
             chunk_store.video_chunk_data(chunk)) for child, content, chunk in rows]


def load_export_rows(
    session: Session, *, selected_ids: Sequence[str], by_video: bool, workspace: str,
) -> list[tuple[VideoParseTaskData, VideoData, ChunkParseTaskData | None,
                VideoChunkData | None, ChunkParseContentData | None]]:
    selection = VideoParseTask.id.in_(selected_ids)
    if by_video:
        newer_task = aliased(VideoParseTask)
        selection = and_(VideoParseTask.video_id.in_(selected_ids), ~exists(select(newer_task.id).where(
            newer_task.video_id == VideoParseTask.video_id,
            newer_task.is_deleted.is_(False), newer_task.version > VideoParseTask.version,
        )).correlate(VideoParseTask))
    # Keep one read snapshot and avoid loading large content detail during export.
    rows = session.execute(
        select(VideoParseTask, Video, ChunkParseTask, VideoChunk, ChunkParseContent)
        .options(load_only(ChunkParseContent.content, ChunkParseContent.version))
        .join(Video, Video.id == VideoParseTask.video_id)
        .join(Series, Series.id == Video.series_id)
        .outerjoin(ChunkParseTask, ChunkParseTask.video_task_id == VideoParseTask.id)
        .outerjoin(VideoChunk, VideoChunk.id == ChunkParseTask.chunk_id)
        .outerjoin(ChunkParseContent, ChunkParseContent.chunk_task_id == ChunkParseTask.id)
        .where(selection, VideoParseTask.is_deleted.is_(False), Series.tenant_id == workspace)
        .order_by(VideoParseTask.id, VideoChunk.sequence_no)
    )
    return [(task_store.task_data(task), task_store.video_data(video),
             chunk_store.task_data(child) if child is not None else None,
             chunk_store.video_chunk_data(chunk) if chunk is not None else None,
             ChunkParseContentData(id=content.id, chunk_task_id=child.id,
                                   content=deepcopy(content.content), version=content.version)
             if content is not None else None)
            for task, video, child, chunk, content in rows]
