from __future__ import annotations

import re

from fastapi import Header, Query, Request
from starlette.concurrency import run_in_threadpool

from ..model.dto.uploadDTO import (
    UploadSessionCreateRequest,
    UploadSessionDetail,
    UploadSessionState,
)
from ..service.pagination import decode_cursor, encode_cursor
from ..model.vo.commonVO import Failure, Page
from .context import get_services
from ..model.vo.uploadVO import UploadCompleteView, UploadSegmentView, UploadSessionView
from ..model.vo.jobVO import ParseJobView
from .jobs import project_job

_CONTENT_RANGE_PATTERN = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")


def create_upload_session(
    body: UploadSessionCreateRequest,
    request: Request,
    series_id: str = Query(alias="seriesId"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> UploadSessionView:
    service = get_services(request).uploads
    state = service.create_session(
        workspace_id=body.workspace_id,
        series_id=series_id,
        original_filename=body.original_filename,
        media_type=body.media_type,
        total_size_bytes=body.total_size_bytes,
        idempotency_key=idempotency_key,
        content_sha256=body.content_sha256,
    )
    detail = service.get_detail(
        state.upload_session_id,
        workspace_id=body.workspace_id,
    )
    return _upload_view(detail)


def list_upload_sessions(
    request: Request,
    workspace_id: str = Query(alias="workspaceId"),
    series_id: str | None = Query(None, alias="seriesId"),
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=100),
    cursor: str | None = Query(None),
) -> Page[UploadSessionView]:
    scope = {"workspaceId": workspace_id, "seriesId": series_id, "status": status, "sort": "upload-sequence-created-id-asc"}
    offset = decode_cursor(cursor, scope=scope)
    items = get_services(request).uploads.list_sessions(workspace_id=workspace_id, series_id=series_id, status=status)
    has_more = offset + limit < len(items)
    return Page(
        items=[project_upload_session(state) for state in items[offset:offset + limit]],
        total=len(items), has_more=has_more,
        next_cursor=encode_cursor(offset + limit, scope=scope) if has_more else None,
    )


def get_upload_session(
    request: Request,
    upload_session_id: str = Query(alias="uploadSessionId"),
    workspace_id: str = Query(alias="workspaceId"),
) -> UploadSessionView:
    detail = get_services(request).uploads.get_detail(
        upload_session_id,
        workspace_id=workspace_id,
    )
    return _upload_view(detail)


def delete_upload_session(
    request: Request,
    upload_session_id: str = Query(alias="uploadSessionId"),
    workspace_id: str = Query(alias="workspaceId"),
) -> UploadSessionView:
    return project_upload_session(get_services(request).uploads.delete_session(
        upload_session_id, workspace_id=workspace_id,
    ))


def retry_upload_parse(
    request: Request,
    upload_session_id: str = Query(alias="uploadSessionId"),
    workspace_id: str = Query(alias="workspaceId"),
    expected_job_version: int = Query(ge=1, alias="expectedJobVersion"),
) -> ParseJobView:
    services = get_services(request)
    job_id = services.uploads.retry_parse(
        upload_session_id,
        workspace_id=workspace_id,
        expected_job_version=expected_job_version,
    )
    return project_job(services.jobs.get(job_id, workspace_id=workspace_id))


async def upload_segment(
    request: Request,
    upload_session_id: str = Query(alias="uploadSessionId"),
    segment_number: int = Query(ge=0, alias="segmentNumber"),
    content_range: str = Header(alias="Content-Range"),
    checksum_sha256: str = Header(alias="X-Content-SHA256"),
    workspace_id: str = Query(alias="workspaceId"),
) -> UploadSegmentView:
    start, end_exclusive, total = _parse_content_range(content_range)
    service = get_services(request).uploads
    body_size = end_exclusive - start
    content_length = request.headers.get("Content-Length")
    if content_length is not None:
        try:
            declared_size = int(content_length)
        except ValueError as exc:
            raise ValueError("Content-Length must be an integer") from exc
        if declared_size != body_size:
            raise ValueError("upload segment body size does not match Content-Range")
    await run_in_threadpool(
        service.require_uploadable_segment,
        upload_session_id,
        segment_number,
        workspace_id=workspace_id,
        start_byte=start,
        end_byte_exclusive=end_exclusive,
        total_size_bytes=total,
    )
    data = await _read_limited_body(request, maximum_bytes=body_size)
    state = await run_in_threadpool(
        service.upload_segment,
        upload_session_id,
        segment_number,
        data,
        workspace_id=workspace_id,
        checksum_sha256=checksum_sha256,
        start_byte=start,
        end_byte_exclusive=end_exclusive,
        total_size_bytes=total,
    )
    return UploadSegmentView(
        upload_session_id=upload_session_id, segment_number=state.segment_number,
        start_byte=state.start_byte, end_byte_exclusive=state.end_byte_exclusive,
        actual_size_bytes=state.actual_size_bytes, checksum_sha256=state.checksum_sha256,
    )


async def complete_upload(
    request: Request,
    upload_session_id: str = Query(alias="uploadSessionId"),
    workspace_id: str = Query(alias="workspaceId"),
) -> UploadCompleteView:
    service = get_services(request).uploads
    completed = await run_in_threadpool(
        service.complete,
        upload_session_id,
        workspace_id=workspace_id,
    )
    detail = await run_in_threadpool(
        service.get_detail,
        upload_session_id,
        workspace_id=workspace_id,
    )
    return UploadCompleteView(
        upload_session=_upload_view(detail),
        video_id=completed.video_id,
        job_id=completed.job_id,
    )


def _upload_view(detail: UploadSessionDetail) -> UploadSessionView:
    return project_upload_session(detail.session)


def _parse_content_range(value: str) -> tuple[int, int, int]:
    match = _CONTENT_RANGE_PATTERN.fullmatch(value.strip())
    if match is None:
        raise ValueError("Content-Range must use 'bytes START-END/TOTAL'")
    start, inclusive_end, total = (int(item) for item in match.groups())
    if inclusive_end < start or total <= inclusive_end:
        raise ValueError("Content-Range contains an invalid byte range")
    return start, inclusive_end + 1, total


async def _read_limited_body(request: Request, *, maximum_bytes: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > maximum_bytes:
            raise ValueError("upload segment body exceeds its Content-Range")
        chunks.append(chunk)
    if size != maximum_bytes:
        raise ValueError("upload segment body size does not match Content-Range")
    return b"".join(chunks)


def project_upload_session(state: UploadSessionState) -> UploadSessionView:
    error = (
        Failure(code=state.last_error_code or "PROCESSING_FAILED", message="文件校验或合并失败，请检查文件后重试")
        if state.last_error else None
    )
    return UploadSessionView(
        upload_session_id=state.upload_session_id,
        workspace_id=state.workspace_id, series_id=state.series_id,
        episode_order=state.episode_order, upload_sequence=state.upload_sequence,
        original_filename=state.original_filename, media_type=state.media_type,
        total_size_bytes=state.total_size_bytes, content_sha256=state.content_sha256,
        preferred_segment_size_bytes=state.preferred_segment_size_bytes,
        total_segments=state.total_segments, status=state.status,
        uploaded_segment_count=state.uploaded_segment_count,
        uploaded_size_bytes=state.uploaded_size_bytes,
        missing_segment_numbers=list(state.missing_segment_numbers),
        completion_stage=state.completion_stage, completion_progress=None,
        estimated_remaining_ms=None, expires_at=state.expires_at,
        video_id=state.video_id, job_id=state.job_id, last_error=error,
        version=state.version, created_at=state.created_at, updated_at=state.updated_at,
    )
