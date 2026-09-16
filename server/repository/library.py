from __future__ import annotations

from dataclasses import replace

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ..model.agent import AgentConversationRecord, AgentMessageRecord, AgentMessageRole
from ..model.dto.libraryDTO import SeriesLastSearchState, SeriesState, VideoSummaryState
from ..model.index_sync import IndexSyncIntentRecord, IndexSyncOperation, IndexSyncStatus
from ..model.job import SeriesParseJobRecord, VideoChunkCheckpointRecord
from ..model.job_progress import calculate_checkpoint_progress
from ..model.upload import UploadSessionRecord
from ..model.video import (
    SeriesRecord,
    VideoIndexStatus,
    VideoRecord,
    VideoChunkRecord,
    VideoChunkEmbeddingRecord,
)
from ..utils.time import utc_now_naive
from .upload import UploadRepository


class SeriesNotFoundError(LookupError):
    pass


class SeriesConflictError(RuntimeError):
    pass


class LibraryRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def create_series(
        self,
        *,
        series_id: str,
        workspace_id: str,
        name: str,
        description: str | None,
        important_extra_data: dict | None,
        character_bible: list[dict],
    ) -> SeriesState:
        for attempt in range(2):
            try:
                with self._session_factory.begin() as session:
                    existing = session.get(SeriesRecord, series_id)
                    if existing is None:
                        existing = session.scalar(
                            select(SeriesRecord).where(
                                SeriesRecord.workspace_id == workspace_id,
                                SeriesRecord.name == name,
                            )
                        )
                    if existing is not None:
                        if (
                            existing.series_id != series_id
                            or existing.workspace_id != workspace_id
                            or existing.name.casefold() != name.casefold()
                            or existing.description != description
                            or existing.important_extra_data != important_extra_data
                            or existing.character_bible != character_bible
                        ):
                            raise SeriesConflictError(
                                "the workspace already has this Series name with "
                                "different fields"
                            )
                        video_count = session.scalar(
                            select(func.count(VideoRecord.video_id)).where(
                                VideoRecord.series_id == existing.series_id
                            )
                        )
                        return self._state(
                            existing,
                            video_count=int(video_count or 0),
                        )

                    record = SeriesRecord(
                        series_id=series_id,
                        workspace_id=workspace_id,
                        name=name,
                        description=description,
                        status="EMPTY",
                        important_extra_data=important_extra_data,
                        character_bible=character_bible,
                    )
                    session.add(record)
                    session.flush()
                    return self._state(record, video_count=0)
            except IntegrityError:
                if attempt == 1:
                    raise
        raise AssertionError("unreachable")

    def update_series(self, series_id: str, *, workspace_id: str, changes: dict) -> None:
        with self._session_factory.begin() as session:
            record = session.scalar(
                select(SeriesRecord).where(
                    SeriesRecord.series_id == series_id,
                    SeriesRecord.workspace_id == workspace_id,
                ).with_for_update()
            )
            if record is None:
                raise SeriesNotFoundError("Series does not exist in this workspace")
            for field in ("description", "important_extra_data"):
                if field in changes:
                    setattr(record, field, changes[field])

    def list_series(
        self,
        workspace_id: str,
        *,
        status: str | None = None,
        query: str | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> list[SeriesState]:
        with self._session_factory() as session:
            records = session.scalars(
                select(SeriesRecord)
                .where(*self._series_filters(workspace_id, status=status, query=query))
                .order_by(SeriesRecord.created_at, SeriesRecord.series_id)
                .offset(offset)
                .limit(limit)
            ).all()
            return self._series_states(session, records)

    def count_series(
        self,
        workspace_id: str,
        *,
        status: str | None = None,
        query: str | None = None,
    ) -> int:
        with self._session_factory() as session:
            return int(session.scalar(
                select(func.count(SeriesRecord.series_id)).where(
                    *self._series_filters(workspace_id, status=status, query=query)
                )
            ) or 0)

    def get_series(self, series_id: str, workspace_id: str) -> SeriesState:
        with self._session_factory() as session:
            series = session.scalar(
                select(SeriesRecord).where(
                    SeriesRecord.series_id == series_id,
                    SeriesRecord.workspace_id == workspace_id,
                )
            )
            if series is None:
                raise SeriesNotFoundError(
                    f"seriesId {series_id!r} does not exist in this workspace"
                )
            state = self._series_states(session, [series], include_videos=True)[0]
        uploads = UploadRepository(self._session_factory).list_for_series(
            series_id, workspace_id=workspace_id,
        )
        return replace(
            state,
            pending_uploads=tuple(
                item for item in sorted(uploads, key=lambda upload: upload.upload_sequence)
                if item.status not in {"COMPLETED", "EXPIRED", "CANCELLED"}
            ),
        )

    def get_video(
        self,
        video_id: str,
        workspace_id: str,
    ) -> VideoSummaryState:
        with self._session_factory() as session:
            states = self._video_states(session, workspace_id=workspace_id, video_id=video_id)
            if not states:
                raise SeriesNotFoundError(
                    f"videoId {video_id!r} does not exist in this workspace"
                )
            return states[0]

    @staticmethod
    def _series_filters(workspace_id: str, *, status: str | None, query: str | None):
        filters = [SeriesRecord.workspace_id == workspace_id]
        if status:
            filters.append(SeriesRecord.status == status)
        if query:
            filters.append(SeriesRecord.name.icontains(query, autoescape=True))
        return filters

    def _series_states(
        self,
        session: Session,
        records: list[SeriesRecord],
        *,
        include_videos: bool = False,
    ) -> list[SeriesState]:
        if not records:
            return []
        series_ids = [record.series_id for record in records]
        videos = self._video_states(
            session, workspace_id=records[0].workspace_id, series_ids=series_ids,
        )
        pending = dict(session.execute(
            select(UploadSessionRecord.series_id, func.count(UploadSessionRecord.upload_session_id))
            .where(
                UploadSessionRecord.series_id.in_(series_ids),
                UploadSessionRecord.workspace_id == records[0].workspace_id,
                UploadSessionRecord.status.not_in({"COMPLETED", "EXPIRED", "CANCELLED"}),
                (UploadSessionRecord.status != "UPLOADING")
                | (UploadSessionRecord.expires_at > utc_now_naive()),
            )
            .group_by(UploadSessionRecord.series_id)
        ).all())
        last_searches: dict[str, SeriesLastSearchState] = {}
        messages = session.scalars(
            select(AgentMessageRecord)
            .join(AgentConversationRecord)
            .where(
                AgentConversationRecord.workspace_id == records[0].workspace_id,
                AgentMessageRecord.role == AgentMessageRole.USER.value,
            )
            .order_by(AgentMessageRecord.created_at.desc(), AgentMessageRecord.message_id.desc())
        )
        for message in messages:
            for series_id in set(message.series_ids).intersection(series_ids):
                last_searches.setdefault(series_id, SeriesLastSearchState(
                    conversation_id=message.conversation_id,
                    query=message.content or "",
                    searched_at=message.created_at,
                ))
            if len(last_searches) == len(series_ids):
                break
        return [
            self._state(
                record,
                video_count=sum(video.series_id == record.series_id for video in videos),
                videos=tuple(video for video in videos if video.series_id == record.series_id)
                if include_videos else (),
                ready_video_count=sum(
                    video.series_id == record.series_id
                    and video.parse_status in {"SUCCEEDED", "COMPLETED"}
                    and video.index_status == VideoIndexStatus.READY
                    for video in videos
                ),
                failed_video_count=sum(
                    video.series_id == record.series_id and video.parse_status == "FAILED"
                    for video in videos
                ),
                pending_upload_count=int(pending.get(record.series_id, 0)),
                last_search=last_searches.get(record.series_id),
                representative_video=next(
                    (video for video in videos if video.series_id == record.series_id and video.keyframe_uris),
                    None,
                ),
            )
            for record in records
        ]

    def _video_states(
        self,
        session: Session,
        *,
        workspace_id: str,
        series_ids: list[str] | None = None,
        video_id: str | None = None,
    ) -> list[VideoSummaryState]:
        latest = select(
            SeriesParseJobRecord.video_id,
            SeriesParseJobRecord.job_id,
            SeriesParseJobRecord.status,
            SeriesParseJobRecord.last_error,
            func.row_number().over(
                partition_by=SeriesParseJobRecord.video_id,
                order_by=(SeriesParseJobRecord.created_at.desc(), SeriesParseJobRecord.job_id.desc()),
            ).label("position"),
        ).subquery()
        statement = (
            select(VideoRecord, latest.c.job_id, latest.c.status, latest.c.last_error)
            .join(SeriesRecord, SeriesRecord.series_id == VideoRecord.series_id)
            .outerjoin(latest, (latest.c.video_id == VideoRecord.video_id) & (latest.c.position == 1))
            .where(SeriesRecord.workspace_id == workspace_id)
            .order_by(VideoRecord.episode_order, VideoRecord.upload_sequence, VideoRecord.video_id)
        )
        if series_ids is not None:
            statement = statement.where(VideoRecord.series_id.in_(series_ids))
        if video_id is not None:
            statement = statement.where(VideoRecord.video_id == video_id)
        rows = session.execute(statement).all()
        if not rows:
            return []
        latest_job_ids = [job_id for _, job_id, _, _ in rows if job_id is not None]
        checkpoints_by_job: dict[str, list[VideoChunkCheckpointRecord]] = {}
        if latest_job_ids:
            checkpoints = session.scalars(
                select(VideoChunkCheckpointRecord)
                .where(VideoChunkCheckpointRecord.job_id.in_(latest_job_ids))
                .order_by(
                    VideoChunkCheckpointRecord.job_id,
                    VideoChunkCheckpointRecord.sequence_no,
                )
            ).all()
            for checkpoint in checkpoints:
                checkpoints_by_job.setdefault(checkpoint.job_id, []).append(checkpoint)
        chunks = session.execute(
            select(
                VideoChunkRecord.video_id,
                VideoChunkRecord.keyframe_uris,
                VideoChunkEmbeddingRecord.text_vector,
                VideoChunkEmbeddingRecord.vision_vector,
                IndexSyncIntentRecord.operation,
                IndexSyncIntentRecord.status,
                IndexSyncIntentRecord.last_error,
            )
            .select_from(VideoChunkRecord)
            .outerjoin(VideoChunkEmbeddingRecord, VideoChunkEmbeddingRecord.video_chunk_id == VideoChunkRecord.video_chunk_id)
            .outerjoin(IndexSyncIntentRecord, IndexSyncIntentRecord.video_chunk_id == VideoChunkRecord.video_chunk_id)
            .where(VideoChunkRecord.video_id.in_([row[0].video_id for row in rows]))
            .order_by(VideoChunkRecord.sequence_no)
        ).all()
        by_video: dict[str, list] = {}
        for chunk in chunks:
            by_video.setdefault(chunk.video_id, []).append(chunk)
        states = []
        for video, job_id, job_status, error in rows:
            video_chunks = by_video.get(video.video_id, [])
            indexed = bool(video_chunks) and all(
                chunk.text_vector and chunk.vision_vector
                and chunk.operation == IndexSyncOperation.UPSERT.value
                and chunk.status == IndexSyncStatus.SYNCED.value
                for chunk in video_chunks
            )
            index_error = next((chunk.last_error for chunk in video_chunks if chunk.last_error), None)
            if indexed:
                index_status = VideoIndexStatus.READY
            elif index_error:
                index_status = VideoIndexStatus.FAILED
            elif video_chunks and all(chunk.text_vector and chunk.vision_vector for chunk in video_chunks):
                index_status = VideoIndexStatus.SYNCING
            else:
                index_status = VideoIndexStatus.NOT_READY
            parse_status = job_status or video.parse_status
            checkpoint_progress = calculate_checkpoint_progress(
                checkpoints_by_job.get(job_id, ()),
                video.duration_ms,
                completed=parse_status in {"SUCCEEDED", "COMPLETED"},
            )
            states.append(self._video_state(
                video, workspace_id=workspace_id, index_status=index_status,
                latest_job_id=job_id, parse_status=parse_status,
                parse_progress=(
                    checkpoint_progress.overall_progress
                    if checkpoint_progress is not None
                    else None
                ),
                last_error=error or index_error,
                keyframe_uris=tuple(dict.fromkeys(
                    uri for chunk in video_chunks for uri in chunk.keyframe_uris
                )),
            ))
        return states

    @staticmethod
    def _state(
        record: SeriesRecord,
        *,
        video_count: int,
        videos: tuple[VideoSummaryState, ...] = (),
        ready_video_count: int = 0,
        failed_video_count: int = 0,
        pending_upload_count: int = 0,
        last_search: SeriesLastSearchState | None = None,
        representative_video: VideoSummaryState | None = None,
    ) -> SeriesState:
        return SeriesState(
            series_id=record.series_id,
            workspace_id=record.workspace_id,
            name=record.name,
            description=record.description,
            status=record.status,
            important_extra_data=record.important_extra_data,
            character_bible=record.character_bible,
            video_count=video_count,
            ready_video_count=ready_video_count,
            failed_video_count=failed_video_count,
            pending_upload_count=pending_upload_count,
            last_search=last_search,
            created_at=record.created_at,
            updated_at=record.updated_at,
            representative_video=representative_video,
            videos=videos,
        )

    @staticmethod
    def _video_state(
        record: VideoRecord,
        *,
        workspace_id: str,
        index_status: VideoIndexStatus,
        latest_job_id: str | None,
        parse_status: str,
        parse_progress: int | None,
        last_error: str | None,
        keyframe_uris: tuple[str, ...],
    ) -> VideoSummaryState:
        return VideoSummaryState(
            video_id=record.video_id,
            workspace_id=workspace_id,
            series_id=record.series_id,
            episode_order=record.episode_order,
            upload_sequence=record.upload_sequence,
            original_filename=record.original_filename,
            original_source_uri=record.original_source_uri,
            inference_proxy_uri=record.inference_proxy_uri,
            content_hash=record.content_hash,
            total_bytes=record.total_bytes,
            duration_ms=record.duration_ms,
            upload_status=record.upload_status,
            parse_status=parse_status,
            upload_progress=record.upload_progress,
            parse_progress=parse_progress,
            index_status=index_status,
            latest_job_id=latest_job_id,
            last_error=last_error,
            keyframe_uris=keyframe_uris,
        )
