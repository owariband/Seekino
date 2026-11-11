"""Read parse rows and map database records to detached data objects."""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy

from sqlalchemy import Select, and_, exists, func, or_, select
from sqlalchemy.orm import Session, aliased, load_only

from ..model.dto.parseContentDTO import ChunkParseContentData
from ..model.dto.taskDTO import (
    ChunkParseTaskData,
    VideoParseTaskData,
)
from ..model.dto.libraryDTO import (
    SeriesData,
    VideoChunkData,
    VideoData,
)
from ..model.dto.searchDTO import (
    PublishedTimelineChunk,
    VideoParseRetrievalChunkData,
    VideoParseRetrievalTaskData,
)
from ..model.parse_content import ChunkParseContent
from ..model.task import ChunkParseTask, VideoParseTask
from ..model.video import Series, Video, VideoChunk
from . import video_parse_chunk as chunk_store
from . import video_parse_task as task_store
from .upload import video_deletion_pending


def series_names(
    session: Session, *, series_ids: Sequence[str], workspace: str,
) -> dict[str, str]:
    return dict(session.execute(select(Series.id, Series.name).where(
        Series.id.in_(series_ids), Series.tenant_id == workspace,
    )).all())


def list_series_videos(
    session: Session, *, series_ids: Sequence[str], episode_order: int | None = None,
) -> list[VideoData]:
    query = select(Video).where(
        Video.series_id.in_(series_ids), ~video_deletion_pending(),
    )
    if episode_order is not None:
        query = query.where(Video.episode_order == episode_order)
    return [task_store.video_data(row) for row in session.scalars(query.order_by(Video.episode_order, Video.id))]


def chunk_video_ids(session: Session, *, chunk_ids: set[str], video_ids: Sequence[str]) -> dict[str, str]:
    return dict(session.execute(select(VideoChunk.id, VideoChunk.video_id).where(
        VideoChunk.id.in_(chunk_ids), VideoChunk.video_id.in_(video_ids),
    )).all())


def list_video_tasks(session: Session, *, video_ids: Sequence[str]) -> list[VideoParseRetrievalTaskData]:
    rows = session.execute(select(
        VideoParseTask.id, VideoParseTask.video_id, VideoParseTask.status,
        VideoParseTask.parse_config, VideoParseTask.chunk_count,
    ).where(
        VideoParseTask.video_id.in_(video_ids), VideoParseTask.is_deleted.is_(False),
    ).order_by(VideoParseTask.video_id, VideoParseTask.version.desc(), VideoParseTask.id.desc()))
    return [VideoParseRetrievalTaskData(
        id=row.id, video_id=row.video_id, status=row.status,
        parse_config=deepcopy(row.parse_config), chunk_count=row.chunk_count,
    ) for row in rows]


def saved_source_status(session: Session, snapshot: dict) -> str:
    """Classify a saved reference after its exact published evidence no longer resolves."""
    generation = VideoParseTask.parse_config["indexing"]["index_generation"].as_integer()
    task = session.execute(select(VideoParseTask.id, VideoParseTask.status, generation.label("generation")).select_from(Series).join(
        Video, Video.series_id == Series.id,
    ).join(VideoParseTask, VideoParseTask.video_id == Video.id).where(
        Series.tenant_id == snapshot["tenant_id"], Series.id == snapshot["series_id"],
        Video.id == snapshot["video_id"], VideoParseTask.is_deleted.is_(False), ~video_deletion_pending(),
    ).order_by(VideoParseTask.version.desc()).limit(1)).first()
    if task is None:
        return "unavailable"
    if task.id != snapshot["parse_task_id"]:
        return "superseded"
    if task.status != "COMPLETE":
        return "unavailable"
    version = session.scalar(select(ChunkParseContent.version).join(
        ChunkParseTask, ChunkParseTask.id == ChunkParseContent.chunk_task_id,
    ).where(ChunkParseTask.video_task_id == task.id, ChunkParseTask.chunk_id == snapshot["video_chunk_id"]))
    if version is not None and version != snapshot["content_version"]:
        return "content_changed"
    if version is not None and task.generation and task.generation != snapshot["index_generation"]:
        return "superseded"
    return "unavailable"


def current_reference_tasks(session: Session, *, tenant_id: str, series_ids=None, video_id=None):
    """A newer task of any profile supersedes a previous reference."""
    newer = aliased(VideoParseTask)
    query = select(VideoParseTask.id, Video.id, Video.series_id).select_from(Series).join(
        Video, Video.series_id == Series.id,
    ).join(VideoParseTask, VideoParseTask.video_id == Video.id).where(
        Series.tenant_id == tenant_id, VideoParseTask.status == "COMPLETE", VideoParseTask.is_deleted.is_(False),
        VideoParseTask.parse_config["profile"].as_string() == "edit_reference",
        VideoParseTask.detail["_worker_runtime_v2"]["accepted"].as_boolean().is_(True),
        ~exists(select(newer.id).where(newer.video_id == Video.id, newer.is_deleted.is_(False),
                                       newer.version > VideoParseTask.version).correlate(Video, VideoParseTask)),
    )
    if series_ids is not None:
        query = query.where(Series.id.in_(series_ids))
    if video_id is not None:
        query = query.where(Video.id == video_id)
    return {task_id: (source, series) for task_id, source, series in session.execute(query)}


