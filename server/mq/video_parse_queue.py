from __future__ import annotations

import math
from copy import deepcopy
from datetime import datetime, timedelta

from sqlalchemy import func, literal, select, tuple_, update
from sqlalchemy.orm import Session, sessionmaker

from ..model.dto.jobDTO import (
    ChunkScheduleEntryState,
    ChunkScheduleState,
    ChunkParseLease,
    IntervalParseLease,
    PrefixParseLease,
    VideoParseLease,
)
from ..model.job import (
    ChunkParseContent,
    ChunkParseTask,
    ChunkParseTaskStatus,
    IntervalParseContent,
    IntervalParseTask,
    IntervalParseTaskStatus,
    VideoParseTask,
    VideoParseTaskStatus,
    interval_full_ready_mask,
)
from ..model.video import VideoChunk
from ..repository.video_parse import (
    RUNTIME_DETAIL_KEY,
    _REFINE_SUBMITTED,
    _aggregate_video_task,
    _ensure_runtime,
    _error_detail,
    _execution_config,
    _indexed_versions,
    _latest_video_task_id,
    _positive_int,
    _submitted_intervals_by_member,
    _video_failure_status,
)
from ..utils.time import datetime_to_unix_time_ms, now_utc
from ..utils.validation import require_nonempty_text


