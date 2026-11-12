from fastapi import APIRouter

from ..api import media
from ..model.dto.mediaDTO import ParseExportOptions, VideoTextTimeline, VideoVisualTimeline
from ..model.vo.searchVO import VideoChunkEvidenceView

router = APIRouter()
clip_router = APIRouter()
router.add_api_route("/api/videos/parse/export/options", media.get_parse_export_options,
                     methods=["GET"], response_model=ParseExportOptions)
router.add_api_route("/api/videos/parse/export", media.export_parse_results, methods=["POST"])
router.add_api_route("/api/videos/content", media.get_video_content, methods=["GET"])
router.add_api_route("/api/videos/text-timeline", media.get_video_text_timeline, methods=["GET"],
                     response_model=VideoTextTimeline)
router.add_api_route("/api/videos/visual-timeline", media.get_video_visual_timeline, methods=["GET"],
                     response_model=VideoVisualTimeline)
router.add_api_route("/api/videos/visual-search", media.search_video_visuals, methods=["POST"],
                     response_model=VideoVisualTimeline)
clip_router.add_api_route("/api/videos/clip", media.get_video_clip, methods=["GET"])
router.add_api_route("/api/media/images/content", media.get_image_content, methods=["GET"])
router.add_api_route("/api/video-chunks/get", media.get_video_chunk, methods=["GET"], response_model=VideoChunkEvidenceView)
