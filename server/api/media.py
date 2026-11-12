from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from fastapi import Depends, Query, Request
from fastapi.responses import FileResponse, Response

from .context import get_identity, get_services, revalidate_identity
from .library import read_series
from ..error.auth import AuthError
from ..service.access import is_series_member, model_query, require_tenant_write, series_access
from ..model.dto.mediaDTO import (
    ParseExportOptions, ParseExportRequest, VideoTextTimeline, VideoVisualQuery, VideoVisualTimeline, parse_export_event_types,
)
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


def get_video_text_timeline(
    request: Request, response: Response, video_id: str = Query(alias="videoId"),
    tenant_id: str = Query(alias="tenantId"),
) -> VideoTextTimeline:
    services = get_services(request)
    video = services.library.get_video(video_id, tenant_id=tenant_id)
    read_series(request, video.series_id, tenant_id=tenant_id)
    response.headers["Cache-Control"] = "private, no-store"
    return services.media.video_text_timeline(video_id, tenant_id=tenant_id)


def _clip_output() -> Iterator[Path]:
    # The request-scoped dependency stays alive until FileResponse finishes sending.
    with TemporaryDirectory(prefix="seekino-clip-") as directory:
        yield Path(directory) / "clip.mp4"


def get_video_visual_timeline(
    request: Request, response: Response, video_id: str = Query(alias="videoId"), tenant_id: str = Query(alias="tenantId"),
) -> VideoVisualTimeline:
    services = get_services(request)
    video = services.library.get_video(video_id, tenant_id=tenant_id)
    read_series(request, video.series_id, tenant_id=tenant_id)
    response.headers["Cache-Control"] = "private, no-store"
    return services.media.video_visual_timeline(video.video_id, tenant_id=tenant_id)


def search_video_visuals(request: Request, response: Response, body: VideoVisualQuery) -> VideoVisualTimeline:
    services = get_services(request)
    identity = get_identity(request)
    video = services.library.get_video(body.video_id, tenant_id=body.tenant_id)
    read_series(request, video.series_id, tenant_id=body.tenant_id)
    response.headers["Cache-Control"] = "private, no-store"
    if getattr(services, "event_retrieval", None) is None:
        return VideoVisualTimeline(ready=False, cues=[])
    with model_query(getattr(services, "auth", None), identity, request.client.host if request.client else "unknown"):
        result = services.event_retrieval.search_visuals(
            body.query, tenant_id=body.tenant_id, series_id=video.series_id, video_id=video.video_id,
        )
    if identity.mode == "cloud":
        request.state.identity = revalidate_identity(request)
        services.media.require_series_access(request.state.identity, [video.series_id], tenant_id=body.tenant_id)
    return result


def get_video_clip(
    request: Request, evidence_ref: str = Query(alias="evidenceRef", min_length=1, max_length=4096),
    start_ms: int = Query(alias="startMs", ge=0), end_ms: int = Query(alias="endMs", gt=0),
    ticket: str | None = Query(None, min_length=1, max_length=8192), inline: bool = Query(False),
    output: Path = Depends(_clip_output, scope="request"),
) -> FileResponse:
    services = get_services(request)
    if ticket is not None:
        if getattr(services, "auth", None) is None or services.auth.config.mode != "cloud":
            raise AuthError("云端片段凭据须在签发服务使用")
        identity = services.auth.authenticate_clip(ticket, evidence_ref, start_ms, end_ms)
    else:
        identity = get_identity(request)
    with model_query(getattr(services, "auth", None), identity, request.client.host if request.client else "unknown",
                     maximum_concurrent=getattr(getattr(services, "agent", None), "active_maximum_concurrent_kicks", None)):
        services.media.write_evidence_clip(identity, evidence_ref, start_ms=start_ms, end_ms=end_ms,
                                           destination=output, public_only=ticket is not None)
    if ticket is not None:
        services.auth.authenticate_clip(ticket, evidence_ref, start_ms, end_ms)
    return FileResponse(output, media_type="video/mp4", filename=f"clip-{start_ms}-{end_ms}.mp4",
                        content_disposition_type="inline" if inline else "attachment",
                        headers={"Cache-Control": "private, no-store"})


def get_image_content(
    request: Request, image_id: str = Query(alias="imageId"),
    tenant_id: str = Query(alias="tenantId"),
) -> FileResponse:
    services = get_services(request)
    identity = get_identity(request)
    if identity.mode == "cloud":
        series = services.media.image_series(image_id, tenant_id=tenant_id, owner_user_id=identity.user_id)
        series_access(identity, series)
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
