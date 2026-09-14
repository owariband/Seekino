from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..storage.artifact import ArtifactStore
from .library import LibraryService


@dataclass(frozen=True, slots=True)
class VideoMedia:
    path: Path
    media_type: str
    size_bytes: int


class MediaService:
    def __init__(
        self,
        library: LibraryService,
        artifact_store: ArtifactStore,
    ) -> None:
        self._library = library
        self._artifact_store = artifact_store

    def get_original(
        self,
        video_id: str,
        *,
        workspace_id: str,
    ) -> VideoMedia:
        video = self._library.get_video(video_id, workspace_id=workspace_id)
        path = self._artifact_store.resolve(video.original_source_uri)
        return VideoMedia(
            path=path,
            media_type=_media_type(path),
            size_bytes=path.stat().st_size,
        )


def _media_type(path: Path) -> str:
    suffix = path.suffix.casefold()
    return {
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".avi": "video/x-msvideo",
    }.get(suffix, "application/octet-stream")
