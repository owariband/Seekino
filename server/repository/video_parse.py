from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker

from ..identity import derive_video_chunk_id
from ..model.dto.jobDTO import (
    ChunkParseContentState,
    ChunkExecutionState,
    ChunkParseLease,
    IndexableChunkState,
    IntervalIndexingSnapshot,
    IntervalMemberSnapshot,
    IntervalParseLease,
    ModelCallReservation,
    PrefixParseLease,
    PreparedVideoParseState,
    VideoParseTaskInputState,
    VideoParseTaskState,
    VideoParseLease,
    VideoChunkState,
)
from ..model.job import (
    ChunkParseContentRecord,
    ChunkParseTaskRecord,
    ChunkParseTaskStatus,
    IntervalParseContentRecord,
    IntervalParseTaskRecord,
    IntervalParseTaskStatus,
    VideoParseTaskRecord,
    VideoParseTaskStatus,
    interval_full_ready_mask,
    interval_ready_mask_bit,
    validate_interval_parse_task_members,
)
from ..model.video import SeriesRecord, VideoChunkRecord, VideoRecord
from ..utils.time import (
    datetime_to_unix_time_ms,
    now_utc,
    unix_time_ms_to_datetime,
)


RUNTIME_DETAIL_KEY = "_worker_runtime_v2"
INDEX_EMBEDDINGS_DETAIL_KEY = "_index_embeddings_v2"
_REFINE_SUBMITTED = {
    IntervalParseTaskStatus.INDEXING.value,
    IntervalParseTaskStatus.INDEXING_FAILED.value,
    IntervalParseTaskStatus.COMPLETE.value,
}


class VideoParseRepositoryError(RuntimeError):
    pass


class VideoParseTaskNotFoundError(VideoParseRepositoryError):
    pass


class VideoParseConflictError(VideoParseRepositoryError):
    pass


class VideoParseLeaseLostError(VideoParseRepositoryError):
    pass


class ModelCallBudgetExhaustedError(VideoParseRepositoryError):
    pass


class ModelCallConcurrencyBusyError(VideoParseRepositoryError):
    pass


class ModelCallLeaseConflictError(VideoParseRepositoryError):
    pass


def invalidate_series_prefixes_from(
    session: Session,
    *,
    series_id: str,
    episode_order: int,
    video_id: str,
    now: datetime,
) -> tuple[str, ...]:
    """Fence and reset the latest enabled Prefix chain from one episode.

    The caller owns the transaction so task creation, retry takeover, or an
    episode-order edit can invalidate dependent Series compact state
    atomically. Only parent rows are locked; content rows are deliberately
    outside this operation's lock set.
    """

    rows = list(session.execute(
        select(
            VideoParseTaskRecord.id,
            VideoParseTaskRecord.video_id,
            VideoParseTaskRecord.version,
            VideoRecord.episode_order,
        )
        .join(
            VideoRecord,
            VideoRecord.video_id == VideoParseTaskRecord.video_id,
        )
        .where(
            VideoRecord.series_id == series_id,
            VideoParseTaskRecord.is_deleted.is_(False),
            or_(
                VideoRecord.episode_order > episode_order,
                and_(
                    VideoRecord.episode_order == episode_order,
                    VideoRecord.video_id >= video_id,
                ),
            ),
        )
        .order_by(
            VideoRecord.episode_order,
            VideoRecord.video_id,
            VideoParseTaskRecord.version.desc(),
            VideoParseTaskRecord.id.desc(),
        )
    ))
    latest_by_video: dict[str, tuple[int, str, str]] = {}
    for task_id, task_video_id, _version, task_episode_order in rows:
        latest_by_video.setdefault(
            task_video_id,
            (task_episode_order, task_video_id, task_id),
        )

    locked: list[VideoParseTaskRecord] = []
    for _order, task_video_id, task_id in sorted(latest_by_video.values()):
        task = session.scalar(
            select(VideoParseTaskRecord)
            .where(
                VideoParseTaskRecord.id == task_id,
                VideoParseTaskRecord.is_deleted.is_(False),
            )
            .with_for_update()
        )
        if task is None:
            raise VideoParseConflictError(
                "Prefix invalidation frontier changed; retry the transaction"
            )
        if _latest_video_task_id(session, task_video_id) != task.id:
            raise VideoParseConflictError(
                "Prefix invalidation frontier changed; retry the transaction"
            )
        if task.parse_config.get("compact_enabled", False):
            locked.append(task)

    now_ms = datetime_to_unix_time_ms(now)
    for task in locked:
        prefix = _ensure_runtime(task.detail)[RUNTIME_DETAIL_KEY]["prefix"]
        expires_at_ms = prefix.get("lease_expire_time_ms")
        if (
            prefix.get("lease_owner") is not None
            and isinstance(expires_at_ms, int)
            and expires_at_ms > now_ms
        ):
            raise VideoParseConflictError(
                "cannot invalidate Series Prefix chain with an active lease"
            )

    invalidated: list[str] = []
    for task in locked:
        detail = _ensure_runtime(task.detail)
        runtime = deepcopy(detail[RUNTIME_DETAIL_KEY])
        prefix = deepcopy(runtime["prefix"])
        prefix.update({
            "next_chunk_sequence_no": 0,
            "video_prefix_compact": "",
            "series_prefix_compact": "",
            "lease_owner": None,
            "lease_expire_time_ms": None,
            "execution_task_id": None,
            "next_execute_time_ms": now_ms,
            "retry_count": 0,
            "last_error": None,
            "lease_version": int(prefix.get("lease_version", 0)) + 1,
        })
        runtime["prefix"] = prefix
        detail[RUNTIME_DETAIL_KEY] = runtime
        task.detail = detail
        task.update_time = now
        invalidated.append(task.id)
    return tuple(invalidated)


