from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlencode

from ..config.media import ImageUploadConfig
from ..constants.media import SERIES_IMAGE_ID_PREFIX
from ..utils.validation import canonical_uuid
from ..constants.identity import LOCAL_USER_ID
from ..model.dto.libraryDTO import SeriesState, VideoSummaryState
from ..model.dto.authDTO import AuthIdentity
from ..model.video import SeriesVisibility, VideoIndexStatus
from ..model.dto.mediaDTO import (
    ImageReference,
    KeyframeImageReference,
    ParseExport,
    ParseExportRecord,
    ParseExportSource,
    PlaybackMedia,
    VideoMedia,
    parse_export_event_types,
)
from ..repository.library import SeriesNotFoundError
from .video_parse_persistence import VideoParseConflictError, VideoParsePersistenceService
from ..schema import VideoChunkEvidence
from ..storage.artifact import ArtifactStore
from ..utils.images import prepare_series_image
from ..utils.media import (
    decode_keyframe_image_id,
    decode_series_image_id,
    encode_chunk_keyframe_image_id,
    encode_video_cover_image_id,
    encode_video_keyframe_image_id,
    image_media_type,
)
from .library import LibraryService
from .access import series_access, require_tenant_read


class MediaMissingError(FileNotFoundError):
    pass


class MediaNotReadyError(RuntimeError):
    pass