class MySqlVideoParseQueue:
    """DB-backed V2 delivery with independent phase leases and fencing."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def claim_realtime_preparation(
        self, video_parse_task_id: str, worker_id: str, *,
        lease_duration: timedelta, now: datetime | None = None,
    ) -> VideoParseLease | None:
        """Claim one fixed topology without accepting the full Parse pipeline."""

        worker = require_nonempty_text(worker_id, "worker_id")
        _require_lease_duration(lease_duration)
        claimed_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            task = session.scalar(select(VideoParseTask).where(
                VideoParseTask.id == video_parse_task_id,
                VideoParseTask.is_deleted.is_(False),
                VideoParseTask.status.in_([
                    VideoParseTaskStatus.PENDING.value, VideoParseTaskStatus.PREPARING.value,
                ]),
                (VideoParseTask.lease_owner.is_(None)
                 | (VideoParseTask.lease_expire_time <= claimed_at)),
            ).with_for_update(skip_locked=True))
            if task is None or _latest_video_task_id(session, task.video_id) != task.id:
                return None
            runtime = _ensure_runtime(task.detail)[RUNTIME_DETAIL_KEY]
            if not _accepted(task) and runtime.get("realtime_only") is not True:
                return None
            task.status = VideoParseTaskStatus.PREPARING.value
            task.lease_owner = worker
            task.lease_version += 1
            task.lease_expire_time = claimed_at + lease_duration
            task.heartbeat_time = claimed_at
            task.update_time = claimed_at
            session.flush()
            return VideoParseLease(
                video_parse_task_id=task.id, video_id=task.video_id, worker_id=worker,
                lease_version=task.lease_version, lease_expire_time=task.lease_expire_time,
                attempt_count=task.attempt_count, max_attempts=task.max_attempts,
                parse_config=deepcopy(task.parse_config),
            )

    def claim_realtime_chunk(
        self, chunk_parse_task_id: str, worker_id: str, *,
        lease_duration: timedelta, now: datetime | None = None,
    ) -> ChunkParseLease | None:
        """Prioritize the requested Chunk while sharing normal lease capacity.

        Realtime bypasses the full scan's ordering gate, never its concurrency,
        retry, latest-task or fencing rules.
        """

        worker = require_nonempty_text(worker_id, "worker_id")
        _require_lease_duration(lease_duration)
        claimed_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            child = session.scalar(select(ChunkParseTask).where(
                ChunkParseTask.id == chunk_parse_task_id,
                ChunkParseTask.status == ChunkParseTaskStatus.QUEUED.value,
                ChunkParseTask.next_execute_time <= claimed_at,
            ).with_for_update(skip_locked=True))
            if child is None:
                return None
            parent = session.scalar(select(VideoParseTask).where(
                VideoParseTask.id == child.video_parse_task_id,
            ).with_for_update())
            if parent is None or parent.is_deleted or _latest_video_task_id(session, parent.video_id) != parent.id:
                return None
            runtime = _ensure_runtime(parent.detail)[RUNTIME_DETAIL_KEY]
            allowed = (
                _accepted(parent) and parent.status == VideoParseTaskStatus.PARSING.value
            ) or (
                not _accepted(parent) and runtime.get("realtime_only") is True
                and parent.status == VideoParseTaskStatus.PENDING.value
            )
            if not allowed or _video_failure_status(parent) is not None:
                return None
            active = int(session.scalar(select(func.count(ChunkParseTask.id)).where(
                ChunkParseTask.video_parse_task_id == parent.id,
                ChunkParseTask.status == ChunkParseTaskStatus.UNDERSTANDING.value,
            )) or 0)
            if active >= _positive_int(parent.parse_config.get("batch_max_size"), "batch_max_size"):
                return None
            child.status = ChunkParseTaskStatus.UNDERSTANDING.value
            child.lease_owner = worker
            child.lease_version += 1
            child.lease_expire_time = claimed_at + lease_duration
            child.start_time = child.start_time or claimed_at
            child.finish_time = None
            child.last_error = None
            child.update_time = claimed_at
            child.detail = _set_dispatch_requested(child.detail, False)
            session.flush()
            return ChunkParseLease(
                chunk_parse_task_id=child.id, video_parse_task_id=parent.id,
                video_chunk_id=child.video_chunk_id, worker_id=worker,
                lease_version=child.lease_version, lease_expire_time=child.lease_expire_time,
                retry_count=child.retry_count,
                max_retry_count=_nonnegative_int(_execution_config(parent.parse_config).get("max_retry_count"), "max_retry_count"),
            )

    def claim_video(
        self,
        worker_id: str,
        *,
        lease_duration: timedelta,
        now: datetime | None = None,
    ) -> VideoParseLease | None:
        worker = require_nonempty_text(worker_id, "worker_id")
        _require_lease_duration(lease_duration)
        claimed_at = now if now is not None else now_utc()
        expires_at = claimed_at + lease_duration
        cursor: tuple[datetime, datetime, str] | None = None
        while True:
            with self._session_factory() as session:
                ordering = (
                    VideoParseTask.next_execute_time,
                    VideoParseTask.create_time,
                    VideoParseTask.id,
                )
                statement = (
                    select(*ordering)
                    .where(
                        VideoParseTask.status.in_([
                            VideoParseTaskStatus.PENDING.value,
                            VideoParseTaskStatus.PREPARING.value,
                            VideoParseTaskStatus.PARSING.value,
                            VideoParseTaskStatus.INDEXING.value,
                        ]),
                        VideoParseTask.lease_owner.is_(None),
                        VideoParseTask.next_execute_time <= claimed_at,
                        VideoParseTask.attempt_count
                        < VideoParseTask.max_attempts,
                        VideoParseTask.is_deleted.is_(False),
                        VideoParseTask.detail[RUNTIME_DETAIL_KEY]["accepted"]
                        .as_boolean()
                        .is_(True),
                    )
                    .order_by(*ordering)
                    .limit(100)
                )
                if cursor is not None:
                    statement = statement.where(tuple_(*ordering) > cursor)
                candidates = list(session.execute(statement))
            if not candidates:
                return None
            for _next_execute, _created, candidate_id in candidates:
                with self._session_factory.begin() as session:
                    task = session.scalar(
                        select(VideoParseTask)
                        .where(
                            VideoParseTask.id == candidate_id,
                            VideoParseTask.status.in_([
                                VideoParseTaskStatus.PENDING.value,
                                VideoParseTaskStatus.PREPARING.value,
                                VideoParseTaskStatus.PARSING.value,
                                VideoParseTaskStatus.INDEXING.value,
                            ]),
                            VideoParseTask.lease_owner.is_(None),
                            VideoParseTask.next_execute_time <= claimed_at,
                            VideoParseTask.attempt_count
                            < VideoParseTask.max_attempts,
                            VideoParseTask.is_deleted.is_(False),
                        )
                        .with_for_update(skip_locked=True)
                    )
                    if task is None or not _accepted(task) or _video_failure_status(task):
                        continue
                    if task.status == VideoParseTaskStatus.PENDING.value:
                        task.status = VideoParseTaskStatus.PREPARING.value
                    task.attempt_count += 1
                    task.lease_owner = worker
                    task.lease_version += 1
                    task.lease_expire_time = expires_at
                    task.heartbeat_time = claimed_at
                    task.start_time = task.start_time or claimed_at
                    task.finish_time = None
                    task.last_error = None
                    task.update_time = claimed_at
                    session.flush()
                    return VideoParseLease(
                        video_parse_task_id=task.id,
                        video_id=task.video_id,
                        worker_id=worker,
                        lease_version=task.lease_version,
                        lease_expire_time=expires_at,
                        attempt_count=task.attempt_count,
                        max_attempts=task.max_attempts,
                        parse_config=deepcopy(dict(task.parse_config)),
                    )
            cursor = tuple(candidates[-1])

    def claim_chunk(
        self,
        worker_id: str,
        *,
        lease_duration: timedelta,
        video_parse_task_id: str | None = None,
        now: datetime | None = None,
    ) -> ChunkParseLease | None:
        worker = require_nonempty_text(worker_id, "worker_id")
        _require_lease_duration(lease_duration)
        claimed_at = now if now is not None else now_utc()
        expires_at = claimed_at + lease_duration
        cursor: tuple[datetime, int, str] | None = None
        while True:
            with self._session_factory() as session:
                ordering = (
                    VideoParseTask.create_time,
                    VideoChunk.sequence_no,
                    ChunkParseTask.id,
                )
                statement = (
                    select(*ordering)
                    .join(
                        VideoChunk,
                        VideoChunk.video_chunk_id
                        == ChunkParseTask.video_chunk_id,
                    )
                    .join(
                        VideoParseTask,
                        VideoParseTask.id
                        == ChunkParseTask.video_parse_task_id,
                    )
                    .where(
                        ChunkParseTask.status
                        == ChunkParseTaskStatus.QUEUED.value,
                        ChunkParseTask.next_execute_time <= claimed_at,
                        VideoParseTask.status
                        == VideoParseTaskStatus.PARSING.value,
                        VideoParseTask.is_deleted.is_(False),
                        VideoParseTask.detail[RUNTIME_DETAIL_KEY]["accepted"]
                        .as_boolean()
                        .is_(True),
                        ChunkParseTask.detail["dispatch_requested"]
                        .as_boolean()
                        .is_(True),
                    )
                    .order_by(*ordering)
                    .limit(200)
                )
                if video_parse_task_id is not None:
                    statement = statement.where(
                        ChunkParseTask.video_parse_task_id
                        == video_parse_task_id
                    )
                if cursor is not None:
                    statement = statement.where(tuple_(*ordering) > cursor)
                candidates = list(session.execute(statement))
            if not candidates:
                return None
            for _created, _sequence, candidate_id in candidates:
                # The candidate query is advisory. Lock and recheck the
                # execution row first, then its parent scheduler row in a
                # fresh transaction so a JOIN cannot choose lock order.
                with self._session_factory.begin() as session:
                    child = session.scalar(
                        select(ChunkParseTask)
                        .where(
                            ChunkParseTask.id == candidate_id,
                            ChunkParseTask.status
                            == ChunkParseTaskStatus.QUEUED.value,
                            ChunkParseTask.next_execute_time <= claimed_at,
                        )
                        .with_for_update(skip_locked=True)
                    )
                    if child is None:
                        continue
                    parent = session.scalar(
                        select(VideoParseTask)
                        .where(
                            VideoParseTask.id
                            == child.video_parse_task_id
                        )
                        .with_for_update()
                    )
                    chunk = session.get(VideoChunk, child.video_chunk_id)
                    if (
                        parent is None
                        or chunk is None
                        or parent.status != VideoParseTaskStatus.PARSING.value
                        or parent.is_deleted
                        or not _accepted(parent)
                        or _video_failure_status(parent) is not None
                        or not _dispatch_requested(child)
                        or not _chunk_claim_allowed(
                            session, parent, chunk.sequence_no
                        )
                    ):
                        continue
                    execution = _execution_config(parent.parse_config)
                    max_retries = _nonnegative_int(
                        execution.get("max_retry_count"), "max_retry_count"
                    )
                    child.status = ChunkParseTaskStatus.UNDERSTANDING.value
                    child.lease_owner = worker
                    child.lease_version += 1
                    child.lease_expire_time = expires_at
                    child.start_time = child.start_time or claimed_at
                    child.finish_time = None
                    child.last_error = None
                    child.update_time = claimed_at
                    child.detail = _set_dispatch_requested(child.detail, False)
                    session.flush()
                    return ChunkParseLease(
                        chunk_parse_task_id=child.id,
                        video_parse_task_id=child.video_parse_task_id,
                        video_chunk_id=child.video_chunk_id,
                        worker_id=worker,
                        lease_version=child.lease_version,
                        lease_expire_time=expires_at,
                        retry_count=child.retry_count,
                        max_retry_count=max_retries,
                    )
            cursor = tuple(candidates[-1])

    def load_chunk_schedule(
        self,
        video_parse_task_id: str,
        *,
        now: datetime | None = None,
    ) -> ChunkScheduleState:
        loaded_at = now if now is not None else now_utc()
        with self._session_factory() as session:
            parent = session.get(VideoParseTask, video_parse_task_id)
            if parent is None or parent.is_deleted:
                return ChunkScheduleState((), 1.0, "VideoParseTask does not exist")
            rows = list(session.execute(
                select(ChunkParseTask, VideoChunk)
                .join(VideoChunk, VideoChunk.video_chunk_id == ChunkParseTask.video_chunk_id)
                .where(ChunkParseTask.video_parse_task_id == video_parse_task_id)
                .order_by(VideoChunk.sequence_no)
            ))
            terminal = None
            if _video_failure_status(parent) is not None:
                terminal = "VideoParseTask is draining after a terminal failure"
            elif any(task.status == ChunkParseTaskStatus.FAILED.value for task, _chunk in rows):
                terminal = "a Chunk Understanding task exhausted its retry budget"
            entries = tuple(
                ChunkScheduleEntryState(
                    chunk_parse_task_id=task.id,
                    sequence_no=chunk.sequence_no,
                    status=task.status,
                    dispatchable=(
                        task.status == ChunkParseTaskStatus.QUEUED.value
                        and task.next_execute_time <= loaded_at
                        and not _dispatch_requested(task)
                        and _chunk_dispatch_allowed(
                            session, parent, chunk.sequence_no, loaded_at
                        )
                    ),
                )
                for task, chunk in rows
            )
            timeout = _schedule_wait_seconds(parent, rows, loaded_at)
            return ChunkScheduleState(entries, timeout, terminal)

    def dispatch_chunk(
        self,
        chunk_parse_task_id: str,
        video_lease: VideoParseLease,
        *,
        now: datetime | None = None,
    ) -> bool:
        dispatched_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            child = session.scalar(
                select(ChunkParseTask)
                .where(ChunkParseTask.id == chunk_parse_task_id)
                .with_for_update()
            )
            if child is None:
                return False
            parent = session.scalar(
                select(VideoParseTask)
                .where(VideoParseTask.id == child.video_parse_task_id)
                .with_for_update()
            )
            chunk = session.get(VideoChunk, child.video_chunk_id)
            if (
                parent is None
                or chunk is None
                or parent.id != video_lease.video_parse_task_id
                or parent.is_deleted
                or parent.lease_owner != video_lease.worker_id
                or parent.lease_version != video_lease.lease_version
                or parent.lease_expire_time is None
                or parent.lease_expire_time <= dispatched_at
                or child.status != ChunkParseTaskStatus.QUEUED.value
                or child.next_execute_time > dispatched_at
                or _dispatch_requested(child)
                or not _accepted(parent)
                or not _chunk_dispatch_allowed(
                    session, parent, chunk.sequence_no, dispatched_at
                )
            ):
                return False
            child.detail = _set_dispatch_requested(child.detail, True)
            child.update_time = dispatched_at
            _record_chunk_dispatch(parent, dispatched_at)
            return True

    def claim_interval_refine(
        self,
        worker_id: str,
        *,
        lease_duration: timedelta,
        video_parse_task_id: str | None = None,
        now: datetime | None = None,
    ) -> IntervalParseLease | None:
        return self._claim_interval(
            worker_id,
            phase=IntervalParseTaskStatus.REFINING.value,
            lease_duration=lease_duration,
            video_parse_task_id=video_parse_task_id,
            now=now,
        )

    def claim_interval_index(
        self,
        worker_id: str,
        *,
        lease_duration: timedelta,
        video_parse_task_id: str | None = None,
        now: datetime | None = None,
    ) -> IntervalParseLease | None:
        return self._claim_interval(
            worker_id,
            phase=IntervalParseTaskStatus.INDEXING.value,
            lease_duration=lease_duration,
            video_parse_task_id=video_parse_task_id,
            now=now,
        )

    def _claim_interval(
        self,
        worker_id: str,
        *,
        phase: str,
        lease_duration: timedelta,
        video_parse_task_id: str | None,
        now: datetime | None,
    ) -> IntervalParseLease | None:
        worker = require_nonempty_text(worker_id, "worker_id")
        _require_lease_duration(lease_duration)
        claimed_at = now if now is not None else now_utc()
        expires_at = claimed_at + lease_duration
        source_status = (
            IntervalParseTaskStatus.WAITING.value
            if phase == IntervalParseTaskStatus.REFINING.value
            else IntervalParseTaskStatus.INDEXING.value
        )
        cursor: tuple[datetime, int, str] | None = None
        while True:
            with self._session_factory() as session:
                ordering = (
                    IntervalParseTask.next_execute_time,
                    IntervalParseTask.start_sequence_no,
                    IntervalParseTask.id,
                )
                statement = (
                    select(*ordering)
                    .join(
                        VideoParseTask,
                        VideoParseTask.id
                        == IntervalParseTask.video_parse_task_id,
                    )
                    .where(
                        IntervalParseTask.status == source_status,
                        IntervalParseTask.lease_owner.is_(None),
                        IntervalParseTask.next_execute_time <= claimed_at,
                        VideoParseTask.status.in_([
                            VideoParseTaskStatus.PARSING.value,
                            VideoParseTaskStatus.INDEXING.value,
                        ]),
                        VideoParseTask.is_deleted.is_(False),
                        VideoParseTask.detail[RUNTIME_DETAIL_KEY]["accepted"]
                        .as_boolean()
                        .is_(True),
                    )
                    .order_by(*ordering)
                    .limit(200)
                )
                if phase == IntervalParseTaskStatus.REFINING.value:
                    full_mask = (
                        literal(1).op("<<")(
                            IntervalParseTask.chunk_count
                        ) - 1
                    )
                    statement = statement.where(
                        IntervalParseTask.ready_mask == full_mask
                    )
                if video_parse_task_id is not None:
                    statement = statement.where(
                        IntervalParseTask.video_parse_task_id
                        == video_parse_task_id
                    )
                if cursor is not None:
                    statement = statement.where(tuple_(*ordering) > cursor)
                candidates = list(session.execute(statement))
            if not candidates:
                return None
            for _next_execute, _start, candidate_id in candidates:
                with self._session_factory.begin() as session:
                    interval = session.scalar(
                        select(IntervalParseTask)
                        .where(
                            IntervalParseTask.id == candidate_id,
                            IntervalParseTask.status == source_status,
                            IntervalParseTask.lease_owner.is_(None),
                            IntervalParseTask.next_execute_time
                            <= claimed_at,
                        )
                        .with_for_update(skip_locked=True)
                    )
                    if interval is None:
                        continue
                    parent = session.scalar(
                        select(VideoParseTask)
                        .where(
                            VideoParseTask.id
                            == interval.video_parse_task_id
                        )
                        .with_for_update()
                    )
                    if (
                        parent is None
                        or parent.status not in {
                            VideoParseTaskStatus.PARSING.value,
                            VideoParseTaskStatus.INDEXING.value,
                        }
                        or parent.is_deleted
                        or not _accepted(parent)
                        or _video_failure_status(parent) is not None
                    ):
                        continue
                    if phase == IntervalParseTaskStatus.REFINING.value:
                        if (
                            interval.ready_mask
                            != interval_full_ready_mask(interval.chunk_count)
                        ):
                            continue
                        if (
                            session.get(
                                IntervalParseContent, interval.id
                            )
                            is not None
                        ):
                            continue
                    elif not _interval_has_index_work(session, interval):
                        continue
                    max_retries = _nonnegative_int(
                        _execution_config(parent.parse_config).get(
                            "max_retry_count"
                        ),
                        "max_retry_count",
                    )
                    interval.status = phase
                    interval.lease_owner = worker
                    interval.lease_version += 1
                    interval.lease_expire_time = expires_at
                    interval.start_time = interval.start_time or claimed_at
                    interval.finish_time = None
                    interval.last_error = None
                    interval.update_time = claimed_at
                    session.flush()
                    return IntervalParseLease(
                        interval_parse_task_id=interval.id,
                        video_parse_task_id=interval.video_parse_task_id,
                        chunk_parse_task_ids=tuple(
                            interval.chunk_parse_task_ids
                        ),
                        start_sequence_no=interval.start_sequence_no,
                        chunk_count=interval.chunk_count,
                        phase=phase,
                        worker_id=worker,
                        lease_version=interval.lease_version,
                        lease_expire_time=expires_at,
                        retry_count=interval.retry_count,
                        max_retry_count=max_retries,
                    )
            cursor = tuple(candidates[-1])

    def heartbeat_video(
        self,
        lease: VideoParseLease,
        *,
        lease_duration: timedelta,
        now: datetime | None = None,
    ) -> bool:
        _require_lease_duration(lease_duration)
        heartbeat_at = now if now is not None else now_utc()
        result = None
        with self._session_factory.begin() as session:
            result = session.execute(
                update(VideoParseTask)
                .where(
                    VideoParseTask.id == lease.video_parse_task_id,
                    VideoParseTask.lease_owner == lease.worker_id,
                    VideoParseTask.lease_version == lease.lease_version,
                    VideoParseTask.lease_expire_time > heartbeat_at,
                )
                .values(
                    heartbeat_time=heartbeat_at,
                    lease_expire_time=heartbeat_at + lease_duration,
                    update_time=heartbeat_at,
                )
            )
        return result.rowcount == 1

    def heartbeat_chunk(
        self,
        lease: ChunkParseLease,
        *,
        lease_duration: timedelta,
        now: datetime | None = None,
    ) -> bool:
        _require_lease_duration(lease_duration)
        heartbeat_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            result = session.execute(
                update(ChunkParseTask)
                .where(
                    ChunkParseTask.id == lease.chunk_parse_task_id,
                    ChunkParseTask.status == ChunkParseTaskStatus.UNDERSTANDING.value,
                    ChunkParseTask.lease_owner == lease.worker_id,
                    ChunkParseTask.lease_version == lease.lease_version,
                    ChunkParseTask.lease_expire_time > heartbeat_at,
                )
                .values(
                    lease_expire_time=heartbeat_at + lease_duration,
                    update_time=heartbeat_at,
                )
            )
            return result.rowcount == 1

    def heartbeat_interval(
        self,
        lease: IntervalParseLease,
        *,
        lease_duration: timedelta,
        now: datetime | None = None,
    ) -> bool:
        _require_lease_duration(lease_duration)
        heartbeat_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            result = session.execute(
                update(IntervalParseTask)
                .where(
                    IntervalParseTask.id == lease.interval_parse_task_id,
                    IntervalParseTask.status == lease.phase,
                    IntervalParseTask.lease_owner == lease.worker_id,
                    IntervalParseTask.lease_version == lease.lease_version,
                    IntervalParseTask.lease_expire_time > heartbeat_at,
                )
                .values(
                    lease_expire_time=heartbeat_at + lease_duration,
                    update_time=heartbeat_at,
                )
            )
            return result.rowcount == 1

    def is_execution_lease_active(
        self,
        video_parse_task_id: str,
        execution_kind: str,
        execution_task_id: str,
        lease_version: int,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Return whether an exact media workspace lease is still live.

        Execution kind is part of the identity because Chunk and Prefix work
        can use the same Chunk task id and independently incremented lease
        versions.
        """

        parent_id = require_nonempty_text(
            video_parse_task_id, "video_parse_task_id"
        )
        task_id = require_nonempty_text(execution_task_id, "execution_task_id")
        if execution_kind not in {"video", "chunk", "interval", "prefix"}:
            raise ValueError(
                "execution_kind must be video, chunk, interval, or prefix"
            )
        if (
            isinstance(lease_version, bool)
            or not isinstance(lease_version, int)
            or lease_version <= 0
        ):
            raise ValueError("lease_version must be a positive integer")
        checked_at = now if now is not None else now_utc()

        with self._session_factory() as session:
            parent = session.get(VideoParseTask, parent_id)
            if parent is None or parent.is_deleted:
                return False
            if execution_kind == "video":
                if task_id != parent_id:
                    return False
                return bool(
                    parent.status in {
                        VideoParseTaskStatus.PREPARING.value,
                        VideoParseTaskStatus.PARSING.value,
                        VideoParseTaskStatus.INDEXING.value,
                    }
                    and parent.lease_owner is not None
                    and parent.lease_version == lease_version
                    and parent.lease_expire_time is not None
                    and parent.lease_expire_time > checked_at
                )
            if execution_kind == "chunk":
                task = session.get(ChunkParseTask, task_id)
                return bool(
                    task is not None
                    and task.video_parse_task_id == parent_id
                    and task.status == ChunkParseTaskStatus.UNDERSTANDING.value
                    and task.lease_owner is not None
                    and task.lease_version == lease_version
                    and task.lease_expire_time is not None
                    and task.lease_expire_time > checked_at
                )
            if execution_kind == "interval":
                interval = session.get(IntervalParseTask, task_id)
                return bool(
                    interval is not None
                    and interval.video_parse_task_id == parent_id
                    and interval.status in {
                        IntervalParseTaskStatus.REFINING.value,
                        IntervalParseTaskStatus.INDEXING.value,
                    }
                    and interval.lease_owner is not None
                    and interval.lease_version == lease_version
                    and interval.lease_expire_time is not None
                    and interval.lease_expire_time > checked_at
                )

            prefix = _ensure_runtime(parent.detail)[RUNTIME_DETAIL_KEY]["prefix"]
            expires_at_ms = prefix.get("lease_expire_time_ms")
            return bool(
                _is_latest_video_task(session, parent)
                and prefix.get("lease_owner") is not None
                and prefix.get("execution_task_id") == task_id
                and prefix.get("lease_version") == lease_version
                and isinstance(expires_at_ms, int)
                and expires_at_ms > datetime_to_unix_time_ms(checked_at)
            )

    def is_execution_lease_committed(
        self,
        lease: (
            VideoParseLease
            | ChunkParseLease
            | IntervalParseLease
            | PrefixParseLease
        ),
    ) -> bool:
        """Return whether this exact fenced execution already committed success."""

        with self._session_factory() as session:
            if isinstance(lease, VideoParseLease):
                task = session.get(
                    VideoParseTask, lease.video_parse_task_id
                )
                return bool(
                    task is not None
                    and not task.is_deleted
                    and task.video_id == lease.video_id
                    and task.lease_version == lease.lease_version
                    and task.status == VideoParseTaskStatus.COMPLETE.value
                )

            parent = session.get(
                VideoParseTask, lease.video_parse_task_id
            )
            if parent is None or parent.is_deleted:
                return False
            if isinstance(lease, PrefixParseLease):
                child = session.get(
                    ChunkParseTask, lease.chunk_parse_task_id
                )
                chunk = session.get(VideoChunk, lease.video_chunk_id)
                if (
                    child is None
                    or chunk is None
                    or child.video_parse_task_id
                    != lease.video_parse_task_id
                    or child.video_chunk_id != lease.video_chunk_id
                    or chunk.sequence_no != lease.sequence_no
                ):
                    return False
                prefix = _ensure_runtime(parent.detail)[RUNTIME_DETAIL_KEY][
                    "prefix"
                ]
                committed = prefix.get("committed_leases", {}).get(
                    str(lease.lease_version)
                )
                return bool(
                    _is_latest_video_task(session, parent)
                    and isinstance(committed, dict)
                    and committed.get("execution_task_id")
                    == lease.chunk_parse_task_id
                    and committed.get("sequence_no") == lease.sequence_no
                    and int(prefix.get("next_chunk_sequence_no", 0))
                    > lease.sequence_no
                )
            if isinstance(lease, ChunkParseLease):
                child = session.get(
                    ChunkParseTask, lease.chunk_parse_task_id
                )
                return bool(
                    child is not None
                    and child.video_parse_task_id == lease.video_parse_task_id
                    and child.video_chunk_id == lease.video_chunk_id
                    and child.lease_version == lease.lease_version
                    and child.status == ChunkParseTaskStatus.COMPLETE.value
                )

            interval = session.get(
                IntervalParseTask, lease.interval_parse_task_id
            )
            if (
                interval is None
                or interval.video_parse_task_id != lease.video_parse_task_id
            ):
                return False
            result = session.get(IntervalParseContent, interval.id)
            if result is None:
                return False
            detail = result.detail if isinstance(result.detail, dict) else {}
            if lease.phase == IntervalParseTaskStatus.REFINING.value:
                return (
                    detail.get("refinement_committed_lease_version")
                    == lease.lease_version
                )
            if lease.phase == IntervalParseTaskStatus.INDEXING.value:
                return bool(
                    interval.status == IntervalParseTaskStatus.COMPLETE.value
                    and detail.get("indexing_committed_lease_version")
                    == lease.lease_version
                )
            return False

    def retry_video(
        self,
        lease: VideoParseLease,
        *,
        error: str,
        retry_at: datetime,
        now: datetime | None = None,
    ) -> bool:
        failed_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            task = session.scalar(
                select(VideoParseTask)
                .where(
                    VideoParseTask.id == lease.video_parse_task_id,
                    VideoParseTask.status.in_([
                        VideoParseTaskStatus.PREPARING.value,
                        VideoParseTaskStatus.PARSING.value,
                        VideoParseTaskStatus.INDEXING.value,
                    ]),
                    VideoParseTask.lease_owner == lease.worker_id,
                    VideoParseTask.lease_version == lease.lease_version,
                    VideoParseTask.lease_expire_time > failed_at,
                )
                .with_for_update()
            )
            if task is None:
                return False
            exhausted = task.attempt_count >= task.max_attempts
            current_status = task.status
            if not exhausted and current_status == VideoParseTaskStatus.PREPARING.value:
                # Retry preparation at its idempotent entry point.
                task.status = VideoParseTaskStatus.PREPARING.value
            task.next_execute_time = retry_at
            task.lease_owner = None
            task.lease_expire_time = None
            task.heartbeat_time = None
            task.finish_time = None
            task.last_error = _error_detail(error)
            task.update_time = failed_at
            if exhausted or _video_failure_status(task) is not None:
                _aggregate_video_task(
                    session, task.id, failed_at, release_terminal_lease=True,
                    failure_status=(
                        VideoParseTaskStatus.INDEXING_FAILED.value
                        if current_status == VideoParseTaskStatus.INDEXING.value
                        else VideoParseTaskStatus.PARSING_FAILED.value
                    ) if exhausted else None,
                )
            return True

    def retry_chunk(
        self,
        lease: ChunkParseLease,
        *,
        error: str,
        retry_at: datetime,
        now: datetime | None = None,
    ) -> bool:
        failed_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            task = session.scalar(
                select(ChunkParseTask)
                .where(
                    ChunkParseTask.id == lease.chunk_parse_task_id,
                    ChunkParseTask.status == ChunkParseTaskStatus.UNDERSTANDING.value,
                    ChunkParseTask.lease_owner == lease.worker_id,
                    ChunkParseTask.lease_version == lease.lease_version,
                    ChunkParseTask.lease_expire_time > failed_at,
                )
                .with_for_update()
            )
            if task is None:
                return False
            task.retry_count += 1
            exhausted = task.retry_count > lease.max_retry_count
            task.status = (
                ChunkParseTaskStatus.FAILED.value
                if exhausted else ChunkParseTaskStatus.QUEUED.value
            )
            task.next_execute_time = retry_at
            task.lease_owner = None
            task.lease_expire_time = None
            task.finish_time = failed_at if exhausted else None
            task.last_error = _error_detail(error)
            task.update_time = failed_at
            if exhausted:
                _aggregate_video_task(session, task.video_parse_task_id, failed_at)
            return True

    def retry_interval(
        self,
        lease: IntervalParseLease,
        *,
        error: str,
        retry_at: datetime,
        now: datetime | None = None,
    ) -> bool:
        failed_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            interval = session.scalar(
                select(IntervalParseTask)
                .where(
                    IntervalParseTask.id == lease.interval_parse_task_id,
                    IntervalParseTask.status == lease.phase,
                    IntervalParseTask.lease_owner == lease.worker_id,
                    IntervalParseTask.lease_version == lease.lease_version,
                    IntervalParseTask.lease_expire_time > failed_at,
                )
                .with_for_update()
            )
            if interval is None:
                return False
            interval.retry_count += 1
            exhausted = interval.retry_count > lease.max_retry_count
            if lease.phase == IntervalParseTaskStatus.REFINING.value:
                interval.status = (
                    IntervalParseTaskStatus.PARSING_FAILED.value
                    if exhausted else IntervalParseTaskStatus.WAITING.value
                )
            else:
                interval.status = (
                    IntervalParseTaskStatus.INDEXING_FAILED.value
                    if exhausted else IntervalParseTaskStatus.INDEXING.value
                )
            interval.next_execute_time = retry_at
            interval.lease_owner = None
            interval.lease_expire_time = None
            interval.finish_time = failed_at if exhausted else None
            interval.last_error = _error_detail(error)
            interval.update_time = failed_at
            if exhausted:
                _aggregate_video_task(session, interval.video_parse_task_id, failed_at)
            return True

    def release_interval_wait(
        self,
        lease: IntervalParseLease,
        *,
        next_check_at: datetime,
        now: datetime | None = None,
    ) -> bool:
        """Release INDEXING dependency wait without consuming retry budget."""

        released_at = now if now is not None else now_utc()
        if lease.phase != IntervalParseTaskStatus.INDEXING.value:
            raise ValueError("only an INDEXING lease can wait for dependencies")
        with self._session_factory.begin() as session:
            result = session.execute(
                update(IntervalParseTask)
                .where(
                    IntervalParseTask.id == lease.interval_parse_task_id,
                    IntervalParseTask.status == IntervalParseTaskStatus.INDEXING.value,
                    IntervalParseTask.lease_owner == lease.worker_id,
                    IntervalParseTask.lease_version == lease.lease_version,
                    IntervalParseTask.lease_expire_time > released_at,
                )
                .values(
                    lease_owner=None,
                    lease_expire_time=None,
                    next_execute_time=next_check_at,
                    update_time=released_at,
                )
            )
            return result.rowcount == 1

    def reap_expired(
        self,
        *,
        retry_delay: timedelta,
        now: datetime | None = None,
        limit: int = 100,
    ) -> int:
        """Reap every lease family without one family consuming another's limit."""

        reaped_at = now if now is not None else now_utc()
        reaped = sum((
            self.reap_expired_videos(
                retry_delay=retry_delay, now=reaped_at, limit=limit
            ),
            self.reap_expired_chunks(
                retry_delay=retry_delay, now=reaped_at, limit=limit
            ),
            self.reap_expired_intervals(
                retry_delay=retry_delay, now=reaped_at, limit=limit
            ),
        ))
        self._finalize_draining_videos(now=reaped_at, limit=limit)
        return reaped

    def _finalize_draining_videos(self, *, now: datetime, limit: int) -> None:
        with self._session_factory() as session:
            candidate_ids = list(session.scalars(
                select(VideoParseTask.id).where(
                    VideoParseTask.status.in_([
                        VideoParseTaskStatus.PARSING.value,
                        VideoParseTaskStatus.INDEXING.value,
                    ]),
                    VideoParseTask.lease_owner.is_(None),
                    VideoParseTask.is_deleted.is_(False),
                    VideoParseTask.detail[RUNTIME_DETAIL_KEY]["failure_status"]
                    .as_string().in_([
                        VideoParseTaskStatus.PARSING_FAILED.value,
                        VideoParseTaskStatus.INDEXING_FAILED.value,
                    ]),
                ).order_by(VideoParseTask.update_time, VideoParseTask.id)
                .limit(limit)
            ))
        for task_id in candidate_ids:
            # No earlier snapshot: parent admission is serialized before the
            # first child read. Never take child locks while holding parent.
            with self._session_factory.begin() as session:
                task = session.scalar(
                    select(VideoParseTask).where(
                        VideoParseTask.id == task_id,
                        VideoParseTask.lease_owner.is_(None),
                        VideoParseTask.is_deleted.is_(False),
                        VideoParseTask.status.in_([
                            VideoParseTaskStatus.PARSING.value,
                            VideoParseTaskStatus.INDEXING.value,
                        ]),
                    ).with_for_update(skip_locked=True)
                )
                if task is not None and _video_failure_status(task) is not None:
                    _aggregate_video_task(
                        session, task.id, now, release_terminal_lease=True
                    )

    def reap_expired_videos(
        self,
        *,
        retry_delay: timedelta,
        now: datetime | None = None,
        limit: int = 100,
    ) -> int:
        if retry_delay < timedelta(0):
            raise ValueError("retry_delay cannot be negative")
        if limit <= 0:
            raise ValueError("limit must be positive")
        reaped_at = now if now is not None else now_utc()
        retry_at = reaped_at + retry_delay
        with self._session_factory() as session:
            candidate_ids = list(session.scalars(
                select(VideoParseTask.id)
                .where(
                    VideoParseTask.status.in_([
                        VideoParseTaskStatus.PREPARING.value,
                        VideoParseTaskStatus.PARSING.value,
                        VideoParseTaskStatus.INDEXING.value,
                    ]),
                    VideoParseTask.lease_expire_time <= reaped_at,
                )
                .order_by(
                    VideoParseTask.lease_expire_time,
                    VideoParseTask.id,
                )
                .limit(limit)
            ))
        changed = 0
        for candidate_id in candidate_ids:
            with self._session_factory.begin() as session:
                task = session.scalar(
                    select(VideoParseTask)
                    .where(
                        VideoParseTask.id == candidate_id,
                        VideoParseTask.status.in_([
                            VideoParseTaskStatus.PREPARING.value,
                            VideoParseTaskStatus.PARSING.value,
                            VideoParseTaskStatus.INDEXING.value,
                        ]),
                        VideoParseTask.lease_expire_time <= reaped_at,
                    )
                    .with_for_update(skip_locked=True)
                )
                if task is None:
                    continue
                exhausted = task.attempt_count >= task.max_attempts
                current_status = task.status
                task.next_execute_time = retry_at
                task.lease_owner = None
                task.lease_expire_time = None
                task.heartbeat_time = None
                task.finish_time = None
                task.last_error = _error_detail("worker lease expired")
                task.processing_timing_complete = False
                task.update_time = reaped_at
                if exhausted or _video_failure_status(task) is not None:
                    _aggregate_video_task(
                        session, task.id, reaped_at, release_terminal_lease=True,
                        failure_status=(
                            VideoParseTaskStatus.INDEXING_FAILED.value
                            if current_status == VideoParseTaskStatus.INDEXING.value
                            else VideoParseTaskStatus.PARSING_FAILED.value
                        ) if exhausted else None,
                    )
                changed += 1
        return changed

    def reap_expired_chunks(
        self,
        *,
        retry_delay: timedelta,
        now: datetime | None = None,
        limit: int = 100,
    ) -> int:
        if retry_delay < timedelta(0):
            raise ValueError("retry_delay cannot be negative")
        if limit <= 0:
            raise ValueError("limit must be positive")
        reaped_at = now if now is not None else now_utc()
        retry_at = reaped_at + retry_delay
        with self._session_factory() as session:
            candidate_ids = list(session.scalars(
                select(ChunkParseTask.id)
                .where(
                    ChunkParseTask.status
                    == ChunkParseTaskStatus.UNDERSTANDING.value,
                    ChunkParseTask.lease_expire_time <= reaped_at,
                )
                .order_by(
                    ChunkParseTask.lease_expire_time,
                    ChunkParseTask.id,
                )
                .limit(limit)
            ))
        changed = 0
        for candidate_id in candidate_ids:
            with self._session_factory.begin() as session:
                task = session.scalar(
                    select(ChunkParseTask)
                    .where(
                        ChunkParseTask.id == candidate_id,
                        ChunkParseTask.status
                        == ChunkParseTaskStatus.UNDERSTANDING.value,
                        ChunkParseTask.lease_expire_time <= reaped_at,
                    )
                    .with_for_update(skip_locked=True)
                )
                if task is None:
                    continue
                parent = session.get(
                    VideoParseTask, task.video_parse_task_id
                )
                if parent is None:
                    continue
                max_retries = _nonnegative_int(
                    _execution_config(parent.parse_config).get(
                        "max_retry_count"
                    ),
                    "max_retry_count",
                )
                task.retry_count += 1
                exhausted = task.retry_count > max_retries
                task.status = (
                    ChunkParseTaskStatus.FAILED.value
                    if exhausted
                    else ChunkParseTaskStatus.QUEUED.value
                )
                task.next_execute_time = retry_at
                task.lease_owner = None
                task.lease_expire_time = None
                task.finish_time = reaped_at if exhausted else None
                task.last_error = _error_detail("worker lease expired")
                task.update_time = reaped_at
                if exhausted:
                    _aggregate_video_task(session, task.video_parse_task_id, reaped_at)
                changed += 1
        return changed

    def reap_expired_intervals(
        self,
        *,
        retry_delay: timedelta,
        now: datetime | None = None,
        limit: int = 100,
    ) -> int:
        if retry_delay < timedelta(0):
            raise ValueError("retry_delay cannot be negative")
        if limit <= 0:
            raise ValueError("limit must be positive")
        reaped_at = now if now is not None else now_utc()
        retry_at = reaped_at + retry_delay
        with self._session_factory() as session:
            candidate_ids = list(session.scalars(
                select(IntervalParseTask.id)
                .where(
                    IntervalParseTask.status.in_([
                        IntervalParseTaskStatus.REFINING.value,
                        IntervalParseTaskStatus.INDEXING.value,
                    ]),
                    IntervalParseTask.lease_expire_time <= reaped_at,
                )
                .order_by(
                    IntervalParseTask.lease_expire_time,
                    IntervalParseTask.start_sequence_no,
                    IntervalParseTask.id,
                )
                .limit(limit)
            ))
        changed = 0
        for candidate_id in candidate_ids:
            with self._session_factory.begin() as session:
                interval = session.scalar(
                    select(IntervalParseTask)
                    .where(
                        IntervalParseTask.id == candidate_id,
                        IntervalParseTask.status.in_([
                            IntervalParseTaskStatus.REFINING.value,
                            IntervalParseTaskStatus.INDEXING.value,
                        ]),
                        IntervalParseTask.lease_expire_time <= reaped_at,
                    )
                    .with_for_update(skip_locked=True)
                )
                if interval is None:
                    continue
                parent = session.get(
                    VideoParseTask, interval.video_parse_task_id
                )
                if parent is None:
                    continue
                max_retries = _nonnegative_int(
                    _execution_config(parent.parse_config).get(
                        "max_retry_count"
                    ),
                    "max_retry_count",
                )
                phase = interval.status
                interval.retry_count += 1
                exhausted = interval.retry_count > max_retries
                if phase == IntervalParseTaskStatus.REFINING.value:
                    interval.status = (
                        IntervalParseTaskStatus.PARSING_FAILED.value
                        if exhausted
                        else IntervalParseTaskStatus.WAITING.value
                    )
                else:
                    interval.status = (
                        IntervalParseTaskStatus.INDEXING_FAILED.value
                        if exhausted
                        else IntervalParseTaskStatus.INDEXING.value
                    )
                interval.next_execute_time = retry_at
                interval.lease_owner = None
                interval.lease_expire_time = None
                interval.finish_time = reaped_at if exhausted else None
                interval.last_error = _error_detail("worker lease expired")
                interval.update_time = reaped_at
                if exhausted:
                    _aggregate_video_task(session, interval.video_parse_task_id, reaped_at)
                changed += 1
        return changed