class VideoParseRepository:
    """Transactional persistence for the V2 parse pipeline.

    Model and Elasticsearch calls must happen outside these methods. Every
    mutating method either fences a Worker lease or locks the parent task row.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def load_task_input(self, video_parse_task_id: str) -> VideoParseTaskInputState:
        with self._session_factory() as session:
            row = session.execute(
                select(VideoParseTaskRecord, VideoRecord, SeriesRecord)
                .join(VideoRecord, VideoRecord.video_id == VideoParseTaskRecord.video_id)
                .join(SeriesRecord, SeriesRecord.series_id == VideoRecord.series_id)
                .where(
                    VideoParseTaskRecord.id == video_parse_task_id,
                    VideoParseTaskRecord.is_deleted.is_(False),
                )
            ).one_or_none()
            if row is None:
                raise VideoParseTaskNotFoundError(
                    f"VideoParseTask {video_parse_task_id!r} does not exist"
                )
            task, video, series = row
            return _task_input_state(task, video, workspace_id=series.workspace_id)

    def ensure_prepared(
        self,
        lease: VideoParseLease,
        video_chunk_ids: Sequence[str],
        *,
        now: datetime | None = None,
        _activate: bool = True,
    ) -> PreparedVideoParseState:
        """Idempotently freeze Chunk and Interval children for a claimed task."""

        prepared_at = now if now is not None else now_utc()
        ordered_chunk_ids = tuple(video_chunk_ids)
        if not ordered_chunk_ids or len(set(ordered_chunk_ids)) != len(ordered_chunk_ids):
            raise ValueError("video_chunk_ids must be a nonempty unique sequence")

        with self._session_factory.begin() as session:
            task = _locked_video_lease(session, lease, prepared_at)
            video = session.get(VideoRecord, task.video_id)
            if video is None:
                raise VideoParseConflictError("VideoParseTask Video no longer exists")
            chunk_rows = list(session.scalars(
                select(VideoChunkRecord)
                .where(VideoChunkRecord.video_chunk_id.in_(ordered_chunk_ids))
                .order_by(VideoChunkRecord.sequence_no)
                .with_for_update()
            ))
            if (
                len(chunk_rows) != len(ordered_chunk_ids)
                or tuple(row.video_chunk_id for row in chunk_rows) != ordered_chunk_ids
                or any(row.video_id != task.video_id for row in chunk_rows)
                or [row.sequence_no for row in chunk_rows] != list(range(len(chunk_rows)))
                or len(chunk_rows) != task.chunk_count
            ):
                raise VideoParseConflictError(
                    "VideoParseTask chunks do not match its fixed ordered topology"
                )
            _validate_chunk_boundaries(video, task.parse_config, chunk_rows)

            existing_chunks = list(session.scalars(
                select(ChunkParseTaskRecord)
                .where(ChunkParseTaskRecord.video_parse_task_id == task.id)
                .order_by(ChunkParseTaskRecord.id)
                .with_for_update()
            ))
            by_video_chunk = {item.video_chunk_id: item for item in existing_chunks}
            if len(by_video_chunk) != len(existing_chunks):
                raise VideoParseConflictError("duplicate ChunkParseTask topology")
            if set(by_video_chunk) - set(ordered_chunk_ids):
                raise VideoParseConflictError("existing ChunkParseTask topology is incompatible")

            chunk_tasks: list[ChunkParseTaskRecord] = []
            for chunk in chunk_rows:
                child = by_video_chunk.get(chunk.video_chunk_id)
                if child is None:
                    child = ChunkParseTaskRecord(
                        id=str(uuid4()),
                        video_parse_task_id=task.id,
                        video_chunk_id=chunk.video_chunk_id,
                        status=ChunkParseTaskStatus.QUEUED.value,
                        next_execute_time=prepared_at,
                        retry_count=0,
                        lease_version=0,
                        detail={},
                        create_time=prepared_at,
                        update_time=prepared_at,
                    )
                    session.add(child)
                    session.flush()
                chunk_tasks.append(child)

            interval_specs = _interval_specs(
                len(chunk_tasks), _interval_size(task.parse_config)
            )
            existing_intervals = list(session.scalars(
                select(IntervalParseTaskRecord)
                .where(IntervalParseTaskRecord.video_parse_task_id == task.id)
                .order_by(IntervalParseTaskRecord.start_sequence_no)
                .with_for_update()
            ))
            by_start = {item.start_sequence_no: item for item in existing_intervals}
            if len(by_start) != len(existing_intervals):
                raise VideoParseConflictError("duplicate IntervalParseTask topology")
            if set(by_start) - {start for start, _count in interval_specs}:
                raise VideoParseConflictError("existing IntervalParseTask topology is incompatible")

            intervals: list[IntervalParseTaskRecord] = []
            for start, count in interval_specs:
                members = chunk_tasks[start:start + count]
                member_ids = [member.id for member in members]
                interval = by_start.get(start)
                if interval is None:
                    ready_mask = 0
                    for index, member in enumerate(members):
                        if member.status == ChunkParseTaskStatus.COMPLETE.value:
                            ready_mask |= interval_ready_mask_bit(index, count)
                    interval = IntervalParseTaskRecord(
                        id=str(uuid4()),
                        chunk_parse_task_id=member_ids[0],
                        video_parse_task_id=task.id,
                        chunk_parse_task_ids=member_ids,
                        start_sequence_no=start,
                        chunk_count=count,
                        ready_mask=ready_mask,
                        status=IntervalParseTaskStatus.WAITING.value,
                        next_execute_time=prepared_at,
                        retry_count=0,
                        lease_version=0,
                        create_time=prepared_at,
                        update_time=prepared_at,
                    )
                    session.add(interval)
                else:
                    validate_interval_parse_task_members(
                        chunk_parse_task_id=interval.chunk_parse_task_id,
                        video_parse_task_id=task.id,
                        start_sequence_no=start,
                        chunk_count=count,
                        chunk_parse_task_ids=interval.chunk_parse_task_ids,
                        members=[
                            (member, chunk_rows[start + index].sequence_no)
                            for index, member in enumerate(members)
                        ],
                    )
                    if tuple(interval.chunk_parse_task_ids) != tuple(member_ids):
                        raise VideoParseConflictError(
                            "existing IntervalParseTask members are incompatible"
                        )
                intervals.append(interval)

            if _activate:
                task.status = VideoParseTaskStatus.PARSING.value
                task.last_error = None
            task.update_time = prepared_at
            task.detail = _ensure_runtime(task.detail)
            video.parse_status = (
                VideoParseTaskStatus.PARSING.value if _activate else task.status
            )
            video.parse_progress = 0
            session.flush()
            return PreparedVideoParseState(
                video_parse_task_id=task.id,
                chunk_parse_task_ids=tuple(item.id for item in chunk_tasks),
                interval_parse_task_ids=tuple(item.id for item in intervals),
            )

    def ensure_video_chunks(
        self,
        lease: VideoParseLease,
        plans: Sequence[tuple[int, int, int]],
        *,
        now: datetime | None = None,
    ) -> tuple[VideoChunkState, ...]:
        """Create the immutable source-time topology while holding Prepare lease."""

        prepared_at = now if now is not None else now_utc()
        normalized = tuple(plans)
        if [item[0] for item in normalized] != list(range(len(normalized))):
            raise ValueError("plans must have consecutive sequence numbers")
        with self._session_factory.begin() as session:
            task = _locked_video_lease(session, lease, prepared_at)
            if task.status != VideoParseTaskStatus.PREPARING.value:
                raise VideoParseConflictError("Video Chunk preparation is already complete")
            video = session.get(VideoRecord, task.video_id)
            if video is None:
                raise VideoParseConflictError("VideoParseTask Video no longer exists")
            if len(normalized) != task.chunk_count:
                raise VideoParseConflictError("Chunk plan count changed after task creation")
            expected = [
                (index, index * _positive_int(task.parse_config.get("chunk_duration"), "chunk_duration"),
                 min(video.duration_seconds, (index + 1) * task.parse_config["chunk_duration"]))
                for index in range(task.chunk_count)
            ]
            if list(normalized) != expected:
                raise VideoParseConflictError("Chunk plans do not match the frozen parse config")
            existing = {
                item.sequence_no: item
                for item in session.scalars(
                    select(VideoChunkRecord)
                    .where(VideoChunkRecord.video_id == video.video_id)
                    .order_by(VideoChunkRecord.sequence_no)
                    .with_for_update()
                )
            }
            if set(existing) - set(range(task.chunk_count)):
                raise VideoParseConflictError("Video has an incompatible fixed topology")
            chunks: list[VideoChunkRecord] = []
            for sequence_no, start_seconds, end_seconds in normalized:
                chunk = existing.get(sequence_no)
                if chunk is None:
                    source_ref = video.inference_proxy_uri or video.original_source_uri
                    chunk = VideoChunkRecord(
                        video_chunk_id=derive_video_chunk_id(video.video_id, sequence_no),
                        video_id=video.video_id,
                        sequence_no=sequence_no,
                        start_seconds=start_seconds,
                        end_seconds=end_seconds,
                        analysis_window_start_seconds=start_seconds,
                        analysis_window_end_seconds=end_seconds,
                        chunk_media_ref=source_ref,
                        analysis_window_media_ref=source_ref,
                        transcript=[],
                        baseline_visual_caption=None,
                        keyframe_uris=[],
                        created_at=prepared_at,
                        updated_at=prepared_at,
                    )
                    session.add(chunk)
                elif (chunk.start_seconds, chunk.end_seconds) != (
                    start_seconds, end_seconds
                ):
                    raise VideoParseConflictError(
                        "existing Video Chunk topology differs from the plan"
                    )
                chunks.append(chunk)
            session.flush()
            return tuple(_video_chunk_state(item) for item in chunks)

    def ensure_parse_tasks(
        self,
        lease: VideoParseLease,
        video_chunk_ids: Sequence[str],
        interval_specs: Sequence[tuple[int, int]],
        *,
        now: datetime | None = None,
    ) -> PreparedVideoParseState:
        expected = _interval_specs(
            len(video_chunk_ids), _interval_size(lease.parse_config)
        )
        if tuple(interval_specs) != expected:
            raise VideoParseConflictError(
                "Interval plans do not match the frozen parse config"
            )
        return self.ensure_prepared(
            lease,
            video_chunk_ids,
            now=now,
            _activate=False,
        )

    def activate_video_task(
        self,
        lease: VideoParseLease,
        *,
        now: datetime | None = None,
    ) -> VideoParseTaskState:
        activated_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            task = _locked_video_lease(session, lease, activated_at)
            if task.status != VideoParseTaskStatus.PREPARING.value:
                raise VideoParseConflictError("only PREPARING tasks can be activated")
            chunk_count = len(list(session.scalars(
                select(ChunkParseTaskRecord.id).where(
                    ChunkParseTaskRecord.video_parse_task_id == task.id
                )
            )))
            interval_count = len(list(session.scalars(
                select(IntervalParseTaskRecord.id).where(
                    IntervalParseTaskRecord.video_parse_task_id == task.id
                )
            )))
            if chunk_count != task.chunk_count or interval_count != len(
                _interval_specs(task.chunk_count, _interval_size(task.parse_config))
            ):
                raise VideoParseConflictError("VideoParseTask preparation is incomplete")
            task.status = VideoParseTaskStatus.PARSING.value
            task.last_error = None
            task.update_time = activated_at
            video = session.get(VideoRecord, task.video_id)
            if video is not None:
                video.parse_status = task.status
                video.parse_progress = 0
            session.flush()
            return _task_state(task)

    def commit_chunk_result(
        self,
        lease: ChunkParseLease,
        *,
        content: Mapping[str, Any],
        detail: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        committed_at = now if now is not None else now_utc()
        payload = deepcopy(dict(content))
        if not payload:
            raise ValueError("content must not be empty")
        with self._session_factory.begin() as session:
            intervals = _locked_parent_intervals(
                session, lease.video_parse_task_id
            )
            task = _locked_chunk_lease(session, lease, committed_at)
            chunk = session.get(VideoChunkRecord, task.video_chunk_id)
            if chunk is None:
                raise VideoParseConflictError("ChunkParseTask VideoChunk no longer exists")
            result = session.scalar(
                select(ChunkParseContentRecord)
                .where(ChunkParseContentRecord.chunk_parse_task_id == task.id)
                .with_for_update()
            )
            result_detail = deepcopy(dict(detail or {}))
            result_detail["scope"] = [chunk.sequence_no]
            if result is None:
                result = ChunkParseContentRecord(
                    id=str(uuid4()),
                    chunk_parse_task_id=task.id,
                    content=payload,
                    version=1,
                    detail=result_detail,
                    create_time=committed_at,
                    update_time=committed_at,
                )
                session.add(result)
            else:
                # A valid lease can only reach this branch after an explicitly
                # recovered incomplete commit. Never replace a COMPLETE result.
                result.content = payload
                result.detail = result_detail
                result.update_time = committed_at

            touched: list[str] = []
            for interval in intervals:
                if task.id not in interval.chunk_parse_task_ids:
                    continue
                index = interval.chunk_parse_task_ids.index(task.id)
                was_ready = (
                    interval.ready_mask
                    == interval_full_ready_mask(interval.chunk_count)
                )
                interval.ready_mask |= interval_ready_mask_bit(index, interval.chunk_count)
                if (
                    not was_ready
                    and interval.ready_mask
                    == interval_full_ready_mask(interval.chunk_count)
                ):
                    # `next_execute_time` doubles as the durable ready-at
                    # ordering key. An Interval that becomes ready later must
                    # not be hidden behind old, incomplete windows.
                    interval.next_execute_time = committed_at
                interval.update_time = committed_at
                touched.append(interval.id)

            task.status = ChunkParseTaskStatus.COMPLETE.value
            task.finish_time = committed_at
            task.lease_owner = None
            task.lease_expire_time = None
            task.last_error = None
            task.update_time = committed_at
            session.flush()
            return tuple(touched)

    def load_chunk_execution(
        self,
        lease: ChunkParseLease,
        *,
        now: datetime | None = None,
    ) -> ChunkExecutionState:
        loaded_at = now if now is not None else now_utc()
        with self._session_factory() as session:
            task = session.get(ChunkParseTaskRecord, lease.chunk_parse_task_id)
            if not _matching_chunk_lease(task, lease, loaded_at):
                raise VideoParseLeaseLostError(
                    "ChunkParseTask lease is no longer current"
                )
            row = session.execute(
                select(
                    VideoChunkRecord,
                    VideoParseTaskRecord,
                    VideoRecord,
                    SeriesRecord,
                )
                .select_from(VideoChunkRecord)
                .join(
                    VideoParseTaskRecord,
                    VideoParseTaskRecord.id == task.video_parse_task_id,
                )
                .join(VideoRecord, VideoRecord.video_id == VideoParseTaskRecord.video_id)
                .join(SeriesRecord, SeriesRecord.series_id == VideoRecord.series_id)
                .where(VideoChunkRecord.video_chunk_id == task.video_chunk_id)
            ).one_or_none()
            if row is None:
                raise VideoParseConflictError("Chunk execution identity is incomplete")
            chunk, parent, video, series = row
            source_media_ref = (
                video.inference_proxy_uri or video.original_source_uri
            )
            return ChunkExecutionState(
                chunk_parse_task_id=task.id,
                video_parse_task_id=task.video_parse_task_id,
                video_chunk_id=chunk.video_chunk_id,
                sequence_no=chunk.sequence_no,
                start_seconds=chunk.start_seconds,
                end_seconds=chunk.end_seconds,
                analysis_window_start_seconds=chunk.analysis_window_start_seconds,
                analysis_window_end_seconds=chunk.analysis_window_end_seconds,
                source_media_ref=source_media_ref,
                chunk_media_ref=chunk.chunk_media_ref,
                analysis_window_media_ref=chunk.analysis_window_media_ref,
                task_input=_task_input_state(
                    parent, video, workspace_id=series.workspace_id
                ),
            )

    def load_interval_members(
        self,
        lease: IntervalParseLease,
        *,
        now: datetime | None = None,
    ) -> IntervalMemberSnapshot:
        loaded_at = now if now is not None else now_utc()
        with self._session_factory() as session:
            interval = _interval_for_lease(session, lease, loaded_at)
            rows = _member_content_rows(session, interval, for_update=False)
            return IntervalMemberSnapshot(
                interval_parse_task_id=interval.id,
                video_parse_task_id=interval.video_parse_task_id,
                start_sequence_no=interval.start_sequence_no,
                chunk_count=interval.chunk_count,
                members=tuple(_content_state(*row) for row in rows),
            )

    def commit_interval_refinement(
        self,
        lease: IntervalParseLease,
        *,
        interval_content: Mapping[str, Any],
        member_updates: Mapping[str, Mapping[str, Any]],
        detail: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        """Apply only explicitly changed fields to current member content.

        Input versions are deliberately not a precondition: overlapping
        Intervals merge onto the rows currently locked in the database.
        """

        committed_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            locked_intervals = _locked_parent_intervals(
                session, lease.video_parse_task_id
            )
            interval = _locked_interval_lease(
                session, lease, committed_at, IntervalParseTaskStatus.REFINING.value
            )
            if session.get(IntervalParseContentRecord, interval.id) is not None:
                raise VideoParseConflictError("Interval refinement was already committed")
            unexpected = set(member_updates) - set(interval.chunk_parse_task_ids)
            if unexpected:
                raise VideoParseConflictError("refinement updates a nonmember Chunk")

            rows = _member_content_rows(session, interval, for_update=True)
            member_scope = list(
                range(
                    interval.start_sequence_no,
                    interval.start_sequence_no + interval.chunk_count,
                )
            )
            changed_member_ids: list[str] = []
            for chunk_task, _chunk, result in rows:
                current = deepcopy(dict(result.content or {}))
                updated = deepcopy(current)
                for field, value in member_updates.get(chunk_task.id, {}).items():
                    if field not in current:
                        raise VideoParseConflictError(
                            f"refinement cannot add unknown field {field!r}"
                        )
                    updated[field] = deepcopy(value)
                content_changed = updated != current
                if content_changed:
                    result.content = updated
                    result.version += 1
                    changed_member_ids.append(chunk_task.id)
                current_detail = deepcopy(dict(result.detail or {}))
                current_detail["scope"] = _ordered_int_union(
                    current_detail.get("scope"), member_scope
                )
                result.detail = current_detail
                result.update_time = committed_at

            session.add(IntervalParseContentRecord(
                interval_parse_task_id=interval.id,
                content=deepcopy(dict(interval_content)),
                detail={
                    **deepcopy(dict(detail or {})),
                    "indexed_versions": {},
                    "refinement_committed_lease_version": lease.lease_version,
                },
                create_time=committed_at,
                update_time=committed_at,
            ))
            interval.status = IntervalParseTaskStatus.INDEXING.value
            interval.lease_owner = None
            interval.lease_expire_time = None
            interval.last_error = None
            interval.finish_time = None
            interval.next_execute_time = committed_at
            interval.update_time = committed_at

            reset_intervals = _reset_indexing_for_changed_members(
                session,
                interval.video_parse_task_id,
                changed_member_ids,
                committed_at,
                exclude_interval_id=interval.id,
                locked_intervals=locked_intervals,
            )
            session.flush()
            return tuple(sorted({interval.id, *reset_intervals}))

    def load_interval_indexing_snapshot(
        self,
        lease: IntervalParseLease,
        *,
        now: datetime | None = None,
    ) -> IntervalIndexingSnapshot:
        loaded_at = now if now is not None else now_utc()
        with self._session_factory() as session:
            interval = _interval_for_lease(session, lease, loaded_at)
            if interval.status != IntervalParseTaskStatus.INDEXING.value:
                raise VideoParseLeaseLostError("Interval is no longer indexing")
            result = session.get(IntervalParseContentRecord, interval.id)
            if result is None:
                raise VideoParseConflictError("INDEXING Interval has no Refine result")
            indexed_versions = _indexed_versions(result.detail)
            submitted = _submitted_intervals_by_member(
                session, interval.video_parse_task_id
            )
            parent = session.get(VideoParseTaskRecord, interval.video_parse_task_id)
            video = session.get(VideoRecord, parent.video_id) if parent is not None else None
            series = session.get(SeriesRecord, video.series_id) if video is not None else None
            if parent is None or video is None or series is None:
                raise VideoParseConflictError("Interval parent identity is incomplete")

            chunks: list[IndexableChunkState] = []
            source_media_ref = (
                video.inference_proxy_uri or video.original_source_uri
            )
            for chunk_task, chunk, content in _member_content_rows(
                session, interval, for_update=False
            ):
                covering = submitted.get(chunk_task.id, ())
                if not covering or any(item.status not in _REFINE_SUBMITTED for item in covering):
                    continue
                chunks.append(IndexableChunkState(
                    chunk_parse_task_id=chunk_task.id,
                    video_chunk_id=chunk.video_chunk_id,
                    sequence_no=chunk.sequence_no,
                    content=deepcopy(dict(content.content or {})),
                    content_version=content.version,
                    content_detail=deepcopy(dict(content.detail or {})),
                    source_media_ref=source_media_ref,
                    chunk_media_ref=chunk.chunk_media_ref,
                    analysis_window_media_ref=chunk.analysis_window_media_ref,
                    workspace_id=series.workspace_id,
                    series_id=series.series_id,
                    video_id=video.video_id,
                    start_seconds=chunk.start_seconds,
                    end_seconds=chunk.end_seconds,
                ))
            return IntervalIndexingSnapshot(
                interval_parse_task_id=interval.id,
                video_parse_task_id=interval.video_parse_task_id,
                chunks=tuple(chunks),
                indexed_versions=indexed_versions,
            )

    def complete_interval_indexing(
        self,
        lease: IntervalParseLease,
        indexed_versions: Mapping[str, int],
        *,
        dependency_poll_delay: timedelta = timedelta(seconds=1),
        now: datetime | None = None,
    ) -> VideoParseTaskState:
        if dependency_poll_delay <= timedelta(0):
            raise ValueError("dependency_poll_delay must be positive")
        completed_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            _locked_parent_intervals(session, lease.video_parse_task_id)
            interval = _locked_interval_lease(
                session, lease, completed_at, IntervalParseTaskStatus.INDEXING.value
            )
            result = session.get(
                IntervalParseContentRecord, interval.id, with_for_update=True
            )
            if result is None:
                raise VideoParseConflictError("INDEXING Interval has no Refine result")
            recorded = _indexed_versions(result.detail)
            submitted = _submitted_intervals_by_member(
                session, interval.video_parse_task_id, for_update=True
            )
            rows = _member_content_rows(session, interval, for_update=True)
            current_versions = {task.id: content.version for task, _chunk, content in rows}
            for chunk_task_id, version in indexed_versions.items():
                if chunk_task_id not in interval.chunk_parse_task_ids:
                    raise VideoParseConflictError("indexed Chunk is not an Interval member")
                covering = submitted.get(chunk_task_id, ())
                if not covering or any(item.status not in _REFINE_SUBMITTED for item in covering):
                    raise VideoParseConflictError("indexed Chunk is not released")
                if current_versions[chunk_task_id] != version:
                    raise VideoParseConflictError("indexed Chunk content version changed")
                recorded[chunk_task_id] = version

            all_current = all(
                recorded.get(task_id) == version
                for task_id, version in current_versions.items()
            )
            result_detail = {
                **deepcopy(dict(result.detail or {})),
                "indexed_versions": recorded,
            }
            if all_current:
                result_detail["indexing_committed_lease_version"] = (
                    lease.lease_version
                )
            result.detail = result_detail
            result.update_time = completed_at
            interval.status = (
                IntervalParseTaskStatus.COMPLETE.value
                if all_current
                else IntervalParseTaskStatus.INDEXING.value
            )
            interval.finish_time = completed_at if all_current else None
            interval.lease_owner = None
            interval.lease_expire_time = None
            interval.last_error = None
            interval.next_execute_time = (
                completed_at if all_current else completed_at + dependency_poll_delay
            )
            interval.update_time = completed_at
            task = session.get(VideoParseTaskRecord, interval.video_parse_task_id)
            if task is None:
                raise VideoParseTaskNotFoundError(
                    f"VideoParseTask {interval.video_parse_task_id!r} does not exist"
                )
            session.flush()
            return _task_state(task)

    def save_index_embeddings(
        self,
        lease: IntervalParseLease,
        chunk_parse_task_id: str,
        content_version: int,
        representations: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> None:
        """Persist reusable encoding results behind the current index lease."""

        saved_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            _locked_parent_intervals(session, lease.video_parse_task_id)
            interval = _locked_interval_lease(
                session, lease, saved_at, IntervalParseTaskStatus.INDEXING.value
            )
            if chunk_parse_task_id not in interval.chunk_parse_task_ids:
                raise VideoParseConflictError("embedding target is not an Interval member")
            covering = _submitted_intervals_by_member(
                session, interval.video_parse_task_id, for_update=True
            ).get(chunk_parse_task_id, ())
            if not covering or any(item.status not in _REFINE_SUBMITTED for item in covering):
                raise VideoParseConflictError("embedding target is not released")
            content = session.scalar(
                select(ChunkParseContentRecord)
                .where(ChunkParseContentRecord.chunk_parse_task_id == chunk_parse_task_id)
                .with_for_update()
            )
            if content is None or content.version != content_version:
                raise VideoParseConflictError("embedding target content version changed")
            detail = deepcopy(dict(content.detail or {}))
            cached = detail.get(INDEX_EMBEDDINGS_DETAIL_KEY)
            cached_representations: dict[str, Any] = {}
            if (
                isinstance(cached, dict)
                and cached.get("content_version") == content_version
                and isinstance(cached.get("representations"), dict)
            ):
                cached_representations = deepcopy(cached["representations"])
            cached_representations.update(deepcopy(dict(representations)))
            detail[INDEX_EMBEDDINGS_DETAIL_KEY] = {
                "content_version": content_version,
                "representations": cached_representations,
            }
            content.detail = detail
            content.update_time = saved_at

    def request_index_rebuild(
        self,
        video_parse_task_id: str,
        *,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        """Reset submitted Intervals for an index-only rebuild.

        Refine results, Chunk completion, indexing checkpoints, and reusable
        embeddings remain intact. Incrementing each Interval lease version
        explicitly fences any index Worker that was active before the reset.
        """

        requested_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            intervals = _locked_parent_intervals(session, video_parse_task_id)
            task = session.scalar(
                select(VideoParseTaskRecord)
                .where(VideoParseTaskRecord.id == video_parse_task_id)
                .with_for_update()
            )
            if task is None or task.is_deleted:
                raise VideoParseTaskNotFoundError(
                    f"VideoParseTask {video_parse_task_id!r} does not exist"
                )
            if _latest_video_task_id(session, task.video_id) != task.id:
                raise VideoParseConflictError(
                    "only the latest VideoParseTask can rebuild its index"
                )

            refined_interval_ids = set(session.scalars(
                select(IntervalParseContentRecord.interval_parse_task_id).where(
                    IntervalParseContentRecord.interval_parse_task_id.in_(
                        tuple(interval.id for interval in intervals)
                    )
                )
            )) if intervals else set()
            reset: list[str] = []
            for interval in intervals:
                if (
                    interval.status not in _REFINE_SUBMITTED
                    or interval.id not in refined_interval_ids
                ):
                    continue
                interval.status = IntervalParseTaskStatus.INDEXING.value
                interval.next_execute_time = requested_at
                interval.retry_count = 0
                interval.lease_owner = None
                interval.lease_version += 1
                interval.lease_expire_time = None
                interval.finish_time = None
                interval.last_error = None
                interval.update_time = requested_at
                reset.append(interval.id)

            task = _aggregate_video_task(session, task.id, requested_at)
            if reset:
                # A completed or exhausted coordinator must be claimable again
                # so it can aggregate the rebuilt Intervals back to COMPLETE.
                task.next_execute_time = requested_at
                task.attempt_count = 0
                task.last_error = None
            session.flush()
            return tuple(reset)

    def aggregate_task(
        self,
        lease: VideoParseLease,
        *,
        now: datetime | None = None,
    ) -> VideoParseTaskState:
        aggregated_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            _locked_video_lease(session, lease, aggregated_at)
            task = _aggregate_video_task(
                session,
                lease.video_parse_task_id,
                aggregated_at,
                release_terminal_lease=True,
            )
            session.flush()
            return _task_state(task)

    def reserve_model_call(
        self,
        lease: VideoParseLease | ChunkParseLease | IntervalParseLease | PrefixParseLease,
        call_id: str,
        *,
        lease_duration: timedelta,
        now: datetime | None = None,
    ) -> ModelCallReservation:
        if not call_id.strip():
            raise ValueError("call_id must not be blank")
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        reserved_at = now if now is not None else now_utc()
        expires_at = reserved_at + lease_duration
        now_ms = datetime_to_unix_time_ms(reserved_at)
        identity = _model_execution_identity(lease)
        with self._session_factory.begin() as session:
            task = _lock_model_execution_lease(session, lease, reserved_at)
            execution = _execution_config(task.parse_config)
            max_calls = _positive_int(execution.get("max_model_calls"), "max_model_calls")
            max_concurrent = _positive_int(
                execution.get("max_concurrent_model_calls"),
                "max_concurrent_model_calls",
            )
            detail = _ensure_runtime(task.detail)
            runtime = deepcopy(detail[RUNTIME_DETAIL_KEY])
            model_calls = deepcopy(runtime["model_calls"])
            active = {
                key: value for key, value in model_calls["active"].items()
                if isinstance(value, dict) and value.get("lease_expire_time_ms", 0) > now_ms
            }
            used = list(model_calls["used_call_ids"])
            existing = active.get(call_id)
            if existing is not None:
                if (
                    existing.get("worker_id") != lease.worker_id
                    or _stored_model_execution_identity(existing) != identity
                ):
                    raise ModelCallLeaseConflictError(
                        "call_id is reserved by another execution lease"
                    )
                first = False
            else:
                first = call_id not in used
                if first and len(used) >= max_calls:
                    raise ModelCallBudgetExhaustedError(
                        "VideoParseTask model-call budget is exhausted"
                    )
                if len(active) >= max_concurrent:
                    raise ModelCallConcurrencyBusyError(
                        "VideoParseTask model-call concurrency is full"
                    )
                if first:
                    used.append(call_id)
            active[call_id] = {
                "worker_id": lease.worker_id,
                "execution_kind": identity[0],
                "execution_task_id": identity[1],
                "execution_lease_version": identity[2],
                "lease_expire_time_ms": datetime_to_unix_time_ms(expires_at),
            }
            model_calls["used_call_ids"] = used
            model_calls["active"] = active
            runtime["model_calls"] = model_calls
            detail[RUNTIME_DETAIL_KEY] = runtime
            task.detail = detail
            task.update_time = reserved_at
            return ModelCallReservation(
                video_parse_task_id=task.id,
                call_id=call_id,
                worker_id=lease.worker_id,
                first_reservation=first,
                used_calls=len(used),
                max_calls=max_calls,
                active_calls=len(active),
                max_concurrent_calls=max_concurrent,
                lease_expire_time=expires_at,
            )

    def renew_model_call(
        self,
        lease: VideoParseLease | ChunkParseLease | IntervalParseLease | PrefixParseLease,
        call_id: str,
        *,
        lease_duration: timedelta,
        now: datetime | None = None,
    ) -> bool:
        """Extend an existing live slot without recreating an expired slot."""

        if not call_id.strip():
            raise ValueError("call_id must not be blank")
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        renewed_at = now if now is not None else now_utc()
        renewed_at_ms = datetime_to_unix_time_ms(renewed_at)
        identity = _model_execution_identity(lease)
        with self._session_factory.begin() as session:
            task = _lock_model_execution_lease(session, lease, renewed_at)
            detail = _ensure_runtime(task.detail)
            runtime = deepcopy(detail[RUNTIME_DETAIL_KEY])
            model_calls = deepcopy(runtime["model_calls"])
            active = deepcopy(model_calls["active"])
            slot = active.get(call_id)
            expires_at_ms = (
                slot.get("lease_expire_time_ms") if isinstance(slot, dict) else None
            )
            if (
                not isinstance(slot, dict)
                or slot.get("worker_id") != lease.worker_id
                or _stored_model_execution_identity(slot) != identity
                or not isinstance(expires_at_ms, int)
                or expires_at_ms <= renewed_at_ms
            ):
                return False
            expires_at = renewed_at + lease_duration
            slot["lease_expire_time_ms"] = datetime_to_unix_time_ms(expires_at)
            active[call_id] = slot
            model_calls["active"] = active
            runtime["model_calls"] = model_calls
            detail[RUNTIME_DETAIL_KEY] = runtime
            task.detail = detail
            task.update_time = renewed_at
            return True

    def release_model_call(
        self,
        video_parse_task_id: str,
        call_id: str,
        worker_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        released_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            task = session.scalar(
                select(VideoParseTaskRecord)
                .where(VideoParseTaskRecord.id == video_parse_task_id)
                .with_for_update()
            )
            if task is None:
                return False
            detail = _ensure_runtime(task.detail)
            runtime = deepcopy(detail[RUNTIME_DETAIL_KEY])
            model_calls = deepcopy(runtime["model_calls"])
            active = deepcopy(model_calls["active"])
            slot = active.get(call_id)
            if slot is None or slot.get("worker_id") != worker_id:
                return False
            del active[call_id]
            model_calls["active"] = active
            runtime["model_calls"] = model_calls
            detail[RUNTIME_DETAIL_KEY] = runtime
            task.detail = detail
            task.update_time = released_at
            return True

    def claim_next_prefix(
        self,
        series_id: str,
        worker_id: str,
        *,
        lease_duration: timedelta,
        now: datetime | None = None,
    ) -> PrefixParseLease | None:
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        if not worker_id.strip():
            raise ValueError("worker_id must not be blank")
        claimed_at = now if now is not None else now_utc()
        now_ms = datetime_to_unix_time_ms(claimed_at)
        expires_at = claimed_at + lease_duration
        with self._session_factory() as session:
            frontier = _series_prefix_frontier(session, series_id)
        if frontier is None:
            return None
        frontier_task_id, _previous_compact = frontier

        with self._session_factory.begin() as session:
            # Lock only the already selected frontier. If another worker owns
            # it, return instead of skipping to a later episode or old task.
            task = session.scalar(
                select(VideoParseTaskRecord)
                .where(VideoParseTaskRecord.id == frontier_task_id)
                .with_for_update(skip_locked=True)
            )
            if task is None or task.is_deleted:
                return None
            current_frontier = _series_prefix_frontier(session, series_id)
            if current_frontier is None or current_frontier[0] != task.id:
                return None
            previous_compact = current_frontier[1]
            config = task.parse_config
            detail = _ensure_runtime(task.detail)
            runtime = deepcopy(detail[RUNTIME_DETAIL_KEY])
            if runtime.get("accepted") is not True:
                return None
            prefix = deepcopy(runtime["prefix"])
            next_execute_ms = prefix.get("next_execute_time_ms", 0)
            if isinstance(next_execute_ms, int) and next_execute_ms > now_ms:
                return None
            max_retries = _nonnegative_int(
                _execution_config(config).get("max_retry_count"),
                "max_retry_count",
            )
            cursor = int(prefix["next_chunk_sequence_no"])
            owner = prefix.get("lease_owner")
            lease_expire_ms = prefix.get("lease_expire_time_ms")
            if (
                owner is not None
                and isinstance(lease_expire_ms, int)
                and lease_expire_ms > now_ms
            ):
                return None
            if owner is not None:
                # Claim-time recovery is the Prefix equivalent of the table
                # lease reaper. Count every abandoned execution before a new
                # owner is allowed to take over, otherwise crashes can retry
                # forever without consuming the configured budget.
                prefix["retry_count"] = int(prefix.get("retry_count", 0)) + 1
                prefix["lease_owner"] = None
                prefix["lease_expire_time_ms"] = None
                prefix["execution_task_id"] = None
                prefix["last_error"] = _error_detail("worker lease expired")
            if int(prefix.get("retry_count", 0)) > max_retries:
                runtime["prefix"] = prefix
                detail[RUNTIME_DETAIL_KEY] = runtime
                task.detail = detail
                task.update_time = claimed_at
                return None
            if cursor >= task.chunk_count or task.status not in {
                VideoParseTaskStatus.PARSING.value,
                VideoParseTaskStatus.INDEXING.value,
                VideoParseTaskStatus.INDEXING_FAILED.value,
                VideoParseTaskStatus.COMPLETE.value,
            }:
                return None
            chunk_row = session.execute(
                select(ChunkParseTaskRecord, VideoChunkRecord, ChunkParseContentRecord)
                .join(VideoChunkRecord, VideoChunkRecord.video_chunk_id == ChunkParseTaskRecord.video_chunk_id)
                .join(ChunkParseContentRecord, ChunkParseContentRecord.chunk_parse_task_id == ChunkParseTaskRecord.id)
                .where(
                    ChunkParseTaskRecord.video_parse_task_id == task.id,
                    VideoChunkRecord.sequence_no == cursor,
                    ChunkParseTaskRecord.status == ChunkParseTaskStatus.COMPLETE.value,
                )
            ).one_or_none()
            if chunk_row is None:
                return None
            chunk_task, chunk, content = chunk_row
            covering = _submitted_intervals_by_member(session, task.id).get(
                chunk_task.id, ()
            )
            if not covering or any(
                item.status not in _REFINE_SUBMITTED for item in covering
            ):
                return None
            prefix["lease_owner"] = worker_id
            prefix["lease_version"] = int(prefix.get("lease_version", 0)) + 1
            prefix["lease_expire_time_ms"] = datetime_to_unix_time_ms(expires_at)
            prefix["execution_task_id"] = chunk_task.id
            prefix["series_prefix_compact"] = previous_compact
            runtime["prefix"] = prefix
            detail[RUNTIME_DETAIL_KEY] = runtime
            task.detail = detail
            task.update_time = claimed_at
            return PrefixParseLease(
                video_parse_task_id=task.id,
                chunk_parse_task_id=chunk_task.id,
                video_chunk_id=chunk.video_chunk_id,
                sequence_no=chunk.sequence_no,
                worker_id=worker_id,
                lease_version=prefix["lease_version"],
                lease_expire_time=expires_at,
                retry_count=int(prefix.get("retry_count", 0)),
                content_version=content.version,
                content=deepcopy(dict(content.content or {})),
                content_detail=deepcopy(dict(content.detail or {})),
                series_prefix_compact=previous_compact,
                video_prefix_compact=str(prefix.get("video_prefix_compact", "")),
            )

    def claim_next_prefix_any(
        self,
        worker_id: str,
        *,
        lease_duration: timedelta,
        now: datetime | None = None,
    ) -> PrefixParseLease | None:
        """Periodic recovery scan across Series after notifications are lost."""

        claimed_at = now if now is not None else now_utc()
        with self._session_factory() as session:
            series_ids = list(session.scalars(
                select(VideoRecord.series_id)
                .join(
                    VideoParseTaskRecord,
                    VideoParseTaskRecord.video_id == VideoRecord.video_id,
                )
                .where(VideoParseTaskRecord.is_deleted.is_(False))
                .distinct()
                .order_by(VideoRecord.series_id)
            ))
        for series_id in series_ids:
            lease = self.claim_next_prefix(
                series_id,
                worker_id,
                lease_duration=lease_duration,
                now=claimed_at,
            )
            if lease is not None:
                return lease
        return None

    def heartbeat_prefix(
        self,
        lease: PrefixParseLease,
        *,
        lease_duration: timedelta,
        now: datetime | None = None,
    ) -> bool:
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        heartbeat_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            task = session.scalar(
                select(VideoParseTaskRecord)
                .where(VideoParseTaskRecord.id == lease.video_parse_task_id)
                .with_for_update()
            )
            if (
                task is None
                or _latest_video_task_id(session, task.video_id) != task.id
            ):
                return False
            prefix = _current_prefix_lease(task, lease, heartbeat_at)
            if prefix is None:
                return False
            detail = _ensure_runtime(task.detail)
            runtime = deepcopy(detail[RUNTIME_DETAIL_KEY])
            prefix["lease_expire_time_ms"] = datetime_to_unix_time_ms(
                heartbeat_at + lease_duration
            )
            runtime["prefix"] = prefix
            detail[RUNTIME_DETAIL_KEY] = runtime
            task.detail = detail
            task.update_time = heartbeat_at
            return True

    def commit_prefix(
        self,
        lease: PrefixParseLease,
        *,
        content_updates: Mapping[str, Any],
        video_prefix_compact: str,
        series_prefix_compact: str | None = None,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        committed_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            locked_intervals = _locked_parent_intervals(
                session, lease.video_parse_task_id
            )
            chunk_task = session.scalar(
                select(ChunkParseTaskRecord)
                .where(
                    ChunkParseTaskRecord.id == lease.chunk_parse_task_id,
                    ChunkParseTaskRecord.video_parse_task_id
                    == lease.video_parse_task_id,
                )
                .with_for_update()
            )
            content = session.scalar(
                select(ChunkParseContentRecord)
                .where(
                    ChunkParseContentRecord.chunk_parse_task_id
                    == lease.chunk_parse_task_id
                )
                .with_for_update()
            )
            task = session.scalar(
                select(VideoParseTaskRecord)
                .where(VideoParseTaskRecord.id == lease.video_parse_task_id)
                .with_for_update()
            )
            prefix = _current_prefix_lease(task, lease, committed_at)
            if prefix is None:
                raise VideoParseLeaseLostError("Prefix lease is no longer current")
            if _latest_video_task_id(session, task.video_id) != task.id:
                raise VideoParseLeaseLostError(
                    "Prefix lease task was superseded by a newer parse version"
                )
            if chunk_task is None or content is None:
                raise VideoParseConflictError("Prefix target no longer exists")
            if content.version != lease.content_version:
                raise VideoParseConflictError("Prefix target content version changed")
            current = deepcopy(dict(content.content or {}))
            updated = deepcopy(current)
            for field, value in content_updates.items():
                if field not in current:
                    raise VideoParseConflictError(
                        f"Prefix cannot add unknown field {field!r}"
                    )
                updated[field] = deepcopy(value)
            changed = updated != current
            reset: tuple[str, ...] = ()
            if changed:
                content.content = updated
                content.version += 1
                reset = _reset_indexing_for_changed_members(
                    session,
                    task.id,
                    [chunk_task.id],
                    committed_at,
                    locked_intervals=locked_intervals,
                )
            content_detail = deepcopy(dict(content.detail or {}))
            content_detail["compact"] = video_prefix_compact
            content.detail = content_detail
            content.update_time = committed_at

            detail = _ensure_runtime(task.detail)
            runtime = deepcopy(detail[RUNTIME_DETAIL_KEY])
            prefix["next_chunk_sequence_no"] = lease.sequence_no + 1
            prefix["video_prefix_compact"] = video_prefix_compact
            if series_prefix_compact is not None:
                prefix["series_prefix_compact"] = series_prefix_compact
            prefix["lease_owner"] = None
            prefix["lease_expire_time_ms"] = None
            prefix["execution_task_id"] = None
            prefix["retry_count"] = 0
            prefix["last_error"] = None
            committed_leases = deepcopy(prefix.get("committed_leases", {}))
            committed_leases[str(lease.lease_version)] = {
                "execution_task_id": lease.chunk_parse_task_id,
                "sequence_no": lease.sequence_no,
            }
            prefix["committed_leases"] = committed_leases
            runtime["prefix"] = prefix
            detail[RUNTIME_DETAIL_KEY] = runtime
            task.detail = detail
            task.update_time = committed_at
            _aggregate_video_task(session, task.id, committed_at)
            return reset

    def retry_prefix(
        self,
        lease: PrefixParseLease,
        *,
        error: str,
        retry_at: datetime,
        now: datetime | None = None,
    ) -> bool:
        failed_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            task = session.scalar(
                select(VideoParseTaskRecord)
                .where(VideoParseTaskRecord.id == lease.video_parse_task_id)
                .with_for_update()
            )
            if (
                task is None
                or _latest_video_task_id(session, task.video_id) != task.id
            ):
                return False
            prefix = _current_prefix_lease(task, lease, failed_at)
            if prefix is None:
                return False
            detail = _ensure_runtime(task.detail)
            runtime = deepcopy(detail[RUNTIME_DETAIL_KEY])
            prefix["lease_owner"] = None
            prefix["lease_expire_time_ms"] = None
            prefix["execution_task_id"] = None
            prefix["next_execute_time_ms"] = datetime_to_unix_time_ms(retry_at)
            prefix["retry_count"] = int(prefix.get("retry_count", 0)) + 1
            prefix["last_error"] = _error_detail(error)
            runtime["prefix"] = prefix
            detail[RUNTIME_DETAIL_KEY] = runtime
            task.detail = detail
            task.update_time = failed_at
            return True

    def release_prefix(
        self,
        lease: PrefixParseLease,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Release a cancelled Prefix execution without recording a failure."""

        released_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            task = session.scalar(
                select(VideoParseTaskRecord)
                .where(VideoParseTaskRecord.id == lease.video_parse_task_id)
                .with_for_update()
            )
            if (
                task is None
                or _latest_video_task_id(session, task.video_id) != task.id
            ):
                return False
            prefix = _current_prefix_lease(task, lease, released_at)
            if prefix is None:
                return False
            detail = _ensure_runtime(task.detail)
            runtime = deepcopy(detail[RUNTIME_DETAIL_KEY])
            prefix["lease_owner"] = None
            prefix["lease_expire_time_ms"] = None
            prefix["execution_task_id"] = None
            prefix["next_execute_time_ms"] = datetime_to_unix_time_ms(released_at)
            runtime["prefix"] = prefix
            detail[RUNTIME_DETAIL_KEY] = runtime
            task.detail = detail
            task.update_time = released_at
            return True


