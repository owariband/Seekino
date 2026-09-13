from __future__ import annotations

import mimetypes
from pathlib import Path


def read_video_file(video_path: str | Path) -> tuple[Path, bytes, str]:
    path = Path(video_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"video file does not exist: {path}")

    mime_type, _ = mimetypes.guess_type(path.name)
    if mime_type is None or not mime_type.startswith("video/"):
        raise ValueError(f"unsupported video file type: {path.suffix or '<none>'}")

    return path, path.read_bytes(), mime_type
