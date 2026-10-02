from __future__ import annotations

from dataclasses import replace
from typing import Literal

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ..model.agent import AgentConversationRecord, AgentMessageRecord, AgentMessageRole
from ..model.dto.libraryDTO import SeriesLastSearchState, SeriesState, VideoSummaryState
from ..model.index_sync import IndexSyncIntentRecord, IndexSyncOperation, IndexSyncStatus
from ..model.job import (
    SeriesParseJobRecord,
    VideoChunkCheckpointRecord,
    VideoParseTaskRecord,
    VideoParseTaskStatus,
)
from ..model.job_progress import calculate_checkpoint_progress
from ..model.upload import UploadSessionRecord
from ..model.video import (
    SeriesRecord,
    VideoIndexStatus,
    VideoRecord,
    VideoChunkRecord,
    VideoChunkEmbeddingRecord,
)
from ..utils.media import stored_keyframe_uri
from ..utils.time import now_utc
from .index_sync import list_ready_video_ids
from .upload import UploadRepository
from .video_parse import (
    VideoParseConflictError,
    invalidate_series_prefixes_from,
)


class SeriesNotFoundError(LookupError):
    pass


class SeriesConflictError(RuntimeError):
    pass


class VideoEpisodeOrderConflictError(RuntimeError):
    pass


