from fastapi import APIRouter

from ..api import media
from ..model.vo.searchVO import VideoChunkEvidenceView

router = APIRouter()
router.add_api_route("/api/videos/content", media.get_video_content, methods=["GET"])
router.add_api_route("/api/media/images/content", media.get_image_content, methods=["GET"])
router.add_api_route("/api/video-chunks/get", media.get_video_chunk, methods=["GET"], response_model=VideoChunkEvidenceView)
