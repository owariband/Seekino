"""Durable user control, independent of parse phase and published content."""
from copy import deepcopy
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from ..constants.parse import RUNTIME_DETAIL_KEY
from ..repository import video_parse_task as tasks, video_parse_chunk as chunks, video_parse_interval as intervals
from ..repository.job import ParseJobNotFoundError
from ..utils.time import datetime_to_unix_time_ms
from .video_parse_errors import VideoParseConflictError
from .video_parse_task import _set_capacity_acquired

ACTIVE_STATUSES = {"PENDING", "PREPARING", "PARSING", "INDEXING"}
# Retain a scheduling fence for consumers started before pause support was deployed.
# Explicit admission checks remain authoritative, including beyond this date.
_PAUSE_HOLD = datetime(2037, 1, 1, tzinfo=UTC)


def is_paused(task) -> bool:
    return (task.detail or {}).get("execution_control", {}).get("paused") is True


def fence_task_leases(session: Session, task, *, now: datetime, hold: datetime) -> None:
    """Revoke every lease on a task and its children without changing user intent.

    Bumps the parent lease_version, clears its owner, parks queued/waiting
    children, revokes prefix/summarize claims and releases capacity. The caller
    chooses the parking target (pause hold vs. now) and persists the parent row.
    """
    now_ms = datetime_to_unix_time_ms(now)
    task.lease_version += 1
    task.lease_owner = task.lease_expire_time = task.heartbeat_time = None
    task.next_execute_time = hold
    detail = task.detail
    runtime = detail.get(RUNTIME_DETAIL_KEY)
    if not isinstance(runtime, dict):
        runtime = {}
        detail[RUNTIME_DETAIL_KEY] = runtime
    task.detail = detail
    for store, running, waiting in ((chunks, "RUNNING", "QUEUED"), (intervals, "REFINING", "WAITING")):
        for child in store.list_tasks(session, task.id, for_update=True):
            if child.status == running:
                child.status = waiting
                child.lease_version += 1
                child.lease_owner = child.lease_expire_time = None
                child.start_time = None
            if child.status in {"QUEUED", "WAITING"}:
                # Old realtime consumers check child due time before acquiring
                # parent capacity. Park these too during a rolling deployment.
                child.next_execute_time = hold
                child.update_time = now_ms
                store.save_task(session, child)
    for key in ("prefix", "summarize"):
        state = runtime.get(key)
        if isinstance(state, dict):
            state["lease_version"] = int(state.get("lease_version", 0)) + 1
            state["lease_owner"] = state["lease_expire_time_ms"] = None
            state["execution_task_id"] = None
            state["next_execute_time_ms"] = datetime_to_unix_time_ms(hold)
            if key == "summarize" and state.get("status") == "RUNNING":
                state["status"] = "PENDING"
    _set_capacity_acquired(task, False)


def set_paused(session: Session, task_id: str, *, tenant_id: str, paused: bool,
               expected_version: int, expected_lease_version: int, now: datetime) -> None:
    task = tasks.get_task(session, task_id, for_update=True, include_deleted=False)
    video = tasks.get_video(session, task.video_id, for_update=True) if task else None
    series = tasks.get_series(session, video.series_id) if video else None
    if task is None or series is None or series.tenant_id != tenant_id:
        raise ParseJobNotFoundError("Video parse task does not exist in this workspace")
    if (tasks.latest_video_task_id(session, task.video_id) != task.id
            or task.version != expected_version or task.lease_version != expected_lease_version):
        raise VideoParseConflictError("解析任务已变化，请刷新后操作")
    if task.status not in ACTIVE_STATUSES:
        raise VideoParseConflictError("仅进行中的解析可以暂停或继续")
    if is_paused(task) == paused:
        return
    detail = deepcopy(task.detail or {})
    runtime = detail.setdefault(RUNTIME_DETAIL_KEY, {})
    if runtime.get("accepted") is not True:
        raise VideoParseConflictError("任务尚未进入正式解析队列")
    now_ms = datetime_to_unix_time_ms(now)
    detail["execution_control"] = {"paused": paused, "updated_at_ms": now_ms}
    if paused:
        for entry in runtime.get("execution_timing", {}).get("entries", {}).values():
            if isinstance(entry, dict) and not entry.get("finished", False):
                entry.update(finished=True, outcome="paused")
    task.detail = detail
    fence_task_leases(session, task, now=now, hold=(_PAUSE_HOLD if paused else now))
    task.update_time = now_ms
    tasks.save_task(session, task)


def delete_task_versions(
    session: Session,
    *,
    tenant_id: str,
    now: datetime,
    video_parse_task_id: str | None = None,
    expected_job_version: int | None = None,
    video_id: str | None = None,
) -> tuple[list[str], list[tuple[str, str]]]:
    """Soft-delete historical parse versions and return their ES document keys.

    The single form targets one version (with its expected job version); the
    history form (`video_id`) targets every non-latest, non-deleted version.
    Returns `(deleted_task_ids, [(video_parse_task_id, chunk_id), ...])` so the
    caller can remove the deleted versions' ES documents after commit.
    """
    now_ms = datetime_to_unix_time_ms(now)
    if video_parse_task_id is not None:
        target = tasks.get_task(session, video_parse_task_id, for_update=True, include_deleted=False)
        video = tasks.get_video(session, target.video_id, for_update=True) if target else None
        series = tasks.get_series(session, video.series_id) if video else None
        if target is None or series is None or series.tenant_id != tenant_id:
            raise ParseJobNotFoundError("Video parse task does not exist in this workspace")
        if tasks.latest_video_task_id(session, target.video_id) == target.id:
            raise VideoParseConflictError("当前版本请通过删除视频操作")
        if expected_job_version is None or target.version != expected_job_version:
            raise VideoParseConflictError("解析任务已变化，请刷新后操作")
        targets = [target]
    else:
        video = tasks.get_video(session, video_id, for_update=True) if video_id is not None else None
        series = tasks.get_series(session, video.series_id) if video else None
        if video is None or series is None or series.tenant_id != tenant_id:
            raise ParseJobNotFoundError("Video parse task does not exist in this workspace")
        latest_id = tasks.latest_video_task_id(session, video_id)
        if latest_id is None:
            raise ParseJobNotFoundError("Video parse task does not exist in this workspace")
        targets = [item for item in tasks.list_tasks(session, video_id, for_update=True)
                   if item.id != latest_id]

    deleted_ids: list[str] = []
    es_keys: list[tuple[str, str]] = []
    for target in targets:
        fence_task_leases(session, target, now=now, hold=now)
        detail = deepcopy(target.detail or {})
        runtime = detail.setdefault(RUNTIME_DETAIL_KEY, {})
        runtime["accepted"] = False
        target.detail = detail
        target.is_deleted = True
        target.update_time = now_ms
        tasks.save_task(session, target)
        deleted_ids.append(target.id)
        for child in chunks.list_tasks(session, target.id):
            es_keys.append((target.id, child.chunk_id))
    return deleted_ids, sorted(set(es_keys))