def _task_input_state(
    task: VideoParseTaskRecord,
    video: VideoRecord,
    *,
    workspace_id: str,
) -> VideoParseTaskInputState:
    return VideoParseTaskInputState(
        task=_task_state(task),
        series_id=video.series_id,
        episode_order=video.episode_order,
        original_filename=video.original_filename,
        original_source_uri=video.original_source_uri,
        inference_proxy_uri=video.inference_proxy_uri,
        content_hash=video.content_hash,
        total_bytes=video.total_bytes,
        duration_seconds=video.duration_seconds,
        workspace_id=workspace_id,
    )


def _video_chunk_state(chunk: VideoChunkRecord) -> VideoChunkState:
    return VideoChunkState(
        video_chunk_id=chunk.video_chunk_id,
        sequence_no=chunk.sequence_no,
        start_seconds=chunk.start_seconds,
        end_seconds=chunk.end_seconds,
        chunk_media_ref=chunk.chunk_media_ref,
        analysis_window_media_ref=chunk.analysis_window_media_ref,
    )


def _task_state(task: VideoParseTaskRecord) -> VideoParseTaskState:
    return VideoParseTaskState(
        video_parse_task_id=task.id,
        video_id=task.video_id,
        pipeline_version=task.pipeline_version,
        status=task.status,
        parse_config=deepcopy(dict(task.parse_config)),
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
        last_error=deepcopy(task.last_error),
        is_test=task.is_test,
        detail=deepcopy(dict(task.detail)),
        create_time=task.create_time,
        update_time=task.update_time,
    )


