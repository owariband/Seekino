from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import asdict
from typing import AsyncIterator

from fastapi import Query, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from ..config.sse import SSEConfig
from ..constants.workspace import DEFAULT_LOCAL_WORKSPACE_ID
from ..model.job import ParseJobStatus
from ..service.job import ParseJobDetail, ParseJobService
from .context import get_services, get_sse_config
from .schema import ParseJobView, VideoChunkCheckpointView

_TERMINAL_JOB_STATUSES = {
    ParseJobStatus.SUCCEEDED.value,
    ParseJobStatus.FAILED.value,
}


def get_job(
    request: Request,
    job_id: str = Query(alias="jobId"),
    workspace_id: str = Query(DEFAULT_LOCAL_WORKSPACE_ID, alias="workspaceId"),
) -> ParseJobView:
    detail = get_services(request).jobs.get(
        job_id,
        workspace_id=workspace_id,
    )
    return _job_view(detail)


def list_jobs(
    request: Request,
    workspace_id: str = Query(DEFAULT_LOCAL_WORKSPACE_ID, alias="workspaceId"),
    limit: int = Query(100, ge=1, le=500),
) -> list[ParseJobView]:
    return [
        _job_view(item)
        for item in get_services(request).jobs.list(
            workspace_id=workspace_id,
            limit=limit,
        )
    ]


async def stream_job_events(
    request: Request,
    job_id: str = Query(alias="jobId"),
    workspace_id: str = Query(DEFAULT_LOCAL_WORKSPACE_ID, alias="workspaceId"),
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
            yield f"event: job\ndata: {payload}\n\n"
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
        exclude={"lease_expires_at", "updated_at"},
    )
    return hashlib.sha256(semantic_state.encode("utf-8")).hexdigest()


def _job_view(detail: ParseJobDetail) -> ParseJobView:
    payload = asdict(detail.job)
    payload.pop("lease_owner")
    payload.pop("lease_version")
    payload.pop("heartbeat_at")
    payload["checkpoints"] = [
        VideoChunkCheckpointView.model_validate(asdict(item))
        for item in detail.checkpoints
    ]
    return ParseJobView.model_validate(payload)
