from __future__ import annotations

from fastapi import APIRouter

from ..api import search
from ..model.vo.searchVO import SearchResultView

router = APIRouter()
router.add_api_route(
    "/api/search",
    search.search_video_chunks,
    methods=["POST"],
    response_model=SearchResultView,
)