def _locked_video_lease(
    session: Session, lease: VideoParseLease, now: datetime
) -> VideoParseTaskRecord:
    task = session.scalar(
        select(VideoParseTaskRecord)
        .where(VideoParseTaskRecord.id == lease.video_parse_task_id)
        .with_for_update()
    )
    if (
        task is None
        or task.lease_owner != lease.worker_id
        or task.lease_version != lease.lease_version
        or task.lease_expire_time is None
        or task.lease_expire_time <= now
    ):
        raise VideoParseLeaseLostError("VideoParseTask lease is no longer current")
    return task


def _locked_chunk_lease(
    session: Session, lease: ChunkParseLease, now: datetime
) -> ChunkParseTaskRecord:
    task = session.scalar(
        select(ChunkParseTaskRecord)
        .where(ChunkParseTaskRecord.id == lease.chunk_parse_task_id)
        .with_for_update()
    )
    if not _matching_chunk_lease(task, lease, now):
        raise VideoParseLeaseLostError("ChunkParseTask lease is no longer current")
    return task


def _matching_chunk_lease(
    task: ChunkParseTaskRecord | None,
    lease: ChunkParseLease,
    now: datetime,
) -> bool:
    return bool(
        task is not None
        and task.video_parse_task_id == lease.video_parse_task_id
        and task.video_chunk_id == lease.video_chunk_id
        and task.status == ChunkParseTaskStatus.UNDERSTANDING.value
        and task.lease_owner == lease.worker_id
        and task.lease_version == lease.lease_version
        and task.lease_expire_time is not None
        and task.lease_expire_time > now
    )


