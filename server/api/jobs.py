from __future__ import annotations

import asyncio
import hashlib
import time
from typing import AsyncIterator

from fastapi import Query, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from ..config.sse import SSEConfig
from ..model.job import ParseJobStatus, VideoParseTaskStatus
from ..model.dto.jobDTO import (
    ParseJobDetail,
    ParseJobSummaryState,
    VideoParseTaskDetail,
    VideoParseTaskState,
)
from ..model.job_progress import calculate_checkpoint_progress
from ..service.job import ParseJobService
from ..service.access import require_tenant_write
from ..error.auth import AuthError
from ..service.pagination import decode_cursor, encode_cursor
from .context import get_identity, get_services, get_sse_config, revalidate_identity
from ..model.vo.commonVO import Page, Failure
from ..model.vo.jobVO import (
    CurrentVideoChunkView,
    JobCounts,
    JobSummary,
    ParseJobView,
    VideoChunkCheckpointView,
    VideoParseTaskView,
)
from ..utils.time import datetime_to_unix_time_ms

_TERMINAL_JOB_STATUSES = {
    ParseJobStatus.SUCCEEDED.value,
    ParseJobStatus.FAILED.value,
}

_TERMINAL_VIDEO_PARSE_TASK_STATUSES = {
    VideoParseTaskStatus.COMPLETE.value,
    VideoParseTaskStatus.PARSING_FAILED.value,
    VideoParseTaskStatus.INDEXING_FAILED.value,
}

_CHECKPOINT_STAGE_RANK = {
    "PREPARED": 0,
    "SPLIT_READY": 1,
    "PARSED": 2,
    "TEXT_EMBEDDED": 3,
    "VISION_EMBEDDED": 4,
    "INDEXED": 5,
}

_CURRENT_STAGE_CHECKPOINT_RANK = {
    "QUEUED": 1,
    "PREPARING": 1,
    "SPLIT_READY": 1,
    "PARSING": 2,
    "TEXT_EMBEDDING": 3,
    "VISION_EMBEDDING": 4,
    "INDEXING": 5,
}


def get_job(
    request: Request,
    job_id: str = Query(alias="jobId"),
    tenant_id: str = Query(alias="tenantId", min_length=1, max_length=64),
) -> ParseJobView:
    require_tenant_write(get_identity(request), tenant_id)
    detail = get_services(request).jobs.get(
        job_id,
        tenant_id=tenant_id,
    )
    return _job_view(detail)


def get_video_parse_task(
    request: Request,
    video_parse_task_id: str = Query(alias="videoParseTaskId"),
    tenant_id: str = Query(alias="tenantId", min_length=1, max_length=64),
) -> VideoParseTaskView:
    require_tenant_write(get_identity(request), tenant_id)
    state = get_services(request).jobs.get_video_parse_task(
        video_parse_task_id,
        tenant_id=tenant_id,
    )
    return project_video_parse_task(state)


def list_video_parse_tasks(
    request: Request,
    tenant_id: str = Query(alias="tenantId", min_length=1, max_length=64),
    series_id: str | None = Query(None, alias="seriesId"),
    status: str | None = Query(None),
    cursor: str | None = Query(None),
    limit: int = Query(50, ge=1, le=100),
) -> Page[VideoParseTaskView]:
    require_tenant_write(get_identity(request), tenant_id)
    service = get_services(request).jobs
    statuses = None
    if status and status != "all":
        statuses = tuple(
            sorted(
                {
                    VideoParseTaskStatus(item.strip()).value
                    for item in status.split(",")
                }
            )
        )
    scope = {
        "tenantId": tenant_id,
        "seriesId": series_id,
        "status": statuses,
        "sort": "createTime,videoParseTaskId:desc",
    }
    offset = decode_cursor(cursor, scope=scope)
    details = service.list_video_parse_tasks(
        tenant_id=tenant_id,
        series_id=series_id,
        statuses=statuses,
        offset=offset,
        limit=limit,
    )
    total = service.count_video_parse_tasks(
        tenant_id=tenant_id,
        series_id=series_id,
        statuses=statuses,
    )
    # Concurrent deletes can make an offset page empty while the separately
    # counted total is still larger. Never emit the same cursor again.
    has_more = bool(details) and offset + len(details) < total
    return Page[VideoParseTaskView](
        items=[project_video_parse_task(detail) for detail in details],
        total=total,
        next_cursor=(
            encode_cursor(offset + len(details), scope=scope) if has_more else None
        ),
        has_more=has_more,
    )


