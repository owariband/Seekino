from __future__ import annotations

import base64
import mimetypes
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar

import httpx
from arkruntime import Ark
from openai import APIError, OpenAI
from pydantic import BaseModel

from .sse_proxy import request_sse_json

_MAX_ARK_INLINE_MEDIA_BYTES = 45 * 1024 * 1024
_MAX_DEEPSEEK_INLINE_IMAGE_BYTES = 32 * 1024 * 1024
_MAX_DEEPSEEK_INLINE_IMAGE_TOTAL_BYTES = 35 * 1024 * 1024
_SUPPORTED_IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
_SUPPORTED_VIDEO_MIME_TYPES = {
    "video/mp4",
    "video/quicktime",
    "video/x-msvideo",
}

_ResultModel = TypeVar("_ResultModel", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class TextInput:
    text: str

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("text input must not be empty")


@dataclass(frozen=True, slots=True)
class ImageInput:
    path: str | Path
    detail: Literal["auto", "low", "high"] = "high"


@dataclass(frozen=True, slots=True)
class VideoInput:
    path: str | Path
    fps: float

    def __post_init__(self) -> None:
        if not 0.2 <= self.fps <= 5.0:
            raise ValueError("video input fps must be between 0.2 and 5.0")


MultimodalInput = TextInput | ImageInput | VideoInput


class MultimodalModel(Protocol):
    provider_name: str
    model_name: str

    def generate(
        self,
        inputs: Sequence[MultimodalInput],
        *,
        schema_name: str,
        result_type: type[_ResultModel],
    ) -> _ResultModel: ...


class ArkMultimodalModel:
    """Send typed multimodal inputs through Ark Responses SSE."""

    provider_name = "ark"

    def __init__(
        self,
        model: str,
        *,
        request_timeout_seconds: float | None = None,
        client: Ark | None = None,
    ) -> None:
        if not model.strip():
            raise ValueError("model must not be empty")
        if request_timeout_seconds is not None and request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be greater than zero")

        self.model_name = model
        if client is not None:
            self._client = client
        elif request_timeout_seconds is not None:
            self._client = Ark.volc(timeout=request_timeout_seconds)
        else:
            self._client = Ark.volc()

    def generate(
        self,
        inputs: Sequence[MultimodalInput],
        *,
        schema_name: str,
        result_type: type[_ResultModel],
    ) -> _ResultModel:
        _validate_generate_request(inputs, schema_name)

        content: list[dict[str, object]] = []
        total_media_bytes = 0
        for item in inputs:
            if isinstance(item, TextInput):
                content.append({"type": "input_text", "text": item.text})
                continue

            media_url, media_bytes = _encode_media_file(
                item.path,
                expected_kind="image" if isinstance(item, ImageInput) else "video",
            )
            total_media_bytes += media_bytes
            if total_media_bytes >= _MAX_ARK_INLINE_MEDIA_BYTES:
                raise ValueError("combined inline media must be smaller than 45 MiB")

            if isinstance(item, ImageInput):
                content.append(
                    {
                        "type": "input_image",
                        "image_url": media_url,
                        "detail": item.detail,
                    }
                )
            else:
                content.append(
                    {
                        "type": "input_video",
                        "video_url": media_url,
                        "fps": item.fps,
                    }
                )

        payload = request_sse_json(
            self._client.responses,
            model=self.model_name,
            input=[{"role": "user", "content": content}],
            text={
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "schema": result_type.model_json_schema(),
                    "strict": True,
                }
            },
        )
        return result_type.model_validate(payload)


class DeepSeekModelError(RuntimeError):
    """The official DeepSeek API failed or returned invalid structured output."""


