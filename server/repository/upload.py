from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
from uuid import uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ..identity import derive_parse_job_id
from ..model.dto.uploadDTO import (
    CompletedUploadState, UploadCompletionClaim, UploadSegmentState, UploadSessionState,
)
from ..model.job import ParseJobStage, ParseJobStatus, SeriesParseJobRecord
from ..model.upload import (
    UploadSegmentRecord,
    UploadSessionRecord,
    UploadSessionStatus,
    UploadCompletionStage,
)
from ..model.video import SeriesRecord, VideoRecord
from ..utils.time import utc_now_naive


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


class UploadRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def create_session(
        self,
        *,
        workspace_id: str,
        series_id: str,
        episode_order: int,
        upload_sequence: int | None,
        original_filename: str,
        media_type: str,
        total_size_bytes: int,
        preferred_segment_size_bytes: int,
        total_segments: int,
        idempotency_key: str,
        expires_at: datetime,
        now: datetime,
        content_sha256: str | None = None,
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
            "content_sha256": content_sha256,
        }
        if upload_sequence is None:
            requested.pop("upload_sequence")
        if content_sha256 is None:
            requested.pop("content_sha256")
        fingerprint_fields = {
            **requested, "upload_sequence": upload_sequence, "content_sha256": content_sha256,
        }
        fingerprint_fields.pop("preferred_segment_size_bytes")
        fingerprint_fields.pop("total_segments")
        request_fingerprint = hashlib.sha256(json.dumps(fingerprint_fields, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
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
                        self._validate_same_session(existing, requested, request_fingerprint=request_fingerprint)
                        return self._snapshot(session, existing)

                    series = session.scalar(select(SeriesRecord).where(
                        SeriesRecord.series_id == series_id
                    ).with_for_update())
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

                    resolved_sequence = upload_sequence
                    if resolved_sequence is None:
                        upload_max = session.scalar(select(func.max(UploadSessionRecord.upload_sequence)).where(
                            UploadSessionRecord.series_id == series_id
                        )) or 0
                        video_max = session.scalar(select(func.max(VideoRecord.upload_sequence)).where(
                            VideoRecord.series_id == series_id
                        )) or 0
                        resolved_sequence = max(upload_max, video_max) + 1

                    record = UploadSessionRecord(
                        upload_session_id=str(uuid4()),
                        workspace_id=workspace_id,
                        series_id=series_id,
                        episode_order=episode_order,
                        upload_sequence=resolved_sequence,
                        original_filename=original_filename,
                        media_type=media_type,
                        total_size_bytes=total_size_bytes,
                        preferred_segment_size_bytes=preferred_segment_size_bytes,
                        total_segments=total_segments,
                        content_sha256=content_sha256,
                        status=UploadSessionStatus.UPLOADING.value,
                        idempotency_key=idempotency_key,
                        request_fingerprint=request_fingerprint,
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
        checked_at = now or utc_now_naive()
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
        registered_at = now or utc_now_naive()
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
                    upload.version += 1
                    upload.updated_at = registered_at
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
        claimed_at = now or utc_now_naive()
        expired = False
        result: UploadCompletionClaim | None = None
        with self._session_factory.begin() as session:
            upload = self._locked_session(session, upload_session_id)
            if upload.status == UploadSessionStatus.COMPLETED.value:
                job_id = upload.job_id
                if job_id is None and upload.video_id is not None:
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
                upload.completion_stage = UploadCompletionStage.VERIFYING.value
                upload.last_error_code = None
                upload.version += 1
                upload.updated_at = claimed_at
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

    def set_completion_stage(self, upload_session_id: str, completion_token: str, stage: UploadCompletionStage) -> None:
        with self._session_factory.begin() as session:
            upload = self._locked_session(session, upload_session_id)
            if upload.status != UploadSessionStatus.COMPLETING.value or upload.completion_token != completion_token:
                raise UploadCompletionLeaseLostError("upload completion claim is no longer current")
            if upload.completion_stage != stage:
                upload.completion_stage = stage
                upload.version += 1

    def list_for_series(self, series_id: str, *, workspace_id: str, status: str | None = None) -> list[UploadSessionState]:
        return self.list_for_workspace(workspace_id, series_id=series_id, status=status)

    def list_for_workspace(self, workspace_id: str, *, series_id: str | None = None, status: str | None = None) -> list[UploadSessionState]:
        if status == "all":
            statuses = None
        elif status is None:
            statuses = {UploadSessionStatus.UPLOADING.value, UploadSessionStatus.COMPLETING.value}
        else:
            statuses = {UploadSessionStatus(value.strip()).value for value in status.split(",")}
        with self._session_factory.begin() as session:
            statement = select(UploadSessionRecord).where(UploadSessionRecord.workspace_id == workspace_id)
            if series_id is not None:
                statement = statement.where(UploadSessionRecord.series_id == series_id)
            records = session.scalars(statement.order_by(UploadSessionRecord.upload_sequence, UploadSessionRecord.created_at, UploadSessionRecord.upload_session_id).with_for_update()).all()
            segments_by_upload: dict[str, list[tuple[int, int]]] = {}
            if records:
                for upload_id, number, size in session.execute(
                    select(UploadSegmentRecord.upload_session_id, UploadSegmentRecord.segment_number, UploadSegmentRecord.actual_size_bytes)
                    .where(UploadSegmentRecord.upload_session_id.in_([record.upload_session_id for record in records]))
                ):
                    segments_by_upload.setdefault(upload_id, []).append((number, size))
            result = []
            checked_at = utc_now_naive()
            for record in records:
                self._expire_if_due(record, checked_at)
                if statuses is None or record.status in statuses:
                    result.append(self._snapshot(session, record, segments=segments_by_upload.get(record.upload_session_id, [])))
            return result

    def release_completion(
        self,
        upload_session_id: str,
        completion_token: str,
        *,
        error: str,
        error_code: str = "PROCESSING_FAILED",
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
                    last_error_code=error_code,
                    completion_stage=None,
                    version=UploadSessionRecord.version + 1,
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
        completed_at = now or utc_now_naive()
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
                    parse_status=ParseJobStatus.QUEUED.value,
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
            upload.last_error_code = None
            upload.content_sha256 = content_hash
            upload.job_id = job_id
            upload.completion_stage = None
            upload.version += 1
            upload.updated_at = completed_at
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
            upload.status not in (UploadSessionStatus.COMPLETED.value, UploadSessionStatus.EXPIRED.value)
            and upload.expires_at <= now
        ):
            upload.status = UploadSessionStatus.EXPIRED.value
            upload.completion_token = None
            upload.completion_started_at = None
            upload.active_slot_key = None
            upload.completion_stage = None
            upload.version += 1
            upload.updated_at = now
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
        *,
        segments: list[tuple[int, int]] | None = None,
    ) -> UploadSessionState:
        if segments is None:
            segments = session.execute(
                select(UploadSegmentRecord.segment_number, UploadSegmentRecord.actual_size_bytes).where(
                    UploadSegmentRecord.upload_session_id == upload.upload_session_id
                )
            ).all()
        present = {number for number, _size in segments}
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
            uploaded_segment_count=len(segments),
            uploaded_size_bytes=sum(size for _number, size in segments),
            expires_at=upload.expires_at,
            video_id=upload.video_id,
            last_error=upload.last_error,
            created_at=upload.created_at,
            updated_at=upload.updated_at,
            content_sha256=upload.content_sha256,
            completion_stage=upload.completion_stage,
            job_id=upload.job_id,
            version=upload.version,
            missing_segment_numbers=tuple(n for n in range(upload.total_segments) if n not in present),
            last_error_code=upload.last_error_code,
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
        *,
        request_fingerprint: str,
    ) -> None:
        if record.request_fingerprint is not None:
            if record.request_fingerprint != request_fingerprint:
                raise UploadConflictError("idempotency key was already used with different upload parameters")
            return
        mismatches = [
            name for name, value in requested.items() if getattr(record, name) != value
        ]
        if mismatches:
            raise UploadConflictError(
                "idempotency key was already used with different fields: "
                + ", ".join(mismatches)
            )


def _active_slot_key(series_id: str, episode_order: int) -> str:
    return f"{series_id}:{episode_order}"
