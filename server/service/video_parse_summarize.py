"""Video summary phase decisions in the caller's transaction; no model or media IO."""

from __future__ import annotations

from .parse_control import is_paused

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agent.video_summarize.state import SummaryChunk, VideoSummarizeInput, VideoSummaryResult

from ..constants.parse import RUNTIME_DETAIL_KEY
from ..model.dto.jobDTO import VideoParseTaskData, VideoSummarizeLease
from ..repository import video_parse_task as task_store
from ..repository import video_parse_chunk as chunk_store
from ..repository import video_parse_interval as interval_store
from ..repository.library import LibraryRepository, VideoEpisodeOrderConflictError
from ..utils.time import datetime_to_unix_time_ms
from .library import update_video_episode_order_in_session
from .video_parse_errors import VideoParseLeaseLostError, VideoParseSnapshotConflictError
from .video_parse_task import _ensure_runtime, _task_input_state, _topology_complete


def _state(task: VideoParseTaskData) -> dict[str, Any]:
    return deepcopy(dict((task.detail or {}).get(RUNTIME_DETAIL_KEY, {}).get("summarize", {})))


def _save(session: Session, task: VideoParseTaskData, state: dict, now: datetime) -> None:
    detail = _ensure_runtime(task.detail)
    detail[RUNTIME_DETAIL_KEY]["summarize"] = state
    task.detail = detail
    task.update_time = datetime_to_unix_time_ms(now)
    task_store.save_task(session, task)


def current_summary_lease(task: VideoParseTaskData | None, lease: VideoSummarizeLease, now: datetime) -> dict | None:
    if task is None or task.is_deleted or task.id != lease.video_parse_task_id:
        return None
    state = _state(task)
    if (state.get("status") != "RUNNING" or state.get("lease_owner") != lease.worker_id
            or state.get("lease_version") != lease.lease_version
            or state.get("source_fingerprint") != lease.source_fingerprint
            or (state.get("lease_expire_time_ms") or 0) <= datetime_to_unix_time_ms(now)):
        return None
    return state