def _interval_for_lease(
    session: Session, lease: IntervalParseLease, now: datetime
) -> IntervalParseTaskRecord:
    interval = session.get(IntervalParseTaskRecord, lease.interval_parse_task_id)
    if not _matching_interval_lease(interval, lease, now):
        raise VideoParseLeaseLostError("IntervalParseTask lease is no longer current")
    return interval


def _locked_interval_lease(
    session: Session,
    lease: IntervalParseLease,
    now: datetime,
    expected_status: str,
) -> IntervalParseTaskRecord:
    interval = session.scalar(
        select(IntervalParseTaskRecord)
        .where(IntervalParseTaskRecord.id == lease.interval_parse_task_id)
        .with_for_update()
    )
    if (
        not _matching_interval_lease(interval, lease, now)
        or interval.status != expected_status
    ):
        raise VideoParseLeaseLostError("IntervalParseTask lease is no longer current")
    return interval


def _matching_interval_lease(
    interval: IntervalParseTaskRecord | None,
    lease: IntervalParseLease,
    now: datetime,
) -> bool:
    return bool(
        interval is not None
        and interval.video_parse_task_id == lease.video_parse_task_id
        and tuple(interval.chunk_parse_task_ids) == lease.chunk_parse_task_ids
        and interval.start_sequence_no == lease.start_sequence_no
        and interval.chunk_count == lease.chunk_count
        and interval.status == lease.phase
        and interval.lease_owner == lease.worker_id
        and interval.lease_version == lease.lease_version
        and interval.lease_expire_time is not None
        and interval.lease_expire_time > now
    )


