from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlencode

from engine.parsev2.schemas import DialogueEvent, OnScreenTextEvent

from ..config.media import ImageUploadConfig
from ..constants.media import CLOUD_CLIP_MAX_MS, SERIES_IMAGE_ID_PREFIX
from ..utils.validation import canonical_uuid
from ..constants.identity import LOCAL_USER_ID
from ..model.dto.libraryDTO import SeriesState, VideoSummaryState
from ..model.dto.authDTO import AuthIdentity
from ..model.video import COMPLETE_VIDEO_UPLOAD_STATUSES
from ..model.dto.mediaDTO import (
    ImageReference,
    KeyframeImageReference,
    ParseExport,
    ParseExportRecord,
    ParseExportSource,
    PlaybackMedia,
    VideoMedia,
    VideoTextCue,
    VideoTextTimeline,
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
from .mcp_evidence import REFERENCE_PREFIX, EvidenceRef, StaleEvidenceError, decode_reference_evidence, resolve_reference_evidence


def validate_clip_range(start_ms: int | None, end_ms: int | None, *, duration_ms: int, cloud: bool) -> None:
    if type(start_ms) is not int or type(end_ms) is not int or not 0 <= start_ms < end_ms <= duration_ms:
        raise ValueError("INVALID_CLIP_RANGE: Supply both start_ms and end_ms within the source duration.")
    if cloud and end_ms - start_ms > CLOUD_CLIP_MAX_MS:
        raise ValueError("CLIP_TOO_LONG: Cloud clips may be at most 60,000 milliseconds.")


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
        reference_index_name: str | None = None,
        probe_playback: Callable[[Path], bool] | None = None,
        config: ImageUploadConfig | None = None,
    ) -> None:
        self._library = library
        self._artifact_store = artifact_store
        self._parse_persistence = parse_persistence
        self._reference_index_name = reference_index_name
        self._probe_playback = probe_playback or _cached_browser_playable
        self.image_upload_config = config or ImageUploadConfig()

    def video_text_timeline(self, video_id: str, *, tenant_id: str) -> VideoTextTimeline:
        if self._parse_persistence is None:
            raise VideoParseConflictError("Video text is unavailable in this execution mode")
        contents = self._parse_persistence.published_video_contents(video_id, tenant_id=tenant_id)
        if contents is None:
            return VideoTextTimeline(ready=False, cues=[])
        cues: dict[tuple[int, int, str], VideoTextCue] = {}
        for content in contents:
            for event in content.timeline:
                if isinstance(event, DialogueEvent):
                    source, texts = "dialogue", [variant.text for variant in event.text_variants]
                elif isinstance(event, OnScreenTextEvent):
                    source, texts = "screen_text", [event.text]
                else:
                    continue
                key = (event.start_seconds, event.end_seconds, source)
                if key in cues:
                    texts = [*cues[key].texts, *texts]
                cues[key] = VideoTextCue(start_ms=event.start_seconds, end_ms=event.end_seconds, source=source,
                                         texts=list(dict.fromkeys(texts)))
        return VideoTextTimeline(ready=True, cues=[cues[key] for key in sorted(cues)])

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

    def require_series_access(self, identity: AuthIdentity, series_ids: list[str], *, tenant_id: str) -> None:
        if identity.mode == "local":
            return
        if not series_ids:
            # Empty scopes may expand only within the caller's existing tenant access.
            require_tenant_read(identity, tenant_id)
        for series_id in dict.fromkeys(series_ids):
            series = self._library.get_series(series_id, tenant_id=tenant_id, owner_user_id=identity.user_id)
            series_access(identity, series)

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
        video = video or self._library.get_video(video_id, tenant_id=tenant_id)
        if video.upload_status not in COMPLETE_VIDEO_UPLOAD_STATUSES:
            return PlaybackMedia("PREPARING", None)
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

    def write_evidence_clip(
        self, identity: AuthIdentity, evidence_ref: str, *, start_ms: int, end_ms: int, destination: Path,
        public_only: bool = False,
    ) -> None:
        """Recheck access and the publication when downloading, including on replayed URLs."""
        reference = decode_reference_evidence(evidence_ref) if evidence_ref.startswith(REFERENCE_PREFIX) else None
        ref = None if reference else EvidenceRef.decode(evidence_ref)
        identity_key = reference if reference else ref.identity
        tenant_id, series_id = identity_key.tenant_id, identity_key.series_id
        cloud = identity.mode == "cloud"
        if public_only:
            if not cloud or tenant_id != identity.public_tenant_id:
                raise SeriesNotFoundError("当前公开素材库中没有此系列")
            self._library.require_public_ready_series(series_id, tenant_id=tenant_id,
                                                      owner_user_id=identity.user_id)
        elif cloud:
            series = self._library.get_series(series_id, tenant_id=tenant_id, owner_user_id=identity.user_id)
            series_access(identity, series)
        elif tenant_id != identity.tenant_id:
            raise SeriesNotFoundError("视频资源不存在")
        if self._parse_persistence is None:
            raise MediaNotReadyError("片段证据读取尚未装配")
        try:
            if reference:
                if self._reference_index_name is None:
                    raise MediaNotReadyError("参考片证据读取尚未装配")
                observation = resolve_reference_evidence(reference, self._parse_persistence, tenant_id=tenant_id,
                                                         index_name=self._reference_index_name)
                duration_ms, video_id, video = observation.duration_ms, reference.video_id, None
            else:
                video = ref.resolve(self._parse_persistence, tenant_id=tenant_id)
                duration_ms, video_id = video.duration_seconds, video.video_id
        except StaleEvidenceError as error:
            raise VideoParseConflictError("EVIDENCE_STALE: 来源版本已变化，请重新检索") from error
        validate_clip_range(start_ms, end_ms, duration_ms=duration_ms, cloud=cloud)
        source = self.get_original(video_id, tenant_id=tenant_id, video=video)
        _encode_source_clip(source.path, destination, start_ms, end_ms, cloud=cloud)
        if public_only:
            self._library.require_public_ready_series(series_id, tenant_id=tenant_id,
                                                      owner_user_id=identity.user_id)

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


