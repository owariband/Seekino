from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from agent.video_parse_v2.scheduler import claimable_chunk_sequences, chunk_index_ready
from agent.video_parse_v2.state import ChunkTaskPhase

from ..constants.parse import (
    CAPACITY_ACQUIRED_KEY,
    PREPARE_FAILURES_KEY,
    RUNTIME_DETAIL_KEY,
    VIDEO_PARSE_CAPACITY_LOCK_NAME,
)
from ..model.dto.jobDTO import (
    ChunkScheduleEntryState,
    ChunkScheduleState,
    ChunkParseLease,
    IntervalParseLease,
    PrefixParseLease,
    VideoParseLease,
    VideoParseRetryResult,
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
    _aggregate_video_task,
    _advance_ready_chunks,
    _ensure_runtime,
    _error_detail,
    _execution_config,
    _latest_video_task_id,
    _record_voiceprint_failure,
    _submitted_intervals_by_member,
    _video_failure_status,
)
from ..utils.time import datetime_to_unix_time_ms, now_utc
from ..utils.validation import require_nonempty_text
from ..utils.retry import task_retry_delay
from ..utils.database import serialized_transaction


class MySqlVideoParseQueue:
    """DB-backed V2 delivery with independent phase leases and fencing."""

    def __init__(
        self, session_factory: sessionmaker[Session], *, video_concurrency: int,
    ) -> None:
        self._session_factory = session_factory
        self.video_concurrency = video_concurrency
        if type(self.video_concurrency) is not int or self.video_concurrency <= 0:
            raise ValueError("video_concurrency must be a positive integer")
        self._last_parent: dict[str, str] = {}
        self._finalize_after: str | None = None

    def _acquire_capacity(self, session: Session, parent: VideoParseTask) -> bool:
        """Called only inside the serialized service admission transaction."""
        if parent.capacity_acquired:
            return True
        occupied = int(session.scalar(select(func.count(VideoParseTask.id)).where(
            _capacity_acquired(),
        )) or 0)
        if occupied >= self.video_concurrency:
            return False
        parent.capacity_acquired = True
        return True

    def claim_realtime_preparation(
        self, video_parse_task_id: str, worker_id: str, *,
        lease_duration: timedelta, now: datetime | None = None,
    ) -> VideoParseLease | None:
        """Claim fixed topology through the same service capacity as full Parse."""
        worker = require_nonempty_text(worker_id, "worker_id")
        _require_lease_duration(lease_duration)
        claimed_at = now if now is not None else now_utc()
        with serialized_transaction(self._session_factory, VIDEO_PARSE_CAPACITY_LOCK_NAME) as session:
            if session is None:
                return None
            task = session.scalar(select(VideoParseTask).where(
                VideoParseTask.id == video_parse_task_id,
                VideoParseTask.is_deleted.is_(False),
                VideoParseTask.status.in_(["PENDING", "PREPARING"]),
                or_(VideoParseTask.lease_owner.is_(None), VideoParseTask.lease_expire_time <= claimed_at),
                VideoParseTask.next_execute_time <= claimed_at,
            ).with_for_update(skip_locked=True))
            if task is None or _latest_video_task_id(session, task.video_id) != task.id:
                return None
            runtime = _ensure_runtime(task.detail)[RUNTIME_DETAIL_KEY]
            if (not _accepted(task) and runtime.get("realtime_only") is not True
                or _video_failure_status(task) is not None
                or not self._acquire_capacity(session, task)):
                return None
            return _claim_video_row(task, worker, claimed_at, lease_duration)

    def claim_realtime_chunk(
        self, chunk_parse_task_id: str, worker_id: str, *,
        lease_duration: timedelta, now: datetime | None = None,
    ) -> ChunkParseLease | None:
        """Prioritize requested understanding, sharing capacity and lease fencing."""
        worker = require_nonempty_text(worker_id, "worker_id")
        _require_lease_duration(lease_duration)
        claimed_at = now if now is not None else now_utc()
        with self._session_factory() as session:
            parent_id = session.scalar(select(ChunkParseTask.video_task_id).where(
                ChunkParseTask.id == chunk_parse_task_id,
            ))
        if parent_id is None:
            return None
        with serialized_transaction(self._session_factory, VIDEO_PARSE_CAPACITY_LOCK_NAME) as session:
            if session is None:
                return None
            parent = session.scalar(select(VideoParseTask).where(
                VideoParseTask.id == parent_id,
            ).with_for_update(skip_locked=True))
            if (parent is None or parent.is_deleted
                or _latest_video_task_id(session, parent.video_id) != parent.id
                or _video_failure_status(parent) is not None):
                return None
            runtime = _ensure_runtime(parent.detail)[RUNTIME_DETAIL_KEY]
            if not ((_accepted(parent) and parent.status == "PARSING") or (
                not _accepted(parent) and runtime.get("realtime_only") is True
                and parent.status == "PENDING"
            )):
                return None
            if not _phase_capacity(session, parent, "parse"):
                return None
            child = session.scalar(select(ChunkParseTask).where(
                ChunkParseTask.id == chunk_parse_task_id,
                ChunkParseTask.video_task_id == parent.id,
                ChunkParseTask.phase == ChunkTaskPhase.PARSE, ChunkParseTask.status == "QUEUED",
                ChunkParseTask.next_execute_time <= claimed_at,
            ).with_for_update(skip_locked=True))
            if child is None or not self._acquire_capacity(session, parent):
                return None
            return _claim_chunk_row(child, parent, worker, claimed_at, lease_duration)

    def claim_video(
        self, worker_id: str, *, lease_duration: timedelta,
        now: datetime | None = None,
    ) -> VideoParseLease | None:
        worker = require_nonempty_text(worker_id, "worker_id")
        _require_lease_duration(lease_duration)
        claimed_at = now if now is not None else now_utc()
        with self._session_factory() as session:
            candidates = list(session.scalars(select(VideoParseTask.id).where(
                VideoParseTask.status.in_(["PENDING", "PREPARING", "PARSING", "INDEXING"]),
                VideoParseTask.lease_owner.is_(None),
                VideoParseTask.next_execute_time <= claimed_at,
                VideoParseTask.is_deleted.is_(False),
                VideoParseTask.detail[RUNTIME_DETAIL_KEY]["accepted"].as_boolean().is_(True),
            ).order_by(
                _capacity_acquired().desc(),
                (VideoParseTask.status == "PENDING"),
                VideoParseTask.next_execute_time, VideoParseTask.create_date, VideoParseTask.id,
            )))
        for candidate_id in candidates:
            with serialized_transaction(self._session_factory, VIDEO_PARSE_CAPACITY_LOCK_NAME) as session:
                if session is None:
                    return None
                parent = session.scalar(select(VideoParseTask).where(
                    VideoParseTask.id == candidate_id,
                    VideoParseTask.status.in_(["PENDING", "PREPARING", "PARSING", "INDEXING"]),
                    VideoParseTask.lease_owner.is_(None),
                    VideoParseTask.next_execute_time <= claimed_at,
                    VideoParseTask.is_deleted.is_(False),
                ).with_for_update(skip_locked=True))
                if (parent is None or not _accepted(parent)
                    or _video_failure_status(parent) is not None
                    or not self._acquire_capacity(session, parent)):
                    continue
                return _claim_video_row(parent, worker, claimed_at, lease_duration)
        return None

    def _parent_candidates(self, phase: str, requested: str | None) -> list[str]:
        with self._session_factory() as session:
            statement = select(VideoParseTask.id).where(
                VideoParseTask.status.in_(["PARSING", "INDEXING"]),
                _capacity_acquired(),
                VideoParseTask.is_deleted.is_(False),
                VideoParseTask.detail[RUNTIME_DETAIL_KEY]["accepted"].as_boolean().is_(True),
            ).order_by(VideoParseTask.id)
            if requested is not None:
                statement = statement.where(VideoParseTask.id == requested)
            parents = list(session.scalars(statement))
        previous = self._last_parent.get(phase)
        if previous in parents:
            offset = parents.index(previous) + 1
            parents = parents[offset:] + parents[:offset]
        return parents

    def claim_chunk(
        self, worker_id: str, *, lease_duration: timedelta,
        video_parse_task_id: str | None = None, now: datetime | None = None,
    ) -> ChunkParseLease | None:
        return self._claim_chunk(worker_id, phase=ChunkTaskPhase.PARSE, lease_duration=lease_duration,
                                 video_parse_task_id=video_parse_task_id, now=now)

    def claim_chunk_index(
        self, worker_id: str, *, lease_duration: timedelta,
        video_parse_task_id: str | None = None, now: datetime | None = None,
    ) -> ChunkParseLease | None:
        return self._claim_chunk(worker_id, phase=ChunkTaskPhase.INDEX, lease_duration=lease_duration,
                                 video_parse_task_id=video_parse_task_id, now=now)

    def _claim_chunk(
        self, worker_id: str, *, phase: int, lease_duration: timedelta,
        video_parse_task_id: str | None, now: datetime | None,
    ) -> ChunkParseLease | None:
        worker = require_nonempty_text(worker_id, "worker_id")
        _require_lease_duration(lease_duration)
        claimed_at = now if now is not None else now_utc()
        stage = "parse" if phase == ChunkTaskPhase.PARSE else "index"
        for parent_id in self._parent_candidates(stage, video_parse_task_id):
            with self._session_factory.begin() as session:
                parent = session.scalar(select(VideoParseTask).where(
                    VideoParseTask.id == parent_id,
                ).with_for_update(skip_locked=True))
                if (not _eligible_parent(parent) or phase == ChunkTaskPhase.PARSE and parent.status != "PARSING"
                    or not _phase_capacity(session, parent, stage)):
                    continue
                children = list(session.scalars(select(ChunkParseTask).join(
                    VideoChunk, VideoChunk.id == ChunkParseTask.chunk_id,
                ).where(
                    ChunkParseTask.video_task_id == parent.id,
                    ChunkParseTask.phase == phase, ChunkParseTask.status == "QUEUED",
                    ChunkParseTask.next_execute_time <= claimed_at,
                ).order_by(VideoChunk.sequence_no).with_for_update(skip_locked=True)))
                for child in children:
                    chunk = session.get(VideoChunk, child.chunk_id)
                    if phase == ChunkTaskPhase.PARSE and not _chunk_claim_allowed(session, parent, chunk.sequence_no, claimed_at):
                        continue
                    if phase == ChunkTaskPhase.INDEX:
                        content = session.scalar(select(ChunkParseContent).where(
                            ChunkParseContent.chunk_task_id == child.id,
                        ))
                        covering = _submitted_intervals_by_member(session, parent.id).get(child.id, ())
                        if not chunk_index_ready(content is not None and content.content is not None, [
                            item.status == "COMPLETE" and session.get(IntervalParseContent, item.id) is not None
                            for item in covering
                        ]):
                            continue
                    lease = _claim_chunk_row(child, parent, worker, claimed_at, lease_duration)
                    if phase == ChunkTaskPhase.PARSE:
                        _record_chunk_dispatch(parent, claimed_at)
                    self._last_parent[stage] = parent.id
                    return lease
        return None

    def load_chunk_schedule(
        self, video_parse_task_id: str, *, now: datetime | None = None,
    ) -> ChunkScheduleState:
        loaded_at = now if now is not None else now_utc()
        with self._session_factory() as session:
            parent = session.get(VideoParseTask, video_parse_task_id)
            if parent is None or parent.is_deleted:
                return ChunkScheduleState((), 1.0, "VideoParseTask does not exist")
            rows = list(session.execute(select(ChunkParseTask, VideoChunk, ChunkParseContent).join(
                VideoChunk, VideoChunk.id == ChunkParseTask.chunk_id,
            ).outerjoin(ChunkParseContent, ChunkParseContent.chunk_task_id == ChunkParseTask.id).where(
                ChunkParseTask.video_task_id == video_parse_task_id,
            ).order_by(VideoChunk.sequence_no)))
            terminal = None
            if _video_failure_status(parent) is not None:
                terminal = "VideoParseTask is draining after a terminal failure"
            elif any(task.status == "FAILED" and (
                parent.parse_config.get("voiceprint_mode", "off") == "off"
                or task.phase != ChunkTaskPhase.PARSE
            ) for task, _chunk, _content in rows):
                terminal = "a Chunk stage exhausted its retry budget"
            return ChunkScheduleState(tuple(ChunkScheduleEntryState(
                chunk_parse_task_id=task.id, sequence_no=chunk.sequence_no,
                status=task.status, phase=task.phase,
                understanding_committed=content is not None and content.content is not None,
            ) for task, chunk, content in rows), _schedule_wait_seconds(
                parent, [(task, chunk) for task, chunk, _content in rows], loaded_at,
            ), terminal)

    def claim_interval_refine(
        self, worker_id: str, *, lease_duration: timedelta,
        video_parse_task_id: str | None = None, now: datetime | None = None,
    ) -> IntervalParseLease | None:
        worker = require_nonempty_text(worker_id, "worker_id")
        _require_lease_duration(lease_duration)
        claimed_at = now if now is not None else now_utc()
        for parent_id in self._parent_candidates("refine", video_parse_task_id):
            with self._session_factory.begin() as session:
                parent = session.scalar(select(VideoParseTask).where(
                    VideoParseTask.id == parent_id,
                ).with_for_update(skip_locked=True))
                if (not _eligible_parent(parent)
                    or (parent.parse_config.get("voiceprint") is not None
                        or parent.parse_config.get("voiceprint_mode", "off") != "off")
                    and dict((parent.detail or {}).get("voiceprint") or {}).get("ready") is not True
                    or not _phase_capacity(session, parent, "refine")):
                    continue
                candidates = list(session.scalars(select(IntervalParseTask).where(
                    IntervalParseTask.video_task_id == parent.id,
                    IntervalParseTask.status == "WAITING",
                    IntervalParseTask.next_execute_time <= claimed_at,
                ).order_by(IntervalParseTask.start_sequence_no).with_for_update(skip_locked=True)))
                for interval in candidates:
                    if (interval.ready_mask != interval_full_ready_mask(interval.chunk_count)
                        or session.get(IntervalParseContent, interval.id) is not None):
                        continue
                    contents = session.execute(select(ChunkParseContent.chunk_task_id).where(
                        ChunkParseContent.chunk_task_id.in_(interval.chunk_task_ids),
                        ChunkParseContent.content.is_not(None),
                    )).all()
                    if len(contents) != interval.chunk_count:
                        continue
                    execution = _execution_config(parent.parse_config)
                    deadline = claimed_at + timedelta(seconds=execution["refine_execution_timeout_seconds"])
                    interval.status = "REFINING"
                    interval.lease_owner = worker
                    interval.lease_version += 1
                    interval.lease_expire_time = min(claimed_at + lease_duration, deadline)
                    interval.start_time = claimed_at
                    interval.finish_time = None
                    interval.last_error = None
                    interval.update_time = datetime_to_unix_time_ms(claimed_at)
                    session.flush()
                    self._last_parent["refine"] = parent.id
                    return IntervalParseLease(
                        interval_parse_task_id=interval.id, video_parse_task_id=parent.id,
                        chunk_parse_task_ids=tuple(interval.chunk_task_ids),
                        start_sequence_no=interval.start_sequence_no, chunk_count=interval.chunk_count,
                        phase="REFINING", worker_id=worker, lease_version=interval.lease_version,
                        lease_expire_time=interval.lease_expire_time, execution_deadline=deadline,
                        retry_count=interval.retry_count,
                        max_retry_count=execution["max_retry_count"],
                        retry_initial_delay_seconds=execution["retry_initial_delay_seconds"],
                        retry_max_delay_seconds=execution["retry_max_delay_seconds"],
                    )
        return None

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
                    update_time=datetime_to_unix_time_ms(heartbeat_at),
                )
            )
        return result.rowcount == 1

    def heartbeat_chunk(
        self, lease: ChunkParseLease, *, lease_duration: timedelta,
        now: datetime | None = None,
    ) -> bool:
        return self._heartbeat_child(lease, lease_duration=lease_duration, now=now)

    def heartbeat_interval(
        self, lease: IntervalParseLease, *, lease_duration: timedelta,
        now: datetime | None = None,
    ) -> bool:
        return self._heartbeat_child(lease, lease_duration=lease_duration, now=now)

    def _heartbeat_child(self, lease, *, lease_duration: timedelta, now: datetime | None) -> bool:
        _require_lease_duration(lease_duration)
        heartbeat_at = now if now is not None else now_utc()
        model = ChunkParseTask if isinstance(lease, ChunkParseLease) else IntervalParseTask
        task_id = lease.chunk_parse_task_id if isinstance(lease, ChunkParseLease) else lease.interval_parse_task_id
        with self._session_factory.begin() as session:
            parent = session.scalar(select(VideoParseTask).where(
                VideoParseTask.id == lease.video_parse_task_id,
            ).with_for_update())
            if parent is None or parent.is_deleted:
                return False
            task = session.scalar(select(model).where(model.id == task_id).with_for_update())
            if not _matches_child(task, parent, lease, heartbeat_at):
                return False
            task.lease_expire_time = min(heartbeat_at + lease_duration, _execution_deadline(task, parent))
            task.update_time = datetime_to_unix_time_ms(heartbeat_at)
            return True

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
                    and task.video_task_id == parent_id
                    and task.status == "RUNNING"
                    and task.lease_owner is not None
                    and task.lease_version == lease_version
                    and task.lease_expire_time is not None
                    and task.lease_expire_time > checked_at
                    and task.start_time is not None
                    and _execution_deadline(task, parent) > checked_at
                )
            if execution_kind == "interval":
                interval = session.get(IntervalParseTask, task_id)
                return bool(
                    interval is not None
                    and interval.video_task_id == parent_id
                    and interval.status == "REFINING"
                    and interval.lease_owner is not None
                    and interval.lease_version == lease_version
                    and interval.lease_expire_time is not None
                    and interval.lease_expire_time > checked_at
                    and interval.start_time is not None
                    and _execution_deadline(interval, parent) > checked_at
                )

            prefix = _ensure_runtime(parent.detail)[RUNTIME_DETAIL_KEY]["prefix"]
            expires_at_ms = prefix.get("lease_expire_time_ms")
            return bool(
                _latest_video_task_id(session, parent.video_id) == parent.id
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
                    or child.video_task_id
                    != lease.video_parse_task_id
                    or child.chunk_id != lease.video_chunk_id
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
                    _latest_video_task_id(session, parent.video_id) == parent.id
                    and isinstance(committed, dict)
                    and committed.get("execution_task_id")
                    == lease.chunk_parse_task_id
                    and committed.get("sequence_no") == lease.sequence_no
                    and int(prefix.get("next_chunk_sequence_no", 0))
                    > lease.sequence_no
                )
            if isinstance(lease, ChunkParseLease):
                child = session.get(ChunkParseTask, lease.chunk_parse_task_id)
                if (child is None or child.video_task_id != lease.video_parse_task_id
                    or child.chunk_id != lease.video_chunk_id):
                    return False
                content = session.scalar(select(ChunkParseContent).where(
                    ChunkParseContent.chunk_task_id == child.id,
                ))
                detail = content.detail if content is not None and isinstance(content.detail, dict) else {}
                if lease.phase == ChunkTaskPhase.PARSE:
                    return detail.get("parse_committed_lease_version") == lease.lease_version
                return lease.phase == ChunkTaskPhase.INDEX and lease.lease_version in detail.get("index_committed_lease_versions", [])

            interval = session.get(
                IntervalParseTask, lease.interval_parse_task_id
            )
            if (
                interval is None
                or interval.video_task_id != lease.video_parse_task_id
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
            return False

    def retry_video(
        self, lease: VideoParseLease, *, error: str,
        now: datetime | None = None,
    ) -> VideoParseRetryResult | None:
        failed_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            task = session.scalar(select(VideoParseTask).where(
                VideoParseTask.id == lease.video_parse_task_id,
                VideoParseTask.status.in_(["PREPARING", "PARSING", "INDEXING"]),
                VideoParseTask.lease_owner == lease.worker_id,
                VideoParseTask.lease_version == lease.lease_version,
                VideoParseTask.lease_expire_time > failed_at,
            ).with_for_update())
            if task is None:
                return None
            execution = _execution_config(task.parse_config)
            prepare_failure_count = None
            retry_at = failed_at + timedelta(seconds=execution["retry_initial_delay_seconds"])
            exhausted = False
            if task.status == "PREPARING":
                detail = _ensure_runtime(task.detail)
                runtime = detail[RUNTIME_DETAIL_KEY]
                failures = runtime.get(PREPARE_FAILURES_KEY, 0)
                if type(failures) is not int or failures < 0:
                    raise ValueError("persisted Prepare failure count must be a nonnegative integer")
                failures += 1
                prepare_failure_count = failures
                runtime[PREPARE_FAILURES_KEY] = failures
                task.detail = detail
                exhausted = failures > execution["max_retry_count"]
                retry_at = failed_at + task_retry_delay(
                    execution["retry_initial_delay_seconds"], execution["retry_max_delay_seconds"],
                    failures,
                )
            elif task.status == "PARSING":
                # An error loading/binding the snapshot can roll back the active
                # marker itself. Real coordinator failures still have a finite budget.
                _voiceprint_failures, exhausted = _record_voiceprint_failure(task, failed_at, pending_failure=True)
                if _voiceprint_failures is not None:
                    retry_at = task.next_execute_time
            task.next_execute_time = retry_at
            task.lease_owner = None
            task.lease_expire_time = None
            task.heartbeat_time = None
            task.finish_time = None
            task.last_error = _error_detail(error)
            task.update_time = datetime_to_unix_time_ms(failed_at)
            _aggregate_released_video(
                session, task, failed_at,
                failure_status="PARSING_FAILED" if exhausted else None,
            )
            return VideoParseRetryResult(
                next_execute_time=(
                    task.next_execute_time if task.status not in {"COMPLETE", "PARSING_FAILED", "INDEXING_FAILED"}
                    and _video_failure_status(task) is None else None
                ),
                prepare_failure_count=prepare_failure_count,
            )

    def retry_chunk(
        self, lease: ChunkParseLease, *, error: str, retry_at: datetime,
        now: datetime | None = None,
    ) -> bool:
        return self._retry_child(lease, error=error, retry_at=retry_at, now=now)

    def retry_interval(
        self, lease: IntervalParseLease, *, error: str, retry_at: datetime,
        now: datetime | None = None,
    ) -> bool:
        return self._retry_child(lease, error=error, retry_at=retry_at, now=now)

    def _retry_child(self, lease, *, error: str, retry_at: datetime, now: datetime | None) -> bool:
        failed_at = now if now is not None else now_utc()
        model = ChunkParseTask if isinstance(lease, ChunkParseLease) else IntervalParseTask
        task_id = lease.chunk_parse_task_id if isinstance(lease, ChunkParseLease) else lease.interval_parse_task_id
        with self._session_factory.begin() as session:
            parent = session.scalar(select(VideoParseTask).where(
                VideoParseTask.id == lease.video_parse_task_id,
            ).with_for_update())
            if parent is None or parent.is_deleted:
                return False
            child = session.scalar(select(model).where(model.id == task_id).with_for_update())
            if not _matches_child(child, parent, lease, failed_at, allow_deadline=True):
                return False
            _fail_child(child, parent, failed_at, error, retry_at=retry_at)
            _aggregate_video_task(session, parent.id, failed_at, release_terminal_lease=True)
            return True

    def release_execution(self, lease, *, now: datetime | None = None) -> bool:
        """Return stopped work; unfinished voice execution retains its failure outcome."""
        released_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            parent = session.scalar(select(VideoParseTask).where(
                VideoParseTask.id == lease.video_parse_task_id,
            ).with_for_update())
            if parent is None or parent.is_deleted:
                return False
            exhausted = False
            if isinstance(lease, VideoParseLease):
                if parent.lease_owner != lease.worker_id or parent.lease_version != lease.lease_version:
                    return False
                _failures, exhausted = _record_voiceprint_failure(parent, released_at)
                if _failures is not None:
                    parent.last_error = _error_detail("Voiceprint preparation execution interrupted before release")
                parent.lease_owner = None
                parent.lease_expire_time = None
                parent.heartbeat_time = None
                if _failures is None:
                    parent.next_execute_time = released_at
            else:
                model = ChunkParseTask if isinstance(lease, ChunkParseLease) else IntervalParseTask
                task_id = lease.chunk_parse_task_id if isinstance(lease, ChunkParseLease) else lease.interval_parse_task_id
                child = session.scalar(select(model).where(model.id == task_id).with_for_update())
                if not _matches_child(child, parent, lease, released_at):
                    return False
                child.status = "QUEUED" if isinstance(lease, ChunkParseLease) else "WAITING"
                child.lease_owner = None
                child.lease_expire_time = None
                child.next_execute_time = released_at
                child.update_time = datetime_to_unix_time_ms(released_at)
            _aggregate_released_video(
                session, parent, released_at,
                failure_status="PARSING_FAILED" if exhausted else None,
            )
            return True

    def reap_expired(
        self, *, now: datetime | None = None, limit: int = 100,
    ) -> int:
        reaped_at = now if now is not None else now_utc()
        reaped = sum((
            self.reap_expired_videos(now=reaped_at, limit=limit),
            self.reap_expired_chunks(now=reaped_at, limit=limit),
            self.reap_expired_intervals(now=reaped_at, limit=limit),
        ))
        self._finalize_draining_videos(now=reaped_at, limit=limit)
        return reaped

    def _finalize_draining_videos(self, *, now: datetime, limit: int) -> None:
        """Reconcile success and failure independently of any coordinator handle."""
        # Page by id: unchanged tasks keep their update_date, so ordering by it
        # would starve every task past the first page.
        def page(after: str | None) -> list[str]:
            query = select(VideoParseTask.id).where(
                VideoParseTask.status.in_(["PARSING", "INDEXING"]),
                VideoParseTask.is_deleted.is_(False),
            )
            if after is not None:
                query = query.where(VideoParseTask.id > after)
            with self._session_factory() as session:
                return list(session.scalars(query.order_by(VideoParseTask.id).limit(limit)))

        candidate_ids = page(self._finalize_after)
        if not candidate_ids and self._finalize_after is not None:
            candidate_ids = page(None)
        self._finalize_after = candidate_ids[-1] if len(candidate_ids) == limit else None
        for task_id in candidate_ids:
            with self._session_factory.begin() as session:
                task = session.scalar(select(VideoParseTask).where(
                    VideoParseTask.id == task_id, VideoParseTask.is_deleted.is_(False),
                    VideoParseTask.status.in_(["PARSING", "INDEXING"]),
                ).with_for_update(skip_locked=True))
                if task is not None:
                    if _video_failure_status(task) is None:
                        waiting = list(session.scalars(select(ChunkParseTask.id).where(
                            ChunkParseTask.video_task_id == task.id,
                            ChunkParseTask.phase == ChunkTaskPhase.REFINE, ChunkParseTask.status == "WAITING",
                        )))
                        if waiting:
                            _advance_ready_chunks(session, task.id, waiting, now)
                    _aggregate_video_task(session, task.id, now, release_terminal_lease=True)

    def reap_expired_videos(
        self, *, now: datetime | None = None, limit: int = 100,
    ) -> int:
        if limit <= 0:
            raise ValueError("limit must be positive")
        reaped_at = now if now is not None else now_utc()
        with self._session_factory() as session:
            ids = list(session.scalars(select(VideoParseTask.id).where(
                VideoParseTask.status.in_(["PREPARING", "PARSING", "INDEXING"]),
                VideoParseTask.is_deleted.is_(False),
                VideoParseTask.lease_owner.is_not(None), VideoParseTask.lease_expire_time <= reaped_at,
            ).order_by(VideoParseTask.lease_expire_time, VideoParseTask.id).limit(limit)))
        changed = 0
        for task_id in ids:
            with self._session_factory.begin() as session:
                task = session.scalar(select(VideoParseTask).where(
                    VideoParseTask.id == task_id, VideoParseTask.lease_owner.is_not(None),
                    VideoParseTask.is_deleted.is_(False),
                    VideoParseTask.lease_expire_time <= reaped_at,
                ).with_for_update(skip_locked=True))
                if task is None:
                    continue
                # Waiting coordinators are free to resume; interrupted voice preparation
                # spends the same finite failure budget as an explicit execution error.
                failures, exhausted = _record_voiceprint_failure(task, reaped_at)
                if failures is None:
                    task.next_execute_time = reaped_at
                else:
                    task.last_error = _error_detail("Voiceprint preparation coordinator lease expired")
                task.lease_owner = None
                task.lease_expire_time = None
                task.heartbeat_time = None
                task.processing_timing_complete = False
                task.update_time = datetime_to_unix_time_ms(reaped_at)
                _aggregate_released_video(
                    session, task, reaped_at, failure_status="PARSING_FAILED" if exhausted else None,
                )
                changed += 1
        return changed

    def reap_expired_chunks(self, *, now: datetime | None = None, limit: int = 100) -> int:
        return self._reap_children(ChunkParseTask, now=now, limit=limit)

    def reap_expired_intervals(
        self, *, now: datetime | None = None, limit: int = 100,
    ) -> int:
        return self._reap_children(IntervalParseTask, now=now, limit=limit)

    def _reap_children(self, model, *, now: datetime | None, limit: int) -> int:
        if limit <= 0:
            raise ValueError("limit must be positive")
        reaped_at = now if now is not None else now_utc()
        running = "RUNNING" if model is ChunkParseTask else "REFINING"
        with self._session_factory() as session:
            rows = list(session.execute(select(model.id, model.video_task_id).join(
                VideoParseTask, VideoParseTask.id == model.video_task_id,
            ).where(
                VideoParseTask.is_deleted.is_(False),
                model.status == running,
                model.lease_expire_time <= reaped_at,
            ).order_by(model.lease_expire_time, model.id).limit(limit)))
        changed = 0
        for child_id, parent_id in rows:
            with self._session_factory.begin() as session:
                parent = session.scalar(select(VideoParseTask).where(
                    VideoParseTask.id == parent_id, VideoParseTask.is_deleted.is_(False),
                ).with_for_update(skip_locked=True))
                if parent is None:
                    continue
                child = session.scalar(select(model).where(
                    model.id == child_id, model.status == running,
                    model.lease_expire_time <= reaped_at,
                ).with_for_update())
                if child is None:
                    continue
                error = "execution deadline exceeded" if _execution_deadline(child, parent) <= reaped_at else "worker lease expired"
                _fail_child(child, parent, reaped_at, error)
                _aggregate_video_task(session, parent.id, reaped_at, release_terminal_lease=True)
                changed += 1
        return changed


def _accepted(task: VideoParseTask) -> bool:
    runtime = (task.detail or {}).get(RUNTIME_DETAIL_KEY)
    return isinstance(runtime, dict) and runtime.get("accepted") is True


def _aggregate_released_video(
    session: Session, task: VideoParseTask, now: datetime, *, failure_status: str | None = None,
) -> None:
    runtime = _ensure_runtime(task.detail)[RUNTIME_DETAIL_KEY]
    if (task.status != "PREPARING" or failure_status is not None
        or _video_failure_status(task) is not None
        or runtime.get("realtime_only") is True and runtime.get("accepted") is not True):
        _aggregate_video_task(
            session, task.id, now, release_terminal_lease=True, failure_status=failure_status,
        )


def _chunk_claim_allowed(
    session: Session,
    parent: VideoParseTask,
    candidate_sequence: int,
    now: datetime,
) -> bool:
    if _video_failure_status(parent) is not None:
        return False
    batch_size = parent.parse_config["batch_max_size"]
    rows = list(session.execute(
        select(VideoChunk.sequence_no, ChunkParseTask.phase, ChunkParseTask.status,
               ChunkParseContent.content.is_not(None))
        .join(ChunkParseTask, ChunkParseTask.chunk_id == VideoChunk.id)
        .outerjoin(ChunkParseContent, ChunkParseContent.chunk_task_id == ChunkParseTask.id)
        .where(ChunkParseTask.video_task_id == parent.id)
        .order_by(VideoChunk.sequence_no)
    ))
    if candidate_sequence not in claimable_chunk_sequences(
        rows, batch_size,
        terminal_failures_ready=parent.parse_config.get("voiceprint_mode", "off") != "off",
    ):
        return False
    dispatch_interval = parent.parse_config["dispatch_interval_seconds"]
    runtime = _ensure_runtime(parent.detail)[RUNTIME_DETAIL_KEY]
    scheduler = runtime.get("chunk_scheduler", {})
    last_ms = scheduler.get("last_dispatch_time_ms") if isinstance(scheduler, dict) else None
    if isinstance(last_ms, int):
        next_ms = last_ms + int(dispatch_interval * 1000)
        if datetime_to_unix_time_ms(now) < next_ms:
            return False
    return True


def _record_chunk_dispatch(parent: VideoParseTask, now: datetime) -> None:
    detail = _ensure_runtime(parent.detail)
    runtime = deepcopy(detail[RUNTIME_DETAIL_KEY])
    runtime["chunk_scheduler"] = {
        "last_dispatch_time_ms": datetime_to_unix_time_ms(now)
    }
    detail[RUNTIME_DETAIL_KEY] = runtime
    parent.detail = detail
    parent.update_time = datetime_to_unix_time_ms(now)


def _schedule_wait_seconds(
    parent: VideoParseTask,
    rows: list[tuple[ChunkParseTask, VideoChunk]],
    now: datetime,
) -> float:
    due = [
        task.next_execute_time
        for task, _chunk in rows
        if task.status == ChunkParseTaskStatus.QUEUED.value
        and task.next_execute_time > now
    ]
    configured = parent.parse_config["dispatch_interval_seconds"]
    if due:
        seconds = min((value - now).total_seconds() for value in due)
        return max(0.05, min(2.0, seconds, configured or 2.0))
    return max(0.05, min(2.0, configured or 2.0))


def _require_lease_duration(value: timedelta) -> None:
    if value <= timedelta(0):
        raise ValueError("lease_duration must be positive")


def _capacity_acquired():
    return VideoParseTask.detail[RUNTIME_DETAIL_KEY][CAPACITY_ACQUIRED_KEY].as_boolean().is_(True)


def _eligible_parent(parent: VideoParseTask | None) -> bool:
    return bool(parent is not None and not parent.is_deleted and _accepted(parent)
                and parent.capacity_acquired and parent.status in {"PARSING", "INDEXING"}
                and _video_failure_status(parent) is None)


def _phase_capacity(session: Session, parent: VideoParseTask, phase: str) -> bool:
    execution = _execution_config(parent.parse_config)
    if phase == "refine":
        statement = select(func.count(IntervalParseTask.id)).where(
            IntervalParseTask.video_task_id == parent.id, IntervalParseTask.status == "REFINING",
        )
    else:
        statement = select(func.count(ChunkParseTask.id)).where(
            ChunkParseTask.video_task_id == parent.id,
            ChunkParseTask.phase == (ChunkTaskPhase.PARSE if phase == "parse" else ChunkTaskPhase.INDEX), ChunkParseTask.status == "RUNNING",
        )
    return int(session.scalar(statement) or 0) < execution[phase + "_concurrency"]


def _claim_video_row(task: VideoParseTask, worker: str, now: datetime, duration: timedelta) -> VideoParseLease:
    if task.status == "PENDING":
        task.status = "PREPARING"
    task.attempt_count += 1
    task.lease_owner = worker
    task.lease_version += 1
    task.lease_expire_time = now + duration
    task.heartbeat_time = now
    task.start_time = task.start_time or now
    task.finish_time = None
    task.last_error = None
    task.update_time = datetime_to_unix_time_ms(now)
    return VideoParseLease(
        video_parse_task_id=task.id, video_id=task.video_id, worker_id=worker,
        lease_version=task.lease_version, lease_expire_time=task.lease_expire_time,
        attempt_count=task.attempt_count, max_attempts=task.max_attempts,
        parse_config=deepcopy(task.parse_config),
    )


def _claim_chunk_row(child: ChunkParseTask, parent: VideoParseTask, worker: str, now: datetime,
                     duration: timedelta) -> ChunkParseLease:
    execution = _execution_config(parent.parse_config)
    phase = "parse" if child.phase == ChunkTaskPhase.PARSE else "index"
    deadline = now + timedelta(seconds=execution[phase + "_execution_timeout_seconds"])
    child.status = "RUNNING"
    child.lease_owner = worker
    child.lease_version += 1
    child.lease_expire_time = min(now + duration, deadline)
    child.start_time = now
    child.finish_time = None
    child.last_error = None
    child.update_time = datetime_to_unix_time_ms(now)
    return ChunkParseLease(
        chunk_parse_task_id=child.id, video_parse_task_id=parent.id, video_chunk_id=child.chunk_id,
        phase=child.phase, worker_id=worker, lease_version=child.lease_version,
        lease_expire_time=child.lease_expire_time, execution_deadline=deadline,
        retry_count=child.retry_count, max_retry_count=execution["max_retry_count"],
        retry_initial_delay_seconds=execution["retry_initial_delay_seconds"],
        retry_max_delay_seconds=execution["retry_max_delay_seconds"],
    )


def _execution_deadline(task, parent: VideoParseTask) -> datetime:
    if task.start_time is None:
        raise ValueError("active Parse task has no execution start time")
    phase = ("refine" if isinstance(task, IntervalParseTask) else
             "parse" if task.phase == ChunkTaskPhase.PARSE else "index")
    return task.start_time + timedelta(seconds=_execution_config(parent.parse_config)[phase + "_execution_timeout_seconds"])


def _matches_child(task, parent: VideoParseTask, lease, now: datetime, *, allow_deadline: bool = False) -> bool:
    chunk = isinstance(lease, ChunkParseLease)
    return bool(task is not None and task.video_task_id == lease.video_parse_task_id
                and task.status == ("RUNNING" if chunk else "REFINING")
                and (task.phase == lease.phase and task.chunk_id == lease.video_chunk_id if chunk else (
                    lease.phase == "REFINING" and tuple(task.chunk_task_ids) == lease.chunk_parse_task_ids
                    and task.start_sequence_no == lease.start_sequence_no and task.chunk_count == lease.chunk_count
                ))
                and task.lease_owner == lease.worker_id and task.lease_version == lease.lease_version
                and task.lease_expire_time is not None and task.start_time is not None
                and (task.lease_expire_time > now and _execution_deadline(task, parent) > now
                     or allow_deadline and _execution_deadline(task, parent) <= now))


def _fail_child(child, parent: VideoParseTask, now: datetime, error: str,
                *, retry_at: datetime | None = None) -> None:
    execution = _execution_config(parent.parse_config)
    child.retry_count += 1
    exhausted = child.retry_count > execution["max_retry_count"]
    if isinstance(child, ChunkParseTask):
        child.status = "FAILED" if exhausted else "QUEUED"
    else:
        child.status = "PARSING_FAILED" if exhausted else "WAITING"
    child.next_execute_time = retry_at or now + task_retry_delay(
        execution["retry_initial_delay_seconds"], execution["retry_max_delay_seconds"], child.retry_count,
    )
    child.lease_owner = None
    child.lease_expire_time = None
    child.finish_time = now if exhausted else None
    child.last_error = _error_detail(error)
    child.update_time = datetime_to_unix_time_ms(now)