def _member_content_rows(
    session: Session,
    interval: IntervalParseTaskRecord,
    *,
    for_update: bool,
) -> list[tuple[ChunkParseTaskRecord, VideoChunkRecord, ChunkParseContentRecord]]:
    statement = (
        select(ChunkParseTaskRecord, VideoChunkRecord, ChunkParseContentRecord)
        .join(VideoChunkRecord, VideoChunkRecord.video_chunk_id == ChunkParseTaskRecord.video_chunk_id)
        .join(ChunkParseContentRecord, ChunkParseContentRecord.chunk_parse_task_id == ChunkParseTaskRecord.id)
        .where(ChunkParseTaskRecord.id.in_(interval.chunk_parse_task_ids))
        .order_by(VideoChunkRecord.sequence_no)
    )
    if for_update:
        statement = statement.with_for_update()
    rows = list(session.execute(statement))
    if (
        len(rows) != interval.chunk_count
        or tuple(row[0].id for row in rows) != tuple(interval.chunk_parse_task_ids)
    ):
        raise VideoParseConflictError("Interval member content is incomplete")
    return rows


def _content_state(
    task: ChunkParseTaskRecord,
    chunk: VideoChunkRecord,
    content: ChunkParseContentRecord,
) -> ChunkParseContentState:
    return ChunkParseContentState(
        chunk_parse_task_id=task.id,
        video_chunk_id=chunk.video_chunk_id,
        sequence_no=chunk.sequence_no,
        content=deepcopy(dict(content.content or {})),
        version=content.version,
        detail=deepcopy(dict(content.detail or {})),
    )


