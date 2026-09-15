from __future__ import annotations

import base64
import binascii
import json
import re
from pathlib import Path

from ..constants.media import SERIES_IMAGE_EXTENSIONS, SERIES_IMAGE_ID_PREFIX
from ..identity import canonical_uuid


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
