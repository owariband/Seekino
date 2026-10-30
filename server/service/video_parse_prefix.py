"""Prefix scheduling and state changes over persistence DTOs.

The caller owns the transaction; repositories perform SQL and explicit saves.
"""

from __future__ import annotations

from .parse_control import is_paused

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from agent.video_parse_v2.scheduler import chunk_index_ready
from engine.parsev2.schemas import VideoParseResult
from engine.parsev2.validation import parse_content_fingerprint, validate_result_time_range

from ..constants.parse import RUNTIME_DETAIL_KEY
from ..model.dto.jobDTO import (
    ChunkParseContentState,
    PrefixParseLease,
    VideoData,
    VideoParseTaskData,
)
from ..model.job import (
    IntervalParseTaskStatus,
    VideoParseTaskStatus,
)
from ..utils.retry import task_retry_delay
from ..utils.time import datetime_to_unix_time_ms, unix_time_ms_to_datetime
from .video_parse_errors import (
    VideoParseConflictError,
    VideoParseSnapshotConflictError,
    VideoParseLeaseLostError,
)
from .video_parse_task import (
    _locked_parent_intervals,
    _submitted_intervals_by_member,
    _advance_ready_chunks,
    _video_failure_status,
    _aggregate_video_task,
    _ensure_runtime,
    _execution_config,
    _nonnegative_int,
    _error_detail,
)
from .video_parse_content import (
    _content_state,
    _ordered_int_union,
)

from ..repository import video_parse_task as task_store
from ..repository import video_parse_chunk as chunk_store
from ..repository import video_parse_interval as interval_store


_REFINE_SUBMITTED = {IntervalParseTaskStatus.COMPLETE.value}


def invalidate_series_prefixes_from(
    session: Session,
    *,
    series_id: str,
    episode_order: int,
    video_id: str,
    now: datetime,
    skip_locked: bool = False,
) -> tuple[str, ...]:
    """Fence and reset the latest enabled Prefix chain from one episode.

    The caller owns the transaction so task creation, retry takeover, or an
    episode-order edit can invalidate dependent Series compact state
    atomically. Only parent rows are locked; content rows are deliberately
    outside this operation's lock set.
    """

    rows = task_store.list_series_tasks(
        session, series_id, from_episode=episode_order, from_video_id=video_id,
        task_id_desc=True,
    )
    latest_by_video: dict[str, tuple[int, str, str]] = {}
    for task, video in rows:
        latest_by_video.setdefault(
            task.video_id,
            (video.episode_order, task.video_id, task.id),
        )

    locked: list[VideoParseTaskData] = []
    for _order, task_video_id, task_id in sorted(latest_by_video.values()):
        task = task_store.get_task(session, task_id, for_update=True, skip_locked=skip_locked, include_deleted=False)
        if task is None:
            if skip_locked:
                raise VideoParseSnapshotConflictError("Prefix parent is busy or changed; retry publication")
            raise VideoParseConflictError(
                "Prefix invalidation frontier changed; retry the transaction"
            )
        if task_store.latest_video_task_id(session, task_video_id) != task.id:
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
        task.update_time = datetime_to_unix_time_ms(now)
        task_store.save_task(session, task)
        invalidated.append(task.id)
    return tuple(invalidated)


def _series_prefix_frontier(
    session: Session,
    series_id: str,
) -> tuple[str, str] | None:
    """Return the first unfinished latest Video task and prior Series compact."""

    rows = task_store.list_series_tasks(session, series_id)
    latest: dict[str, tuple[VideoParseTaskData, VideoData]] = {}
    for task, video in rows:
        latest.setdefault(video.id, (task, video))

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