def list_jobs(
    request: Request,
    tenant_id: str = Query(alias="tenantId", min_length=1, max_length=64),
    series_id: str | None = Query(None, alias="seriesId"),
    status: str | None = Query(None),
    cursor: str | None = Query(None),
    limit: int = Query(50, ge=1, le=100),
) -> Page[ParseJobView]:
    require_tenant_write(get_identity(request), tenant_id)
    service = get_services(request).jobs
    statuses = None
    if status and status != "all":
        statuses = tuple(sorted({ParseJobStatus(item.strip()).value for item in status.split(",")}))
    scope = {"tenantId": tenant_id, "seriesId": series_id, "status": statuses, "sort": "createdAt,jobId:desc"}
    offset = decode_cursor(cursor, scope=scope)
    details = service.list(
        tenant_id=tenant_id, series_id=series_id, statuses=statuses, offset=offset, limit=limit,
    )
    total = service.count(tenant_id=tenant_id, series_id=series_id, statuses=statuses)
    has_more = offset + len(details) < total
    return Page[ParseJobView](
        items=[_job_view(detail) for detail in details], total=total,
        next_cursor=encode_cursor(offset + len(details), scope=scope) if has_more else None,
        has_more=has_more,
    )


def get_job_summary(
    request: Request,
    tenant_id: str = Query(alias="tenantId", min_length=1, max_length=64),
    series_id: str | None = Query(None, alias="seriesId"),
) -> JobSummary:
    require_tenant_write(get_identity(request), tenant_id)
    return project_job_summary(get_services(request).jobs.summary(tenant_id=tenant_id, series_id=series_id))


