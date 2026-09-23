from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Literal


_SUPPORTED_IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
_SUPPORTED_VIDEO_MIME_TYPES = {
    "video/mp4",
    "video/quicktime",
    "video/x-msvideo",
}


def encode_media_file(
    media_path: str | Path,
    *,
    expected_kind: Literal["image", "video"],
) -> tuple[str, int]:
    mime_type, media_bytes = read_media_file(
        media_path,
        expected_kind=expected_kind,
    )
    media_url = (
        f"data:{mime_type};base64,{base64.b64encode(media_bytes).decode('ascii')}"
    )
    return media_url, len(media_bytes)


def read_media_file(
    media_path: str | Path,
    *,
    expected_kind: Literal["image", "video"],
) -> tuple[str, bytes]:
    path = Path(media_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"media file does not exist: {path}")

    mime_type, _ = mimetypes.guess_type(path.name)
    supported_types = (
        _SUPPORTED_IMAGE_MIME_TYPES
        if expected_kind == "image"
        else _SUPPORTED_VIDEO_MIME_TYPES
    )
    if mime_type not in supported_types:
        raise ValueError(f"unsupported {expected_kind} file type: {path.suffix}")

    return mime_type, path.read_bytes()


def read_video_file(video_path: str | Path) -> tuple[Path, bytes, str]:
    path = Path(video_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"video file does not exist: {path}")

    mime_type, _ = mimetypes.guess_type(path.name)
    if mime_type is None or not mime_type.startswith("video/"):
        raise ValueError(f"unsupported video file type: {path.suffix or '<none>'}")

    return path, path.read_bytes(), mime_type