def _encode_source_clip(source: Path, output: Path, start_ms: int, end_ms: int, *, cloud: bool) -> None:
    start, end = f"{start_ms / 1000:.3f}", f"{end_ms / 1000:.3f}"
    start_us, end_us = start_ms * 1000, end_ms * 1000
    # Keep source frame timestamps and audio alignment across non-keyframe cuts.
    # Cap the last packet's display duration when the cut falls inside a frame.
    try:
        subprocess.run([
            "ffmpeg", "-v", "error", "-nostdin", "-copyts", "-start_at_zero", "-ss", start,
            "-protocol_whitelist", "file", "-format_whitelist", "mov,avi", "-threads", "2", "-i", str(source),
            "-map", "0:v:0", "-map", "0:a:0?", "-map_metadata", "-1", "-map_chapters", "-1",
            "-vf", f"settb=1/1000000,trim=start_pts={start_us}:end_pts={end_us},setpts=PTS-{start_us},"
                   "pad=ceil(iw/2)*2:ceil(ih/2)*2",
            "-af", f"aresample=48000,atrim=start={start}:end={end},asetpts=PTS-{start_ms * 48}",
            "-fps_mode:v", "passthrough", "-enc_time_base:v", "1:1000000",
            "-c:v", "libx264", "-bf", "0", "-threads", "2", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-bsf:v", f"setts=duration='min(DURATION,max(1,{end_us - start_us}-PTS))'",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-movie_timescale", "1000000", str(output),
        ], check=True, timeout=180 if cloud else None, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        video = subprocess.run([
            "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=index",
            "-of", "csv=p=0", str(output),
        ], check=True, capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as error:
        raise MediaNotReadyError("无法生成此视频片段") from error
    if not video.stdout.strip():
        raise ValueError("INVALID_CLIP_RANGE: The selected range contains no source video frame.")


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
