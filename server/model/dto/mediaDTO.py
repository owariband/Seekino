from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True, slots=True)
class VideoMedia:
    path: Path
    media_type: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class PlaybackMedia:
    status: Literal["PREPARING", "READY", "FAILED", "MISSING"]
    url: str | None
    expires_at: int | None = None


@dataclass(frozen=True, slots=True)
class ImageReference:
    image_id: str
    url: str
    expires_at: int | None = None