def _current_prefix_lease(
    task: VideoParseTaskData | None,
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


def heartbeat_prefix(
    session: Session,
    *,
    lease: PrefixParseLease,
    lease_duration: timedelta,
    heartbeat_at: datetime,
) -> bool:
    task = task_store.get_task(session, lease.video_parse_task_id, for_update=True)
    if (
        task is None
        or task_store.latest_video_task_id(session, task.video_id) != task.id
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
    task.update_time = datetime_to_unix_time_ms(heartbeat_at)
    task_store.save_task(session, task)
    return True


def load_prefix_content(
    session: Session,
    *,
    lease: PrefixParseLease,
    loaded_at: datetime,
) -> ChunkParseContentState:
    task = task_store.get_task(session, lease.video_parse_task_id)
    if _current_prefix_lease(task, lease, loaded_at) is None:
        raise VideoParseLeaseLostError("Prefix lease is no longer current")
    rows = chunk_store.list_task_chunk_content_rows(
        session, lease.video_parse_task_id, chunk_task_ids=[lease.chunk_parse_task_id],
    )
    row = rows[0] if rows else None
    if row is None:
        raise VideoParseConflictError("Prefix target no longer exists")
    return _content_state(*row)


def commit_prefix(
    session: Session,
    *,
    lease: PrefixParseLease,
    content_updates: Mapping[str, Any],
    video_prefix_compact: str,
    series_prefix_compact: str | None,
    input_version: int | None,
    input_fingerprint: str | None,
    committed_at: datetime,
) -> tuple[str, ...]:
    locked_intervals = _locked_parent_intervals(
        session, lease.video_parse_task_id
    )
    chunk_task = chunk_store.get_task(
        session, lease.chunk_parse_task_id, video_task_id=lease.video_parse_task_id, for_update=True,
    )
    content = chunk_store.get_content(session, lease.chunk_parse_task_id, for_update=True)
    task = task_store.get_task(session, lease.video_parse_task_id, for_update=True)
    prefix = _current_prefix_lease(task, lease, committed_at)
    if prefix is None:
        raise VideoParseLeaseLostError("Prefix lease is no longer current")
    if task_store.latest_video_task_id(session, task.video_id) != task.id:
        raise VideoParseLeaseLostError(
            "Prefix lease task was superseded by a newer parse version"
        )
    if chunk_task is None or content is None:
        raise VideoParseConflictError("Prefix target no longer exists")
    expected_version = lease.content_version if input_version is None else input_version
    expected_fingerprint = (
        parse_content_fingerprint(lease.content) if input_fingerprint is None else input_fingerprint
    )
    if (content.version != expected_version
            or parse_content_fingerprint(dict(content.content or {})) != expected_fingerprint):
        raise VideoParseSnapshotConflictError("Prefix target content snapshot changed")
    current = deepcopy(dict(content.content or {}))
    updated = deepcopy(current)
    for field, value in content_updates.items():
        if field not in current:
            raise VideoParseConflictError(
                f"Prefix cannot add unknown field {field!r}"
            )
        updated[field] = deepcopy(value)
    chunk = chunk_store.get_video_chunk(session, chunk_task.chunk_id)
    if chunk is None:
        raise VideoParseConflictError("Prefix target media identity is missing")
    scope = _ordered_int_union(dict(content.detail or {}).get("scope"), [chunk.sequence_no])
    if scope[-1] >= task.chunk_count:
        raise VideoParseConflictError("Prefix scope exceeds its fixed Video topology")
    video = task_store.get_video(session, task.video_id)
    if video is None:
        raise VideoParseConflictError("Prefix source Video is missing")
    chunk_duration = task.parse_config.get("chunk_duration")
    if type(chunk_duration) is not int or chunk_duration <= 0:
        raise VideoParseConflictError("frozen parse config has an invalid chunk_duration")
    chunk_duration_ms = chunk_duration * 1000
    updated = validate_result_time_range(
        VideoParseResult.model_validate(updated),
        start_seconds=scope[0] * chunk_duration_ms,
        end_seconds=min(video.duration_ms, (scope[-1] + 1) * chunk_duration_ms),
    ).model_dump(mode="json")
    changed = updated != current
    reset: tuple[str, ...] = ()
    if changed:
        content.content = updated
        content.version += 1
        chunk_store.save_content(session, content)
        reset = _advance_ready_chunks(
            session,
            task.id,
            [chunk_task.id],
            committed_at,
            locked_intervals=locked_intervals,
            invalidate_index=True,
        )
    content_detail = deepcopy(dict(content.detail or {}))
    content_detail["compact"] = video_prefix_compact
    content.detail = content_detail
    content.update_time = datetime_to_unix_time_ms(committed_at)
    chunk_store.save_content(session, content)

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
    task.update_time = datetime_to_unix_time_ms(committed_at)
    task_store.save_task(session, task)
    task = _aggregate_video_task(session, task.id, committed_at)
    if changed and _video_failure_status(task) is None:
        task.next_execute_time = committed_at
        task_store.save_task(session, task)
    return reset


def retry_prefix(
    session: Session,
    *,
    lease: PrefixParseLease,
    error: str,
    failed_at: datetime,
) -> bool:
    task = task_store.get_task(session, lease.video_parse_task_id, for_update=True)
    if (
        task is None
        or task_store.latest_video_task_id(session, task.video_id) != task.id
    ):
        return False
    prefix = _current_prefix_lease(task, lease, failed_at)
    if prefix is None:
        return False
    execution = _execution_config(task.parse_config)
    failure_count = int(prefix.get("retry_count", 0)) + 1
    retry_at = failed_at + task_retry_delay(
        initial_delay_seconds=execution["retry_initial_delay_seconds"],
        max_delay_seconds=execution["retry_max_delay_seconds"],
        failure_count=failure_count,
    )
    detail = _ensure_runtime(task.detail)
    runtime = deepcopy(detail[RUNTIME_DETAIL_KEY])
    prefix["lease_owner"] = None
    prefix["lease_expire_time_ms"] = None
    prefix["execution_task_id"] = None
    prefix["next_execute_time_ms"] = datetime_to_unix_time_ms(retry_at)
    prefix["retry_count"] = failure_count
    prefix["last_error"] = _error_detail(error)
    runtime["prefix"] = prefix
    detail[RUNTIME_DETAIL_KEY] = runtime
    task.detail = detail
    task.update_time = datetime_to_unix_time_ms(failed_at)
    task_store.save_task(session, task)
    return True


def release_prefix(
    session: Session,
    *,
    lease: PrefixParseLease,
    released_at: datetime,
) -> bool:
    task = task_store.get_task(session, lease.video_parse_task_id, for_update=True)
    if (
        task is None
        or task_store.latest_video_task_id(session, task.video_id) != task.id
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
    task.update_time = datetime_to_unix_time_ms(released_at)
    task_store.save_task(session, task)
    return True


def claim_next_prefix(
    session: Session,
    *,
    series_id: str,
    worker_id: str,
    claimed_at: datetime,
    expires_at: datetime,
    frontier_task_id: str,
    now_ms: int,
) -> PrefixParseLease | None:
    # Lock only the already selected frontier. If another worker owns
    # it, return instead of skipping to a later episode or old task.
    task = task_store.get_task(session, frontier_task_id, for_update=True, skip_locked=True)
    if task is None or task.is_deleted:
        return None
    current_frontier = _series_prefix_frontier(session, series_id)
    if current_frontier is None or current_frontier[0] != task.id:
        return None
    previous_compact = current_frontier[1]
    config = task.parse_config
    detail = _ensure_runtime(task.detail)
    runtime = deepcopy(detail[RUNTIME_DETAIL_KEY])
    if runtime.get("accepted") is not True or is_paused(task):
        return None
    prefix = deepcopy(runtime["prefix"])
    next_execute_ms = prefix.get("next_execute_time_ms", 0)
    if isinstance(next_execute_ms, int) and next_execute_ms > now_ms:
        return None
    execution = _execution_config(config)
    max_retries = _nonnegative_int(
        execution.get("max_retry_count"),
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
        prefix["next_execute_time_ms"] = datetime_to_unix_time_ms(
            claimed_at + task_retry_delay(
                initial_delay_seconds=execution["retry_initial_delay_seconds"],
                max_delay_seconds=execution["retry_max_delay_seconds"],
                failure_count=prefix["retry_count"],
            )
        )
    if owner is not None or int(prefix.get("retry_count", 0)) > max_retries:
        runtime["prefix"] = prefix
        detail[RUNTIME_DETAIL_KEY] = runtime
        task.detail = detail
        task.update_time = datetime_to_unix_time_ms(claimed_at)
        task_store.save_task(session, task)
        return None
    if cursor >= task.chunk_count or task.status not in {
        VideoParseTaskStatus.PARSING.value,
        VideoParseTaskStatus.INDEXING.value,
        VideoParseTaskStatus.INDEXING_FAILED.value,
        VideoParseTaskStatus.COMPLETE.value,
    }:
        return None
    rows = chunk_store.list_task_chunk_content_rows(
        session, task.id, sequence_no=cursor, content_not_null=True,
    )
    chunk_row = rows[0] if rows else None
    if chunk_row is None:
        return None
    chunk_task, chunk, content = chunk_row
    covering = _submitted_intervals_by_member(session, task.id).get(
        chunk_task.id, ()
    )
    refined = interval_store.list_content_ids(session, [item.id for item in covering])
    if not chunk_index_ready(bool(content.content), [
        item.status in _REFINE_SUBMITTED and item.id in refined for item in covering
    ]):
        return None
    prefix["lease_owner"] = worker_id
    prefix["lease_version"] = int(prefix.get("lease_version", 0)) + 1
    prefix["lease_expire_time_ms"] = datetime_to_unix_time_ms(expires_at)
    prefix["execution_task_id"] = chunk_task.id
    prefix["series_prefix_compact"] = previous_compact
    runtime["prefix"] = prefix
    detail[RUNTIME_DETAIL_KEY] = runtime
    task.detail = detail
    task.update_time = datetime_to_unix_time_ms(claimed_at)
    task_store.save_task(session, task)
    return PrefixParseLease(
        video_parse_task_id=task.id,
        chunk_parse_task_id=chunk_task.id,
        video_chunk_id=chunk.id,
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