class DeepSeekMultimodalModel:
    """Send text and ordered images through DeepSeek's official Responses API."""

    provider_name = "deepseek"

    def __init__(
        self,
        model: str,
        *,
        base_url: str,
        api_key: str,
        max_output_tokens: int,
        request_timeout_seconds: float,
        reasoning_effort: Literal["none", "low", "high", "max"] = "none",
        client: Any | None = None,
    ) -> None:
        if not model.strip():
            raise ValueError("model must not be empty")
        if not base_url.strip():
            raise ValueError("base_url must not be empty")
        if not api_key.strip():
            raise ValueError("api_key must not be empty")
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be greater than zero")
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be greater than zero")

        self.model_name = model
        self.base_url = base_url.rstrip("/")
        self.max_output_tokens = max_output_tokens
        self.reasoning_effort = reasoning_effort
        self._client = client or OpenAI(
            api_key=api_key,
            base_url=self.base_url,
            timeout=request_timeout_seconds,
            max_retries=0,
        )

    def generate(
        self,
        inputs: Sequence[MultimodalInput],
        *,
        schema_name: str,
        result_type: type[_ResultModel],
    ) -> _ResultModel:
        _validate_generate_request(inputs, schema_name)

        content: list[dict[str, object]] = []
        total_image_bytes = 0
        for item in inputs:
            if isinstance(item, VideoInput):
                raise ValueError(
                    "DeepSeekMultimodalModel does not support VideoInput; "
                    "provide ordered ImageInput values"
                )
            if isinstance(item, TextInput):
                content.append({"type": "input_text", "text": item.text})
                continue

            media_url, image_bytes = _encode_media_file(
                item.path,
                expected_kind="image",
            )
            if image_bytes > _MAX_DEEPSEEK_INLINE_IMAGE_BYTES:
                raise ValueError("each DeepSeek inline image must be at most 32 MiB")
            total_image_bytes += image_bytes
            if total_image_bytes > _MAX_DEEPSEEK_INLINE_IMAGE_TOTAL_BYTES:
                raise ValueError(
                    "DeepSeek inline images are too large for the 48 MiB "
                    "base64 request limit"
                )
            content.append(
                {
                    "type": "input_image",
                    "image_url": media_url,
                    "detail": item.detail,
                }
            )

        try:
            response = self._client.responses.create(
                model=self.model_name,
                input=[{"role": "user", "content": content}],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": schema_name,
                        "schema": _inline_schema_refs(
                            result_type.model_json_schema()
                        ),
                        "strict": True,
                    }
                },
                reasoning={"effort": self.reasoning_effort},
                temperature=0,
                max_output_tokens=self.max_output_tokens,
            )
        except APIError as error:
            raise DeepSeekModelError(
                f"DeepSeek request failed for model {self.model_name}: {error}"
            ) from error

        if getattr(response, "status", None) == "incomplete":
            raise DeepSeekModelError(
                "DeepSeek exhausted max_output_tokens before completing the response"
            )
        output_text = getattr(response, "output_text", None)
        if not isinstance(output_text, str) or not output_text.strip():
            raise DeepSeekModelError("DeepSeek returned no output text")

        try:
            return result_type.model_validate_json(output_text)
        except (ValueError, TypeError) as error:
            raise DeepSeekModelError(
                f"DeepSeek output does not match schema {schema_name}"
            ) from error


class OllamaModelError(RuntimeError):
    """The Ollama request failed or returned invalid structured output."""


