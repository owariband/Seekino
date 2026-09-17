from __future__ import annotations

from fastapi import APIRouter

from ..api import library
from ..model.vo.commonVO import ImageAsset, Page
from ..model.vo.libraryVO import SeriesView, SeriesSummary, VideoSummary

router = APIRouter()
router.add_api_route(
    "/api/series/create",
    library.create_series,
    methods=["POST"],
    response_model=SeriesView,
    status_code=201,
)
router.add_api_route(
    "/api/series/update",
    library.update_series,
    methods=["POST"],
    response_model=SeriesView,
)
router.add_api_route(
    "/api/videos/update_episode",
    library.update_video_episode,
    methods=["POST"],
    response_model=VideoSummary,
)
router.add_api_route(
    "/api/series/images/upload",
    library.upload_series_image,
    methods=["POST"],
    response_model=ImageAsset,
    status_code=201,
)
router.add_api_route(
    "/api/series/list",
    library.list_series,
    methods=["GET"],
    response_model=Page[SeriesSummary],
)
router.add_api_route(
    "/api/series/get",
    library.get_series,
    methods=["GET"],
    response_model=SeriesView,
)
