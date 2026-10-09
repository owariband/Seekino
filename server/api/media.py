from __future__ import annotations

from typing import Literal

from fastapi import Query, Request
from fastapi.responses import FileResponse

from .context import get_identity, get_services
from .library import read_series
from ..service.access import require_series_read, require_tenant_read
from ..model.vo.searchVO import VideoChunkEvidenceView
from .search import evidence_view


def get_video_content(
    request: Request, video_id: str = Query(alias="videoId"),
    tenant_id: str = Query(alias="tenantId"),
    variant: Literal["playback", "original"] = Query("playback"),
) -> FileResponse:
    services = get_services(request)
    identity = get_identity(request)
    require_tenant_read(identity, tenant_id)
    if identity.mode == "cloud":
        video = services.library.get_video(video_id, tenant_id=tenant_id)
        read_series(request, video.series_id, tenant_id=tenant_id)
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
    require_tenant_read(identity, tenant_id)
    if identity.mode == "cloud":
        series = services.media.image_series(image_id, tenant_id=tenant_id, owner_user_id=identity.user_id)
        require_series_read(identity, series)
        if tenant_id == identity.public_tenant_id:
            services.media.require_public_series(series)
    media = services.media.get_image(image_id, tenant_id=tenant_id)
    return FileResponse(media.path, media_type=media.media_type, content_disposition_type="inline",
                        headers={"Cache-Control": "private, no-store"} if identity.mode == "cloud" else None)


def get_video_chunk(
    request: Request, video_chunk_id: str = Query(alias="videoChunkId"),
    tenant_id: str = Query(alias="tenantId"),
) -> VideoChunkEvidenceView:
    identity = get_identity(request)
    require_tenant_read(identity, tenant_id)
    service = get_services(request).media
    evidence = service.get_chunk_evidence(video_chunk_id, tenant_id=tenant_id)
    if identity.mode == "cloud":
        read_series(request, evidence.series_id, tenant_id=tenant_id)
    return evidence_view(evidence, service, tenant_id=tenant_id)[0]
