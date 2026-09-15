from __future__ import annotations

from fastapi import APIRouter

from ..api import library
from ..api.schema import SeriesView

router = APIRouter()
router.add_api_route(
    "/api/series/create",
    library.create_series,
    methods=["POST"],
    response_model=SeriesView,
    status_code=201,
)
router.add_api_route(
    "/api/series/list",
    library.list_series,
    methods=["GET"],
    response_model=list[SeriesView],
)
router.add_api_route(
    "/api/series/get",
    library.get_series,
    methods=["GET"],
    response_model=SeriesView,
)
router.add_api_route(
    "/api/videos/content",
    library.get_video_content,
    methods=["GET"],
)