async def stream_job_events(
    request: Request,
    job_id: str = Query(alias="jobId"),
    tenant_id: str = Query(alias="tenantId", min_length=1, max_length=64),
) -> StreamingResponse:
    identity = await run_in_threadpool(get_identity, request)
    await run_in_threadpool(require_tenant_write, identity, tenant_id)
    service = get_services(request).jobs
    initial = await run_in_threadpool(
        service.get,
        job_id,
        tenant_id=tenant_id,
    )
    return StreamingResponse(
        _job_events(
            request, service, job_id, tenant_id, initial, get_sse_config(request)
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def stream_video_parse_task_events(
    request: Request,
    video_parse_task_id: str = Query(alias="videoParseTaskId"),
    tenant_id: str = Query(alias="tenantId", min_length=1, max_length=64),
) -> StreamingResponse:
    identity = await run_in_threadpool(get_identity, request)
    await run_in_threadpool(require_tenant_write, identity, tenant_id)
    service = get_services(request).jobs
    initial = await run_in_threadpool(
        service.get_video_parse_task,
        video_parse_task_id,
        tenant_id=tenant_id,
    )
    return StreamingResponse(
        _video_parse_task_events(
            request,
            service,
            video_parse_task_id,
            tenant_id,
            initial,
            get_sse_config(request),
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def _job_events(
    request: Request,
    service: ParseJobService,
    job_id: str,
    tenant_id: str,
    initial: ParseJobDetail,
    config: SSEConfig,
) -> AsyncIterator[str]:
    previous_fingerprint: str | None = None
    detail = initial
    last_event_at = time.monotonic()
    first_poll = True
    while True:
        if not await _require_stream_tenant_write(request, tenant_id):
            return
        if not first_poll:
            detail = await run_in_threadpool(service.get, job_id, tenant_id=tenant_id)
        first_poll = False
        view = _job_view(detail)
        payload = view.model_dump_json(by_alias=True)
        fingerprint = _job_event_fingerprint(view)
        if fingerprint != previous_fingerprint:
            last_event_at = time.monotonic()
            yield f"event: job\nid: {view.job_id}:{view.version}\ndata: {payload}\n\n"
            previous_fingerprint = fingerprint
        elif time.monotonic() - last_event_at >= config.keepalive_interval_seconds:
            last_event_at = time.monotonic()
            yield ": keep-alive\n\n"
        if detail.job.status in _TERMINAL_JOB_STATUSES:
            return
        if await request.is_disconnected():
            return
        await asyncio.sleep(config.poll_interval_seconds)


def _job_event_fingerprint(view: ParseJobView) -> str:
    """Ignore lease heartbeats when deciding whether the UI state changed."""

    semantic_state = view.model_dump_json(
        by_alias=True,
        exclude={"updated_at"},
    )
    return hashlib.sha256(semantic_state.encode("utf-8")).hexdigest()


async def _video_parse_task_events(
    request: Request,
    service: ParseJobService,
    video_parse_task_id: str,
    tenant_id: str,
    initial: VideoParseTaskDetail,
    config: SSEConfig,
) -> AsyncIterator[str]:
    previous_fingerprint: str | None = None
    detail = initial
    last_event_at = time.monotonic()
    first_poll = True
    while True:
        if not await _require_stream_tenant_write(request, tenant_id):
            return
        if not first_poll:
            detail = await run_in_threadpool(
                service.get_video_parse_task, video_parse_task_id, tenant_id=tenant_id,
            )
        first_poll = False
        view = project_video_parse_task(detail)
        payload = view.model_dump_json(by_alias=True)
        fingerprint = _video_parse_task_event_fingerprint(view)
        if fingerprint != previous_fingerprint:
            last_event_at = time.monotonic()
            yield (
                "event: video-parse-task\n"
                f"id: {view.video_parse_task_id}:{fingerprint}\n"
                f"data: {payload}\n\n"
            )
            previous_fingerprint = fingerprint
        elif time.monotonic() - last_event_at >= config.keepalive_interval_seconds:
            last_event_at = time.monotonic()
            yield ": keep-alive\n\n"
        if detail.task.status in _TERMINAL_VIDEO_PARSE_TASK_STATUSES:
            return
        if await request.is_disconnected():
            return
        await asyncio.sleep(config.poll_interval_seconds)


async def _require_stream_tenant_write(request: Request, tenant_id: str) -> bool:
    try:
        identity = await run_in_threadpool(revalidate_identity, request)
        await run_in_threadpool(require_tenant_write, identity, tenant_id)
    except AuthError:
        return False
    return True


def _video_parse_task_event_fingerprint(view: VideoParseTaskView) -> str:
    """Ignore worker lease heartbeats and internal runtime-only updates."""

    semantic_state = view.model_dump_json(
        by_alias=True,
        exclude={"heartbeat_time", "lease_expire_time", "update_time"},
    )
    return hashlib.sha256(semantic_state.encode("utf-8")).hexdigest()


def _job_view(detail: ParseJobDetail) -> ParseJobView:
    return project_job(detail)


def project_job(detail: ParseJobDetail) -> ParseJobView:
    job = detail.job
    total_duration_seconds = job.duration_seconds
    checkpoints = tuple(sorted(detail.checkpoints, key=lambda item: item.sequence_no))
    completed = (
        job.status == ParseJobStatus.SUCCEEDED.value or job.stage == "COMPLETED"
    )
    checkpoint_progress = calculate_checkpoint_progress(
        checkpoints,
        total_duration_seconds,
        completed=completed,
    )

    overall_progress = (
        checkpoint_progress.overall_progress
        if checkpoint_progress is not None
        else None
    )
    stage_progress: int | None = None
    completed_duration_seconds = (
        checkpoint_progress.completed_duration_seconds
        if checkpoint_progress is not None
        else None
    )
    completed_through_seconds = (
        checkpoint_progress.completed_through_seconds
        if checkpoint_progress is not None
        else None
    )
    completed_chunk_count = (
        checkpoint_progress.completed_chunk_count
        if checkpoint_progress is not None
        else None
    )
    current_chunk: CurrentVideoChunkView | None = None
    if checkpoint_progress is not None and checkpoint_progress.current is not None:
        current_chunk = CurrentVideoChunkView(
            sequence_no=checkpoint_progress.current.sequence_no,
            start_seconds=checkpoint_progress.current.start_seconds,
            end_seconds=checkpoint_progress.current.end_seconds,
        )

    if completed:
        stage_progress = 100
    elif checkpoint_progress is not None:
        required_rank = _CURRENT_STAGE_CHECKPOINT_RANK.get(job.stage)
        if required_rank is not None:
            stage_completed = [
                item
                for item in checkpoints
                if _CHECKPOINT_STAGE_RANK[item.stage] >= required_rank
            ]
            stage_completed_duration_seconds = sum(
                item.chunk_end_seconds - item.chunk_start_seconds
                for item in stage_completed
            )
            stage_progress = min(
                100,
                stage_completed_duration_seconds * 100 // total_duration_seconds,
            )

    return ParseJobView(
        job_id=job.job_id, upload_session_id=job.upload_session_id,
        tenant_id=job.tenant_id, series_id=job.series_id,
        video_id=job.video_id, series_name=job.series_name, episode_order=job.episode_order,
        original_filename=job.original_filename, pipeline_version=job.pipeline_version,
        parse_config=job.parse_config,
        retry_of_job_id=None, status=job.status, stage=job.stage, progress=job.progress,
        overall_progress=overall_progress, stage_progress=stage_progress,
        completed_duration_seconds=completed_duration_seconds,
        completed_through_seconds=completed_through_seconds,
        video_duration_seconds=total_duration_seconds,
        total_duration_seconds=total_duration_seconds,
        processing_duration_ms=detail.processing_duration_ms,
        processing_timing_complete=detail.processing_timing_complete,
        completed_chunk_count=completed_chunk_count,
        total_chunk_count=len(checkpoints), current_chunk=current_chunk,
        attempt_count=job.attempt_count, max_attempts=job.max_attempts,
        available_at=job.available_at if job.status in {ParseJobStatus.QUEUED, ParseJobStatus.RETRY_WAIT} else None,
        estimated_remaining_ms=None, started_at=job.started_at, completed_at=job.completed_at,
        last_error=Failure(code="PROCESSING_FAILED", message="视频建库失败，请检查任务后重试") if job.last_error else None,
        checkpoints=[VideoChunkCheckpointView(
            sequence_no=item.sequence_no, video_chunk_id=item.video_chunk_id, stage=item.stage,
            chunk_start_seconds=item.chunk_start_seconds, chunk_end_seconds=item.chunk_end_seconds,
            last_error=Failure(code="PROCESSING_FAILED", message="片段处理发生错误") if item.last_error else None,
        ) for item in checkpoints],
        can_retry=(
            job.status == ParseJobStatus.FAILED.value
            and job.is_primary_upload_job
            and job.upload_session_id is not None
        ),
        can_reparse=(
            job.status
            in {
                ParseJobStatus.SUCCEEDED.value,
                ParseJobStatus.FAILED.value,
            }
            and job.is_primary_upload_job
            and job.upload_session_id is not None
        ),
        can_cancel=False, version=job.version,
        created_at=job.created_at, updated_at=job.updated_at,
    )


def project_job_summary(state: ParseJobSummaryState) -> JobSummary:
    return JobSummary(
        tenant_id=state.tenant_id, series_id=state.series_id,
        counts=JobCounts(
            queued=state.counts.get("QUEUED", 0), running=state.counts.get("RUNNING", 0),
            retry_wait=state.counts.get("RETRY_WAIT", 0), cancelling=state.counts.get("CANCELLING", 0),
            completed=state.counts.get("SUCCEEDED", 0), failed=state.counts.get("FAILED", 0),
            cancelled=state.counts.get("CANCELLED", 0),
        ),
        checkpoint_count=state.checkpoint_count, version=state.version, updated_at=state.updated_at,
    )


def project_video_parse_task(
    state: VideoParseTaskState | VideoParseTaskDetail,
) -> VideoParseTaskView:
    observation = state if isinstance(state, VideoParseTaskDetail) else None
    task = observation.task if observation is not None else state
    error = None
    if task.last_error is not None:
        error = Failure(
            code="PROCESSING_FAILED",
            message=(
                "检索索引发布失败，请重试"
                if task.status == VideoParseTaskStatus.INDEXING_FAILED.value
                else "视频解析失败，请重试"
            ),
        )
    public_detail = {
        key: value
        for key, value in task.detail.items()
        if key != "_worker_runtime_v2"
    }
    return VideoParseTaskView(
        video_parse_task_id=task.video_parse_task_id,
        video_id=task.video_id,
        pipeline_version=task.pipeline_version,
        status=task.status,
        parse_config=task.parse_config,
        chunk_count=task.chunk_count,
        version=task.version,
        next_execute_time=datetime_to_unix_time_ms(task.next_execute_time),
        attempt_count=task.attempt_count,
        max_attempts=task.max_attempts,
        lease_owner=task.lease_owner,
        lease_version=task.lease_version,
        lease_expire_time=(
            datetime_to_unix_time_ms(task.lease_expire_time)
            if task.lease_expire_time is not None else None
        ),
        heartbeat_time=(
            datetime_to_unix_time_ms(task.heartbeat_time)
            if task.heartbeat_time is not None else None
        ),
        start_time=(
            datetime_to_unix_time_ms(task.start_time)
            if task.start_time is not None else None
        ),
        finish_time=(
            datetime_to_unix_time_ms(task.finish_time)
            if task.finish_time is not None else None
        ),
        processing_duration_ms=task.processing_duration_ms,
        processing_timing_complete=task.processing_timing_complete,
        last_error=error,
        is_test=task.is_test,
        detail=public_detail,
        create_time=datetime_to_unix_time_ms(task.create_time),
        update_time=datetime_to_unix_time_ms(task.update_time),
        tenant_id=observation.tenant_id if observation is not None else None,
        series_id=observation.series_id if observation is not None else None,
        series_name=observation.series_name if observation is not None else None,
        episode_order=(observation.episode_order if observation is not None else None),
        original_filename=(
            observation.original_filename if observation is not None else None
        ),
        upload_session_id=(
            observation.upload_session_id if observation is not None else None
        ),
        duration_seconds=(
            observation.duration_seconds if observation is not None else None
        ),
        can_retry=(
            observation is not None
            and observation.is_latest
            and not observation.has_active_prefix_lease
            and not observation.has_active_execution_lease
            and observation.upload_session_id is not None
            and task.status
            in {
                VideoParseTaskStatus.PARSING_FAILED.value,
                VideoParseTaskStatus.INDEXING_FAILED.value,
            }
        ),
        can_reparse=(
            observation is not None
            and observation.is_latest
            and not observation.has_active_prefix_suffix_lease
            and observation.upload_session_id is not None
            and (
                task.status in _TERMINAL_VIDEO_PARSE_TASK_STATUSES
                or (
                    observation.pending_takeover_enabled
                    and _is_unaccepted_pending_task(task)
                )
            )
        ),
        can_delete=(
            observation is not None
            and observation.is_latest
            and observation.upload_session_id is not None
            and not observation.has_active_deletion_lease
        ),
    )


def _is_unaccepted_pending_task(task: VideoParseTaskState) -> bool:
    if task.status != VideoParseTaskStatus.PENDING.value:
        return False
    runtime = task.detail.get("_worker_runtime_v2")
    return not isinstance(runtime, dict) or runtime.get("accepted") is not True
