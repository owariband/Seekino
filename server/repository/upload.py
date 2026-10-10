from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import math
from typing import Literal
from uuid import uuid4

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from engine.parsev2.planning import plan_intervals

from ..model.identifiers import derive_parse_job_id
from ..model.dto.jobDTO import VideoParseTaskState
from ..model.dto.uploadDTO import (
    CompletedUploadState, UploadCompletionClaim, UploadSegmentState,
    UploadSessionDeletion, UploadSessionState,
)
from ..model.index_sync import IndexSyncIntent
from ..model.job import (
    ChunkParseTask,
    ChunkParseTaskStatus,
    IntervalParseContent,
    IntervalParseTask,
    IntervalParseTaskStatus,
    ParseJobStage,
    ParseJobStatus,
    SeriesParseJob,
    VideoParseTask,
    VideoParseTaskStatus,
    validate_interval_parse_task_members,
)
from ..model.job import VideoChunkCheckpoint
from ..model.parse_usage import ParseModelCall, ParseUsageAttempt
from ..model.upload import (
    UPLOAD_DELETION_PENDING_ERROR_CODE,
    UploadSegment,
    UploadSession,
    UploadSessionStatus,
    UploadCompletionStage,
)
from ..model.video import (
    ContextSnapshot,
    Series,
    VideoChunkEmbedding,
    VideoChunkInfo,
    VideoChunk,
    Video,
)
from ..utils.time import datetime_to_unix_time_ms, now_utc
from .index_sync import refresh_series_statuses
from .video_parse import (
    RUNTIME_DETAIL_KEY,
    VideoParseConflictError,
    invalidate_series_prefixes_from,
)


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
        tenant_id: str,
        series_id: str,
        episode_order: int,
        original_filename: str,
        media_type: str,
        total_size_bytes: int,
        preferred_segment_size_bytes: int,
        total_segments: int,
        idempotency_key: str,
        expires_at: datetime,
        now: datetime,
        content_sha256: str | None = None,
        parse_config: dict | None = None,
        requested_parse_config: dict | None = None,
        video_parse_config: dict | None = None,
        requested_video_parse_config: dict | None = None,
        created_by_user_id: str | None = None,
    ) -> UploadSessionState:
        if (
            isinstance(episode_order, bool)
            or not isinstance(episode_order, int)
            or episode_order <= 0
            or episode_order > 2_147_483_647
        ):
            raise ValueError("episode_order must be a positive 32-bit integer")
        if not isinstance(video_parse_config, dict):
            raise ValueError(
                "video_parse_config is required for a new upload session"
            )
        requested = {
            "tenant_id": tenant_id,
            **({"created_by_user_id": created_by_user_id} if created_by_user_id is not None else {}),
            "series_id": series_id,
            "episode_order": episode_order,
            "original_filename": original_filename,
            "media_type": media_type,
            "total_size_bytes": total_size_bytes,
            "idempotency_key": idempotency_key,
            "content_sha256": content_sha256,
        }
        if requested_parse_config is not None:
            requested["parse_config"] = requested_parse_config
        if requested_video_parse_config is not None:
            requested["video_parse_config"] = requested_video_parse_config
        request_fingerprint = hashlib.sha256(json.dumps(requested, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        for attempt in range(2):
            try:
                # Replay locks only Upload: completion uses Upload then Series.
                with self._session_factory.begin() as session:
                    existing = session.scalar(
                        select(UploadSession).where(
                            UploadSession.tenant_id == tenant_id,
                            UploadSession.created_by_user_id == created_by_user_id,
                            UploadSession.idempotency_key == idempotency_key,
                        ).with_for_update()
                    )
                    if existing is not None:
                        self._validate_same_session(existing, requested, request_fingerprint=request_fingerprint)
                        self._expire_if_due(existing, now)
                        return self._snapshot(session, existing)

                with self._session_factory.begin() as session:
                    # Lock before ordinary reads so MySQL's snapshot includes the
                    # preceding upload that released this Series lock.
                    series = session.scalar(select(Series).where(
                        Series.series_id == series_id
                    ).with_for_update())
                    if series is None or series.tenant_id != tenant_id:
                        raise UploadSeriesNotFoundError(
                            f"seriesId {series_id!r} does not exist in this workspace"
                        )
                    existing = session.scalar(
                        select(UploadSession).where(
                            UploadSession.tenant_id == tenant_id,
                            UploadSession.created_by_user_id == created_by_user_id,
                            UploadSession.idempotency_key == idempotency_key,
                        )
                    )
                    if existing is not None:
                        # Release Series before returning to the Upload-only replay.
                        continue

                    occupying_uploads = session.scalars(
                        select(UploadSession)
                        .where(
                            UploadSession.series_id == series_id,
                            UploadSession.episode_order == episode_order,
                        )
                        .order_by(UploadSession.upload_session_id)
                        .with_for_update()
                    ).all()
                    for occupying_upload in occupying_uploads:
                        self._expire_if_due(occupying_upload, now)
                    session.flush()
                    occupied_upload_id = next(
                        (
                            item.upload_session_id
                            for item in occupying_uploads
                            if item.active_slot_key is not None
                        ),
                        None,
                    )
                    occupied_video_id = session.scalar(
                        select(Video.video_id).where(
                            Video.series_id == series_id,
                            Video.episode_order == episode_order,
                        )
                    )
                    if occupied_upload_id is not None or occupied_video_id is not None:
                        raise UploadConflictError(
                            "episode order is already occupied in this Series"
                        )

                    record = UploadSession(
                        upload_session_id=str(uuid4()),
                        tenant_id=tenant_id,
                        created_by_user_id=created_by_user_id,
                        series_id=series_id,
                        episode_order=episode_order,
                        upload_sequence=episode_order,
                        original_filename=original_filename,
                        media_type=media_type,
                        total_size_bytes=total_size_bytes,
                        preferred_segment_size_bytes=preferred_segment_size_bytes,
                        total_segments=total_segments,
                        content_sha256=content_sha256,
                        parse_config=parse_config,
                        video_parse_config=video_parse_config,
                        detail={},
                        status=UploadSessionStatus.UPLOADING.value,
                        idempotency_key=idempotency_key,
                        request_fingerprint=request_fingerprint,
                        active_slot_key=_active_slot_key(series_id, episode_order),
                        expires_at=expires_at,
                        created_at=now,
                        updated_at=now,
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
        checked_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            record = session.scalar(
                select(UploadSession)
                .where(
                    UploadSession.upload_session_id == upload_session_id
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
            return session.get(Video, video_id) is not None

    def retry_parse(
        self,
        upload_session_id: str,
        *,
        tenant_id: str,
        expected_job_version: int,
        now: datetime,
        actor_user_id: str | None = None,
    ) -> VideoParseTaskState:
        """Retry the latest failed V2 task without changing its identity."""

        with self._session_factory.begin() as session:
            upload = self._locked_session(session, upload_session_id)
            if upload.tenant_id != tenant_id:
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
            if upload.video_id is None:
                raise UploadConflictError(
                    "completed upload session is missing its Video"
                )
            task_id = session.scalar(
                select(VideoParseTask.id)
                .where(
                    VideoParseTask.video_id == upload.video_id,
                    VideoParseTask.is_deleted.is_(False),
                )
                .order_by(VideoParseTask.version.desc())
                .limit(1)
            )
            if task_id is None:
                raise UploadConflictError(
                    "upload session VideoParseTask no longer exists"
                )

            # Match result commits: Interval -> Chunk -> parent. Queue claims
            # still acquire their execution row before checking the parent.
            intervals = list(session.scalars(
                select(IntervalParseTask)
                .where(IntervalParseTask.video_parse_task_id == task_id)
                .order_by(
                    IntervalParseTask.start_sequence_no,
                    IntervalParseTask.id,
                )
                .with_for_update()
            ))
            chunks = list(session.scalars(
                select(ChunkParseTask)
                .where(ChunkParseTask.video_parse_task_id == task_id)
                .order_by(ChunkParseTask.id)
                .with_for_update()
            ))
            task = session.scalar(
                select(VideoParseTask)
                .where(
                    VideoParseTask.video_id == upload.video_id,
                    VideoParseTask.is_deleted.is_(False),
                )
                .order_by(VideoParseTask.version.desc())
                .limit(1)
                .with_for_update()
            )
            if task is None or task.id != task_id:
                raise UploadConflictError(
                    "upload session VideoParseTask changed; refresh before retrying"
                )
            if task.version != expected_job_version:
                raise UploadConflictError(
                    "VideoParseTask version changed; refresh before retrying"
                )
            if _has_active_prefix_lease(task, now):
                raise UploadStateError(
                    "the VideoParseTask Prefix worker is active and cannot be retried"
                )
            if task.status not in {
                VideoParseTaskStatus.PARSING_FAILED.value,
                VideoParseTaskStatus.INDEXING_FAILED.value,
            }:
                raise UploadStateError(
                    "only a failed VideoParseTask can be retried"
                )
            if any(
                child.lease_owner is not None
                and child.lease_expire_time is not None
                and child.lease_expire_time > now
                for child in [*chunks, *intervals]
            ):
                raise UploadStateError(
                    "the VideoParseTask execution worker is active and cannot be retried"
                )

            chunk_rows = list(session.execute(
                select(ChunkParseTask, VideoChunk)
                .join(VideoChunk, VideoChunk.video_chunk_id
                      == ChunkParseTask.video_chunk_id)
                .where(ChunkParseTask.video_parse_task_id == task.id)
                .order_by(VideoChunk.sequence_no)
            ))
            prepared = _parse_preparation_complete(
                session, task, [(child, chunk) for child, chunk in chunk_rows], intervals,
            )
            if task.status == VideoParseTaskStatus.INDEXING_FAILED.value and not prepared:
                raise UploadConflictError("indexing retry requires complete preparation")

            # Restore each failed child at its own phase, including mixed failures.
            for child in [*chunks, *intervals]:
                if isinstance(child, ChunkParseTask):
                    if child.status != ChunkParseTaskStatus.FAILED.value:
                        continue
                    child.status = ChunkParseTaskStatus.QUEUED.value
                elif child.status == IntervalParseTaskStatus.PARSING_FAILED.value:
                    child.status = IntervalParseTaskStatus.WAITING.value
                elif child.status == IntervalParseTaskStatus.INDEXING_FAILED.value:
                    if session.get(IntervalParseContent, child.id) is None:
                        raise UploadConflictError("indexing retry is missing its Refine result")
                    child.status = IntervalParseTaskStatus.INDEXING.value
                else:
                    continue
                child.next_execute_time = now
                child.finish_time = None
                child.lease_owner = None
                child.lease_expire_time = None
                child.update_time = now

            if not prepared:
                task.status = VideoParseTaskStatus.PREPARING.value
            elif any(child.status != ChunkParseTaskStatus.COMPLETE.value for child in chunks) or any(
                interval.status in {
                    IntervalParseTaskStatus.WAITING.value,
                    IntervalParseTaskStatus.REFINING.value,
                } for interval in intervals
            ):
                task.status = VideoParseTaskStatus.PARSING.value
            elif any(interval.status == IntervalParseTaskStatus.INDEXING.value for interval in intervals):
                task.status = VideoParseTaskStatus.INDEXING.value
            else:
                # Completion (including videos without a full Interval) belongs to the host.
                task.status = VideoParseTaskStatus.PARSING.value
            task.max_attempts = max(task.max_attempts, task.attempt_count + 1)
            task.next_execute_time = now
            task.lease_owner = None
            task.lease_expire_time = None
            task.heartbeat_time = None
            task.finish_time = None
            detail = deepcopy(dict(task.detail or {}))
            runtime = dict(detail.get(RUNTIME_DETAIL_KEY) or {})
            runtime.pop("failure_status", None)
            if actor_user_id is None:
                runtime.pop("actor_user_id", None)
            else:
                runtime["actor_user_id"] = actor_user_id
            detail[RUNTIME_DETAIL_KEY] = runtime
            task.detail = detail
            task.update_time = now
            session.flush()
            return self._task_snapshot(task)

    def reparse(
        self,
        upload_session_id: str,
        *,
        tenant_id: str,
        expected_job_version: int,
        now: datetime,
        execution_mode: Literal["v1", "v2"] = "v1",
        video_parse_config: dict | None = None,
        snapshot_resolver: Callable[[dict], dict] | None = None,
        actor_user_id: str | None = None,
    ) -> VideoParseTaskState:
        """Create a new V2 task while retaining every prior task and result."""

        if execution_mode not in {"v1", "v2"}:
            raise ValueError("execution_mode must be 'v1' or 'v2'")

        with self._session_factory.begin() as session:
            upload = self._locked_session(session, upload_session_id)
            if upload.tenant_id != tenant_id:
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
            if upload.video_id is None:
                raise UploadConflictError(
                    "completed upload session is missing its Video"
                )
            latest = session.scalar(
                select(VideoParseTask)
                .where(
                    VideoParseTask.video_id == upload.video_id,
                    VideoParseTask.is_deleted.is_(False),
                )
                .order_by(VideoParseTask.version.desc())
                .limit(1)
            )
            if latest is None:
                raise UploadConflictError(
                    "upload session VideoParseTask no longer exists"
                )
            candidate_id = latest.id
            candidate_runtime = latest.detail.get(RUNTIME_DETAIL_KEY, {})
            realtime_only = isinstance(candidate_runtime, dict) and candidate_runtime.get("realtime_only") is True
            realtime_chunks = []
            if realtime_only:
                # Match result commits and retries: Interval -> Chunk -> parent.
                list(session.scalars(select(IntervalParseTask).where(
                    IntervalParseTask.video_parse_task_id == candidate_id,
                ).order_by(IntervalParseTask.start_sequence_no, IntervalParseTask.id).with_for_update()))
                realtime_chunks = list(session.scalars(select(ChunkParseTask).where(
                    ChunkParseTask.video_parse_task_id == candidate_id,
                ).order_by(ChunkParseTask.id).with_for_update()))
            latest = session.scalar(select(VideoParseTask).where(
                VideoParseTask.video_id == upload.video_id,
                VideoParseTask.is_deleted.is_(False),
            ).order_by(VideoParseTask.version.desc()).limit(1).with_for_update().execution_options(populate_existing=True))
            if latest is None or latest.id != candidate_id:
                raise UploadConflictError("VideoParseTask changed; refresh before reparsing")
            if latest.version != expected_job_version:
                raise UploadConflictError(
                    "VideoParseTask version changed; refresh before reparsing"
                )
            if _has_active_prefix_lease(latest, now):
                raise UploadStateError(
                    "the VideoParseTask Prefix worker is active and cannot be reparsed"
                )
            snapshot = latest.parse_config if video_parse_config is None else video_parse_config
            if snapshot_resolver is not None:
                snapshot = snapshot_resolver(deepcopy(snapshot))
            if realtime_only and snapshot != latest.parse_config:
                raise UploadConflictError(
                    "realtime base Parse freezes its configuration; promotion must use the same snapshot"
                )
            runtime = latest.detail.get("_worker_runtime_v2", {})
            accepted = isinstance(runtime, dict) and runtime.get("accepted") is True
            if (
                execution_mode == "v2"
                and (latest.status == VideoParseTaskStatus.PENDING.value or realtime_only)
                and not accepted
            ):
                video = session.get(Video, upload.video_id)
                if video is None:
                    raise UploadConflictError(
                        "upload session Video no longer exists"
                    )
                latest.chunk_count = _require_compatible_video_topology(
                    session,
                    video,
                    snapshot,
                )
                execution = snapshot.get("execution")
                max_retry_count = (
                    execution.get("max_retry_count")
                    if isinstance(execution, dict)
                    else None
                )
                if (
                    isinstance(max_retry_count, bool)
                    or not isinstance(max_retry_count, int)
                    or max_retry_count < 0
                ):
                    raise UploadConflictError(
                        "VideoParseTask parse_config has invalid max_retry_count"
                    )
                latest.parse_config = snapshot
                latest.max_attempts = max_retry_count + 1
                detail = dict(latest.detail)
                existing_runtime = detail.get("_worker_runtime_v2")
                runtime = (
                    dict(existing_runtime)
                    if isinstance(existing_runtime, dict)
                    else {}
                )
                runtime.pop("actor_user_id", None)
                runtime.update({
                    **({"actor_user_id": actor_user_id} if actor_user_id is not None else {}),
                    "accepted": True,
                    "execution_mode": "v2",
                    **({"realtime_only": False} if realtime_only else {}),
                })
                runtime.pop("failure_status", None)
                detail["_worker_runtime_v2"] = runtime
                latest.detail = detail
                if realtime_only:
                    for child in realtime_chunks:
                        if child.status != ChunkParseTaskStatus.FAILED.value:
                            continue
                        child.status = ChunkParseTaskStatus.QUEUED.value
                        child.retry_count = 0
                        child.next_execute_time = now
                        child.finish_time = None
                        child.last_error = None
                        child.lease_owner = None
                        child.lease_expire_time = None
                        child.update_time = now
                    active_topology = (
                        latest.lease_owner is not None
                        and latest.lease_expire_time is not None
                        and latest.lease_expire_time > now
                    )
                    latest.status = (VideoParseTaskStatus.PREPARING.value if active_topology
                                     else VideoParseTaskStatus.PENDING.value)
                    latest.finish_time = None
                    latest.last_error = None
                    # A live topology lease retains its fencing token. Once it
                    # finishes it observes accepted=true and activates Parse.
                    if latest.lease_expire_time is None or latest.lease_expire_time <= now:
                        latest.lease_owner = None
                        latest.lease_expire_time = None
                        latest.heartbeat_time = None
                latest.next_execute_time = now
                latest.update_time = now
                _invalidate_series_prefixes_for_reparse(
                    session,
                    video=video,
                    now=now,
                )
                session.flush()
                return self._task_snapshot(latest)
            terminal_statuses = {
                VideoParseTaskStatus.COMPLETE.value,
                VideoParseTaskStatus.PARSING_FAILED.value,
                VideoParseTaskStatus.INDEXING_FAILED.value,
            }
            if latest.status not in terminal_statuses:
                raise UploadStateError(
                    "only a complete or failed VideoParseTask can be reparsed"
                )
            video = session.get(Video, upload.video_id)
            if video is None:
                raise UploadConflictError("upload session Video no longer exists")
            chunk_count = _require_compatible_video_topology(
                session,
                video,
                snapshot,
            )
            execution = snapshot.get("execution")
            if not isinstance(execution, dict):
                raise UploadConflictError(
                    "VideoParseTask parse_config is missing execution settings"
                )
            max_retry_count = execution.get("max_retry_count")
            if (
                isinstance(max_retry_count, bool)
                or not isinstance(max_retry_count, int)
                or max_retry_count < 0
            ):
                raise UploadConflictError(
                    "VideoParseTask parse_config has invalid max_retry_count"
                )
            task = VideoParseTask(
                id=str(uuid4()),
                created_by_user_id=actor_user_id,
                video_id=video.video_id,
                pipeline_version=latest.pipeline_version,
                status=VideoParseTaskStatus.PENDING.value,
                parse_config=snapshot,
                chunk_count=chunk_count,
                version=latest.version + 1,
                next_execute_time=now,
                attempt_count=0,
                max_attempts=max_retry_count + 1,
                lease_version=0,
                processing_duration_ms=0,
                processing_timing_complete=True,
                is_test=latest.is_test,
                is_deleted=False,
                detail={
                    "_worker_runtime_v2": {
                        **({"actor_user_id": actor_user_id} if actor_user_id is not None else {}),
                        "accepted": execution_mode == "v2",
                        "execution_mode": execution_mode,
                    }
                },
                create_time=now,
                update_time=now,
            )
            session.add(task)
            session.flush()
            _invalidate_series_prefixes_for_reparse(
                session,
                video=video,
                now=now,
            )
            session.flush()
            return self._task_snapshot(task)

    def list_pending_deletions(
        self,
        *,
        limit: int = 100,
    ) -> list[tuple[str, str]]:
        with self._session_factory() as session:
            return list(session.execute(
                select(
                    UploadSession.upload_session_id,
                    UploadSession.tenant_id,
                )
                .where(
                    UploadSession.last_error_code
                    == UPLOAD_DELETION_PENDING_ERROR_CODE,
                )
                .order_by(
                    UploadSession.updated_at,
                    UploadSession.upload_session_id,
                )
                .limit(limit)
            ))

    def list_segments(self, upload_session_id: str) -> list[UploadSegmentState]:
        with self._session_factory() as session:
            if session.get(UploadSession, upload_session_id) is None:
                raise UploadSessionNotFoundError(
                    f"uploadSessionId {upload_session_id!r} does not exist"
                )
            records = session.scalars(
                select(UploadSegment)
                .where(
                    UploadSegment.upload_session_id == upload_session_id
                )
                .order_by(UploadSegment.segment_number)
            ).all()
            return [self._segment_snapshot(item) for item in records]

    def begin_delete_session(
        self,
        upload_session_id: str,
        *,
        tenant_id: str,
        now: datetime,
    ) -> UploadSessionDeletion:
        with self._session_factory.begin() as session:
            upload = self._locked_session(session, upload_session_id)
            if upload.tenant_id != tenant_id:
                raise UploadSessionNotFoundError("upload session does not exist in this workspace")
            if upload.status == UploadSessionStatus.COMPLETING.value:
                raise UploadCompletionInProgressError("upload completion is already in progress")

            video_id = upload.video_id
            job_id = upload.job_id
            if video_id is not None or job_id is not None:
                shared_upload = session.scalar(
                    select(UploadSession.upload_session_id)
                    .where(
                        UploadSession.upload_session_id != upload_session_id,
                        or_(
                            UploadSession.video_id == video_id
                            if video_id is not None else False,
                            UploadSession.job_id == job_id
                            if job_id is not None else False,
                        ),
                    )
                    .with_for_update()
                )
                if shared_upload is not None:
                    raise UploadConflictError(
                        "another upload session references this task's Video or ParseJob"
                    )

            jobs: list[SeriesParseJob] = []
            if video_id is not None:
                jobs = list(session.scalars(
                    select(SeriesParseJob)
                    .where(SeriesParseJob.video_id == video_id)
                    .order_by(SeriesParseJob.job_id)
                    .with_for_update()
                ))
            elif job_id is not None:
                job = session.scalar(
                    select(SeriesParseJob)
                    .where(SeriesParseJob.job_id == job_id)
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
                    legacy_now = datetime_to_unix_time_ms(now)
                    job.completed_at = legacy_now
                    job.last_error = "upload task deletion requested"
                    job.version += 1
                    job.updated_at = legacy_now

            v2_tasks: list[VideoParseTask] = []
            if video_id is not None:
                v2_tasks = list(session.scalars(
                    select(VideoParseTask)
                    .where(VideoParseTask.video_id == video_id)
                    .order_by(VideoParseTask.version)
                    .with_for_update()
                ))
                v2_task_ids = [task.id for task in v2_tasks]
                active_child_lease = False
                if v2_task_ids:
                    active_child_lease = session.scalar(
                        select(ChunkParseTask.id)
                        .where(
                            ChunkParseTask.video_parse_task_id.in_(v2_task_ids),
                            ChunkParseTask.lease_owner.is_not(None),
                            ChunkParseTask.lease_expire_time > now,
                        )
                        .limit(1)
                    ) is not None or session.scalar(
                        select(IntervalParseTask.id)
                        .where(
                            IntervalParseTask.video_parse_task_id.in_(v2_task_ids),
                            IntervalParseTask.lease_owner.is_not(None),
                            IntervalParseTask.lease_expire_time > now,
                        )
                        .limit(1)
                    ) is not None
                active_main_lease = any(
                    task.lease_owner is not None
                    and task.lease_expire_time is not None
                    and task.lease_expire_time > now
                    for task in v2_tasks
                )
                active_prefix_lease = any(
                    _has_active_prefix_lease(task, now) for task in v2_tasks
                )
                if active_main_lease or active_child_lease or active_prefix_lease:
                    raise UploadStateError(
                        "the upload task is being parsed and cannot be deleted"
                    )
                # The external artifact cleanup happens after this transaction.
                # Fence every V2 claim during that gap; the final Video delete
                # removes these rows through their existing cascade.
                for task in v2_tasks:
                    task.is_deleted = True
                    task.update_time = now
            if jobs and video_id is not None:
                video = session.get(Video, video_id)
                if video is not None:
                    video.parse_status = ParseJobStatus.FAILED.value

            job_ids = tuple(sorted(
                {job.job_id for job in jobs}
                | ({job_id} if job_id is not None else set())
            ))

            video_chunk_ids: set[str] = set()
            video_parse_document_keys: set[tuple[str, str]] = set()
            if video_id is not None:
                video_chunk_ids.update(session.scalars(
                    select(VideoChunk.video_chunk_id).where(
                        VideoChunk.video_id == video_id
                    )
                ))
            if v2_tasks:
                video_parse_document_keys.update(session.execute(
                    select(
                        ChunkParseTask.video_parse_task_id,
                        ChunkParseTask.video_chunk_id,
                    ).where(
                        ChunkParseTask.video_parse_task_id.in_(
                            [task.id for task in v2_tasks]
                        )
                    )
                ).all())
            if job_ids:
                video_chunk_ids.update(session.scalars(
                    select(VideoChunkCheckpoint.video_chunk_id).where(
                        VideoChunkCheckpoint.job_id.in_(job_ids)
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
            removed = session.execute(delete(UploadSegment).where(
                UploadSegment.upload_session_id == upload_session_id
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
                video_parse_document_keys=tuple(
                    sorted(video_parse_document_keys)
                ),
            )

    def finish_delete_session(
        self,
        deletion: UploadSessionDeletion,
        *,
        now: datetime,
    ) -> UploadSessionState:
        with self._session_factory.begin() as session:
            upload = self._locked_session(
                session,
                deletion.session.upload_session_id,
            )
            if upload.status != UploadSessionStatus.EXPIRED.value:
                raise UploadStateError("upload session deletion is no longer current")
            if (
                upload.video_id is None
                and upload.job_id is None
                and upload.initial_video_parse_task_id is None
            ):
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
                session.execute(delete(ParseModelCall).where(
                    ParseModelCall.job_id.in_(deletion.job_ids)
                ))
                session.execute(delete(ParseUsageAttempt).where(
                    ParseUsageAttempt.job_id.in_(deletion.job_ids)
                ))
                session.execute(delete(VideoChunkCheckpoint).where(
                    VideoChunkCheckpoint.job_id.in_(deletion.job_ids)
                ))
                session.execute(delete(SeriesParseJob).where(
                    SeriesParseJob.job_id.in_(deletion.job_ids)
                ))
            upload.job_id = None
            upload.video_id = None
            upload.initial_video_parse_task_id = None
            session.flush()
            if deletion.video_id is not None:
                session.execute(delete(ContextSnapshot).where(
                    ContextSnapshot.video_id == deletion.video_id
                ))
                if deletion.video_chunk_ids:
                    chunk_ids = deletion.video_chunk_ids
                    session.execute(delete(IndexSyncIntent).where(
                        IndexSyncIntent.video_chunk_id.in_(chunk_ids)
                    ))
                    session.execute(delete(VideoChunkInfo).where(
                        VideoChunkInfo.video_chunk_id.in_(chunk_ids)
                    ))
                    session.execute(delete(VideoChunkEmbedding).where(
                        VideoChunkEmbedding.video_chunk_id.in_(chunk_ids)
                    ))
                session.execute(delete(VideoChunk).where(
                    VideoChunk.video_id == deletion.video_id
                ))
                session.execute(delete(Video).where(
                    Video.video_id == deletion.video_id
                ))
            upload.version += 1
            upload.updated_at = now
            upload.last_error_code = None
            session.flush()

            remaining_video_count = int(session.scalar(
                select(func.count(Video.video_id)).where(
                    Video.series_id == upload.series_id
                )
            ) or 0)
            if remaining_video_count:
                refresh_series_statuses(session, {upload.series_id})
            else:
                series = session.scalar(
                    select(Series)
                    .where(Series.series_id == upload.series_id)
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
        now: datetime | None = None,
    ) -> UploadSegmentState:
        registered_at = now if now is not None else now_utc()
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
                    UploadSegment,
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
                    record = UploadSegment(
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
        claimed_at = now if now is not None else now_utc()
        expired = False
        result: UploadCompletionClaim | None = None
        with self._session_factory.begin() as session:
            upload = self._locked_session(session, upload_session_id)
            if upload.status == UploadSessionStatus.COMPLETED.value:
                job_id = upload.job_id
                if job_id is None and upload.video_id is not None:
                    job_id = session.scalar(
                        select(SeriesParseJob.job_id).where(
                            SeriesParseJob.video_id == upload.video_id
                        )
                    )
                result = UploadCompletionClaim(
                    session=self._snapshot(session, upload),
                    segments=tuple(self._segment_snapshots(session, upload)),
                    completion_token=None,
                    existing_job_id=job_id,
                    existing_video_parse_task_id=(
                        upload.initial_video_parse_task_id
                    ),
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
                    existing_video_parse_task_id=None,
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

    def list_for_series(self, series_id: str, *, tenant_id: str, status: str | None = None) -> list[UploadSessionState]:
        return self.list_for_workspace(tenant_id, series_id=series_id, status=status)

    def list_for_workspace(self, tenant_id: str, *, series_id: str | None = None, status: str | None = None) -> list[UploadSessionState]:
        if status == "all":
            statuses = None
        elif status is None:
            statuses = {UploadSessionStatus.UPLOADING.value, UploadSessionStatus.COMPLETING.value}
        else:
            statuses = {UploadSessionStatus(value.strip()).value for value in status.split(",")}
        with self._session_factory.begin() as session:
            statement = select(UploadSession).where(UploadSession.tenant_id == tenant_id)
            if series_id is not None:
                statement = statement.where(UploadSession.series_id == series_id)
            records = session.scalars(statement.order_by(UploadSession.episode_order, UploadSession.created_at, UploadSession.upload_session_id).with_for_update()).all()
            segments_by_upload: dict[str, list[tuple[int, int]]] = {}
            if records:
                for upload_id, number, size in session.execute(
                    select(UploadSegment.upload_session_id, UploadSegment.segment_number, UploadSegment.actual_size_bytes)
                    .where(UploadSegment.upload_session_id.in_([record.upload_session_id for record in records]))
                ):
                    segments_by_upload.setdefault(upload_id, []).append((number, size))
            result = []
            checked_at = now_utc()
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
                update(UploadSession)
                .where(
                    UploadSession.upload_session_id == upload_session_id,
                    UploadSession.status
                    == UploadSessionStatus.COMPLETING.value,
                    UploadSession.completion_token == completion_token,
                )
                .values(
                    status=UploadSessionStatus.UPLOADING.value,
                    completion_token=None,
                    completion_started_at=None,
                    last_error=summary,
                    last_error_code=error_code,
                    completion_stage=None,
                    version=UploadSession.version + 1,
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
        duration_seconds: int,
        pipeline_version: str,
        max_job_attempts: int,
        video_parse_config: dict,
        max_video_parse_attempts: int,
        execution_mode: Literal["v1", "v2"] = "v1",
        now: datetime | None = None,
    ) -> CompletedUploadState:
        if (
            isinstance(max_video_parse_attempts, bool)
            or not isinstance(max_video_parse_attempts, int)
            or max_video_parse_attempts <= 0
        ):
            raise ValueError("max_video_parse_attempts must be a positive integer")
        if execution_mode not in {"v1", "v2"}:
            raise ValueError("execution_mode must be 'v1' or 'v2'")
        completed_at = now if now is not None else now_utc()
        legacy_completed_at = datetime_to_unix_time_ms(completed_at)
        job_id = (
            derive_parse_job_id(video_id, pipeline_version)
            if execution_mode == "v1"
            else None
        )
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
                select(Video).where(
                    Video.series_id == upload.series_id,
                    or_(
                        Video.episode_order == upload.episode_order,
                        Video.content_hash == content_hash,
                    ),
                )
            )
            if conflict is not None and conflict.video_id != video_id:
                raise UploadConflictError(
                    "episode order or source content already belongs to another Video"
                )
            video = session.get(Video, video_id)
            if video is None:
                video = Video(
                    video_id=video_id,
                    series_id=upload.series_id,
                    episode_order=upload.episode_order,
                    upload_sequence=upload.upload_sequence,
                    original_filename=upload.original_filename,
                    original_source_uri=original_source_uri,
                    inference_proxy_uri=None,
                    content_hash=content_hash,
                    total_bytes=upload.total_size_bytes,
                    duration_seconds=duration_seconds,
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
                or video.duration_seconds != duration_seconds
                or video.original_source_uri != original_source_uri
            ):
                raise UploadConflictError(
                    f"videoId {video_id!r} already identifies another Video"
                )

            session.flush()
            chunk_count = _require_compatible_video_topology(
                session,
                video,
                video_parse_config,
            )

            video_parse_task_id = str(uuid4())
            video_parse_task = VideoParseTask(
                id=video_parse_task_id,
                created_by_user_id=upload.created_by_user_id,
                video_id=video_id,
                pipeline_version=pipeline_version,
                status=VideoParseTaskStatus.PENDING.value,
                parse_config=video_parse_config,
                chunk_count=chunk_count,
                version=1,
                next_execute_time=completed_at,
                attempt_count=0,
                max_attempts=max_video_parse_attempts,
                lease_version=0,
                processing_duration_ms=0,
                processing_timing_complete=True,
                is_test=False,
                is_deleted=False,
                detail={
                    "_worker_runtime_v2": {
                        **({"actor_user_id": upload.created_by_user_id} if upload.created_by_user_id is not None else {}),
                        "accepted": execution_mode == "v2",
                        "execution_mode": execution_mode,
                    }
                },
                create_time=completed_at,
                update_time=completed_at,
            )
            session.add(video_parse_task)
            session.flush()

            if job_id is not None:
                job = session.get(SeriesParseJob, job_id)
                if job is None:
                    job = SeriesParseJob(
                        job_id=job_id,
                        created_by_user_id=upload.created_by_user_id,
                        series_id=upload.series_id,
                        video_id=video_id,
                        pipeline_version=pipeline_version,
                        parse_config=upload.parse_config,
                        status=ParseJobStatus.QUEUED.value,
                        stage=ParseJobStage.QUEUED.value,
                        progress=0,
                        attempt_count=0,
                        max_attempts=max_job_attempts,
                        available_at=legacy_completed_at,
                        lease_version=0,
                    )
                    session.add(job)

            series = session.get(Series, upload.series_id)
            if series is None:
                raise UploadSeriesNotFoundError(
                    f"seriesId {upload.series_id!r} disappeared during completion"
                )
            upload.status = UploadSessionStatus.COMPLETED.value
            upload.video_id = video_id
            upload.completed_at = completed_at
            upload.completion_token = None
            upload.completion_started_at = None
            upload.last_error = None
            upload.last_error_code = None
            upload.content_sha256 = content_hash
            upload.job_id = job_id
            upload.initial_video_parse_task_id = video_parse_task_id
            upload.video_parse_config = video_parse_config
            upload.completion_stage = None
            upload.version += 1
            upload.updated_at = completed_at
            session.flush()
            if series.status != "READY":
                refresh_series_statuses(session, {upload.series_id})
            return CompletedUploadState(
                session=self._snapshot(session, upload),
                video_id=video_id,
                job_id=job_id,
                video_parse_task_id=video_parse_task_id,
            )

    @staticmethod
    def _locked_session(
        session: Session,
        upload_session_id: str,
    ) -> UploadSession:
        record = session.scalar(
            select(UploadSession)
            .where(UploadSession.upload_session_id == upload_session_id)
            .with_for_update()
        )
        if record is None:
            raise UploadSessionNotFoundError(
                f"uploadSessionId {upload_session_id!r} does not exist"
            )
        return record

    @staticmethod
    def _require_uploading(upload: UploadSession) -> None:
        if upload.status != UploadSessionStatus.UPLOADING.value:
            raise UploadStateError(
                f"upload session does not accept segments in {upload.status}"
            )

    @staticmethod
    def _expire_if_due(upload: UploadSession, now: datetime) -> bool:
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
        upload: UploadSession,
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
        upload: UploadSession,
        *,
        segments: list[tuple[int, int]] | None = None,
    ) -> UploadSessionState:
        if segments is None:
            segments = session.execute(
                select(UploadSegment.segment_number, UploadSegment.actual_size_bytes).where(
                    UploadSegment.upload_session_id == upload.upload_session_id
                )
            ).all()
        present = {number for number, _size in segments}
        return UploadSessionState(
            upload_session_id=upload.upload_session_id,
            tenant_id=upload.tenant_id,
            created_by_user_id=upload.created_by_user_id,
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
            initial_video_parse_task_id=upload.initial_video_parse_task_id,
            parse_config=upload.parse_config,
            video_parse_config=upload.video_parse_config,
            detail=upload.detail,
            version=upload.version,
            missing_segment_numbers=tuple(n for n in range(upload.total_segments) if n not in present),
            last_error_code=upload.last_error_code,
        )

    @staticmethod
    def _task_snapshot(task: VideoParseTask) -> VideoParseTaskState:
        return VideoParseTaskState(
            video_parse_task_id=task.id,
            created_by_user_id=task.created_by_user_id,
            video_id=task.video_id,
            pipeline_version=task.pipeline_version,
            status=task.status,
            parse_config=dict(task.parse_config),
            chunk_count=task.chunk_count,
            version=task.version,
            next_execute_time=task.next_execute_time,
            attempt_count=task.attempt_count,
            max_attempts=task.max_attempts,
            lease_owner=task.lease_owner,
            lease_version=task.lease_version,
            lease_expire_time=task.lease_expire_time,
            heartbeat_time=task.heartbeat_time,
            start_time=task.start_time,
            finish_time=task.finish_time,
            processing_duration_ms=task.processing_duration_ms,
            processing_timing_complete=task.processing_timing_complete,
            last_error=task.last_error,
            is_test=task.is_test,
            detail=dict(task.detail),
            create_time=task.create_time,
            update_time=task.update_time,
        )

    @classmethod
    def _segment_snapshots(
        cls,
        session: Session,
        upload: UploadSession,
    ) -> list[UploadSegmentState]:
        records = session.scalars(
            select(UploadSegment)
            .where(
                UploadSegment.upload_session_id
                == upload.upload_session_id
            )
            .order_by(UploadSegment.segment_number)
        ).all()
        return [cls._segment_snapshot(item) for item in records]

    @staticmethod
    def _segment_snapshot(record: UploadSegment) -> UploadSegmentState:
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
        record: UploadSession,
        requested: dict[str, object],
        *,
        request_fingerprint: str,
    ) -> None:
        if record.request_fingerprint is not None:
            if record.request_fingerprint == request_fingerprint:
                return
            # Historical rows predate both episode_order and the V2 snapshot in
            # the fingerprint.  They are replayable only with their persisted
            # order; changing that value is always a conflict.
            if (
                requested.get("episode_order") == record.episode_order
                and "video_parse_config" not in requested
            ):
                legacy_fields = dict(requested)
                legacy_fields.pop("episode_order", None)
                legacy_fingerprint = hashlib.sha256(
                    json.dumps(
                        legacy_fields,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
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


def _invalidate_series_prefixes_for_reparse(
    session: Session,
    *,
    video: Video,
    now: datetime,
) -> None:
    try:
        invalidate_series_prefixes_from(
            session,
            series_id=video.series_id,
            episode_order=video.episode_order,
            video_id=video.video_id,
            now=now,
        )
    except VideoParseConflictError as exc:
        raise UploadStateError(str(exc)) from exc


def _has_active_prefix_lease(
    task: VideoParseTask,
    now: datetime,
) -> bool:
    detail = task.detail
    if not isinstance(detail, dict):
        return False
    runtime = detail.get("_worker_runtime_v2")
    if not isinstance(runtime, dict):
        return False
    prefix = runtime.get("prefix")
    if not isinstance(prefix, dict) or prefix.get("lease_owner") is None:
        return False
    expires_at_ms = prefix.get("lease_expire_time_ms")
    return (
        isinstance(expires_at_ms, int)
        and not isinstance(expires_at_ms, bool)
        and expires_at_ms > datetime_to_unix_time_ms(now)
    )


def _planned_chunk_count(duration_seconds: int, parse_config: dict) -> int:
    if (
        isinstance(duration_seconds, bool)
        or not isinstance(duration_seconds, int)
        or duration_seconds <= 0
    ):
        raise UploadConflictError("Video duration must be a positive integer")
    chunk_duration = parse_config.get("chunk_duration")
    if (
        isinstance(chunk_duration, bool)
        or not isinstance(chunk_duration, int)
        or chunk_duration <= 0
    ):
        raise UploadConflictError(
            "VideoParseTask parse_config has invalid chunk_duration"
        )
    # The stored media duration is milliseconds; task configuration stays seconds.
    return math.ceil(duration_seconds / (chunk_duration * 1000))


def _require_compatible_video_topology(
    session: Session,
    video: Video,
    parse_config: dict,
    *,
    allow_incomplete: bool = False,
) -> int:
    chunk_count = _planned_chunk_count(video.duration_seconds, parse_config)
    chunk_duration = parse_config["chunk_duration"] * 1000
    topology = session.execute(
        select(
            VideoChunk.sequence_no,
            VideoChunk.start_seconds,
            VideoChunk.end_seconds,
        )
        .where(VideoChunk.video_id == video.video_id)
        .order_by(VideoChunk.sequence_no)
    ).all()
    if not topology:
        return chunk_count
    expected = [
        (
            sequence_no,
            sequence_no * chunk_duration,
            min(video.duration_seconds, (sequence_no + 1) * chunk_duration),
        )
        for sequence_no in range(chunk_count)
    ]
    actual = [tuple(row) for row in topology]
    matches = set(actual).issubset(expected) if allow_incomplete else actual == expected
    if not matches:
        raise UploadConflictError(
            "V2 parse config is incompatible with the Video's fixed chunk topology"
        )
    return chunk_count


def _parse_preparation_complete(
    session: Session,
    task: VideoParseTask,
    chunk_rows: list[tuple[ChunkParseTask, VideoChunk]],
    intervals: list[IntervalParseTask],
) -> bool:
    """Check the persisted preparation before choosing a retry entry point."""

    video = session.get(Video, task.video_id)
    if video is None:
        raise UploadConflictError("VideoParseTask Video no longer exists")
    chunk_count = _require_compatible_video_topology(
        session, video, task.parse_config, allow_incomplete=True,
    )
    if task.chunk_count != chunk_count or len(chunk_rows) > chunk_count:
        raise UploadConflictError("VideoParseTask has incompatible chunk preparation")
    for child, chunk in chunk_rows:
        if chunk.video_id != task.video_id:
            raise UploadConflictError("VideoParseTask has a Chunk from another Video")
    if len(chunk_rows) < chunk_count:
        return False

    interval_size = math.ceil(
        task.parse_config["interval_duration"] / task.parse_config["chunk_duration"]
    )
    expected_intervals = {
        plan.start_sequence_no: plan.chunk_count
        for plan in plan_intervals(chunk_count=chunk_count, size=interval_size)
    }
    for interval in intervals:
        start = interval.start_sequence_no
        expected_count = expected_intervals.get(start)
        if expected_count is None or interval.chunk_count != expected_count:
            raise UploadConflictError("VideoParseTask has incompatible Interval preparation")
        try:
            validate_interval_parse_task_members(
                chunk_parse_task_id=interval.chunk_parse_task_id,
                video_parse_task_id=task.id,
                start_sequence_no=start,
                chunk_count=expected_count,
                chunk_parse_task_ids=interval.chunk_parse_task_ids,
                members=[
                    (child, chunk.sequence_no)
                    for child, chunk in chunk_rows[start:start + expected_count]
                ],
            )
        except ValueError as exc:
            raise UploadConflictError("VideoParseTask has incompatible Interval members") from exc
    return len(intervals) == len(expected_intervals)
