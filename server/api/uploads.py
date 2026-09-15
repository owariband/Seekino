from __future__ import annotations

import re
from dataclasses import asdict

from fastapi import Header, Query, Request
from starlette.concurrency import run_in_threadpool

from ..constants.workspace import DEFAULT_LOCAL_WORKSPACE_ID
from ..service.upload import UploadSessionDetail
from .context import get_services
from .schema import (
    UploadCompleteView,
    UploadSegmentView,
    UploadSessionCreateRequest,
    UploadSessionView,
)

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
        episode_order=body.episode_order,
        upload_sequence=body.upload_sequence,
        original_filename=body.original_filename,
        media_type=body.media_type,
        total_size_bytes=body.total_size_bytes,
        idempotency_key=idempotency_key,
    )
    detail = service.get_detail(
        state.upload_session_id,
        workspace_id=body.workspace_id,
    )
    return _upload_view(detail)


def get_upload_session(
    request: Request,
    upload_session_id: str = Query(alias="uploadSessionId"),
    workspace_id: str = Query(DEFAULT_LOCAL_WORKSPACE_ID, alias="workspaceId"),
) -> UploadSessionView:
    detail = get_services(request).uploads.get_detail(
        upload_session_id,
        workspace_id=workspace_id,
    )
    return _upload_view(detail)


async def upload_segment(
    request: Request,
    upload_session_id: str = Query(alias="uploadSessionId"),
    segment_number: int = Query(ge=0, alias="segmentNumber"),
    content_range: str = Header(alias="Content-Range"),
    checksum_sha256: str = Header(alias="X-Content-SHA256"),
    workspace_id: str = Query(DEFAULT_LOCAL_WORKSPACE_ID, alias="workspaceId"),
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
    payload = asdict(state)
    payload.pop("artifact_uri")
    payload["upload_session_id"] = upload_session_id
    return UploadSegmentView.model_validate(payload)


async def complete_upload(
    request: Request,
    upload_session_id: str = Query(alias="uploadSessionId"),
    workspace_id: str = Query(DEFAULT_LOCAL_WORKSPACE_ID, alias="workspaceId"),
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
    payload = asdict(detail.session)
    payload["missing_segment_numbers"] = list(detail.missing_segment_numbers)
    return UploadSessionView.model_validate(payload)


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