def _accepted(task: VideoParseTask) -> bool:
    runtime = (task.detail or {}).get(RUNTIME_DETAIL_KEY)
    return isinstance(runtime, dict) and runtime.get("accepted") is True


def _is_latest_video_task(
    session: Session, task: VideoParseTask
) -> bool:
    latest_id = session.scalar(
        select(VideoParseTask.id)
        .where(
            VideoParseTask.video_id == task.video_id,
            VideoParseTask.is_deleted.is_(False),
        )
        .order_by(
            VideoParseTask.version.desc(),
            VideoParseTask.id,
        )
        .limit(1)
    )
    return latest_id == task.id


def _chunk_dispatch_allowed(
    session: Session,
    parent: VideoParseTask,
    candidate_sequence: int,
    now: datetime,
) -> bool:
    if _video_failure_status(parent) is not None:
        return False
    batch_size = _positive_int(parent.parse_config.get("batch_max_size"), "batch_max_size")
    active_count = int(session.scalar(
        select(func.count(ChunkParseTask.id))
        .where(
            ChunkParseTask.video_parse_task_id == parent.id,
            ChunkParseTask.status == ChunkParseTaskStatus.UNDERSTANDING.value,
        )
    ) or 0)
    requested = list(session.scalars(
        select(ChunkParseTask)
        .where(
            ChunkParseTask.video_parse_task_id == parent.id,
            ChunkParseTask.status == ChunkParseTaskStatus.QUEUED.value,
        )
    ))
    reserved_count = sum(_dispatch_requested(item) for item in requested)
    if active_count + reserved_count >= batch_size:
        return False
    rows = list(session.execute(
        select(VideoChunk.sequence_no, ChunkParseTask.status)
        .join(ChunkParseTask, ChunkParseTask.video_chunk_id == VideoChunk.video_chunk_id)
        .where(ChunkParseTask.video_parse_task_id == parent.id)
        .order_by(VideoChunk.sequence_no)
    ))
    opened_through = min(batch_size, len(rows)) - 1
    batch_start = 0
    while batch_start < len(rows):
        batch = rows[batch_start:batch_start + batch_size]
        if batch_start == 0:
            opened_through = batch[-1][0]
        else:
            previous = rows[max(0, batch_start - batch_size):batch_start]
            required = math.ceil(len(previous) * 0.9)
            completed = sum(
                status == ChunkParseTaskStatus.COMPLETE.value
                for _sequence, status in previous
            )
            if completed < required:
                break
            opened_through = batch[-1][0]
        batch_start += batch_size
    if candidate_sequence > opened_through:
        return False
    dispatch_interval = parent.parse_config.get("dispatch_interval_seconds", 0)
    if isinstance(dispatch_interval, bool) or not isinstance(dispatch_interval, (int, float)) or dispatch_interval < 0:
        raise ValueError("parse_config.dispatch_interval_seconds must be nonnegative")
    runtime = _ensure_runtime(parent.detail)[RUNTIME_DETAIL_KEY]
    scheduler = runtime.get("chunk_scheduler", {})
    last_ms = scheduler.get("last_dispatch_time_ms") if isinstance(scheduler, dict) else None
    if isinstance(last_ms, int):
        next_ms = last_ms + int(dispatch_interval * 1000)
        if datetime_to_unix_time_ms(now) < next_ms:
            return False
    return True


