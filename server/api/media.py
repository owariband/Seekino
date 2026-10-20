from __future__ import annotations

import json
from typing import Literal

from fastapi import Query, Request
from fastapi.responses import FileResponse, Response

from .context import get_identity, get_services
from .library import read_series
from ..service.access import is_series_member, require_tenant_write
from ..model.dto.mediaDTO import ParseExportOptions, ParseExportRequest, parse_export_event_types
from ..repository.library import SeriesNotFoundError
from ..model.vo.searchVO import VideoChunkEvidenceView
from .search import evidence_view


def get_parse_export_options(
    request: Request, tenant_id: str = Query(alias="tenantId", min_length=1, max_length=64),
) -> ParseExportOptions:
    require_tenant_write(get_identity(request), tenant_id)
    return ParseExportOptions(event_types=list(parse_export_event_types()))


def export_parse_results(request: Request, body: ParseExportRequest) -> Response:
    require_tenant_write(get_identity(request), body.tenant_id)
    exported = get_services(request).media.export_parse_results(
        tenant_id=body.tenant_id, video_ids=body.video_ids or (),
        video_parse_task_ids=body.video_parse_task_ids or (), event_types=body.event_types,
    )
    summary = {
        "record_count": exported.record_count, "event_count": exported.event_count,
        "skipped": {"no_matching_events": exported.skipped_empty_chunks},
    }
    return Response(
        exported.content, media_type="application/x-ndjson",
        headers={
            "Content-Disposition": 'attachment; filename="parse-export.jsonl"',
            "Cache-Control": "private, no-store",
            "X-Export-Summary": json.dumps(summary, separators=(",", ":")),
        },
    )


def get_video_content(
    request: Request, video_id: str = Query(alias="videoId"),
    tenant_id: str = Query(alias="tenantId"),
    variant: Literal["playback", "original"] = Query("playback"),
) -> FileResponse:
    services = get_services(request)
    identity = get_identity(request)
    if identity.mode == "cloud":
        video = services.library.get_video(video_id, tenant_id=tenant_id)
        read_series(request, video.series_id, tenant_id=tenant_id)
        if variant == "original" and not is_series_member(identity, tenant_id):
            raise SeriesNotFoundError("视频资源不存在")
    service = services.media
    media = (service.get_playback if variant == "playback" else service.get_original)(
        video_id, tenant_id=tenant_id,
    )
    return FileResponse(media.path, media_type=media.media_type, content_disposition_type="inline",
                        headers={"Cache-Control": "private, no-store"} if identity.mode == "cloud" else None)


def get_image_content(
    request: Request, image_id: str = Query(alias="imageId"),
    tenant_id: str = Query(alias="tenantId"),
) -> FileResponse:
    services = get_services(request)
    identity = get_identity(request)
    if identity.mode == "cloud":
        series = services.media.image_series(image_id, tenant_id=tenant_id, owner_user_id=identity.user_id)
        services.media.require_readable_series(identity, series)
    media = services.media.get_image(image_id, tenant_id=tenant_id)
    return FileResponse(media.path, media_type=media.media_type, content_disposition_type="inline",
                        headers={"Cache-Control": "private, no-store"} if identity.mode == "cloud" else None)


def get_video_chunk(
    request: Request, video_chunk_id: str = Query(alias="videoChunkId"),
    tenant_id: str = Query(alias="tenantId"),
) -> VideoChunkEvidenceView:
    identity = get_identity(request)
    service = get_services(request).media
    evidence = service.get_chunk_evidence(video_chunk_id, tenant_id=tenant_id)
    if identity.mode == "cloud":
        read_series(request, evidence.series_id, tenant_id=tenant_id)
    return evidence_view(
        evidence, service, tenant_id=tenant_id,
        public_view=not is_series_member(identity, tenant_id),
    )[0]