def _locked_parent_intervals(
    session: Session, video_parse_task_id: str
) -> list[IntervalParseTaskRecord]:
    return list(session.scalars(
        select(IntervalParseTaskRecord)
        .where(IntervalParseTaskRecord.video_parse_task_id == video_parse_task_id)
        .order_by(
            IntervalParseTaskRecord.start_sequence_no,
            IntervalParseTaskRecord.id,
        )
        .with_for_update()
    ))


def _submitted_intervals_by_member(
    session: Session,
    video_parse_task_id: str,
    *,
    for_update: bool = False,
) -> dict[str, tuple[IntervalParseTaskRecord, ...]]:
    intervals = _locked_parent_intervals(session, video_parse_task_id) if for_update else list(
        session.scalars(
            select(IntervalParseTaskRecord)
            .where(IntervalParseTaskRecord.video_parse_task_id == video_parse_task_id)
            .order_by(
                IntervalParseTaskRecord.start_sequence_no,
                IntervalParseTaskRecord.id,
            )
        )
    )
    grouped: dict[str, list[IntervalParseTaskRecord]] = {}
    for interval in intervals:
        for task_id in interval.chunk_parse_task_ids:
            grouped.setdefault(task_id, []).append(interval)
    return {key: tuple(value) for key, value in grouped.items()}


def _reset_indexing_for_changed_members(
    session: Session,
    video_parse_task_id: str,
    changed_member_ids: Sequence[str],
    changed_at: datetime,
    *,
    exclude_interval_id: str | None = None,
    locked_intervals: Sequence[IntervalParseTaskRecord] | None = None,
) -> tuple[str, ...]:
    changed = set(changed_member_ids)
    if not changed:
        return ()
    reset: list[str] = []
    intervals = (
        tuple(locked_intervals)
        if locked_intervals is not None
        else tuple(_locked_parent_intervals(session, video_parse_task_id))
    )
    for interval in intervals:
        if interval.id == exclude_interval_id or not changed.intersection(interval.chunk_parse_task_ids):
            continue
        if interval.status not in _REFINE_SUBMITTED:
            continue
        interval.status = IntervalParseTaskStatus.INDEXING.value
        interval.finish_time = None
        interval.lease_owner = None
        interval.lease_expire_time = None
        interval.next_execute_time = changed_at
        interval.update_time = changed_at
        reset.append(interval.id)
    return tuple(reset)


def _aggregate_video_task(
    session: Session,
    video_parse_task_id: str,
    now: datetime,
    *,
    release_terminal_lease: bool = False,
) -> VideoParseTaskRecord:
    task = session.scalar(
        select(VideoParseTaskRecord)
        .where(VideoParseTaskRecord.id == video_parse_task_id)
        .with_for_update()
    )
    if task is None:
        raise VideoParseTaskNotFoundError(
            f"VideoParseTask {video_parse_task_id!r} does not exist"
        )
    chunks = list(session.scalars(
        select(ChunkParseTaskRecord).where(
            ChunkParseTaskRecord.video_parse_task_id == video_parse_task_id
        )
    ))
    intervals = list(session.scalars(
        select(IntervalParseTaskRecord).where(
            IntervalParseTaskRecord.video_parse_task_id == video_parse_task_id
        )
    ))
    if not chunks or len(chunks) != task.chunk_count or not intervals:
        status = VideoParseTaskStatus.PARSING.value
    elif any(item.status == ChunkParseTaskStatus.FAILED.value for item in chunks) or any(
        item.status == IntervalParseTaskStatus.PARSING_FAILED.value for item in intervals
    ):
        status = VideoParseTaskStatus.PARSING_FAILED.value
    elif any(item.status != ChunkParseTaskStatus.COMPLETE.value for item in chunks) or any(
        item.status in {
            IntervalParseTaskStatus.WAITING.value,
            IntervalParseTaskStatus.REFINING.value,
        }
        for item in intervals
    ):
        status = VideoParseTaskStatus.PARSING.value
    elif any(item.status == IntervalParseTaskStatus.INDEXING_FAILED.value for item in intervals):
        status = VideoParseTaskStatus.INDEXING_FAILED.value
    elif any(item.status == IntervalParseTaskStatus.INDEXING.value for item in intervals):
        status = VideoParseTaskStatus.INDEXING.value
    else:
        status = VideoParseTaskStatus.COMPLETE.value
    persisted_status = status
    if not release_terminal_lease and task.lease_owner is not None:
        if status == VideoParseTaskStatus.PARSING_FAILED.value:
            persisted_status = VideoParseTaskStatus.PARSING.value
        elif status in {
            VideoParseTaskStatus.COMPLETE.value,
            VideoParseTaskStatus.INDEXING_FAILED.value,
        }:
            persisted_status = VideoParseTaskStatus.INDEXING.value
    task.status = persisted_status
    task.finish_time = now if persisted_status in {
        VideoParseTaskStatus.COMPLETE.value,
        VideoParseTaskStatus.PARSING_FAILED.value,
        VideoParseTaskStatus.INDEXING_FAILED.value,
    } else None
    task.update_time = now
    if release_terminal_lease and persisted_status in {
        VideoParseTaskStatus.COMPLETE.value,
        VideoParseTaskStatus.PARSING_FAILED.value,
        VideoParseTaskStatus.INDEXING_FAILED.value,
    }:
        task.lease_owner = None
        task.lease_expire_time = None
        task.heartbeat_time = None
    video = session.get(VideoRecord, task.video_id)
    if video is not None:
        video.parse_status = persisted_status
        video.parse_progress = (
            100 if persisted_status == VideoParseTaskStatus.COMPLETE.value else 0
        )
    return task


def _interval_specs(chunk_count: int, interval_size: int) -> tuple[tuple[int, int], ...]:
    if chunk_count < interval_size:
        return ((0, chunk_count),)
    return tuple((start, interval_size) for start in range(chunk_count - interval_size + 1))


def _interval_size(parse_config: Mapping[str, Any]) -> int:
    chunk_duration = _positive_int(parse_config.get("chunk_duration"), "chunk_duration")
    interval_duration = _positive_int(
        parse_config.get("interval_duration"), "interval_duration"
    )
    size = math.ceil(interval_duration / chunk_duration)
    if size > 63:
        raise VideoParseConflictError("interval_size exceeds the 63-member bitmap limit")
    return size