def _chunk_claim_allowed(
    session: Session,
    parent: VideoParseTask,
    candidate_sequence: int,
) -> bool:
    batch_size = _positive_int(parent.parse_config.get("batch_max_size"), "batch_max_size")
    active_count = int(session.scalar(
        select(func.count(ChunkParseTask.id)).where(
            ChunkParseTask.video_parse_task_id == parent.id,
            ChunkParseTask.status == ChunkParseTaskStatus.UNDERSTANDING.value,
        )
    ) or 0)
    if active_count >= batch_size:
        return False
    rows = list(session.execute(
        select(VideoChunk.sequence_no, ChunkParseTask.status)
        .join(ChunkParseTask, ChunkParseTask.video_chunk_id == VideoChunk.video_chunk_id)
        .where(ChunkParseTask.video_parse_task_id == parent.id)
        .order_by(VideoChunk.sequence_no)
    ))
    opened_through = min(batch_size, len(rows)) - 1
    batch_start = batch_size
    while batch_start < len(rows):
        previous = rows[batch_start - batch_size:batch_start]
        required = math.ceil(len(previous) * 0.9)
        completed = sum(
            status == ChunkParseTaskStatus.COMPLETE.value
            for _sequence, status in previous
        )
        if completed < required:
            break
        opened_through = rows[min(batch_start + batch_size, len(rows)) - 1][0]
        batch_start += batch_size
    return candidate_sequence <= opened_through