class OllamaMultimodalModel:
    """Send text and ordered images through Ollama's OpenAI-compatible API."""

    provider_name = "ollama"

    def __init__(
        self,
        model: str,
        *,
        base_url: str,
        max_output_tokens: int,
        request_timeout_seconds: float,
        client: httpx.Client | None = None,
    ) -> None:
        if not model.strip():
            raise ValueError("model must not be empty")
        if not base_url.strip():
            raise ValueError("base_url must not be empty")
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be greater than zero")
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be greater than zero")

        self.model_name = model
        self.base_url = base_url.rstrip("/")
        self.max_output_tokens = max_output_tokens
        self._client = client or httpx.Client(timeout=request_timeout_seconds)

    def generate(
        self,
        inputs: Sequence[MultimodalInput],
        *,
        schema_name: str,
        result_type: type[_ResultModel],
    ) -> _ResultModel:
        _validate_generate_request(inputs, schema_name)

        content: list[dict[str, object]] = []
        for item in inputs:
            if isinstance(item, VideoInput):
                raise ValueError(
                    "OllamaMultimodalModel does not support VideoInput; "
                    "provide ordered ImageInput values"
                )
            if isinstance(item, TextInput):
                content.append({"type": "text", "text": item.text})
                continue

            media_url, _ = _encode_media_file(item.path, expected_kind="image")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": media_url},
                }
            )

        try:
            response = self._client.post(
                f"{self.base_url}/v1/chat/completions",
                json={
                    "model": self.model_name,
                    "messages": [{"role": "user", "content": content}],
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": schema_name,
                            "strict": True,
                            "schema": _inline_schema_refs(
                                result_type.model_json_schema()
                            ),
                        },
                    },
                    "temperature": 0,
                    "max_tokens": self.max_output_tokens,
                    "stream": False,
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise OllamaModelError(
                f"Ollama request failed for model {self.model_name}: {error}"
            ) from error

        try:
            response_payload = response.json()
            choice = response_payload["choices"][0]
            output_text = choice["message"]["content"]
            finish_reason = choice.get("finish_reason")
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise OllamaModelError(
                "Ollama returned an invalid chat completion envelope"
            ) from error
        if finish_reason == "length":
            raise OllamaModelError(
                "Ollama exhausted max_output_tokens before completing the response"
            )
        if not isinstance(output_text, str) or not output_text.strip():
            raise OllamaModelError("Ollama returned no output text")

        try:
            return result_type.model_validate_json(output_text)
        except (ValueError, TypeError) as error:
            raise OllamaModelError(
                f"Ollama output does not match schema {schema_name}"
            ) from error


class LlamaCppModelError(RuntimeError):
    """The llama.cpp request failed or returned invalid structured output."""


class LlamaCppMultimodalModel:
    """Send text, images, and video through llama.cpp's chat completion API."""

    provider_name = "llama_cpp"

    def __init__(
        self,
        model: str,
        *,
        base_url: str,
        max_output_tokens: int,
        request_timeout_seconds: float,
        expected_server_video_fps: float,
        client: httpx.Client | None = None,
    ) -> None:
        if not model.strip():
            raise ValueError("model must not be empty")
        if not base_url.strip():
            raise ValueError("base_url must not be empty")
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be greater than zero")
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be greater than zero")
        if not 0.2 <= expected_server_video_fps <= 5.0:
            raise ValueError(
                "expected_server_video_fps must be between 0.2 and 5.0"
            )

        self.model_name = model
        self.base_url = base_url.rstrip("/")
        self.max_output_tokens = max_output_tokens
        self.expected_server_video_fps = expected_server_video_fps
        self._client = client or httpx.Client(timeout=request_timeout_seconds)

    def generate(
        self,
        inputs: Sequence[MultimodalInput],
        *,
        schema_name: str,
        result_type: type[_ResultModel],
    ) -> _ResultModel:
        _validate_generate_request(inputs, schema_name)

        content: list[dict[str, object]] = []
        for item in inputs:
            if isinstance(item, TextInput):
                content.append({"type": "text", "text": item.text})
                continue
            if isinstance(item, ImageInput):
                media_url, _ = _encode_media_file(item.path, expected_kind="image")
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": media_url},
                    }
                )
                continue
            if item.fps != self.expected_server_video_fps:
                raise ValueError(
                    f"video input fps {item.fps} does not match the declared "
                    "llama.cpp server video fps "
                    f"{self.expected_server_video_fps}"
                )
            _, media_bytes = _read_media_file(item.path, expected_kind="video")
            content.append(
                {
                    "type": "input_video",
                    "input_video": {
                        "data": base64.b64encode(media_bytes).decode("ascii")
                    },
                }
            )

        try:
            response = self._client.post(
                f"{self.base_url}/v1/chat/completions",
                json={
                    "model": self.model_name,
                    "messages": [{"role": "user", "content": content}],
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": schema_name,
                            "strict": True,
                            "schema": _inline_schema_refs(
                                result_type.model_json_schema()
                            ),
                        },
                    },
                    "temperature": 0,
                    "max_tokens": self.max_output_tokens,
                    "stream": False,
                },
            )
        except httpx.HTTPError as error:
            raise LlamaCppModelError(
                f"llama.cpp request failed for model {self.model_name}: {error}"
            ) from error

        if not response.is_success:
            detail = _llama_cpp_error_detail(response)
            raise LlamaCppModelError(
                f"llama.cpp request failed for model {self.model_name}: "
                f"HTTP {response.status_code}; server detail: {detail}"
            )

        try:
            response_payload = response.json()
            choice = response_payload["choices"][0]
            output_text = choice["message"]["content"]
            finish_reason = choice.get("finish_reason")
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise LlamaCppModelError(
                "llama.cpp returned an invalid chat completion envelope"
            ) from error
        if finish_reason == "length":
            raise LlamaCppModelError(
                "llama.cpp exhausted max_output_tokens before completing the response"
            )
        if not isinstance(output_text, str) or not output_text.strip():
            raise LlamaCppModelError("llama.cpp returned no output text")

        try:
            return result_type.model_validate_json(output_text)
        except (ValueError, TypeError) as error:
            raise LlamaCppModelError(
                f"llama.cpp output does not match schema {schema_name}"
            ) from error


