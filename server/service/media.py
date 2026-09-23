from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlencode

from ..config.media import ImageUploadConfig
from ..constants.media import SERIES_IMAGE_ID_PREFIX
from ..identity import canonical_uuid
from ..model.dto.libraryDTO import VideoSummaryState
from ..model.dto.mediaDTO import (
    ImageReference,
    KeyframeImageReference,
    PlaybackMedia,
    VideoMedia,
)
from ..repository.library import SeriesNotFoundError
from ..repository.video import VideoRepository
from ..schema import VideoChunkEvidence
from ..storage.artifact import ArtifactStore
from ..utils.images import prepare_series_image
from ..utils.media import (
    decode_keyframe_image_id,
    decode_series_image_id,
    encode_chunk_keyframe_image_id,
    encode_video_keyframe_image_id,
    image_media_type,
)
from .library import LibraryService


class MediaMissingError(FileNotFoundError):
    pass


class MediaNotReadyError(RuntimeError):
    pass


class MediaService:
    def __init__(
        self,
        library: LibraryService,
        artifact_store: ArtifactStore,
        video_repository: VideoRepository | None = None,
        *,
        probe_playback: Callable[[Path], bool] | None = None,
        config: ImageUploadConfig | None = None,
    ) -> None:
        self._library = library
        self._artifact_store = artifact_store
        self._video_repository = video_repository
        self._probe_playback = probe_playback or _cached_browser_playable
        self.image_upload_config = config or ImageUploadConfig()

    def upload_series_image(
        self, series_id: str, data: bytes, *, workspace_id: str,
    ) -> ImageReference:
        series = self._library.get_series(series_id, workspace_id=workspace_id)
        extension, model_data = prepare_series_image(data, config=self.image_upload_config)
        image_id = self._artifact_store.put_series_image(
            series.series_id, data, extension=extension, model_data=model_data,
        )
        return ImageReference(image_id, "/api/media/images/content?" + urlencode({
            "workspaceId": workspace_id, "imageId": image_id,
        }))

    def get_original(
        self,
        video_id: str,
        *,
        workspace_id: str,
        video: VideoSummaryState | VideoChunkEvidence | None = None,
    ) -> VideoMedia:
        video = video or self._library.get_video(video_id, workspace_id=workspace_id)
        if video.video_id != video_id or getattr(video, "workspace_id", workspace_id) != workspace_id:
            raise SeriesNotFoundError("视频资源不存在")
        try:
            path = self._artifact_store.resolve(video.original_source_uri)
            size = path.stat().st_size
        except FileNotFoundError as exc:
            raise MediaMissingError("原视频文件已丢失") from exc
        except ValueError as exc:
            raise MediaNotReadyError("尚无受管的原视频文件") from exc
        return VideoMedia(
            path=path,
            media_type=_media_type(path),
            size_bytes=size,
        )

    def playback_view(
        self, video_id: str, *, workspace_id: str,
        video: VideoSummaryState | VideoChunkEvidence | None = None,
    ) -> PlaybackMedia:
        # File presence and codec facts are rechecked when a public VO is built.
        try:
            media = self.get_original(video_id, workspace_id=workspace_id, video=video)
        except MediaMissingError:
            return PlaybackMedia("MISSING", None)
        except (MediaNotReadyError, ValueError, OSError):
            return PlaybackMedia("FAILED", None)
        try:
            playable = self._probe_playback(media.path)
        except (OSError, ValueError, subprocess.SubprocessError):
            playable = False
        if not playable:
            return PlaybackMedia("FAILED", None)
        return PlaybackMedia("READY", "/api/videos/content?" + urlencode({
            "workspaceId": workspace_id, "videoId": video_id, "variant": "playback",
        }))

    def get_playback(self, video_id: str, *, workspace_id: str) -> VideoMedia:
        playback = self.playback_view(video_id, workspace_id=workspace_id)
        if playback.status == "MISSING":
            raise MediaMissingError("原视频文件已丢失")
        if playback.status != "READY":
            raise MediaNotReadyError("原视频尚无可确认兼容浏览器的播放媒体")
        return self.get_original(video_id, workspace_id=workspace_id)

    def thumbnail_view(
        self, video_id: str, *, workspace_id: str, video: VideoSummaryState | None = None,
    ) -> ImageReference | None:
        video = video or self._library.get_video(video_id, workspace_id=workspace_id)
        if video.video_id != video_id or getattr(video, "workspace_id", workspace_id) != workspace_id:
            raise SeriesNotFoundError("视频资源不存在")
        for index, uri in enumerate(getattr(video, "keyframe_uris", ())):
            try:
                path = self._artifact_store.resolve(uri)
                if image_media_type(path) is None:
                    continue
            except (OSError, ValueError):
                continue
            image_id = encode_video_keyframe_image_id(video_id, index)
            return ImageReference(image_id, "/api/media/images/content?" + urlencode({
                "workspaceId": workspace_id, "imageId": image_id,
            }))
        return None

    def keyframe_views(
        self,
        evidence: VideoChunkEvidence,
        *,
        workspace_id: str,
    ) -> tuple[KeyframeImageReference, ...]:
        if evidence.workspace_id != workspace_id:
            raise SeriesNotFoundError("片段资源不存在")
        for refresh in (False, True):
            projected: list[KeyframeImageReference] = []
            for index, keyframe in enumerate(evidence.keyframe_uris):
                if (
                    keyframe.at_seconds is None
                    or not evidence.start_seconds <= keyframe.at_seconds < evidence.end_seconds
                    or keyframe.at_seconds >= evidence.duration_seconds
                ):
                    continue
                try:
                    path = self._artifact_store.resolve(keyframe.uri)
                    if image_media_type(path) is None:
                        continue
                except (OSError, ValueError):
                    continue
                image_id = encode_chunk_keyframe_image_id(
                    evidence.video_chunk_id,
                    index,
                )
                projected.append(
                    KeyframeImageReference(
                        image_id=image_id,
                        url="/api/media/images/content?" + urlencode({
                            "workspaceId": workspace_id,
                            "imageId": image_id,
                        }),
                        at_seconds=keyframe.at_seconds,
                    )
                )
            if (
                projected
                or refresh
                or evidence.keyframe_uris
                or self._video_repository is None
            ):
                return tuple(projected)

            # Agent messages persist an evidence snapshot. A later, safe
            # keyframe backfill updates the canonical VideoChunk rather than
            # rewriting every historical message JSON, so retry projection
            # once with the current registered media facts.
            try:
                current = self.get_chunk_evidence(
                    evidence.video_chunk_id,
                    workspace_id=workspace_id,
                )
            except (LookupError, ValueError):
                return ()
            if (
                current.video_id != evidence.video_id
                or current.start_seconds != evidence.start_seconds
                or current.end_seconds != evidence.end_seconds
            ):
                return ()
            evidence = current
        raise AssertionError("unreachable")

    def get_image(self, image_id: str, *, workspace_id: str) -> VideoMedia:
        if isinstance(image_id, str) and image_id.startswith(SERIES_IMAGE_ID_PREFIX):
            try:
                series_id, _checksum, _extension = decode_series_image_id(image_id)
            except ValueError as exc:
                raise SeriesNotFoundError("图片资源不存在") from exc
            self._library.get_series(series_id, workspace_id=workspace_id)
            try:
                path = self._artifact_store.resolve_series_image(image_id, series_id=series_id)
                media_type = image_media_type(path)
                size = path.stat().st_size
            except FileNotFoundError as exc:
                raise MediaMissingError("背景图片已丢失") from exc
            except (OSError, ValueError) as exc:
                raise MediaNotReadyError("背景图片尚不可读取") from exc
            if media_type is None:
                raise MediaNotReadyError("背景资源不是可确认的图片")
            return VideoMedia(path, media_type, size)
        try:
            scope, identifier, index = decode_keyframe_image_id(image_id)
        except ValueError as exc:
            raise SeriesNotFoundError("图片资源不存在") from exc
        if scope == "video":
            video = self._library.get_video(identifier, workspace_id=workspace_id)
            sources = getattr(video, "keyframe_uris", ())
        else:
            evidence = self.get_chunk_evidence(
                identifier,
                workspace_id=workspace_id,
            )
            sources = tuple(item.uri for item in evidence.keyframe_uris)
        if index >= len(sources):
            raise SeriesNotFoundError("图片资源不存在")
        try:
            path = self._artifact_store.resolve(sources[index])
            media_type = image_media_type(path)
            size = path.stat().st_size
        except FileNotFoundError as exc:
            raise MediaMissingError("关键帧图片已丢失") from exc
        except (OSError, ValueError) as exc:
            raise MediaNotReadyError("关键帧图片尚不可读取") from exc
        if media_type is None:
            raise MediaNotReadyError("关键帧资源不是可确认的图片")
        return VideoMedia(path, media_type, size)

    def get_chunk_evidence(self, video_chunk_id: str, *, workspace_id: str) -> VideoChunkEvidence:
        if self._video_repository is None:
            raise MediaNotReadyError("片段证据读取尚未装配")
        chunk_id = canonical_uuid(video_chunk_id, "video_chunk_id")
        evidence = self._video_repository.hydrate_video_chunk_states([chunk_id])[0].evidence
        if evidence.workspace_id != workspace_id:
            raise SeriesNotFoundError("片段资源不存在")
        return evidence


def _media_type(path: Path) -> str:
    suffix = path.suffix.casefold()
    return {
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".avi": "video/x-msvideo",
    }.get(suffix, "application/octet-stream")


def _cached_browser_playable(path: Path) -> bool:
    facts = path.stat()
    return _playback_codec_fact(path, facts.st_size, facts.st_mtime_ns, facts.st_ino)


@lru_cache(maxsize=256)
def _playback_codec_fact(path: Path, size: int, modified_ns: int, inode: int) -> bool:
    return _is_browser_playable(path)


def _is_browser_playable(path: Path) -> bool:
    result = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries", "format=format_name:stream=codec_type,codec_name,pix_fmt",
        "-of", "json", str(path),
    ], check=True, capture_output=True, text=True, timeout=5)
    payload = json.loads(result.stdout)
    if "mp4" not in payload.get("format", {}).get("format_name", "").split(","):
        return False
    streams = payload.get("streams", [])
    videos = [stream for stream in streams if stream.get("codec_type") == "video"]
    audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
    return bool(videos) and all(
        stream.get("codec_name") == "h264" and stream.get("pix_fmt") in {"yuv420p", "yuvj420p"}
        for stream in videos
    ) and all(stream.get("codec_name") == "aac" for stream in audio)
