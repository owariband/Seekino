from __future__ import annotations

from fastapi import Request

from ..schema import VideoChunkSearchResult
from .context import get_services
from .schema import VideoSearchRequest


def search_video_chunks(
    body: VideoSearchRequest,
    request: Request,
) -> VideoChunkSearchResult:
    return get_services(request).retrieval.search(
        body.query,
        workspace_id=body.workspace_id,
        series_ids=body.series_ids,
        top_k=body.top_k,
    )
