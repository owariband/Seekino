from __future__ import annotations

from typing import Literal

from fastapi import Query, Request
from fastapi.responses import FileResponse

from .context import get_services
from ..model.vo.searchVO import VideoChunkEvidenceView
from .search import evidence_view


def get_video_content(
    request: Request, video_id: str = Query(alias="videoId"),
    workspace_id: str = Query(alias="workspaceId"),
    variant: Literal["playback", "original"] = Query("playback"),
) -> FileResponse:
    service = get_services(request).media
    media = (service.get_playback if variant == "playback" else service.get_original)(
        video_id, workspace_id=workspace_id,
    )
    return FileResponse(media.path, media_type=media.media_type, content_disposition_type="inline")


def get_image_content(
    request: Request, image_id: str = Query(alias="imageId"),
    workspace_id: str = Query(alias="workspaceId"),
) -> FileResponse:
    media = get_services(request).media.get_image(image_id, workspace_id=workspace_id)
    return FileResponse(media.path, media_type=media.media_type, content_disposition_type="inline")


def get_video_chunk(
    request: Request, video_chunk_id: str = Query(alias="videoChunkId"),
    workspace_id: str = Query(alias="workspaceId"),
) -> VideoChunkEvidenceView:
    service = get_services(request).media
    evidence = service.get_chunk_evidence(video_chunk_id, workspace_id=workspace_id)
    return evidence_view(evidence, service, workspace_id=workspace_id)[0]
