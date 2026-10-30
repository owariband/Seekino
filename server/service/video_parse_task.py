"""Task state decisions over locked data snapshots in the caller's transaction."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from agent.video_parse_v2.scheduler import chunk_index_ready, summarize_video_task

from ..constants.parse import RUNTIME_DETAIL_KEY, CAPACITY_ACQUIRED_KEY
from ..model.dto.jobDTO import (
    IntervalParseTaskData,
    SeriesData,
    VideoData,
    VideoParseLease,
    VideoParseTaskData,
    VideoParseTaskInputState,
    VideoParseTaskState,
)
from ..model.job import (
    ChunkTaskPhase,
    ChunkParseTaskStatus,
    VideoParseTaskStatus,
)
from ..repository import video_parse_task as task_store
from ..repository import video_parse_chunk as chunk_store
from ..repository import video_parse_interval as interval_store
from ..repository.video_parse_task import latest_video_task_id as _latest_video_task_id
from ..utils.time import datetime_to_unix_time_ms
from .video_parse_errors import (
    VideoParseTaskNotFoundError,
    VideoParseConflictError,
    VideoParseLeaseLostError,
)


def _task_input_state(
    task: VideoParseTaskData,
    video: VideoData,
    *,
    tenant_id: str,
    series: SeriesData | None = None,
) -> VideoParseTaskInputState:
    return VideoParseTaskInputState(
        task=_task_state(task),
        series_id=video.series_id,
        episode_order=video.episode_order,
        original_filename=video.filename,
        original_source_uri=video.source_uri,
        inference_proxy_uri=video.proxy_uri,
        content_hash=video.hash,
        total_bytes=video.size_bytes,
        duration_seconds=video.duration_ms,
        tenant_id=tenant_id,
        background=(
            {"series_id": series.id, "description": series.description,
             "important_extra_data": deepcopy(series.important_extra_data),
             "character_bible": deepcopy(series.character_bible)}
            if series is not None else {}
        ),
    )


def _task_state(task: VideoParseTaskData) -> VideoParseTaskState:
    return VideoParseTaskState(
        video_parse_task_id=task.id,
        created_by_user_id=task.creator_id,
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
        create_time=task.create_date,
        update_time=task.update_date,
    )


def _locked_video_lease(
    session: Session, lease: VideoParseLease, now: datetime
) -> VideoParseTaskData:
    task = task_store.get_task(session, lease.video_parse_task_id, for_update=True)
    if (
        task is None
        or task.lease_owner != lease.worker_id
        or task.lease_version != lease.lease_version
        or task.lease_expire_time is None
        or task.lease_expire_time <= now
    ):
        raise VideoParseLeaseLostError("VideoParseTask lease is no longer current")
    return task


def _locked_parent_task(session: Session, video_parse_task_id: str) -> VideoParseTaskData:
    parent = task_store.get_task(session, video_parse_task_id, for_update=True)
    if parent is None or parent.is_deleted:
        raise VideoParseTaskNotFoundError(f"VideoParseTask {video_parse_task_id!r} does not exist")
    return parent


def _locked_parent_intervals(
    session: Session, video_parse_task_id: str
) -> list[IntervalParseTaskData]:
    _locked_parent_task(session, video_parse_task_id)
    return interval_store.list_tasks(session, video_parse_task_id, for_update=True)


def _submitted_intervals_by_member(
    session: Session,
    video_parse_task_id: str,
    *,
    for_update: bool = False,
) -> dict[str, tuple[IntervalParseTaskData, ...]]:
    intervals = (_locked_parent_intervals(session, video_parse_task_id) if for_update
                 else interval_store.list_tasks(session, video_parse_task_id))
    grouped: dict[str, list[IntervalParseTaskData]] = {}
    for interval in intervals:
        for task_id in interval.chunk_task_ids:
            grouped.setdefault(task_id, []).append(interval)
    return {key: tuple(value) for key, value in grouped.items()}


def _advance_ready_chunks(
    session: Session, video_parse_task_id: str, member_ids: Sequence[str], now: datetime,
    *, locked_intervals: Sequence[IntervalParseTaskData] | None = None,
    invalidate_index: bool = False,
) -> tuple[str, ...]:
    """Apply Agent readiness to every member in the same result transaction."""
    _locked_parent_task(session, video_parse_task_id)
    intervals = (list(locked_intervals) if locked_intervals is not None
                 else _locked_parent_intervals(session, video_parse_task_id))
    refined = interval_store.list_content_ids(session, [item.id for item in intervals])
    rows = chunk_store.list_task_content_rows(session, video_parse_task_id, task_ids=member_ids, for_update=True)
    queued = []
    for child, content in rows:
        covering = [item for item in intervals if child.id in item.chunk_task_ids]
        if not chunk_index_ready(bool(content.content), [
            item.status == "COMPLETE" and item.id in refined for item in covering
        ]):
            continue
        if not invalidate_index and not (child.phase == ChunkTaskPhase.REFINE and child.status == "WAITING"):
            continue
        child.phase = ChunkTaskPhase.INDEX
        child.status = ChunkParseTaskStatus.QUEUED.value
        child.retry_count = 0
        # Revocation is monotonic even when no worker currently owns this row.
        if invalidate_index:
            child.lease_version += 1
        child.lease_owner = None
        child.lease_expire_time = None
        child.next_execute_time = now
        child.start_time = None
        child.finish_time = None
        child.last_error = None
        child.update_time = datetime_to_unix_time_ms(now)
        chunk_store.save_task(session, child)
        queued.append(child.id)
    return tuple(queued)


def _video_failure_status(task: VideoParseTaskData) -> str | None:
    runtime = (task.detail or {}).get(RUNTIME_DETAIL_KEY)
    status = runtime.get("failure_status") if isinstance(runtime, dict) else None
    return status if status in {
        VideoParseTaskStatus.PARSING_FAILED.value,
        VideoParseTaskStatus.INDEXING_FAILED.value,
    } else None


def _requires_video_asr(task: VideoParseTaskData) -> bool:
    asr = (task.parse_config.get("models") or {}).get("asr") or {}
    return asr.get("mode") != "none" and asr.get("scope") == "video"


def _video_asr_prepared(task: VideoParseTaskData) -> bool:
    # This is a lease-scoped dispatch receipt, never a durable transcript.
    runtime = (task.detail or {}).get(RUNTIME_DETAIL_KEY) or {}
    return runtime.get("asr_prepared_lease_version") == task.lease_version


def _aggregate_video_task(
    session: Session, video_parse_task_id: str, now: datetime,
    *, release_terminal_lease: bool = False, failure_status: str | None = None,
) -> VideoParseTaskData:
    task = _locked_parent_task(session, video_parse_task_id)
    session.flush()
    before = deepcopy(task)
    runtime = _ensure_runtime(task.detail)[RUNTIME_DETAIL_KEY]
    chunk_rows = chunk_store.list_task_chunk_content_rows(session, task.id, include_missing_content=True)
    intervals = interval_store.list_tasks(session, task.id)
    chunks = [child for child, _chunk, _content in chunk_rows]
    if (runtime.get("realtime_only") is True and runtime.get("accepted") is not True
        and failure_status is None and _video_failure_status(task) is None):
        task.status = "PREPARING" if task.status == "PREPARING" or task.lease_owner is not None else "PENDING"
        task.finish_time = None
        if task.lease_owner is None and not any(child.lease_owner for child in chunks):
            _set_capacity_acquired(task, False)
        _touch_if_modified(session, task, before, now)
        return task
    refined = interval_store.list_content_ids(session, [item.id for item in intervals])
    topology_complete = _topology_complete(task, chunk_rows, intervals)
    recorded_failure = _video_failure_status(task) or (
        task.status if task.status in {"PARSING_FAILED", "INDEXING_FAILED"} else None
    )
    scoring_pending = (task.parse_config.get("voiceprint_mode", "off") != "off"
                       and dict((task.detail or {}).get("voiceprint") or {}).get("ready") is not True)
    parsing_failed = ("PARSING_FAILED" in {failure_status, recorded_failure}
                      or not scoring_pending and any(
                          item.phase == ChunkTaskPhase.PARSE and item.status == "FAILED" for item in chunks)
                      or any(item.status == "PARSING_FAILED" for item in intervals))
    indexing_failed = ("INDEXING_FAILED" in {failure_status, recorded_failure}
                       or any(item.phase == ChunkTaskPhase.INDEX and item.status == "FAILED" for item in chunks))
    status = summarize_video_task(
        topology_complete=topology_complete,
        understanding_complete=all(content is not None and bool(content.content)
                                   for _, _, content in chunk_rows),
        refinement_complete=all(item.status == "COMPLETE" and item.id in refined for item in intervals),
        indexing_complete=all(
            child.phase == ChunkTaskPhase.INDEX and child.status == "COMPLETE" and content is not None
            and child.indexed_content_version == content.version
            for child, _, content in chunk_rows
        ),
        parsing_failed=parsing_failed, indexing_failed=indexing_failed,
    )
    previous_status = task.status
    failure = status if status in {"PARSING_FAILED", "INDEXING_FAILED"} else None
    if failure:
        detail = _ensure_runtime(task.detail)
        detail[RUNTIME_DETAIL_KEY]["failure_status"] = failure
        task.detail = detail
        if previous_status not in {"PARSING_FAILED", "INDEXING_FAILED"} and (
            any(child.lease_owner is not None for child in [*chunks, *intervals]) or not release_terminal_lease
        ):
            status = "PARSING" if failure == "PARSING_FAILED" else "INDEXING"
    elif status == "COMPLETE" and task.lease_owner is not None and not release_terminal_lease:
        status = "INDEXING"
    task.status = status
    if status in {"COMPLETE", "PARSING_FAILED", "INDEXING_FAILED"}:
        if previous_status != status or task.finish_time is None:
            task.finish_time = now
        _set_capacity_acquired(task, False)
        if release_terminal_lease:
            task.lease_owner = None
            task.lease_expire_time = None
            task.heartbeat_time = None
    else:
        task.finish_time = None
    _touch_if_modified(session, task, before, now)
    video = task_store.get_video(session, task.video_id)
    if video is not None:
        video.parse_status = status
        video.parse_progress = 100 if status == "COMPLETE" else 0
        task_store.update_video(
            session, video.id, parse_status=video.parse_status, parse_progress=video.parse_progress,
        )
    return task


def _topology_complete(task: VideoParseTaskData, chunk_rows: Sequence, intervals: Sequence[IntervalParseTaskData]) -> bool:
    """Check the same committed coverage for aggregation and Video summarization."""
    chunks = [child for child, _chunk, _content in chunk_rows]
    expected = _interval_specs(task.chunk_count, _interval_size(task.parse_config))
    return (
        task.chunk_count > 0 and len(chunks) == task.chunk_count
        and [chunk.sequence_no for _, chunk, _ in chunk_rows] == list(range(task.chunk_count))
        and all(chunk.video_id == task.video_id for _, chunk, _ in chunk_rows)
        and [(item.start_sequence_no, item.chunk_count) for item in intervals] == list(expected)
        and all(tuple(item.chunk_task_ids) == tuple(
            child.id for child in chunks[item.start_sequence_no:item.start_sequence_no + item.chunk_count]
        ) for item in intervals)
    )


def _touch_if_modified(session: Session, task: VideoParseTaskData, before: VideoParseTaskData, now: datetime) -> None:
    if task != before:
        task.update_time = datetime_to_unix_time_ms(now)
        task_store.save_task(session, task)


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


def _capacity_acquired(task: VideoParseTaskData) -> bool:
    runtime = (task.detail or {}).get(RUNTIME_DETAIL_KEY)
    return isinstance(runtime, dict) and runtime.get(CAPACITY_ACQUIRED_KEY) is True


def _set_capacity_acquired(task: VideoParseTaskData, acquired: bool) -> None:
    detail = deepcopy(task.detail or {})
    runtime = detail.get(RUNTIME_DETAIL_KEY)
    if not isinstance(runtime, dict):
        runtime = {}
    if acquired:
        runtime[CAPACITY_ACQUIRED_KEY] = True
    else:
        runtime.pop(CAPACITY_ACQUIRED_KEY, None)
    detail[RUNTIME_DETAIL_KEY] = runtime
    task.detail = detail