def _record_chunk_dispatch(parent: VideoParseTask, now: datetime) -> None:
    detail = _ensure_runtime(parent.detail)
    runtime = deepcopy(detail[RUNTIME_DETAIL_KEY])
    runtime["chunk_scheduler"] = {
        "last_dispatch_time_ms": datetime_to_unix_time_ms(now)
    }
    detail[RUNTIME_DETAIL_KEY] = runtime
    parent.detail = detail
    parent.update_time = now


def _dispatch_requested(task: ChunkParseTask) -> bool:
    return isinstance(task.detail, dict) and task.detail.get("dispatch_requested") is True


def _set_dispatch_requested(
    detail_value: dict | None, requested: bool
) -> dict:
    detail = deepcopy(dict(detail_value or {}))
    detail["dispatch_requested"] = requested
    return detail


def _schedule_wait_seconds(
    parent: VideoParseTask,
    rows: list[tuple[ChunkParseTask, VideoChunk]],
    now: datetime,
) -> float:
    due = [
        task.next_execute_time
        for task, _chunk in rows
        if task.status == ChunkParseTaskStatus.QUEUED.value
        and not _dispatch_requested(task)
        and task.next_execute_time > now
    ]
    interval = parent.parse_config.get("dispatch_interval_seconds", 0)
    configured = float(interval) if isinstance(interval, (int, float)) and not isinstance(interval, bool) else 0.0
    if due:
        seconds = min((value - now).total_seconds() for value in due)
        return max(0.05, min(2.0, seconds, configured or 2.0))
    return max(0.05, min(2.0, configured or 2.0))


def _interval_has_index_work(
    session: Session, interval: IntervalParseTask
) -> bool:
    result = session.get(IntervalParseContent, interval.id)
    if result is None:
        return False
    indexed = _indexed_versions(result.detail)
    contents = {
        task_id: version
        for task_id, version in session.execute(
            select(ChunkParseTask.id, ChunkParseContent.version)
            .join(ChunkParseContent, ChunkParseContent.chunk_parse_task_id == ChunkParseTask.id)
            .where(ChunkParseTask.id.in_(interval.chunk_parse_task_ids))
        )
    }
    covering = _submitted_intervals_by_member(session, interval.video_parse_task_id)
    released = {
        task_id
        for task_id in interval.chunk_parse_task_ids
        if task_id in covering
        and all(item.status in _REFINE_SUBMITTED for item in covering[task_id])
    }
    if any(indexed.get(task_id) != contents.get(task_id) for task_id in released):
        return True
    return len(released) == interval.chunk_count


def _require_lease_duration(value: timedelta) -> None:
    if value <= timedelta(0):
        raise ValueError("lease_duration must be positive")


def _nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"parse_config.{name} must be a nonnegative integer")
    return value
