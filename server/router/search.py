from __future__ import annotations

from fastapi import APIRouter

from ..api import search
from ..schema import VideoChunkSearchResult

router = APIRouter()
router.add_api_route(
    "/api/search",
    search.search_video_chunks,
    methods=["POST"],
    response_model=VideoChunkSearchResult,
)
