from __future__ import annotations

from fastapi import APIRouter

from ..api import uploads
from ..model.vo.commonVO import Page
from ..api.schema import UploadCompleteView, UploadSegmentView, UploadSessionView
from ..model.vo.jobVO import ParseJobView

router = APIRouter()
router.add_api_route(
    "/api/uploads/list", uploads.list_upload_sessions, methods=["GET"],
    response_model=Page[UploadSessionView],
)
router.add_api_route(
    "/api/uploads/create",
    uploads.create_upload_session,
    methods=["POST"],
    response_model=UploadSessionView,
    status_code=201,
)
router.add_api_route(
    "/api/uploads/get",
    uploads.get_upload_session,
    methods=["GET"],
    response_model=UploadSessionView,
)
router.add_api_route(
    "/api/uploads/delete",
    uploads.delete_upload_session,
    methods=["POST"],
    response_model=UploadSessionView,
)
router.add_api_route(
    "/api/uploads/retry-parse",
    uploads.retry_upload_parse,
    methods=["POST"],
    response_model=ParseJobView,
)
router.add_api_route(
    "/api/uploads/reparse",
    uploads.reparse_upload,
    methods=["POST"],
    response_model=ParseJobView,
)
router.add_api_route(
    "/api/uploads/segments/upload",
    uploads.upload_segment,
    methods=["PUT"],
    response_model=UploadSegmentView,
)
router.add_api_route(
    "/api/uploads/complete",
    uploads.complete_upload,
    methods=["POST"],
    response_model=UploadCompleteView,
)
