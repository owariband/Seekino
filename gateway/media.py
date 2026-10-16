from __future__ import annotations

import base64
import mimetypes
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


_SUPPORTED_IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
_SUPPORTED_VIDEO_MIME_TYPES = {
    "video/mp4",
    "video/quicktime",
    "video/x-msvideo",
}


@dataclass(frozen=True, slots=True)
class ToolImage:
    data_url: str
    size_bytes: int
    description: str = ""


@dataclass(frozen=True, slots=True)
class ToolImageArtifact:
    images: tuple[ToolImage, ...]

    def __post_init__(self) -> None:
        if len(self.images) > 4 or sum(image.size_bytes for image in self.images) > 8 * 1024 * 1024:
            raise ValueError("tool images exceed their independent media budget")

    @classmethod
    def from_paths(
        cls, images: list[tuple[str | Path, str]], *,
        maximum_images: int = 4, maximum_bytes: int = 8 * 1024 * 1024,
    ) -> ToolImageArtifact:
        if len(images) > maximum_images:
            raise ValueError("tool image count exceeds its budget")
        encoded = []
        total = 0
        for path, description in images:
            url, size = encode_media_file(path, expected_kind="image")
            total += size
            if total > maximum_bytes:
                raise ValueError("tool image bytes exceed their budget")
            encoded.append(ToolImage(url, size, description))
        return cls(tuple(encoded))

    def openai_content(self) -> list[dict]:
        content = []
        for image in self.images:
            if image.description:
                content.append({"type": "text", "text": image.description})
            content.append({
                "type": "image_url", "image_url": {
                    "url": image.data_url, "detail": "high",
                },
            })
        return content


def image_message_content(
    text: str | list[dict[str, Any]], image_urls: Sequence[str] = (),
) -> str | list[dict[str, Any]]:
    if not image_urls:
        return text
    content = deepcopy(text) if isinstance(text, list) else [{"type": "text", "text": text}]
    return [*content, *[
        {"type": "image_url", "image_url": {"url": url}} for url in image_urls
    ]]


def prepare_input_messages(
    prompt: str,
    *,
    initial_messages: Sequence[dict[str, Any]] | None = None,
    image_urls: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Copy caller messages and attach historical and current images as content."""
    messages = (
        deepcopy(list(initial_messages))
        if initial_messages is not None else [{"role": "user", "content": prompt}]
    )
    if not messages:
        raise ValueError("initial_messages must not be empty when provided")
    for message in messages:
        urls = message.pop("imageUrls", ())
        if urls:
            message["content"] = image_message_content(message.get("content", ""), urls)
    if image_urls:
        if messages[-1].get("role") != "user":
            messages.append({"role": "user", "content": prompt})
        messages[-1]["content"] = image_message_content(messages[-1].get("content", ""), image_urls)
    return messages


def require_vision_input(
    messages: Sequence[dict[str, Any]], *, supports_vision_input: bool, model_name: str,
) -> None:
    """Reject actual image input explicitly instead of silently projecting it to text."""
    if supports_vision_input:
        return
    if any(
        isinstance(block, dict) and block.get("type") in {"image_url", "input_image", "image"}
        for message in messages
        for block in (message.get("content") if isinstance(message.get("content"), list) else ())
    ):
        raise ValueError(f"Model {model_name!r} does not support image input (supports_vision_input=False)")


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
