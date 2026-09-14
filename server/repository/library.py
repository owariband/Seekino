from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ..model.video import SeriesRecord, VideoRecord


class SeriesNotFoundError(LookupError):
    pass


class SeriesConflictError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class VideoSummaryState:
    video_id: str
    episode_order: int
    upload_sequence: int
    original_filename: str
    original_source_uri: str
    inference_proxy_uri: str | None
    content_hash: str
    total_bytes: int
    duration_ms: int
    upload_status: str
    parse_status: str
    upload_progress: int
    parse_progress: int


@dataclass(frozen=True, slots=True)
class SeriesState:
    series_id: str
    workspace_id: str
    name: str
    description: str | None
    status: str
    important_extra_data: dict | None
    character_bible: list[dict]
    video_count: int
    videos: tuple[VideoSummaryState, ...] = ()


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

    def list_series(self, workspace_id: str) -> list[SeriesState]:
        with self._session_factory() as session:
            rows = session.execute(
                select(SeriesRecord, func.count(VideoRecord.video_id))
                .outerjoin(VideoRecord, VideoRecord.series_id == SeriesRecord.series_id)
                .where(SeriesRecord.workspace_id == workspace_id)
                .group_by(SeriesRecord.series_id)
                .order_by(SeriesRecord.created_at, SeriesRecord.series_id)
            ).all()
            return [
                self._state(series, video_count=int(video_count))
                for series, video_count in rows
            ]

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
            videos = session.scalars(
                select(VideoRecord)
                .where(VideoRecord.series_id == series_id)
                .order_by(VideoRecord.episode_order)
            ).all()
            video_states = tuple(self._video_state(item) for item in videos)
            return self._state(
                series,
                video_count=len(video_states),
                videos=video_states,
            )

    def get_video(
        self,
        video_id: str,
        workspace_id: str,
    ) -> VideoSummaryState:
        with self._session_factory() as session:
            record = session.scalar(
                select(VideoRecord)
                .join(SeriesRecord, SeriesRecord.series_id == VideoRecord.series_id)
                .where(
                    VideoRecord.video_id == video_id,
                    SeriesRecord.workspace_id == workspace_id,
                )
            )
            if record is None:
                raise SeriesNotFoundError(
                    f"videoId {video_id!r} does not exist in this workspace"
                )
            return self._video_state(record)

    @staticmethod
    def _state(
        record: SeriesRecord,
        *,
        video_count: int,
        videos: tuple[VideoSummaryState, ...] = (),
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
            videos=videos,
        )

    @staticmethod
    def _video_state(record: VideoRecord) -> VideoSummaryState:
        return VideoSummaryState(
            video_id=record.video_id,
            episode_order=record.episode_order,
            upload_sequence=record.upload_sequence,
            original_filename=record.original_filename,
            original_source_uri=record.original_source_uri,
            inference_proxy_uri=record.inference_proxy_uri,
            content_hash=record.content_hash,
            total_bytes=record.total_bytes,
            duration_ms=record.duration_ms,
            upload_status=record.upload_status,
            parse_status=record.parse_status,
            upload_progress=record.upload_progress,
            parse_progress=record.parse_progress,
        )