def _source(session: Session, task: VideoParseTaskData, *, episode_order: int | None = None,
            lock_background: bool = False):
    runtime = (task.detail or {}).get(RUNTIME_DETAIL_KEY, {})
    if (task.is_deleted or is_paused(task) or runtime.get("accepted") is not True
            or task_store.latest_video_task_id(session, task.video_id) != task.id):
        return None
    video = task_store.get_video(session, task.video_id)
    series = task_store.get_series(session, video.series_id, for_update=lock_background) if video else None
    if video is None or series is None:
        return None
    # Refine's transaction may have established a MySQL RR snapshot before it
    # obtained the parent lock. Read committed children through current reads.
    intervals = interval_store.list_tasks(session, task.id, for_update=True)
    if not intervals or any(item.status != "COMPLETE" for item in intervals):
        return None
    refined = interval_store.list_content_ids(session, [item.id for item in intervals], for_update=True)
    if any(item.id not in refined for item in intervals):
        return None
    rows = chunk_store.list_task_chunk_content_rows(session, task.id, include_missing_content=True, for_update=True)
    if (not _topology_complete(task, rows, intervals)
            or not all(content is not None and bool(content.content) for _, _, content in rows)):
        return None
    if episode_order is not None:
        video = replace(video, episode_order=episode_order)
    task_input = _task_input_state(task, video, tenant_id=series.tenant_id, series=series)
    inputs = VideoSummarizeInput(
        video_id=video.id, duration_ms=video.duration_ms, episode_order=video.episode_order,
        background=deepcopy(task_input.background),
        chunks=tuple(SummaryChunk(child.id, chunk.sequence_no, content.version, deepcopy(content.content))
                     for child, chunk, content in rows),
    )
    fingerprint = hashlib.sha256(json.dumps({
        "task_id": task.id, "version": task.version, "video_id": video.id,
        "hash": video.hash, "source_uri": video.source_uri, "duration_ms": video.duration_ms,
        "episode_order": video.episode_order, "background": inputs.background,
        "chunks": [[item.chunk_parse_task_id, item.sequence_no, item.version, item.content]
                   for item in inputs.chunks],
    }, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return task_input, inputs, fingerprint


def mark_summary_ready(session: Session, task_id: str, *, now: datetime) -> bool:
    task = task_store.get_task(session, task_id, for_update=True)
    if task is None:
        return False
    source = _source(session, task)
    if source is None:
        return False
    state = _state(task)
    if state.get("source_fingerprint") == source[2] and state.get("status") != "CANCELLED":
        return False
    _save(session, task, {
        "status": "PENDING", "source_fingerprint": source[2],
        "lease_version": int(state.get("lease_version", 0)) + 1,
        "lease_owner": None, "lease_expire_time_ms": None, "retry_count": 0,
        "next_execute_time_ms": datetime_to_unix_time_ms(now), "last_error": None,
    }, now)
    return True


def claim_next_summary(session: Session, worker_id: str, *, lease_duration: timedelta,
                       config_snapshot: dict | None, now: datetime) -> VideoSummarizeLease | None:
    now_ms = datetime_to_unix_time_ms(now)
    for task_id in task_store.list_due_summary_task_ids(session, now_ms):
        task = task_store.get_task(session, task_id, for_update=True, skip_locked=True)
        if task is None:
            continue
        state = _state(task)
        status = state.get("status")
        if status == "RUNNING":
            if (state.get("lease_expire_time_ms") or 0) > now_ms:
                continue
        elif status not in {"PENDING", "RETRY_WAIT"} or state.get("next_execute_time_ms", 0) > now_ms:
            continue
        source = _source(session, task)
        if source is None:
            state.update(status="CANCELLED", lease_owner=None, lease_expire_time_ms=None)
            _save(session, task, state, now)
            continue
        task_input, _inputs, fingerprint = source
        changed = state.get("source_fingerprint") != fingerprint
        snapshot = deepcopy(config_snapshot if changed else state.get("config_snapshot", config_snapshot))
        if not snapshot:
            continue
        retry_count = 0 if changed else int(state.get("retry_count", 0)) + (status == "RUNNING")
        if retry_count > snapshot["limits"]["max_retries"]:
            state.update(status="FAILED", lease_owner=None, lease_expire_time_ms=None,
                         retry_count=retry_count, last_error="summary lease expired")
            _save(session, task, state, now)
            continue
        expires = now + lease_duration
        state.update(status="RUNNING", source_fingerprint=fingerprint, config_snapshot=snapshot,
                     retry_count=retry_count, lease_owner=worker_id,
                     lease_version=int(state.get("lease_version", 0)) + 1,
                     lease_expire_time_ms=datetime_to_unix_time_ms(expires))
        _save(session, task, state, now)
        return VideoSummarizeLease(task.id, task.video_id, worker_id, state["lease_version"], expires,
                                   retry_count, fingerprint, task_input, snapshot)
    return None


def _locked_current(session: Session, lease: VideoSummarizeLease, now: datetime, *, skip_locked: bool = False):
    task = task_store.get_task(session, lease.video_parse_task_id, for_update=True, skip_locked=skip_locked)
    if task is None and skip_locked:
        raise VideoParseSnapshotConflictError("Summary parent is busy or changed; retry publication")
    state = current_summary_lease(task, lease, now)
    if state is None or task_store.latest_video_task_id(session, lease.video_id) != lease.video_parse_task_id:
        raise VideoParseLeaseLostError("Video summary lease is no longer current")
    return task, state


def load_summary_input(session: Session, lease: VideoSummarizeLease, *, now: datetime) -> VideoSummarizeInput:
    task, _state_value = _locked_current(session, lease, now)
    source = _source(session, task)
    if source is None or source[2] != lease.source_fingerprint:
        raise VideoParseSnapshotConflictError("Video summary sources changed")
    return source[1]


def heartbeat_summary(session: Session, lease: VideoSummarizeLease, *, lease_duration: timedelta, now: datetime) -> bool:
    try:
        task, state = _locked_current(session, lease, now)
    except VideoParseLeaseLostError:
        return False
    state["lease_expire_time_ms"] = datetime_to_unix_time_ms(now + lease_duration)
    _save(session, task, state, now)
    return True


def complete_summary(session: Session, library: LibraryRepository, lease: VideoSummarizeLease,
                     result: VideoSummaryResult, *, cover_uri: str | None, now: datetime) -> bool:
    # Acquire Upload -> Video -> Prefix/parent locks in the same order as a manual edit.
    # The savepoint lets an occupied slot preserve independently useful metadata.
    requested_order = result.episode_order or lease.task_input.episode_order
    def publish(order: int, conflict: str | None) -> None:
        # The savepoint covers validation AND publication. Releasing a smaller
        # savepoint before validation can commit an episode edit on SQLite.
        with session.begin_nested():
            video, previous_order = update_video_episode_order_in_session(
                library, session, video_id=lease.video_id, tenant_id=lease.task_input.tenant_id,
                episode_order=order, now=now, skip_locked=True,
            )
            if previous_order != lease.task_input.episode_order:
                raise VideoParseSnapshotConflictError("Video episode order changed while summarizing")
            task, state = _locked_current(session, lease, now, skip_locked=True)
            source = _source(session, task, episode_order=previous_order, lock_background=True)
            if source is None or source[2] != lease.source_fingerprint:
                raise VideoParseSnapshotConflictError("Video summary sources changed before publication")
            library.update_video_metadata(lease.video_id, session=session, display_name=result.title,
                                          description=result.description, cover_uri=cover_uri)
            state.update(status="COMPLETE", lease_owner=None, lease_expire_time_ms=None, last_error=None,
                         result=result.submission.model_dump(mode="json"), frame_receipts=list(result.frame_receipts),
                         applied_episode_order=video.episode_order, episode_order_conflict=conflict,
                         finished_at_ms=datetime_to_unix_time_ms(now))
            _save(session, task, state, now)
            session.flush()

    try:
        publish(requested_order, None)
    except (VideoEpisodeOrderConflictError, IntegrityError) as error:
        conflict = str(error) if isinstance(error, VideoEpisodeOrderConflictError) else "episode slot is occupied"
        publish(lease.task_input.episode_order, conflict)
    return True


def retry_summary(session: Session, lease: VideoSummarizeLease, *, error: str,
                  retry_at: datetime, max_retries: int, now: datetime) -> bool:
    try:
        task, state = _locked_current(session, lease, now)
    except VideoParseLeaseLostError:
        return False
    attempts = int(state.get("retry_count", 0)) + 1
    state.update(status="RETRY_WAIT" if attempts <= max_retries else "FAILED",
                 retry_count=attempts, lease_owner=None, lease_expire_time_ms=None,
                 next_execute_time_ms=datetime_to_unix_time_ms(retry_at), last_error=error[:8192])
    _save(session, task, state, now)
    return True


def release_summary(session: Session, lease: VideoSummarizeLease, *, now: datetime) -> bool:
    try:
        task, state = _locked_current(session, lease, now)
    except VideoParseLeaseLostError:
        return False
    state.update(status="PENDING", lease_owner=None, lease_expire_time_ms=None,
                 next_execute_time_ms=datetime_to_unix_time_ms(now))
    _save(session, task, state, now)
    return True
