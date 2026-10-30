from __future__ import annotations

import asyncio
import hashlib
import time
from typing import AsyncIterator

from fastapi import Query, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from ..config.sse import SSEConfig
from ..model.job import VideoParseTaskStatus
from ..model.dto.jobDTO import VideoParseTaskDetail, VideoParseTaskState
from ..service.job import ParseJobService
from ..service.access import require_tenant_write
from ..error.auth import AuthError
from ..service.pagination import decode_cursor, encode_cursor
from .context import get_identity, get_services, get_sse_config, revalidate_identity
from ..model.vo.commonVO import Page, Failure
from ..model.vo.jobVO import VideoParseTaskView
from ..utils.time import datetime_to_unix_time_ms

_TERMINAL_VIDEO_PARSE_TASK_STATUSES = {
    VideoParseTaskStatus.COMPLETE.value,
    VideoParseTaskStatus.PARSING_FAILED.value,
    VideoParseTaskStatus.INDEXING_FAILED.value,
}

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
    if task.last_error and task.last_error.get("code") == "PARSE_NODE_FAILED":
        stage = {"asr": "语音转写", "visual": "画面准备", "ocr": "画面文字识别",
                 "understanding": "视频理解", "commit": "结果提交"}.get(task.last_error.get("stage"), "解析")
        reason = {"model_timeout": "模型请求超时", "timeout": "执行超时",
                  "output_token_limit": "模型输出被截断", "loop_limit": "模型调用次数耗尽",
                  "required_tool_missing": "模型未提交必要结果",
                  "invalid_model_response": "模型返回不符合结果要求"}.get(task.last_error.get("reason"), "执行异常")
        error = Failure(code="PARSE_NODE_FAILED", message=f"{stage}失败：{reason}。本次解析已停止派发，请检查后手动重试。")
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
                    not observation.has_active_execution_lease
                    and not _is_unaccepted_pending_task(task)
                )
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