class MediaService:
    def __init__(
        self,
        library: LibraryService,
        artifact_store: ArtifactStore,
        *,
        parse_persistence: VideoParsePersistenceService | None = None,
        probe_playback: Callable[[Path], bool] | None = None,
        config: ImageUploadConfig | None = None,
    ) -> None:
        self._library = library
        self._artifact_store = artifact_store
        self._parse_persistence = parse_persistence
        self._probe_playback = probe_playback or _cached_browser_playable
        self.image_upload_config = config or ImageUploadConfig()

    def export_parse_results(
        self, *, tenant_id: str, video_ids: Sequence[str] = (),
        video_parse_task_ids: Sequence[str] = (), event_types: Sequence[str] | None = None,
    ) -> ParseExport:
        supported = parse_export_event_types()
        selected = set(supported if event_types is None else event_types)
        if not selected or selected.difference(supported):
            raise ValueError("eventTypes must select one or more supported types: " + ", ".join(supported))
        if self._parse_persistence is None:
            raise VideoParseConflictError("ParseV2 export is not available in this execution mode")
        snapshots = self._parse_persistence.load_export_snapshot(
            tenant_id=tenant_id, video_ids=video_ids, video_parse_task_ids=video_parse_task_ids,
        )
        lines: list[str] = []
        event_count = 0
        skipped = 0
        for snapshot in snapshots:
            indexed_events = [(index, event) for index, event in enumerate(snapshot.content.timeline)
                              if event.event_type in selected]
            if not indexed_events:
                skipped += 1
                continue
            record = ParseExportRecord(
                source=ParseExportSource(
                    video_id=snapshot.video_id, parse_task_id=snapshot.parse_task_id,
                    chunk_id=snapshot.chunk_id, content_version=snapshot.content_version,
                    timeline_indices=[index for index, _ in indexed_events],
                ),
                persons=snapshot.content.persons,
                timeline=[event for _, event in indexed_events],
            )
            lines.append(record.model_dump_json())
            event_count += len(indexed_events)
        return ParseExport(
            content=("".join(line + "\n" for line in lines)).encode("utf-8"),
            record_count=len(lines), event_count=event_count, skipped_empty_chunks=skipped,
        )

    def publication_ready(self, series: SeriesState, *, require_published: bool = True) -> bool:
        if (
            (require_published and series.visibility != SeriesVisibility.PUBLIC)
            or series.status != "READY" or series.video_count == 0
            or series.ready_video_count != series.video_count
            or series.pending_upload_count > 0 or series.pending_uploads
            or len(series.videos) != series.video_count
        ):
            return False
        return all(
            video.index_status == VideoIndexStatus.READY
            and video.parse_status in {"SUCCEEDED", "COMPLETED", "COMPLETE"}
            and self.playback_view(video.video_id, tenant_id=series.tenant_id, video=video).status == "READY"
            for video in series.videos
        )

    def require_public_series(self, series: SeriesState) -> None:
        if not series.publisher_active or not self.publication_ready(series):
            raise SeriesNotFoundError("公开课程不存在或尚不可用")

    def require_readable_series(self, identity: AuthIdentity, series: SeriesState) -> Literal["member", "public"]:
        access = series_access(identity, series)
        if access == "public":
            self.require_public_series(series)
        return access

    def require_series_access(self, identity: AuthIdentity, series_ids: list[str], *, tenant_id: str) -> None:
        if identity.mode == "local":
            return
        if not series_ids:
            # Empty scopes may expand only within the caller's existing tenant access.
            require_tenant_read(identity, tenant_id)
        for series_id in dict.fromkeys(series_ids):
            series = self._library.get_series(series_id, tenant_id=tenant_id, owner_user_id=identity.user_id)
            self.require_readable_series(identity, series)

    def image_series(
        self, image_id: str, *, tenant_id: str, owner_user_id: str = LOCAL_USER_ID,
    ) -> SeriesState:
        try:
            if isinstance(image_id, str) and image_id.startswith(SERIES_IMAGE_ID_PREFIX):
                series_id, _checksum, _extension = decode_series_image_id(image_id)
            else:
                scope, identifier, _index = decode_keyframe_image_id(image_id)
                if scope in {"video", "video_cover"}:
                    series_id = self._library.get_video(identifier, tenant_id=tenant_id).series_id
                else:
                    series_id = self.get_chunk_evidence(identifier, tenant_id=tenant_id).series_id
        except ValueError as exc:
            raise SeriesNotFoundError("图片资源不存在") from exc
        return self._library.get_series(series_id, tenant_id=tenant_id, owner_user_id=owner_user_id)

    def video_series(self, video_id: str, *, tenant_id: str) -> SeriesState:
        video = self._library.get_video(video_id, tenant_id=tenant_id)
        return self._library.get_series(video.series_id, tenant_id=tenant_id)

    def upload_series_image(
        self, series_id: str, data: bytes, *, tenant_id: str,
    ) -> ImageReference:
        series = self._library.get_series(series_id, tenant_id=tenant_id)
        extension, model_data = prepare_series_image(data, config=self.image_upload_config)
        image_id = self._artifact_store.put_series_image(
            series.series_id, data, extension=extension, model_data=model_data,
        )
        return ImageReference(image_id, "/api/media/images/content?" + urlencode({
            "tenantId": tenant_id, "imageId": image_id,
        }))

    def get_original(
        self,
        video_id: str,
        *,
        tenant_id: str,
        video: VideoSummaryState | VideoChunkEvidence | None = None,
    ) -> VideoMedia:
        video = video or self._library.get_video(video_id, tenant_id=tenant_id)
        if video.video_id != video_id or getattr(video, "tenant_id", tenant_id) != tenant_id:
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
        self, video_id: str, *, tenant_id: str,
        video: VideoSummaryState | VideoChunkEvidence | None = None,
    ) -> PlaybackMedia:
        # File presence and codec facts are rechecked when a public VO is built.
        try:
            media = self.get_original(video_id, tenant_id=tenant_id, video=video)
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
            "tenantId": tenant_id, "videoId": video_id, "variant": "playback",
        }))

    def get_playback(self, video_id: str, *, tenant_id: str) -> VideoMedia:
        playback = self.playback_view(video_id, tenant_id=tenant_id)
        if playback.status == "MISSING":
            raise MediaMissingError("原视频文件已丢失")
        if playback.status != "READY":
            raise MediaNotReadyError("原视频尚无可确认兼容浏览器的播放媒体")
        return self.get_original(video_id, tenant_id=tenant_id)

    def thumbnail_view(
        self, video_id: str, *, tenant_id: str, video: VideoSummaryState | None = None,
    ) -> ImageReference | None:
        video = video or self._library.get_video(video_id, tenant_id=tenant_id)
        if video.video_id != video_id or getattr(video, "tenant_id", tenant_id) != tenant_id:
            raise SeriesNotFoundError("视频资源不存在")
        cover_uri = getattr(video, "cover_uri", None)
        sources = [(encode_video_cover_image_id(video_id), cover_uri)] if cover_uri else []
        sources.extend((encode_video_keyframe_image_id(video_id, index), uri)
                       for index, uri in enumerate(getattr(video, "keyframe_uris", ())))
        for image_id, uri in sources:
            try:
                path = self._artifact_store.resolve(uri)
                if image_media_type(path) is None:
                    continue
            except (OSError, ValueError):
                continue
            return ImageReference(image_id, "/api/media/images/content?" + urlencode({
                "tenantId": tenant_id, "imageId": image_id,
            }))
        return None

    def keyframe_views(
        self,
        evidence: VideoChunkEvidence,
        *,
        tenant_id: str,
    ) -> tuple[KeyframeImageReference, ...]:
        if evidence.tenant_id != tenant_id:
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
                            "tenantId": tenant_id,
                            "imageId": image_id,
                        }),
                        at_seconds=keyframe.at_seconds,
                    )
                )
            if (
                projected
                or refresh
                or evidence.keyframe_uris
                or self._parse_persistence is None
            ):
                return tuple(projected)

            # Agent messages keep their evidence snapshot. Refresh registered
            # keyframes from current V2 facts without rewriting that snapshot.
            try:
                current = self.get_chunk_evidence(
                    evidence.video_chunk_id,
                    tenant_id=tenant_id,
                )
            except (LookupError, ValueError, MediaNotReadyError):
                return ()
            if (
                current.video_id != evidence.video_id
                or current.start_seconds != evidence.start_seconds
                or current.end_seconds != evidence.end_seconds
            ):
                return ()
            evidence = current
        raise AssertionError("unreachable")

    def get_image(self, image_id: str, *, tenant_id: str) -> VideoMedia:
        if isinstance(image_id, str) and image_id.startswith(SERIES_IMAGE_ID_PREFIX):
            try:
                series_id, _checksum, _extension = decode_series_image_id(image_id)
            except ValueError as exc:
                raise SeriesNotFoundError("图片资源不存在") from exc
            self._library.get_series(series_id, tenant_id=tenant_id)
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
        if scope in {"video", "video_cover"}:
            video = self._library.get_video(identifier, tenant_id=tenant_id)
            if scope == "video_cover":
                cover_uri = getattr(video, "cover_uri", None)
                sources = (cover_uri,) if cover_uri else ()
            else:
                sources = getattr(video, "keyframe_uris", ())
        else:
            evidence = self.get_chunk_evidence(
                identifier,
                tenant_id=tenant_id,
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

    def get_chunk_evidence(self, video_chunk_id: str, *, tenant_id: str) -> VideoChunkEvidence:
        if self._parse_persistence is None:
            raise MediaNotReadyError("片段证据读取尚未装配")
        try:
            return self._parse_persistence.get_chunk_evidence(
                video_chunk_id, tenant_id=tenant_id,
            )
        except VideoParseConflictError as error:
            raise MediaNotReadyError("片段的最新 V2 证据尚不可读取") from error


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