class LibraryRepository:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        execution_mode: Literal["v1", "v2"] = "v1",
    ) -> None:
        if execution_mode not in {"v1", "v2"}:
            raise ValueError("execution_mode must be 'v1' or 'v2'")
        self._session_factory = session_factory
        self._execution_mode = execution_mode

    def create_series(
        self,
        *,
        series_id: str,
        workspace_id: str,
        name: str,
        description: str | None,
        transcript_language_hint: str | None,
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
                            or existing.transcript_language_hint
                            != transcript_language_hint
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
                        transcript_language_hint=transcript_language_hint,
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
            for field in (
                "description", "transcript_language_hint", "important_extra_data",
            ):
                if field in changes:
                    setattr(record, field, changes[field])

    def update_video_episode_order(
        self,
        video_id: str,
        *,
        workspace_id: str,
        episode_order: int,
    ) -> None:
        if (
            isinstance(episode_order, bool)
            or not isinstance(episode_order, int)
            or not 0 < episode_order <= 2_147_483_647
        ):
            raise ValueError("episode_order must be a positive 32-bit integer")
        try:
            with self._session_factory.begin() as session:
                # Upload mutations lock Upload before Video elsewhere. Keep the
                # same order so an episode edit can safely race retry/delete.
                uploads = list(session.scalars(
                    select(UploadSessionRecord)
                    .where(UploadSessionRecord.video_id == video_id)
                    .order_by(UploadSessionRecord.upload_session_id)
                    .with_for_update()
                ))
                video = session.scalar(
                    select(VideoRecord)
                    .where(VideoRecord.video_id == video_id)
                    .with_for_update()
                )
                if video is None:
                    raise SeriesNotFoundError(
                        f"videoId {video_id!r} does not exist in this workspace"
                    )
                series_workspace = session.scalar(
                    select(SeriesRecord.workspace_id).where(
                        SeriesRecord.series_id == video.series_id,
                    )
                )
                if series_workspace != workspace_id:
                    raise SeriesNotFoundError(
                        f"videoId {video_id!r} does not exist in this workspace"
                    )

                occupied_video_id = session.scalar(
                    select(VideoRecord.video_id)
                    .where(
                        VideoRecord.series_id == video.series_id,
                        VideoRecord.episode_order == episode_order,
                        VideoRecord.video_id != video.video_id,
                    )
                    .with_for_update()
                )
                active_slot_key = f"{video.series_id}:{episode_order}"
                occupied_upload_id = session.scalar(
                    select(UploadSessionRecord.upload_session_id)
                    .where(
                        UploadSessionRecord.active_slot_key == active_slot_key,
                        or_(
                            UploadSessionRecord.video_id.is_(None),
                            UploadSessionRecord.video_id != video.video_id,
                        ),
                    )
                    .with_for_update()
                )
                if occupied_video_id is not None or occupied_upload_id is not None:
                    raise VideoEpisodeOrderConflictError(
                        "episode order is already occupied in this Series"
                    )

                previous_episode_order = video.episode_order
                video.episode_order = episode_order
                video.upload_sequence = episode_order
                for upload in uploads:
                    next_active_slot_key = (
                        active_slot_key
                        if upload.active_slot_key is not None
                        else None
                    )
                    if (
                        upload.episode_order == episode_order
                        and upload.active_slot_key == next_active_slot_key
                    ):
                        continue
                    upload.episode_order = episode_order
                    upload.upload_sequence = episode_order
                    upload.active_slot_key = next_active_slot_key
                    upload.version += 1
                session.flush()
                if previous_episode_order != episode_order:
                    try:
                        invalidate_series_prefixes_from(
                            session,
                            series_id=video.series_id,
                            episode_order=min(previous_episode_order, episode_order),
                            video_id=video.video_id,
                            now=now_utc(),
                        )
                    except VideoParseConflictError as exc:
                        raise VideoEpisodeOrderConflictError(str(exc)) from exc
        except IntegrityError as exc:
            raise VideoEpisodeOrderConflictError(
                "episode order is already occupied in this Series"
            ) from exc

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
            statement = (
                select(SeriesRecord)
                .where(*self._series_filters(
                    workspace_id,
                    status=status if self._execution_mode == "v1" else None,
                    query=query,
                ))
                .order_by(SeriesRecord.created_at, SeriesRecord.series_id)
            )
            if self._execution_mode == "v1":
                records = session.scalars(
                    statement.offset(offset).limit(limit)
                ).all()
                return self._series_states(session, records)
            states = self._series_states(session, session.scalars(statement).all())
            if status is not None:
                states = [item for item in states if item.status == status]
            return states[offset : None if limit is None else offset + limit]

    def count_series(
        self,
        workspace_id: str,
        *,
        status: str | None = None,
        query: str | None = None,
    ) -> int:
        if self._execution_mode == "v2":
            return len(self.list_series(
                workspace_id,
                status=status,
                query=query,
            ))
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
                item for item in sorted(uploads, key=lambda upload: upload.episode_order)
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
        ready_video_ids = (
            set(list_ready_video_ids(session, series_ids))
            if self._execution_mode == "v1"
            else {
                video.video_id
                for video in videos
                if video.index_status == VideoIndexStatus.READY
            }
        )
        pending = dict(session.execute(
            select(UploadSessionRecord.series_id, func.count(UploadSessionRecord.upload_session_id))
            .where(
                UploadSessionRecord.series_id.in_(series_ids),
                UploadSessionRecord.workspace_id == records[0].workspace_id,
                UploadSessionRecord.status.not_in({"COMPLETED", "EXPIRED", "CANCELLED"}),
                (UploadSessionRecord.status != "UPLOADING")
                | (UploadSessionRecord.expires_at > now_utc()),
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
                status=(
                    _v2_series_status(
                        [video for video in videos if video.series_id == record.series_id]
                    )
                    if self._execution_mode == "v2"
                    else record.status
                ),
                video_count=sum(video.series_id == record.series_id for video in videos),
                videos=tuple(video for video in videos if video.series_id == record.series_id)
                if include_videos else (),
                ready_video_count=sum(
                    video.series_id == record.series_id
                    and video.video_id in ready_video_ids
                    for video in videos
                ),
                failed_video_count=sum(
                    video.series_id == record.series_id
                    and (
                        video.parse_status == "FAILED"
                        or video.parse_status.endswith("_FAILED")
                    )
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
        if self._execution_mode == "v2":
            return self._video_states_v2(
                session,
                workspace_id=workspace_id,
                series_ids=series_ids,
                video_id=video_id,
            )
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
            .order_by(VideoRecord.episode_order, VideoRecord.video_id)
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
                video.duration_seconds,
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
                    uri
                    for chunk in video_chunks
                    for value in chunk.keyframe_uris
                    if (uri := stored_keyframe_uri(value)) is not None
                )),
            ))
        return states

    def _video_states_v2(
        self,
        session: Session,
        *,
        workspace_id: str,
        series_ids: list[str] | None,
        video_id: str | None,
    ) -> list[VideoSummaryState]:
        latest = (
            select(
                VideoParseTaskRecord.video_id,
                VideoParseTaskRecord.id.label("task_id"),
                VideoParseTaskRecord.status,
                VideoParseTaskRecord.last_error,
                func.row_number().over(
                    partition_by=VideoParseTaskRecord.video_id,
                    order_by=(
                        VideoParseTaskRecord.version.desc(),
                        VideoParseTaskRecord.id.desc(),
                    ),
                ).label("position"),
            )
            .where(VideoParseTaskRecord.is_deleted.is_(False))
            .subquery()
        )
        statement = (
            select(
                VideoRecord,
                latest.c.task_id,
                latest.c.status,
                latest.c.last_error,
            )
            .join(SeriesRecord, SeriesRecord.series_id == VideoRecord.series_id)
            .outerjoin(
                latest,
                (latest.c.video_id == VideoRecord.video_id)
                & (latest.c.position == 1),
            )
            .where(SeriesRecord.workspace_id == workspace_id)
            .order_by(VideoRecord.episode_order, VideoRecord.video_id)
        )
        if series_ids is not None:
            statement = statement.where(VideoRecord.series_id.in_(series_ids))
        if video_id is not None:
            statement = statement.where(VideoRecord.video_id == video_id)
        rows = session.execute(statement).all()
        if not rows:
            return []

        keyframes_by_video: dict[str, list[str]] = {}
        chunks = session.execute(
            select(VideoChunkRecord.video_id, VideoChunkRecord.keyframe_uris)
            .where(VideoChunkRecord.video_id.in_([row[0].video_id for row in rows]))
            .order_by(VideoChunkRecord.video_id, VideoChunkRecord.sequence_no)
        )
        for chunk in chunks:
            values = keyframes_by_video.setdefault(chunk.video_id, [])
            values.extend(
                uri
                for value in chunk.keyframe_uris
                if (uri := stored_keyframe_uri(value)) is not None
            )

        states: list[VideoSummaryState] = []
        for video, _task_id, task_status, last_error in rows:
            if task_status == VideoParseTaskStatus.COMPLETE.value:
                index_status = VideoIndexStatus.READY
            elif task_status == VideoParseTaskStatus.INDEXING.value:
                index_status = VideoIndexStatus.SYNCING
            elif task_status == VideoParseTaskStatus.INDEXING_FAILED.value:
                index_status = VideoIndexStatus.FAILED
            else:
                index_status = VideoIndexStatus.NOT_READY
            parse_status = task_status or video.parse_status
            states.append(self._video_state(
                video,
                workspace_id=workspace_id,
                index_status=index_status,
                latest_job_id=None,
                parse_status=parse_status,
                parse_progress=(
                    100
                    if task_status == VideoParseTaskStatus.COMPLETE.value
                    else video.parse_progress
                ),
                last_error=_v2_error_message(last_error),
                keyframe_uris=tuple(dict.fromkeys(
                    keyframes_by_video.get(video.video_id, ())
                )),
            ))
        return states

    @staticmethod
    def _state(
        record: SeriesRecord,
        *,
        status: str | None = None,
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
            transcript_language_hint=record.transcript_language_hint,
            status=record.status if status is None else status,
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
            duration_seconds=record.duration_seconds,
            upload_status=record.upload_status,
            parse_status=parse_status,
            upload_progress=record.upload_progress,
            parse_progress=parse_progress,
            index_status=index_status,
            latest_job_id=latest_job_id,
            last_error=last_error,
            keyframe_uris=keyframe_uris,
        )


def _v2_series_status(videos: list[VideoSummaryState]) -> str:
    if not videos:
        return "EMPTY"
    if any(video.index_status == VideoIndexStatus.READY for video in videos):
        return "READY"
    if all(video.parse_status.endswith("_FAILED") for video in videos):
        return "FAILED"
    return "INDEXING"


def _v2_error_message(value: object) -> str | None:
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        message = value.get("message")
        return message if isinstance(message, str) and message else None
    return None
