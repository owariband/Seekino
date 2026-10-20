from fastapi import APIRouter

from ..api import media
from ..model.dto.mediaDTO import ParseExportOptions
from ..model.vo.searchVO import VideoChunkEvidenceView

router = APIRouter()
router.add_api_route("/api/videos/parse/export/options", media.get_parse_export_options,
                     methods=["GET"], response_model=ParseExportOptions)
router.add_api_route("/api/videos/parse/export", media.export_parse_results, methods=["POST"])
router.add_api_route("/api/videos/content", media.get_video_content, methods=["GET"])
router.add_api_route("/api/media/images/content", media.get_image_content, methods=["GET"])
router.add_api_route("/api/video-chunks/get", media.get_video_chunk, methods=["GET"], response_model=VideoChunkEvidenceView)
