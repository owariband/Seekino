from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Literal

from gateway.media import encode_media_file

from ..constants.media import SERIES_IMAGE_EXTENSIONS, SERIES_IMAGE_ID_PREFIX
from .validation import canonical_uuid


KeyframeImageScope = Literal["video", "video_chunk"]


def image_uri_to_data_url(uri: str, *, resolve: Callable[[str], Path]) -> str:
    """Encode an image through the caller's authorized storage resolver."""
    return encode_media_file(resolve(uri), expected_kind="image")[0]


def encode_series_image_id(series_id: str, sha256: str, extension: str) -> str:
    series_id = canonical_uuid(series_id, "series_id")
    if not isinstance(sha256, str) or re.fullmatch(r"[0-9a-f]{64}", sha256) is None:
        raise ValueError("image checksum must be a SHA-256 hex digest")
    if not isinstance(extension, str) or extension not in SERIES_IMAGE_EXTENSIONS:
        raise ValueError("unsupported Series image extension")
    encoded = base64.urlsafe_b64encode(
        json.dumps([series_id, sha256, extension], separators=(",", ":")).encode()
    ).decode().rstrip("=")
    return SERIES_IMAGE_ID_PREFIX + encoded


def decode_series_image_id(image_id: str) -> tuple[str, str, str]:
    if (
        not isinstance(image_id, str)
        or not image_id.startswith(SERIES_IMAGE_ID_PREFIX)
        or len(image_id) > 256
    ):
        raise ValueError("invalid Series image ID")
    encoded = image_id[len(SERIES_IMAGE_ID_PREFIX):]
    if re.fullmatch(r"[A-Za-z0-9_-]+", encoded) is None:
        raise ValueError("invalid Series image ID")
    try:
        payload = json.loads(base64.b64decode(
            encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True,
        ))
        if not isinstance(payload, list) or len(payload) != 3:
            raise ValueError("invalid Series image ID payload")
        series_id, checksum, extension = payload
        if encode_series_image_id(series_id, checksum, extension) != image_id:
            raise ValueError("noncanonical Series image ID")
    except (TypeError, ValueError, binascii.Error) as exc:
        raise ValueError("invalid Series image ID") from exc
    return series_id, checksum, extension


def stored_keyframe_uri(value: object) -> str | None:
    """Read the URI from current structured or historical keyframe JSON."""

    if isinstance(value, str):
        uri = value
    elif isinstance(value, dict):
        uri = value.get("uri")
    else:
        uri = None
    return uri if isinstance(uri, str) and uri and uri == uri.strip() else None


def stored_keyframe_at_seconds(value: object) -> int | None:
    if isinstance(value, dict):
        at_seconds = value.get("at_seconds", value.get("atSeconds"))
    else:
        at_seconds = getattr(value, "at_seconds", None)
    return (
        at_seconds
        if isinstance(at_seconds, int) and not isinstance(at_seconds, bool)
        else None
    )


def merge_keyframe_uri_values(
    preferred: Iterable[str | dict[str, object]],
    existing: object,
    *,
    start_seconds: int,
    end_seconds: int,
) -> list[str | dict[str, object]]:
    """Prefer current keyframes and keep one URI per valid timeline position."""

    historical = existing if isinstance(existing, (list, tuple)) else ()
    merged: list[str | dict[str, object]] = []
    seen_uris: set[str] = set()
    seen_positions: set[int] = set()
    for value in (*preferred, *historical):
        uri = stored_keyframe_uri(value)
        if uri is None or uri in seen_uris:
            continue
        at_seconds = stored_keyframe_at_seconds(value)
        if at_seconds is not None and start_seconds <= at_seconds < end_seconds:
            if at_seconds in seen_positions:
                continue
            seen_positions.add(at_seconds)
        seen_uris.add(uri)
        merged.append(dict(value) if isinstance(value, dict) else value)
    return merged


def encode_video_keyframe_image_id(video_id: str, index: int) -> str:
    return _encode_keyframe_image_id(
        [canonical_uuid(video_id, "video_id"), _keyframe_index(index)]
    )


def encode_chunk_keyframe_image_id(video_chunk_id: str, index: int) -> str:
    return _encode_keyframe_image_id(
        [
            "video_chunk",
            canonical_uuid(video_chunk_id, "video_chunk_id"),
            _keyframe_index(index),
        ]
    )


def decode_keyframe_image_id(
    image_id: str,
) -> tuple[KeyframeImageScope, str, int]:
    if (
        not isinstance(image_id, str)
        or not image_id
        or len(image_id) > 256
        or re.fullmatch(r"[A-Za-z0-9_-]+", image_id) is None
    ):
        raise ValueError("invalid keyframe image ID")
    try:
        payload = json.loads(
            base64.b64decode(
                image_id + "=" * (-len(image_id) % 4),
                altchars=b"-_",
                validate=True,
            )
        )
        if isinstance(payload, list) and len(payload) == 2:
            identifier, index = payload
            identifier = canonical_uuid(identifier, "video_id")
            index = _keyframe_index(index)
            if encode_video_keyframe_image_id(identifier, index) != image_id:
                raise ValueError("noncanonical keyframe image ID")
            return "video", identifier, index
        if (
            isinstance(payload, list)
            and len(payload) == 3
            and payload[0] == "video_chunk"
        ):
            identifier = canonical_uuid(payload[1], "video_chunk_id")
            index = _keyframe_index(payload[2])
            if encode_chunk_keyframe_image_id(identifier, index) != image_id:
                raise ValueError("noncanonical keyframe image ID")
            return "video_chunk", identifier, index
        raise ValueError("invalid keyframe image ID payload")
    except (TypeError, ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValueError("invalid keyframe image ID") from exc


def _encode_keyframe_image_id(payload: list[object]) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


def _keyframe_index(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("keyframe index must be a nonnegative integer")
    return value


def image_media_type(path: Path) -> str | None:
    with path.open("rb") as source:
        header = source.read(16)
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp"
    return None