def list_tasks(session: Session, *, task_ids: Sequence[str]) -> list[VideoParseTaskData]:
    return [task_store.task_data(row) for row in session.scalars(select(VideoParseTask).where(
        VideoParseTask.id.in_(task_ids), VideoParseTask.is_deleted.is_(False),
    ))]


def list_indexed_chunk_rows(
    session: Session, *, task_ids: Sequence[str], phase: int, status: str,
) -> list[VideoParseRetrievalChunkData]:
    rows = session.execute(select(
        ChunkParseTask.video_task_id, VideoChunk.video_id, VideoChunk.sequence_no,
    ).select_from(ChunkParseTask).join(
        VideoChunk, VideoChunk.id == ChunkParseTask.chunk_id,
    ).join(ChunkParseContent, ChunkParseContent.chunk_task_id == ChunkParseTask.id).where(
        ChunkParseTask.video_task_id.in_(task_ids), ChunkParseTask.phase == phase,
        ChunkParseTask.status == status,
        ChunkParseTask.indexed_content_version == ChunkParseContent.version,
    ).order_by(ChunkParseTask.video_task_id, VideoChunk.sequence_no))
    return [VideoParseRetrievalChunkData(
        video_task_id=row.video_task_id, video_id=row.video_id, sequence_no=row.sequence_no,
    ) for row in rows]


def _published_timeline_query(query: Select, workspace: str, task_status: str, phase: int, status: str) -> Select:
    newer = aliased(VideoParseTask)
    return query.select_from(Series).join(Video, Video.series_id == Series.id).join(
        VideoParseTask, VideoParseTask.video_id == Video.id,
    ).join(ChunkParseTask, ChunkParseTask.video_task_id == VideoParseTask.id).join(
        VideoChunk, and_(VideoChunk.id == ChunkParseTask.chunk_id, VideoChunk.video_id == Video.id),
    ).join(ChunkParseContent, and_(ChunkParseContent.chunk_task_id == ChunkParseTask.id,
                                   ChunkParseContent.version == ChunkParseTask.indexed_content_version)).where(
        Series.tenant_id == workspace,
        func.coalesce(VideoParseTask.parse_config["profile"].as_string(), "content") == "content",
        VideoParseTask.status == task_status, VideoParseTask.is_deleted.is_(False),
        ChunkParseTask.phase == phase, ChunkParseTask.status == status,
        ~exists(select(newer.id).where(newer.video_id == Video.id, newer.is_deleted.is_(False),
                                       newer.version > VideoParseTask.version).correlate(Video, VideoParseTask)),
    )


def list_published_timeline_chunks(
    session: Session, *, workspace: str, series_id: str, task_status: str, phase: int, status: str,
) -> list[PublishedTimelineChunk]:
    """Read lightweight publication identities in viewing order, across encoder generations."""
    array_length = func.json_array_length if session.get_bind().dialect.name == "sqlite" else func.json_length
    columns = select(
        Video.id, VideoParseTask.id, VideoChunk.id, ChunkParseContent.version,
        VideoParseTask.parse_config["indexing"]["index_generation"].as_integer(),
        array_length(ChunkParseContent.content, "$.timeline"),
    )
    query = _published_timeline_query(columns, workspace, task_status, phase, status)
    rows = session.execute(query.where(Series.id == series_id).order_by(
        Video.episode_order, Video.id, VideoChunk.sequence_no, VideoChunk.id,
    ))
    return [PublishedTimelineChunk(*row) for row in rows]


def published_video_contents(
    session: Session, *, workspace: str, video_id: str, task_status: str, phase: int, status: str,
) -> list[dict] | None:
    """Read all current Chunk contents and their coverage in one snapshot."""
    columns = select(VideoParseTask.chunk_count, VideoChunk.sequence_no, ChunkParseContent.content)
    query = _published_timeline_query(columns, workspace, task_status, phase, status)
    rows = session.execute(query.where(
        Video.id == video_id, ~video_deletion_pending(),
    ).order_by(VideoChunk.sequence_no)).all()
    if not rows or len(rows) != rows[0].chunk_count or any(row.sequence_no != i for i, row in enumerate(rows)):
        return None
    return [row.content for row in rows]


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
    session: Session, *, task_ids: Sequence[str], chunk_ids: set[str] | None,
    task_status: str, phase: int, status: str,
) -> list[tuple[SeriesData, VideoData, VideoChunkData, VideoParseTaskData,
                ChunkParseTaskData, ChunkParseContentData]]:
    query = _candidate_query(
        task_ids=task_ids, task_status=task_status, phase=phase, status=status,
    )
    if chunk_ids is not None:
        query = query.where(VideoChunk.id.in_(chunk_ids))
    rows = session.execute(query)
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
