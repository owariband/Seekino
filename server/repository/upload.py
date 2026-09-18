from __future__ import annotations

from datetime import timedelta
import hashlib
import json
from uuid import uuid4

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ..identity import derive_parse_job_id
from ..model.dto.uploadDTO import (
    CompletedUploadState, UploadCompletionClaim, UploadSegmentState,
    UploadSessionDeletion, UploadSessionState,
)
from ..model.index_sync import IndexSyncIntentRecord
from ..model.job import ParseJobStage, ParseJobStatus, SeriesParseJobRecord
from ..model.job import VideoChunkCheckpointRecord
from ..model.parse_usage import ParseModelCallRecord, ParseUsageAttemptRecord
from ..model.upload import (
    UPLOAD_DELETION_PENDING_ERROR_CODE,
    UploadSegmentRecord,
    UploadSessionRecord,
    UploadSessionStatus,
    UploadCompletionStage,
)
from ..model.video import (
    ContextSnapshotRecord,
    SeriesRecord,
    VideoChunkEmbeddingRecord,
    VideoChunkInfoRecord,
    VideoChunkRecord,
    VideoRecord,
)
from ..utils.time import now_unix_time_ms, timedelta_to_ms
from .index_sync import refresh_series_statuses


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
        episode_order: int | None = None,
        upload_sequence: int | None = None,
        original_filename: str,
        media_type: str,
        total_size_bytes: int,
        preferred_segment_size_bytes: int,
        total_segments: int,
        idempotency_key: str,
        expires_at: int,
        now: int,
        content_sha256: str | None = None,
    ) -> UploadSessionState:
        requested = {
            "workspace_id": workspace_id,
            "series_id": series_id,
            "original_filename": original_filename,
            "media_type": media_type,
            "total_size_bytes": total_size_bytes,
            "idempotency_key": idempotency_key,
            "content_sha256": content_sha256,
        }
        request_fingerprint = hashlib.sha256(json.dumps(requested, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        for attempt in range(2):
            try:
                # Replay locks only Upload: completion uses Upload then Series.
                with self._session_factory.begin() as session:
                    existing = session.scalar(
                        select(UploadSessionRecord).where(
                            UploadSessionRecord.workspace_id == workspace_id,
                            UploadSessionRecord.idempotency_key == idempotency_key,
                        ).with_for_update()
                    )
                    if existing is not None:
                        self._validate_same_session(existing, requested, request_fingerprint=request_fingerprint)
                        self._expire_if_due(existing, now)
                        return self._snapshot(session, existing)

                with self._session_factory.begin() as session:
                    # Lock before ordinary reads so MySQL's snapshot includes the
                    # preceding upload that released this Series lock.
                    series = session.scalar(select(SeriesRecord).where(
                        SeriesRecord.series_id == series_id
                    ).with_for_update())
                    if series is None or series.workspace_id != workspace_id:
                        raise UploadSeriesNotFoundError(
                            f"seriesId {series_id!r} does not exist in this workspace"
                        )
                    existing = session.scalar(
                        select(UploadSessionRecord).where(
                            UploadSessionRecord.workspace_id == workspace_id,
                            UploadSessionRecord.idempotency_key == idempotency_key,
                        )
                    )
                    if existing is not None:
                        # Release Series before returning to the Upload-only replay.
                        continue

                    upload_orders = session.execute(select(
                        func.max(UploadSessionRecord.episode_order),
                        func.max(UploadSessionRecord.upload_sequence),
                    ).where(UploadSessionRecord.series_id == series_id)).one()
                    video_orders = session.execute(select(
                        func.max(VideoRecord.episode_order),
                        func.max(VideoRecord.upload_sequence),
                    ).where(VideoRecord.series_id == series_id)).one()
                    next_order = max(value or 0 for value in (*upload_orders, *video_orders)) + 1

                    record = UploadSessionRecord(
                        upload_session_id=str(uuid4()),
                        workspace_id=workspace_id,
                        series_id=series_id,
                        episode_order=next_order,
                        upload_sequence=next_order,
                        original_filename=original_filename,
                        media_type=media_type,
                        total_size_bytes=total_size_bytes,
                        preferred_segment_size_bytes=preferred_segment_size_bytes,
                        total_segments=total_segments,
                        content_sha256=content_sha256,
                        status=UploadSessionStatus.UPLOADING.value,
                        idempotency_key=idempotency_key,
                        request_fingerprint=request_fingerprint,
                        active_slot_key=_active_slot_key(series_id, next_order),
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
        now: int | None = None,
    ) -> UploadSessionState:
        checked_at = now if now is not None else now_unix_time_ms()
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

    def video_exists(self, video_id: str) -> bool:
        with self._session_factory() as session:
            return session.get(VideoRecord, video_id) is not None

    def retry_parse(
        self,
        upload_session_id: str,
        *,
        workspace_id: str,
        expected_job_version: int,
        now: int,
    ) -> str:
        """Requeue the ParseJob owned by an UploadSession.

        The lock order is UploadSession -> ParseJob -> Video -> Series. The
        version predicate also fences duplicate submissions if a caller loses
        the successful HTTP response and retries with the same version.
        """
        with self._session_factory.begin() as session:
            upload = self._locked_session(session, upload_session_id)
            if upload.workspace_id != workspace_id:
                raise UploadSessionNotFoundError(
                    "upload session does not exist in this workspace"
                )
            if (
                upload.status != UploadSessionStatus.COMPLETED.value
                or upload.last_error_code == UPLOAD_DELETION_PENDING_ERROR_CODE
            ):
                raise UploadStateError(
                    "only a completed upload session can retry parsing"
                )
            if upload.job_id is None or upload.video_id is None:
                raise UploadConflictError(
                    "completed upload session is missing its ParseJob or Video"
                )

            job = session.scalar(
                select(SeriesParseJobRecord)
                .where(SeriesParseJobRecord.job_id == upload.job_id)
                .with_for_update()
            )
            if job is None:
                raise UploadConflictError(
                    "upload session ParseJob no longer exists"
                )
            if (
                job.video_id != upload.video_id
                or job.series_id != upload.series_id
            ):
                raise UploadConflictError(
                    "upload session does not own its linked ParseJob"
                )
            if job.version != expected_job_version:
                raise UploadConflictError(
                    "ParseJob version changed; refresh before retrying"
                )
            if job.status != ParseJobStatus.FAILED.value:
                raise UploadStateError("only a failed ParseJob can be retried")

            update_result = session.execute(
                update(SeriesParseJobRecord)
                .where(
                    SeriesParseJobRecord.job_id == job.job_id,
                    SeriesParseJobRecord.version == expected_job_version,
                    SeriesParseJobRecord.status == ParseJobStatus.FAILED.value,
                )
                .values(
                    status=ParseJobStatus.QUEUED.value,
                    max_attempts=max(job.max_attempts, job.attempt_count + 1),
                    available_at=now,
                    lease_owner=None,
                    lease_expires_at=None,
                    heartbeat_at=None,
                    completed_at=None,
                    last_error=None,
                    version=SeriesParseJobRecord.version + 1,
                    updated_at=now,
                )
            )
            if update_result.rowcount != 1:
                raise UploadConflictError(
                    "ParseJob version changed; refresh before retrying"
                )

            video = session.scalar(
                select(VideoRecord)
                .where(VideoRecord.video_id == upload.video_id)
                .with_for_update()
            )
            if video is None:
                raise UploadConflictError("upload session Video no longer exists")
            series = session.scalar(
                select(SeriesRecord)
                .where(SeriesRecord.series_id == upload.series_id)
                .with_for_update()
            )
            if series is None:
                raise UploadSeriesNotFoundError(
                    "upload session Series no longer exists"
                )
            if video.parse_status not in {
                ParseJobStatus.SUCCEEDED.value,
                "COMPLETED",
            }:
                video.parse_status = ParseJobStatus.QUEUED.value
                series.status = "INDEXING"
            return job.job_id

    def reparse(
        self,
        upload_session_id: str,
        *,
        workspace_id: str,
        expected_job_version: int,
        additional_attempts: int,
        now: int,
    ) -> str:
        """Restart one UploadSession's terminal ParseJob from its source Video.

        Unlike ``retry_parse``, this deliberately removes every checkpoint so
        the worker cannot resume prior model output. A previously successful
        Video remains searchable until the replacement parse is atomically
        imported; usage rows and published artifacts are retained as history.
        """

        if (
            isinstance(additional_attempts, bool)
            or not isinstance(additional_attempts, int)
            or additional_attempts <= 0
        ):
            raise ValueError("additional_attempts must be a positive integer")

        with self._session_factory.begin() as session:
            upload = self._locked_session(session, upload_session_id)
            if upload.workspace_id != workspace_id:
                raise UploadSessionNotFoundError(
                    "upload session does not exist in this workspace"
                )
            if (
                upload.status != UploadSessionStatus.COMPLETED.value
                or upload.last_error_code == UPLOAD_DELETION_PENDING_ERROR_CODE
            ):
                raise UploadStateError(
                    "only a completed upload session can be fully reparsed"
                )
            if upload.job_id is None or upload.video_id is None:
                raise UploadConflictError(
                    "completed upload session is missing its ParseJob or Video"
                )

            job = session.scalar(
                select(SeriesParseJobRecord)
                .where(SeriesParseJobRecord.job_id == upload.job_id)
                .with_for_update()
            )
            if job is None:
                raise UploadConflictError(
                    "upload session ParseJob no longer exists"
                )
            if (
                job.video_id != upload.video_id
                or job.series_id != upload.series_id
            ):
                raise UploadConflictError(
                    "upload session does not own its linked ParseJob"
                )
            if job.version != expected_job_version:
                raise UploadConflictError(
                    "ParseJob version changed; refresh before reparsing"
                )
            terminal_statuses = {
                ParseJobStatus.SUCCEEDED.value,
                ParseJobStatus.FAILED.value,
            }
            if job.status not in terminal_statuses:
                raise UploadStateError(
                    "only a completed or failed ParseJob can be fully reparsed"
                )

            previous_status = job.status
            update_result = session.execute(
                update(SeriesParseJobRecord)
                .where(
                    SeriesParseJobRecord.job_id == job.job_id,
                    SeriesParseJobRecord.version == expected_job_version,
                    SeriesParseJobRecord.status == previous_status,
                )
                .values(
                    status=ParseJobStatus.QUEUED.value,
                    stage=ParseJobStage.QUEUED.value,
                    progress=0,
                    max_attempts=max(
                        job.max_attempts,
                        job.attempt_count + additional_attempts,
                    ),
                    available_at=now,
                    lease_owner=None,
                    lease_expires_at=None,
                    heartbeat_at=None,
                    started_at=None,
                    completed_at=None,
                    last_error=None,
                    version=SeriesParseJobRecord.version + 1,
                    updated_at=now,
                )
            )
            if update_result.rowcount != 1:
                raise UploadConflictError(
                    "ParseJob version changed; refresh before reparsing"
                )

            session.execute(
                delete(VideoChunkCheckpointRecord).where(
                    VideoChunkCheckpointRecord.job_id == job.job_id
                )
            )
            video = session.scalar(
                select(VideoRecord)
                .where(VideoRecord.video_id == upload.video_id)
                .with_for_update()
            )
            if video is None:
                raise UploadConflictError("upload session Video no longer exists")
            series = session.scalar(
                select(SeriesRecord)
                .where(SeriesRecord.series_id == upload.series_id)
                .with_for_update()
            )
            if series is None:
                raise UploadSeriesNotFoundError(
                    "upload session Series no longer exists"
                )

            if video.parse_status not in {
                ParseJobStatus.SUCCEEDED.value,
                "COMPLETED",
            }:
                video.parse_status = ParseJobStatus.QUEUED.value
                video.parse_progress = 0
                series.status = "INDEXING"
            return job.job_id

    def list_pending_deletions(
        self,
        *,
        limit: int = 100,
    ) -> list[tuple[str, str]]:
        with self._session_factory() as session:
            return list(session.execute(
                select(
                    UploadSessionRecord.upload_session_id,
                    UploadSessionRecord.workspace_id,
                )
                .where(
                    UploadSessionRecord.last_error_code
                    == UPLOAD_DELETION_PENDING_ERROR_CODE,
                )
                .order_by(
                    UploadSessionRecord.updated_at,
                    UploadSessionRecord.upload_session_id,
                )
                .limit(limit)
            ))

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

    def begin_delete_session(
        self,
        upload_session_id: str,
        *,
        workspace_id: str,
        now: int,
    ) -> UploadSessionDeletion:
        with self._session_factory.begin() as session:
            upload = self._locked_session(session, upload_session_id)
            if upload.workspace_id != workspace_id:
                raise UploadSessionNotFoundError("upload session does not exist in this workspace")
            if upload.status == UploadSessionStatus.COMPLETING.value:
                raise UploadCompletionInProgressError("upload completion is already in progress")

            video_id = upload.video_id
            job_id = upload.job_id
            if video_id is not None or job_id is not None:
                shared_upload = session.scalar(
                    select(UploadSessionRecord.upload_session_id)
                    .where(
                        UploadSessionRecord.upload_session_id != upload_session_id,
                        or_(
                            UploadSessionRecord.video_id == video_id
                            if video_id is not None else False,
                            UploadSessionRecord.job_id == job_id
                            if job_id is not None else False,
                        ),
                    )
                    .with_for_update()
                )
                if shared_upload is not None:
                    raise UploadConflictError(
                        "another upload session references this task's Video or ParseJob"
                    )

            jobs: list[SeriesParseJobRecord] = []
            if video_id is not None:
                jobs = list(session.scalars(
                    select(SeriesParseJobRecord)
                    .where(SeriesParseJobRecord.video_id == video_id)
                    .order_by(SeriesParseJobRecord.job_id)
                    .with_for_update()
                ))
            elif job_id is not None:
                job = session.scalar(
                    select(SeriesParseJobRecord)
                    .where(SeriesParseJobRecord.job_id == job_id)
                    .with_for_update()
                )
                if job is not None:
                    jobs = [job]
            if any(job.status == ParseJobStatus.RUNNING.value for job in jobs):
                raise UploadStateError(
                    "the upload task is being parsed and cannot be deleted"
                )
            for job in jobs:
                if job.status in {
                    ParseJobStatus.QUEUED.value,
                    ParseJobStatus.RETRY_WAIT.value,
                }:
                    # Fencing every Job in the same transaction prevents a Worker
                    # from claiming another pipeline for the deleting Video.
                    job.status = ParseJobStatus.FAILED.value
                    job.lease_owner = None
                    job.lease_expires_at = None
                    job.heartbeat_at = None
                    job.completed_at = now
                    job.last_error = "upload task deletion requested"
                    job.version += 1
                    job.updated_at = now
            if jobs and video_id is not None:
                video = session.get(VideoRecord, video_id)
                if video is not None:
                    video.parse_status = ParseJobStatus.FAILED.value

            job_ids = tuple(sorted(
                {job.job_id for job in jobs}
                | ({job_id} if job_id is not None else set())
            ))

            video_chunk_ids: set[str] = set()
            if video_id is not None:
                video_chunk_ids.update(session.scalars(
                    select(VideoChunkRecord.video_chunk_id).where(
                        VideoChunkRecord.video_id == video_id
                    )
                ))
            if job_ids:
                video_chunk_ids.update(session.scalars(
                    select(VideoChunkCheckpointRecord.video_chunk_id).where(
                        VideoChunkCheckpointRecord.job_id.in_(job_ids)
                    )
                ))

            changed = upload.status != UploadSessionStatus.EXPIRED.value
            # Retain the allocated order and idempotency key to fence late requests.
            upload.expires_at = min(upload.expires_at, now)
            upload.status = UploadSessionStatus.EXPIRED.value
            upload.completion_token = None
            upload.completion_started_at = None
            upload.active_slot_key = None
            upload.completion_stage = None
            upload.last_error = None
            # This marker is the durable cleanup identity for both unfinished
            # uploads (which have no Video/Job IDs) and completed ones.
            upload.last_error_code = UPLOAD_DELETION_PENDING_ERROR_CODE
            removed = session.execute(delete(UploadSegmentRecord).where(
                UploadSegmentRecord.upload_session_id == upload_session_id
            ))
            if changed or removed.rowcount:
                upload.version += 1
                upload.updated_at = now
            session.flush()
            return UploadSessionDeletion(
                session=self._snapshot(session, upload, segments=[]),
                video_id=video_id,
                job_id=job_id,
                job_ids=job_ids,
                video_chunk_ids=tuple(sorted(video_chunk_ids)),
            )

    def finish_delete_session(
        self,
        deletion: UploadSessionDeletion,
        *,
        now: int,
    ) -> UploadSessionState:
        with self._session_factory.begin() as session:
            upload = self._locked_session(
                session,
                deletion.session.upload_session_id,
            )
            if upload.status != UploadSessionStatus.EXPIRED.value:
                raise UploadStateError("upload session deletion is no longer current")
            if upload.video_id is None and upload.job_id is None:
                upload.last_error_code = None
                session.flush()
                return self._snapshot(session, upload, segments=[])
            if (
                upload.video_id != deletion.video_id
                or upload.job_id != deletion.job_id
            ):
                raise UploadConflictError(
                    "upload session deletion targets changed during cleanup"
                )

            if deletion.job_ids:
                session.execute(delete(ParseModelCallRecord).where(
                    ParseModelCallRecord.job_id.in_(deletion.job_ids)
                ))
                session.execute(delete(ParseUsageAttemptRecord).where(
                    ParseUsageAttemptRecord.job_id.in_(deletion.job_ids)
                ))
                session.execute(delete(VideoChunkCheckpointRecord).where(
                    VideoChunkCheckpointRecord.job_id.in_(deletion.job_ids)
                ))
                session.execute(delete(SeriesParseJobRecord).where(
                    SeriesParseJobRecord.job_id.in_(deletion.job_ids)
                ))
            upload.job_id = None
            upload.video_id = None
            session.flush()
            if deletion.video_id is not None:
                session.execute(delete(ContextSnapshotRecord).where(
                    ContextSnapshotRecord.video_id == deletion.video_id
                ))
                if deletion.video_chunk_ids:
                    chunk_ids = deletion.video_chunk_ids
                    session.execute(delete(IndexSyncIntentRecord).where(
                        IndexSyncIntentRecord.video_chunk_id.in_(chunk_ids)
                    ))
                    session.execute(delete(VideoChunkInfoRecord).where(
                        VideoChunkInfoRecord.video_chunk_id.in_(chunk_ids)
                    ))
                    session.execute(delete(VideoChunkEmbeddingRecord).where(
                        VideoChunkEmbeddingRecord.video_chunk_id.in_(chunk_ids)
                    ))
                session.execute(delete(VideoChunkRecord).where(
                    VideoChunkRecord.video_id == deletion.video_id
                ))
                session.execute(delete(VideoRecord).where(
                    VideoRecord.video_id == deletion.video_id
                ))
            upload.version += 1
            upload.updated_at = now
            upload.last_error_code = None
            session.flush()

            remaining_video_count = int(session.scalar(
                select(func.count(VideoRecord.video_id)).where(
                    VideoRecord.series_id == upload.series_id
                )
            ) or 0)
            if remaining_video_count:
                refresh_series_statuses(session, {upload.series_id})
            else:
                series = session.scalar(
                    select(SeriesRecord)
                    .where(SeriesRecord.series_id == upload.series_id)
                    .with_for_update()
                )
                if series is not None:
                    series.status = "EMPTY"
            return self._snapshot(session, upload, segments=[])

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
        now: int | None = None,
    ) -> UploadSegmentState:
        registered_at = now if now is not None else now_unix_time_ms()
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
        now: int | None = None,
        stale_after: timedelta = timedelta(minutes=15),
    ) -> UploadCompletionClaim:
        claimed_at = now if now is not None else now_unix_time_ms()
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
                        and started_at + timedelta_to_ms(stale_after) > claimed_at
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
            checked_at = now_unix_time_ms()
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
        now: int | None = None,
    ) -> CompletedUploadState:
        completed_at = now if now is not None else now_unix_time_ms()
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
    def _expire_if_due(upload: UploadSessionRecord, now: int) -> bool:
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
            if record.request_fingerprint == request_fingerprint:
                return
            # Previously the fingerprint also captured client-provided order.
            legacy_fields = {
                **requested, "episode_order": record.episode_order,
            }
            for original_sequence in (None, record.upload_sequence):
                legacy_fields["upload_sequence"] = original_sequence
                legacy_fingerprint = hashlib.sha256(json.dumps(legacy_fields, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                if record.request_fingerprint == legacy_fingerprint:
                    return
            raise UploadConflictError("idempotency key was already used with different upload parameters")
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