def _validate_chunk_boundaries(
    video: VideoRecord,
    parse_config: Mapping[str, Any],
    chunks: Sequence[VideoChunkRecord],
) -> None:
    duration = _positive_int(parse_config.get("chunk_duration"), "chunk_duration")
    expected = [
        (sequence, sequence * duration, min(video.duration_seconds, (sequence + 1) * duration))
        for sequence in range(len(chunks))
    ]
    actual = [(item.sequence_no, item.start_seconds, item.end_seconds) for item in chunks]
    if actual != expected:
        raise VideoParseConflictError("Video chunks do not match parse_config.chunk_duration")


def _ordered_int_union(existing: Any, additions: Sequence[int]) -> list[int]:
    values: list[int] = []
    if isinstance(existing, list):
        values.extend(item for item in existing if isinstance(item, int) and not isinstance(item, bool))
    values.extend(additions)
    return sorted(set(values))


def _indexed_versions(detail: Mapping[str, Any] | None) -> dict[str, int]:
    raw = (detail or {}).get("indexed_versions", {})
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): value
        for key, value in raw.items()
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
    }


def _ensure_runtime(detail_value: Mapping[str, Any] | None) -> dict[str, Any]:
    detail = deepcopy(dict(detail_value or {}))
    raw = detail.get(RUNTIME_DETAIL_KEY)
    runtime = deepcopy(raw) if isinstance(raw, dict) else {}
    model_calls = runtime.get("model_calls")
    if not isinstance(model_calls, dict):
        model_calls = {}
    used = model_calls.get("used_call_ids")
    active = model_calls.get("active")
    runtime["model_calls"] = {
        "used_call_ids": list(used) if isinstance(used, list) else [],
        "active": deepcopy(active) if isinstance(active, dict) else {},
    }
    prefix = runtime.get("prefix")
    if not isinstance(prefix, dict):
        prefix = {}
    committed_leases = prefix.get("committed_leases")
    if not isinstance(committed_leases, dict):
        committed_leases = {}
    runtime["prefix"] = {
        "next_chunk_sequence_no": int(prefix.get("next_chunk_sequence_no", 0)),
        "lease_owner": prefix.get("lease_owner"),
        "lease_version": int(prefix.get("lease_version", 0)),
        "lease_expire_time_ms": prefix.get("lease_expire_time_ms"),
        "execution_task_id": prefix.get("execution_task_id"),
        "next_execute_time_ms": prefix.get("next_execute_time_ms", 0),
        "retry_count": int(prefix.get("retry_count", 0)),
        "video_prefix_compact": str(prefix.get("video_prefix_compact", "")),
        "series_prefix_compact": str(prefix.get("series_prefix_compact", "")),
        "last_error": deepcopy(prefix.get("last_error")),
        "committed_leases": deepcopy(committed_leases),
    }
    if "accepted" in runtime:
        runtime["accepted"] = runtime["accepted"] is True
    detail[RUNTIME_DETAIL_KEY] = runtime
    return detail


def _series_prefix_frontier(
    session: Session,
    series_id: str,
) -> tuple[str, str] | None:
    """Return the first unfinished latest Video task and prior Series compact."""

    rows = session.execute(
        select(VideoParseTaskRecord, VideoRecord)
        .join(VideoRecord, VideoRecord.video_id == VideoParseTaskRecord.video_id)
        .where(
            VideoRecord.series_id == series_id,
            VideoParseTaskRecord.is_deleted.is_(False),
        )
        .order_by(
            VideoRecord.episode_order,
            VideoRecord.video_id,
            VideoParseTaskRecord.version.desc(),
            VideoParseTaskRecord.id,
        )
    ).all()
    latest: dict[str, tuple[VideoParseTaskRecord, VideoRecord]] = {}
    for task, video in rows:
        latest.setdefault(video.video_id, (task, video))

    previous_compact = ""
    for task, _video in latest.values():
        if not task.parse_config.get("compact_enabled", False):
            continue
        runtime = _ensure_runtime(task.detail)[RUNTIME_DETAIL_KEY]
        if runtime.get("accepted") is not True:
            return None
        prefix = runtime["prefix"]
        if int(prefix["next_chunk_sequence_no"]) < task.chunk_count:
            return task.id, previous_compact
        previous_compact = str(prefix.get("series_prefix_compact", ""))
    return None


def _latest_video_task_id(session: Session, video_id: str) -> str | None:
    return session.scalar(
        select(VideoParseTaskRecord.id)
        .where(
            VideoParseTaskRecord.video_id == video_id,
            VideoParseTaskRecord.is_deleted.is_(False),
        )
        .order_by(
            VideoParseTaskRecord.version.desc(),
            VideoParseTaskRecord.id,
        )
        .limit(1)
    )


def _current_prefix_lease(
    task: VideoParseTaskRecord | None,
    lease: PrefixParseLease,
    now: datetime,
) -> dict[str, Any] | None:
    if task is None or task.is_deleted:
        return None
    runtime = _ensure_runtime(task.detail)[RUNTIME_DETAIL_KEY]
    prefix = deepcopy(runtime["prefix"])
    expires_ms = prefix.get("lease_expire_time_ms")
    if (
        prefix.get("lease_owner") != lease.worker_id
        or prefix.get("lease_version") != lease.lease_version
        or not isinstance(expires_ms, int)
        or unix_time_ms_to_datetime(expires_ms) <= now
        or prefix.get("next_chunk_sequence_no") != lease.sequence_no
        or prefix.get("execution_task_id") != lease.chunk_parse_task_id
    ):
        return None
    return prefix


def _model_execution_identity(
    lease: VideoParseLease | ChunkParseLease | IntervalParseLease | PrefixParseLease,
) -> tuple[str, str, int]:
    if isinstance(lease, VideoParseLease):
        return "video", lease.video_parse_task_id, lease.lease_version
    if isinstance(lease, ChunkParseLease):
        return "chunk", lease.chunk_parse_task_id, lease.lease_version
    if isinstance(lease, IntervalParseLease):
        return "interval", lease.interval_parse_task_id, lease.lease_version
    return "prefix", lease.chunk_parse_task_id, lease.lease_version


def _stored_model_execution_identity(
    slot: Mapping[str, Any],
) -> tuple[str, str, int] | None:
    kind = slot.get("execution_kind")
    task_id = slot.get("execution_task_id")
    version = slot.get("execution_lease_version")
    if (
        not isinstance(kind, str)
        or not isinstance(task_id, str)
        or isinstance(version, bool)
        or not isinstance(version, int)
    ):
        return None
    return kind, task_id, version


def _lock_model_execution_lease(
    session: Session,
    lease: VideoParseLease | ChunkParseLease | IntervalParseLease | PrefixParseLease,
    now: datetime,
) -> VideoParseTaskRecord:
    """Fence a child execution before locking its parent budget row.

    Chunk and Interval completion paths never aggregate or lock the parent.
    Prefix is the sole child-like path that also owns state on the parent row.
    """

    if isinstance(lease, ChunkParseLease):
        task = session.scalar(
            select(ChunkParseTaskRecord)
            .where(
                ChunkParseTaskRecord.id == lease.chunk_parse_task_id,
                ChunkParseTaskRecord.video_parse_task_id
                == lease.video_parse_task_id,
            )
            .with_for_update()
        )
        if not _matching_chunk_lease(task, lease, now):
            raise VideoParseLeaseLostError("ChunkParseTask lease is no longer current")
    elif isinstance(lease, IntervalParseLease):
        interval = session.scalar(
            select(IntervalParseTaskRecord)
            .where(
                IntervalParseTaskRecord.id == lease.interval_parse_task_id,
                IntervalParseTaskRecord.video_parse_task_id
                == lease.video_parse_task_id,
            )
            .with_for_update()
        )
        if (
            not _matching_interval_lease(interval, lease, now)
            or interval.status != lease.phase
        ):
            raise VideoParseLeaseLostError("IntervalParseTask lease is no longer current")
    parent = session.scalar(
        select(VideoParseTaskRecord)
        .where(VideoParseTaskRecord.id == lease.video_parse_task_id)
        .with_for_update()
    )
    if parent is None or parent.is_deleted:
        raise VideoParseTaskNotFoundError(
            f"VideoParseTask {lease.video_parse_task_id!r} does not exist"
        )
    if isinstance(lease, VideoParseLease):
        if (
            parent.lease_owner != lease.worker_id
            or parent.lease_version != lease.lease_version
            or parent.lease_expire_time is None
            or parent.lease_expire_time <= now
        ):
            raise VideoParseLeaseLostError("VideoParseTask lease is no longer current")
    elif isinstance(lease, PrefixParseLease):
        if (
            _current_prefix_lease(parent, lease, now) is None
            or _latest_video_task_id(session, parent.video_id) != parent.id
        ):
            raise VideoParseLeaseLostError("Prefix lease is no longer current")
    return parent


def _execution_config(parse_config: Mapping[str, Any]) -> Mapping[str, Any]:
    value = parse_config.get("execution")
    if not isinstance(value, dict):
        raise VideoParseConflictError("parse_config.execution is missing")
    return value


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise VideoParseConflictError(f"parse_config.{name} must be a positive integer")
    return value


def _nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise VideoParseConflictError(
            f"parse_config.{name} must be a nonnegative integer"
        )
    return value


def _error_detail(error: str) -> dict[str, str]:
    return {"message": (error.strip() or "video parse worker failed")[:8192]}