def _validate_generate_request(
    inputs: Sequence[MultimodalInput],
    schema_name: str,
) -> None:
    if not inputs:
        raise ValueError("at least one multimodal input is required")
    if not schema_name.strip():
        raise ValueError("schema_name must not be empty")


def _llama_cpp_error_detail(response: httpx.Response) -> str:
    """Extract a bounded diagnostic without echoing the request or video payload."""
    try:
        payload = response.json()
    except ValueError:
        return "no structured error detail"

    detail: object | None = None
    if isinstance(payload, Mapping):
        error = payload.get("error")
        if isinstance(error, Mapping):
            detail = error.get("message")
        elif isinstance(error, str):
            detail = error
        if detail is None:
            detail = payload.get("message")

    if not isinstance(detail, str) or not detail.strip():
        return "no structured error detail"
    normalized = " ".join(detail.split())
    return normalized[:500]


def _encode_media_file(
    media_path: str | Path,
    *,
    expected_kind: Literal["image", "video"],
) -> tuple[str, int]:
    mime_type, media_bytes = _read_media_file(
        media_path,
        expected_kind=expected_kind,
    )
    media_url = (
        f"data:{mime_type};base64,{base64.b64encode(media_bytes).decode('ascii')}"
    )
    return media_url, len(media_bytes)


def _read_media_file(
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

    media_bytes = path.read_bytes()
    return mime_type, media_bytes


def _inline_schema_refs(schema: Mapping[str, Any]) -> dict[str, Any]:
    definitions = schema.get("$defs", {})

    def expand(value: Any) -> Any:
        if isinstance(value, list):
            return [expand(item) for item in value]
        if not isinstance(value, dict):
            return value
        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            name = reference.rsplit("/", 1)[-1]
            target = definitions.get(name)
            if not isinstance(target, dict):
                raise ValueError(f"unresolved local JSON schema reference: {reference}")
            merged = {
                **target,
                **{key: item for key, item in value.items() if key != "$ref"},
            }
            return expand(merged)
        return {
            key: expand(item)
            for key, item in value.items()
            if key != "$defs"
        }

    expanded = expand(dict(schema))
    if not isinstance(expanded, dict):
        raise ValueError("JSON schema root must be an object")
    return expanded
