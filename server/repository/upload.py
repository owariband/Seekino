from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ..identity import derive_parse_job_id
from ..model.job import ParseJobStage, ParseJobStatus, SeriesParseJobRecord
from ..model.upload import (
    UploadSegmentRecord,
    UploadSessionRecord,
    UploadSessionStatus,
)
from ..model.video import SeriesRecord, VideoRecord


class UploadSessionNotFoundError(LookupError):
    pass


class UploadSeriesNotFoundError(LookupError):
    pass


class UploadConflictError(RuntimeError):
    pass


class UploadStateError(RuntimeError):
    pass


class UploadCompletionInProgressError(UploadStateError):
    pass


class UploadCompletionLeaseLostError(UploadStateError):
    pass


@dataclass(frozen=True, slots=True)
class UploadSegmentState:
    segment_number: int
    start_byte: int
    end_byte_exclusive: int
    actual_size_bytes: int
    checksum_sha256: str
    artifact_uri: str


@dataclass(frozen=True, slots=True)
class UploadSessionState:
    upload_session_id: str
    workspace_id: str
    series_id: str
    episode_order: int
    upload_sequence: int
    original_filename: str
    media_type: str
    total_size_bytes: int
    preferred_segment_size_bytes: int
    total_segments: int
    status: str
    uploaded_segment_count: int
    uploaded_size_bytes: int
    expires_at: datetime
    video_id: str | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class UploadCompletionClaim:
    session: UploadSessionState
    segments: tuple[UploadSegmentState, ...]
    completion_token: str | None
    existing_job_id: str | None

    @property
    def already_completed(self) -> bool:
        return self.session.status == UploadSessionStatus.COMPLETED.value


@dataclass(frozen=True, slots=True)
class CompletedUploadState:
    session: UploadSessionState
    video_id: str
    job_id: str


class UploadRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def create_session(
        self,
        *,
        workspace_id: str,
        series_id: str,
        episode_order: int,
        upload_sequence: int,
        original_filename: str,
        media_type: str,
        total_size_bytes: int,
        preferred_segment_size_bytes: int,
        total_segments: int,
        idempotency_key: str,
        expires_at: datetime,
        now: datetime,
    ) -> UploadSessionState:
        requested = {
            "workspace_id": workspace_id,
            "series_id": series_id,
            "episode_order": episode_order,
            "upload_sequence": upload_sequence,
            "original_filename": original_filename,
            "media_type": media_type,
            "total_size_bytes": total_size_bytes,
            "preferred_segment_size_bytes": preferred_segment_size_bytes,
            "total_segments": total_segments,
            "idempotency_key": idempotency_key,
        }
        for attempt in range(2):
            try:
                with self._session_factory.begin() as session:
                    existing = session.scalar(
                        select(UploadSessionRecord).where(
                            UploadSessionRecord.workspace_id == workspace_id,
                            UploadSessionRecord.idempotency_key == idempotency_key,
                        )
                    )
                    if existing is not None:
                        self._expire_if_due(existing, now)
                        self._validate_same_session(existing, requested)
                        return self._snapshot(session, existing)

                    series = session.get(SeriesRecord, series_id)
                    if series is None or series.workspace_id != workspace_id:
                        raise UploadSeriesNotFoundError(
                            f"seriesId {series_id!r} does not exist in this workspace"
                        )
                    episode_session = session.scalar(
                        select(UploadSessionRecord).where(
                            UploadSessionRecord.active_slot_key
                            == _active_slot_key(series_id, episode_order),
                        )
                    )
                    if (
                        episode_session is not None
                        and self._expire_if_due(episode_session, now)
                    ):
                        session.flush()
                        episode_session = None
                    if episode_session is not None:
                        raise UploadConflictError(
                            "the Series episode already has another upload session"
                        )
                    existing_video = session.scalar(
                        select(VideoRecord.video_id).where(
                            VideoRecord.series_id == series_id,
                            VideoRecord.episode_order == episode_order,
                        )
                    )
                    if existing_video is not None:
                        raise UploadConflictError(
                            "the Series episode already has a Video"
                        )

                    record = UploadSessionRecord(
                        upload_session_id=str(uuid4()),
                        workspace_id=workspace_id,
                        series_id=series_id,
                        episode_order=episode_order,
                        upload_sequence=upload_sequence,
                        original_filename=original_filename,
                        media_type=media_type,
                        total_size_bytes=total_size_bytes,
                        preferred_segment_size_bytes=preferred_segment_size_bytes,
                        total_segments=total_segments,
                        status=UploadSessionStatus.UPLOADING.value,
                        idempotency_key=idempotency_key,
                        active_slot_key=_active_slot_key(series_id, episode_order),
                        expires_at=expires_at,
                    )
                    session.add(record)
                    session.flush()
                    return self._snapshot(session, record)
            except IntegrityError:
                if attempt == 1:
                    raise
        raise AssertionError("unreachable")

    def get_session(
        self,
        upload_session_id: str,
        *,
        now: datetime | None = None,
    ) -> UploadSessionState:
        checked_at = now or _utc_now()
        with self._session_factory.begin() as session:
            record = session.scalar(
                select(UploadSessionRecord)
                .where(
                    UploadSessionRecord.upload_session_id == upload_session_id
                )
                .with_for_update()
            )
            if record is None:
                raise UploadSessionNotFoundError(
                    f"uploadSessionId {upload_session_id!r} does not exist"
                )
            self._expire_if_due(record, checked_at)
            return self._snapshot(session, record)

    def list_segments(self, upload_session_id: str) -> list[UploadSegmentState]:
        with self._session_factory() as session:
            if session.get(UploadSessionRecord, upload_session_id) is None:
                raise UploadSessionNotFoundError(
                    f"uploadSessionId {upload_session_id!r} does not exist"
                )
            records = session.scalars(
                select(UploadSegmentRecord)
                .where(
                    UploadSegmentRecord.upload_session_id == upload_session_id
                )
                .order_by(UploadSegmentRecord.segment_number)
            ).all()
            return [self._segment_snapshot(item) for item in records]

    def register_segment(
        self,
        upload_session_id: str,
        *,
        segment_number: int,
        start_byte: int,
        end_byte_exclusive: int,
        actual_size_bytes: int,
        checksum_sha256: str,
        artifact_uri: str,
        now: datetime | None = None,
    ) -> UploadSegmentState:
        registered_at = now or _utc_now()
        expired = False
        result: UploadSegmentState | None = None
        with self._session_factory.begin() as session:
            upload = self._locked_session(session, upload_session_id)
            if self._expire_if_due(upload, registered_at):
                expired = True
            else:
                self._require_uploading(upload)
                expected_start, expected_end = self._expected_range(
                    upload,
                    segment_number,
                )
                if (start_byte, end_byte_exclusive) != (
                    expected_start,
                    expected_end,
                ):
                    raise UploadConflictError(
                        f"segment {segment_number} byte range does not match the session"
                    )
                if actual_size_bytes != expected_end - expected_start:
                    raise UploadConflictError(
                        f"segment {segment_number} size does not match its byte range"
                    )

                existing = session.get(
                    UploadSegmentRecord,
                    (upload_session_id, segment_number),
                )
                if existing is not None:
                    if (
                        existing.start_byte != start_byte
                        or existing.end_byte_exclusive != end_byte_exclusive
                        or existing.actual_size_bytes != actual_size_bytes
                        or existing.checksum_sha256 != checksum_sha256
                        or existing.artifact_uri != artifact_uri
                    ):
                        raise UploadConflictError(
                            f"segment {segment_number} was already uploaded differently"
                        )
                    result = self._segment_snapshot(existing)
                else:
                    record = UploadSegmentRecord(
                        upload_session_id=upload_session_id,
                        segment_number=segment_number,
                        start_byte=start_byte,
                        end_byte_exclusive=end_byte_exclusive,
                        actual_size_bytes=actual_size_bytes,
                        checksum_sha256=checksum_sha256,
                        artifact_uri=artifact_uri,
                        created_at=registered_at,
                    )
                    session.add(record)
                    session.flush()
                    result = self._segment_snapshot(record)
        if expired:
            raise UploadStateError("upload session has expired")
        assert result is not None
        return result

    def claim_completion(
        self,
        upload_session_id: str,
        *,
        now: datetime | None = None,
        stale_after: timedelta = timedelta(minutes=15),
    ) -> UploadCompletionClaim:
        claimed_at = now or _utc_now()
        expired = False
        result: UploadCompletionClaim | None = None
        with self._session_factory.begin() as session:
            upload = self._locked_session(session, upload_session_id)
            if upload.status == UploadSessionStatus.COMPLETED.value:
                job_id = None
                if upload.video_id is not None:
                    job_id = session.scalar(
                        select(SeriesParseJobRecord.job_id).where(
                            SeriesParseJobRecord.video_id == upload.video_id
                        )
                    )
                result = UploadCompletionClaim(
                    session=self._snapshot(session, upload),
                    segments=tuple(self._segment_snapshots(session, upload)),
                    completion_token=None,
                    existing_job_id=job_id,
                )
            elif self._expire_if_due(upload, claimed_at):
                expired = True
            else:
                if upload.status == UploadSessionStatus.EXPIRED.value:
                    expired = True
                if upload.status == UploadSessionStatus.COMPLETING.value:
                    started_at = upload.completion_started_at
                    if (
                        started_at is not None
                        and started_at + stale_after > claimed_at
                    ):
                        raise UploadCompletionInProgressError(
                            "upload completion is already in progress"
                        )

                segments = self._segment_snapshots(session, upload)
                if len(segments) != upload.total_segments:
                    present = {item.segment_number for item in segments}
                    missing = [
                        number
                        for number in range(upload.total_segments)
                        if number not in present
                    ]
                    raise UploadStateError(
                        "upload cannot complete with missing segments: "
                        + ", ".join(str(item) for item in missing)
                    )
                completion_token = str(uuid4())
                upload.status = UploadSessionStatus.COMPLETING.value
                upload.completion_token = completion_token
                upload.completion_started_at = claimed_at
                upload.last_error = None
                session.flush()
                result = UploadCompletionClaim(
                    session=self._snapshot(session, upload),
                    segments=tuple(segments),
                    completion_token=completion_token,
                    existing_job_id=None,
                )
        if expired:
            raise UploadStateError("upload session has expired")
        assert result is not None
        return result

    def release_completion(
        self,
        upload_session_id: str,
        completion_token: str,
        *,
        error: str,
    ) -> bool:
        summary = error.strip()[:8192] or "upload completion failed"
        with self._session_factory.begin() as session:
            result = session.execute(
                update(UploadSessionRecord)
                .where(
                    UploadSessionRecord.upload_session_id == upload_session_id,
                    UploadSessionRecord.status
                    == UploadSessionStatus.COMPLETING.value,
                    UploadSessionRecord.completion_token == completion_token,
                )
                .values(
                    status=UploadSessionStatus.UPLOADING.value,
                    completion_token=None,
                    completion_started_at=None,
                    last_error=summary,
                )
            )
            return result.rowcount == 1

    def complete_upload(
        self,
        upload_session_id: str,
        completion_token: str,
        *,
        video_id: str,
        original_source_uri: str,
        content_hash: str,
        duration_ms: int,
        pipeline_version: str,
        max_job_attempts: int,
        now: datetime | None = None,
    ) -> CompletedUploadState:
        completed_at = now or _utc_now()
        job_id = derive_parse_job_id(video_id, pipeline_version)
        with self._session_factory.begin() as session:
            upload = self._locked_session(session, upload_session_id)
            if (
                upload.status != UploadSessionStatus.COMPLETING.value
                or upload.completion_token != completion_token
            ):
                raise UploadCompletionLeaseLostError(
                    "upload completion claim is no longer current"
                )

            conflict = session.scalar(
                select(VideoRecord).where(
                    VideoRecord.series_id == upload.series_id,
                    or_(
                        VideoRecord.episode_order == upload.episode_order,
                        VideoRecord.content_hash == content_hash,
                    ),
                )
            )
            if conflict is not None and conflict.video_id != video_id:
                raise UploadConflictError(
                    "episode order or source content already belongs to another Video"
                )
            video = session.get(VideoRecord, video_id)
            if video is None:
                video = VideoRecord(
                    video_id=video_id,
                    series_id=upload.series_id,
                    episode_order=upload.episode_order,
                    upload_sequence=upload.upload_sequence,
                    original_filename=upload.original_filename,
                    original_source_uri=original_source_uri,
                    inference_proxy_uri=None,
                    content_hash=content_hash,
                    total_bytes=upload.total_size_bytes,
                    duration_ms=duration_ms,
                    upload_status="COMPLETED",
                    parse_status="QUEUED",
                    upload_progress=100,
                    parse_progress=0,
                )
                session.add(video)
            elif (
                video.series_id != upload.series_id
                or video.episode_order != upload.episode_order
                or video.content_hash != content_hash
                or video.duration_ms != duration_ms
                or video.original_source_uri != original_source_uri
            ):
                raise UploadConflictError(
                    f"videoId {video_id!r} already identifies another Video"
                )

            job = session.get(SeriesParseJobRecord, job_id)
            if job is None:
                job = SeriesParseJobRecord(
                    job_id=job_id,
                    series_id=upload.series_id,
                    video_id=video_id,
                    pipeline_version=pipeline_version,
                    status=ParseJobStatus.QUEUED.value,
                    stage=ParseJobStage.QUEUED.value,
                    progress=0,
                    attempt_count=0,
                    max_attempts=max_job_attempts,
                    available_at=completed_at,
                    lease_version=0,
                )
                session.add(job)

            series = session.get(SeriesRecord, upload.series_id)
            if series is None:
                raise UploadSeriesNotFoundError(
                    f"seriesId {upload.series_id!r} disappeared during completion"
                )
            series.status = "INDEXING"
            upload.status = UploadSessionStatus.COMPLETED.value
            upload.video_id = video_id
            upload.completed_at = completed_at
            upload.completion_token = None
            upload.completion_started_at = None
            upload.last_error = None
            session.flush()
            return CompletedUploadState(
                session=self._snapshot(session, upload),
                video_id=video_id,
                job_id=job_id,
            )

    @staticmethod
    def _locked_session(
        session: Session,
        upload_session_id: str,
    ) -> UploadSessionRecord:
        record = session.scalar(
            select(UploadSessionRecord)
            .where(UploadSessionRecord.upload_session_id == upload_session_id)
            .with_for_update()
        )
        if record is None:
            raise UploadSessionNotFoundError(
                f"uploadSessionId {upload_session_id!r} does not exist"
            )
        return record

    @staticmethod
    def _require_uploading(upload: UploadSessionRecord) -> None:
        if upload.status != UploadSessionStatus.UPLOADING.value:
            raise UploadStateError(
                f"upload session does not accept segments in {upload.status}"
            )

    @staticmethod
    def _expire_if_due(upload: UploadSessionRecord, now: datetime) -> bool:
        if (
            upload.status != UploadSessionStatus.COMPLETED.value
            and upload.expires_at <= now
        ):
            upload.status = UploadSessionStatus.EXPIRED.value
            upload.completion_token = None
            upload.completion_started_at = None
            upload.active_slot_key = None
            return True
        return upload.status == UploadSessionStatus.EXPIRED.value

    @staticmethod
    def _expected_range(
        upload: UploadSessionRecord,
        segment_number: int,
    ) -> tuple[int, int]:
        if segment_number < 0 or segment_number >= upload.total_segments:
            raise UploadConflictError("segment number is outside the upload session")
        start = segment_number * upload.preferred_segment_size_bytes
        end = min(start + upload.preferred_segment_size_bytes, upload.total_size_bytes)
        return start, end

    @classmethod
    def _snapshot(
        cls,
        session: Session,
        upload: UploadSessionRecord,
    ) -> UploadSessionState:
        uploaded_segment_count, uploaded_size_bytes = session.execute(
            select(
                func.count(UploadSegmentRecord.segment_number),
                func.coalesce(func.sum(UploadSegmentRecord.actual_size_bytes), 0),
            ).where(
                UploadSegmentRecord.upload_session_id
                == upload.upload_session_id
            )
        ).one()
        return UploadSessionState(
            upload_session_id=upload.upload_session_id,
            workspace_id=upload.workspace_id,
            series_id=upload.series_id,
            episode_order=upload.episode_order,
            upload_sequence=upload.upload_sequence,
            original_filename=upload.original_filename,
            media_type=upload.media_type,
            total_size_bytes=upload.total_size_bytes,
            preferred_segment_size_bytes=upload.preferred_segment_size_bytes,
            total_segments=upload.total_segments,
            status=upload.status,
            uploaded_segment_count=int(uploaded_segment_count),
            uploaded_size_bytes=int(uploaded_size_bytes),
            expires_at=upload.expires_at,
            video_id=upload.video_id,
            last_error=upload.last_error,
            created_at=upload.created_at,
            updated_at=upload.updated_at,
        )

    @classmethod
    def _segment_snapshots(
        cls,
        session: Session,
        upload: UploadSessionRecord,
    ) -> list[UploadSegmentState]:
        records = session.scalars(
            select(UploadSegmentRecord)
            .where(
                UploadSegmentRecord.upload_session_id
                == upload.upload_session_id
            )
            .order_by(UploadSegmentRecord.segment_number)
        ).all()
        return [cls._segment_snapshot(item) for item in records]

    @staticmethod
    def _segment_snapshot(record: UploadSegmentRecord) -> UploadSegmentState:
        return UploadSegmentState(
            segment_number=record.segment_number,
            start_byte=record.start_byte,
            end_byte_exclusive=record.end_byte_exclusive,
            actual_size_bytes=record.actual_size_bytes,
            checksum_sha256=record.checksum_sha256,
            artifact_uri=record.artifact_uri,
        )

    @staticmethod
    def _validate_same_session(
        record: UploadSessionRecord,
        requested: dict[str, object],
    ) -> None:
        mismatches = [
            name for name, value in requested.items() if getattr(record, name) != value
        ]
        if mismatches:
            raise UploadConflictError(
                "idempotency key was already used with different fields: "
                + ", ".join(mismatches)
            )


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _active_slot_key(series_id: str, episode_order: int) -> str:
    return f"{series_id}:{episode_order}"
