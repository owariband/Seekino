from __future__ import annotations

import asyncio
import hashlib
import time
from typing import AsyncIterator

from fastapi import Query, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from ..config.sse import SSEConfig
from ..model.job import ParseJobStatus
from ..model.dto.jobDTO import (
    ParseJobDetail,
    ParseJobSummaryState,
)
from ..model.job_progress import calculate_checkpoint_progress
from ..service.job import ParseJobService
from ..service.pagination import decode_cursor, encode_cursor
from .context import get_services, get_sse_config
from ..model.vo.commonVO import Page, Failure
from ..model.vo.jobVO import (
    CurrentVideoChunkView,
    JobCounts,
    JobSummary,
    ParseJobView,
    VideoChunkCheckpointView,
)

_TERMINAL_JOB_STATUSES = {
    ParseJobStatus.SUCCEEDED.value,
    ParseJobStatus.FAILED.value,
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
    workspace_id: str = Query(alias="workspaceId", min_length=1, max_length=64),
) -> ParseJobView:
    detail = get_services(request).jobs.get(
        job_id,
        workspace_id=workspace_id,
    )
    return _job_view(detail)


def list_jobs(
    request: Request,
    workspace_id: str = Query(alias="workspaceId", min_length=1, max_length=64),
    series_id: str | None = Query(None, alias="seriesId"),
    status: str | None = Query(None),
    cursor: str | None = Query(None),
    limit: int = Query(50, ge=1, le=100),
) -> Page[ParseJobView]:
    service = get_services(request).jobs
    statuses = None
    if status and status != "all":
        statuses = tuple(sorted({ParseJobStatus(item.strip()).value for item in status.split(",")}))
    scope = {"workspaceId": workspace_id, "seriesId": series_id, "status": statuses, "sort": "createdAt,jobId:desc"}
    offset = decode_cursor(cursor, scope=scope)
    details = service.list(
        workspace_id=workspace_id, series_id=series_id, statuses=statuses, offset=offset, limit=limit,
    )
    total = service.count(workspace_id=workspace_id, series_id=series_id, statuses=statuses)
    has_more = offset + len(details) < total
    return Page[ParseJobView](
        items=[_job_view(detail) for detail in details], total=total,
        next_cursor=encode_cursor(offset + len(details), scope=scope) if has_more else None,
        has_more=has_more,
    )


def get_job_summary(
    request: Request,
    workspace_id: str = Query(alias="workspaceId", min_length=1, max_length=64),
    series_id: str | None = Query(None, alias="seriesId"),
) -> JobSummary:
    return project_job_summary(get_services(request).jobs.summary(workspace_id=workspace_id, series_id=series_id))


async def stream_job_events(
    request: Request,
    job_id: str = Query(alias="jobId"),
    workspace_id: str = Query(alias="workspaceId", min_length=1, max_length=64),
) -> StreamingResponse:
    service = get_services(request).jobs
    initial = await run_in_threadpool(
        service.get,
        job_id,
        workspace_id=workspace_id,
    )
    return StreamingResponse(
        _job_events(
            request, service, job_id, workspace_id, initial, get_sse_config(request)
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
    workspace_id: str,
    initial: ParseJobDetail,
    config: SSEConfig,
) -> AsyncIterator[str]:
    previous_fingerprint: str | None = None
    detail = initial
    last_event_at = time.monotonic()
    while True:
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
        detail = await run_in_threadpool(
            service.get,
            job_id,
            workspace_id=workspace_id,
        )


def _job_event_fingerprint(view: ParseJobView) -> str:
    """Ignore lease heartbeats when deciding whether the UI state changed."""

    semantic_state = view.model_dump_json(
        by_alias=True,
        exclude={"updated_at"},
    )
    return hashlib.sha256(semantic_state.encode("utf-8")).hexdigest()


def _job_view(detail: ParseJobDetail) -> ParseJobView:
    return project_job(detail)


def project_job(detail: ParseJobDetail) -> ParseJobView:
    job = detail.job
    total_duration_ms = job.duration_ms
    checkpoints = tuple(sorted(detail.checkpoints, key=lambda item: item.sequence_no))
    completed = (
        job.status == ParseJobStatus.SUCCEEDED.value or job.stage == "COMPLETED"
    )
    checkpoint_progress = calculate_checkpoint_progress(
        checkpoints,
        total_duration_ms,
        completed=completed,
    )

    overall_progress = (
        checkpoint_progress.overall_progress
        if checkpoint_progress is not None
        else None
    )
    stage_progress: int | None = None
    completed_duration_ms = (
        checkpoint_progress.completed_duration_ms
        if checkpoint_progress is not None
        else None
    )
    completed_through_ms = (
        checkpoint_progress.completed_through_ms
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
            start_ms=checkpoint_progress.current.start_ms,
            end_ms=checkpoint_progress.current.end_ms,
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
            stage_completed_duration_ms = sum(
                item.chunk_end_ms - item.chunk_start_ms
                for item in stage_completed
            )
            stage_progress = min(
                100,
                stage_completed_duration_ms * 100 // total_duration_ms,
            )

    return ParseJobView(
        job_id=job.job_id, upload_session_id=job.upload_session_id,
        workspace_id=job.workspace_id, series_id=job.series_id,
        video_id=job.video_id, series_name=job.series_name, episode_order=job.episode_order,
        original_filename=job.original_filename, pipeline_version=job.pipeline_version,
        retry_of_job_id=None, status=job.status, stage=job.stage, progress=job.progress,
        overall_progress=overall_progress, stage_progress=stage_progress,
        completed_duration_ms=completed_duration_ms,
        completed_through_ms=completed_through_ms,
        total_duration_ms=total_duration_ms,
        completed_chunk_count=completed_chunk_count,
        total_chunk_count=len(checkpoints), current_chunk=current_chunk,
        attempt_count=job.attempt_count, max_attempts=job.max_attempts,
        available_at=job.available_at if job.status in {ParseJobStatus.QUEUED, ParseJobStatus.RETRY_WAIT} else None,
        estimated_remaining_ms=None, started_at=job.started_at, completed_at=job.completed_at,
        last_error=Failure(code="PROCESSING_FAILED", message="视频建库失败，请检查任务后重试") if job.last_error else None,
        checkpoints=[VideoChunkCheckpointView(
            sequence_no=item.sequence_no, video_chunk_id=item.video_chunk_id, stage=item.stage,
            chunk_start_ms=item.chunk_start_ms, chunk_end_ms=item.chunk_end_ms,
            last_error=Failure(code="PROCESSING_FAILED", message="片段处理发生错误") if item.last_error else None,
        ) for item in checkpoints],
        can_retry=(
            job.status == ParseJobStatus.FAILED.value
            and job.is_primary_upload_job
            and job.upload_session_id is not None
        ),
        can_cancel=False, version=job.version,
        created_at=job.created_at, updated_at=job.updated_at,
    )


def project_job_summary(state: ParseJobSummaryState) -> JobSummary:
    return JobSummary(
        workspace_id=state.workspace_id, series_id=state.series_id,
        counts=JobCounts(
            queued=state.counts.get("QUEUED", 0), running=state.counts.get("RUNNING", 0),
            retry_wait=state.counts.get("RETRY_WAIT", 0), cancelling=state.counts.get("CANCELLING", 0),
            completed=state.counts.get("SUCCEEDED", 0), failed=state.counts.get("FAILED", 0),
            cancelled=state.counts.get("CANCELLED", 0),
        ),
        checkpoint_count=state.checkpoint_count, version=state.version, updated_at=state.updated_at,
    )
