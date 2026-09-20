from __future__ import annotations

import base64
import json
import logging
import mimetypes
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any, Literal, Protocol, TypeVar

import httpx
from arkruntime import Ark
from openai import APIError, OpenAI
from pydantic import BaseModel

from .constants import ARK_PROVIDER, DEEPSEEK_PROVIDER
from .model_utils import deepseek_non_thinking_extra_body, log_model_fallback
from .schema_utils import inline_schema_refs
from .sse_proxy import request_sse_json, request_sse_text
from .usage import track_model_call

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
_ArkThinkingMode = Literal["enabled", "disabled", "auto"]
_logger = logging.getLogger(__name__)


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


class StructuredAgentModel(MultimodalModel, Protocol):
    """Structured model calls shared by the agent harness and parser."""


class StreamingChatModel(Protocol):
    provider_name: str
    model_name: str

    def stream_text(self, inputs: Sequence[MultimodalInput]) -> Iterator[str]: ...


class ArkMultimodalModel:
    """Send typed multimodal inputs through Ark Responses SSE."""

    provider_name = ARK_PROVIDER

    def __init__(
        self,
        model: str,
        *,
        request_timeout_seconds: float | None = None,
        max_output_tokens: int | None = None,
        thinking_mode: _ArkThinkingMode | None = None,
        fallback_model: str | None = None,
        client: Ark | None = None,
    ) -> None:
        if not model.strip():
            raise ValueError("model must not be empty")
        if request_timeout_seconds is not None and request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be greater than zero")
        if max_output_tokens is not None and max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be greater than zero")
        if fallback_model is not None and not fallback_model.strip():
            raise ValueError("fallback_model must not be empty")

        self.model_name = model
        self.primary_model_name = model
        self.fallback_model_name = (
            fallback_model if fallback_model != model else None
        )
        self.fallback_provider_name = (
            ARK_PROVIDER if self.fallback_model_name is not None else None
        )
        self.max_output_tokens = max_output_tokens
        self.thinking_mode = thinking_mode
        self._request_timeout_seconds = request_timeout_seconds
        self._client = client

    def generate(
        self,
        inputs: Sequence[MultimodalInput],
        *,
        schema_name: str,
        result_type: type[_ResultModel],
        request_timeout_seconds: float | None = None,
    ) -> _ResultModel:
        _validate_generate_request(inputs, schema_name)
        if request_timeout_seconds is not None and request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be greater than zero")

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

        request: dict[str, Any] = dict(
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
        if self.max_output_tokens is not None:
            request["max_output_tokens"] = self.max_output_tokens
        if self.thinking_mode is not None:
            request["thinking"] = {"type": self.thinking_mode}
        effective_timeout = request_timeout_seconds
        if self._request_timeout_seconds is not None:
            effective_timeout = min(
                self._request_timeout_seconds,
                request_timeout_seconds or self._request_timeout_seconds,
            )
        if effective_timeout is not None:
            request["timeout"] = effective_timeout
        client = self._client or self._new_client()
        try:
            def invoke(model_name: str) -> _ResultModel:
                request["model"] = model_name
                with track_model_call(
                    provider=self.provider_name,
                    model=model_name,
                    operation=schema_name,
                ) as call:
                    payload = request_sse_json(
                        client.responses, usage_call=call, **request,
                    )
                    return result_type.model_validate(payload)

            try:
                return invoke(self.model_name)
            except Exception as exc:
                if self.fallback_model_name is None:
                    raise
                log_model_fallback(
                    _logger,
                    primary_provider=ARK_PROVIDER,
                    primary_model=self.model_name,
                    fallback_provider=ARK_PROVIDER,
                    fallback_model=self.fallback_model_name,
                    error=exc,
                )
                return invoke(self.fallback_model_name)
        finally:
            if self._client is None:
                client.close()

    def _new_client(self) -> Ark:
        # Ark documents its client as thread unsafe: never share one across Kicks.
        options: dict[str, Any] = {"max_retries": 0}
        if self._request_timeout_seconds is not None:
            options["timeout"] = self._request_timeout_seconds
        return Ark.volc(**options)

    def stream_text(self, inputs: Sequence[MultimodalInput]) -> Iterator[str]:
        content = _text_only_inputs(inputs, input_type="input_text")
        request: dict[str, Any] = {
            "model": self.model_name,
            "input": [{"role": "user", "content": content}],
        }
        if self.max_output_tokens is not None:
            request["max_output_tokens"] = self.max_output_tokens
        if self.thinking_mode is not None:
            request["thinking"] = {"type": self.thinking_mode}
        client = self._client or self._new_client()
        try:
            with track_model_call(provider=self.provider_name, model=self.model_name, operation="text_generation") as call:
                yield from request_sse_text(client.responses, usage_call=call, **request)
        finally:
            if self._client is None:
                client.close()


class DeepSeekModelError(RuntimeError):
    """The official DeepSeek API failed or returned invalid structured output."""


class DeepSeekStructuredTextModel:
    """Return one validated text-only structure through a required function call."""

    provider_name = DEEPSEEK_PROVIDER

    def __init__(
        self,
        model: str,
        *,
        base_url: str,
        api_key: str,
        max_output_tokens: int,
        request_timeout_seconds: float,
        fallback_model: ArkMultimodalModel | None = None,
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
        if (
            fallback_model is not None
            and fallback_model.provider_name != ARK_PROVIDER
        ):
            raise ValueError("fallback_model must use the ark provider")

        self.model_name = model
        self.primary_model_name = model
        self.fallback_model_name = (
            fallback_model.model_name if fallback_model is not None else None
        )
        self.fallback_provider_name = (
            fallback_model.provider_name if fallback_model is not None else None
        )
        self.base_url = base_url.rstrip("/")
        self.max_output_tokens = max_output_tokens
        self._request_timeout_seconds = request_timeout_seconds
        self._fallback_model = fallback_model
        self._owns_client = client is None
        self._closed = False
        self._client = (
            client
            if client is not None
            else OpenAI(
                api_key=api_key,
                base_url=self.base_url,
                timeout=request_timeout_seconds,
                max_retries=0,
            )
        )

    def generate(
        self,
        inputs: Sequence[MultimodalInput],
        *,
        schema_name: str,
        result_type: type[_ResultModel],
    ) -> _ResultModel:
        _validate_generate_request(inputs, schema_name)
        if any(not isinstance(item, TextInput) for item in inputs):
            raise ValueError(
                "DeepSeekStructuredTextModel only supports TextInput values"
            )
        prompt = "\n\n".join(item.text for item in inputs)
        tool = {
            "type": "function",
            "function": {
                "name": schema_name,
                "description": "Return the validated structured evaluation result.",
                "parameters": inline_schema_refs(result_type.model_json_schema()),
            },
        }

        request_started_at = monotonic()
        try:
            with track_model_call(
                provider=self.provider_name,
                model=self.model_name,
                operation=schema_name,
            ) as call:
                try:
                    _logger.info(
                        "event=structured_model_request_contract provider=%s "
                        "model=%s tool_choice=required tool_count=1 tool_names=%s",
                        self.provider_name,
                        self.model_name,
                        schema_name,
                    )
                    response = self._client.chat.completions.create(
                        model=self.model_name,
                        messages=[{"role": "user", "content": prompt}],
                        tools=[tool],
                        tool_choice="required",
                        temperature=0,
                        max_tokens=self.max_output_tokens,
                        timeout=self._request_timeout_seconds,
                        extra_body=deepseek_non_thinking_extra_body(),
                    )
                except APIError as error:
                    call.capture_response(error)
                    raise DeepSeekModelError(
                        f"DeepSeek request failed for model {self.model_name}: {error}"
                    ) from error

                call.capture_response(response)
                choices = getattr(response, "choices", None)
                if not isinstance(choices, list) or len(choices) != 1:
                    raise DeepSeekModelError(
                        "DeepSeek returned an invalid structured chat envelope"
                    )
                choice = choices[0]
                finish_reason = getattr(choice, "finish_reason", None)
                if finish_reason != "tool_calls":
                    raise DeepSeekModelError(
                        "DeepSeek structured chat did not finish with a function call"
                    )
                message = getattr(choice, "message", None)
                tool_calls = getattr(message, "tool_calls", None)
                if not isinstance(tool_calls, list) or len(tool_calls) != 1:
                    raise DeepSeekModelError(
                        "DeepSeek structured chat returned an invalid function call"
                    )
                tool_call = tool_calls[0]
                function = getattr(tool_call, "function", None)
                if (
                    getattr(tool_call, "type", None) != "function"
                    or getattr(function, "name", None) != schema_name
                ):
                    raise DeepSeekModelError(
                        "DeepSeek structured chat called an unexpected function"
                    )
                arguments = getattr(function, "arguments", None)
                if not isinstance(arguments, str) or not arguments.strip():
                    raise DeepSeekModelError(
                        "DeepSeek structured chat returned empty function arguments"
                    )
                try:
                    return result_type.model_validate_json(arguments)
                except (ValueError, TypeError) as error:
                    raise DeepSeekModelError(
                        f"DeepSeek output does not match schema {schema_name}"
                    ) from error
        except DeepSeekModelError as error:
            if self._fallback_model is None:
                raise
            fallback_timeout = self._request_timeout_seconds - (
                monotonic() - request_started_at
            )
            if fallback_timeout <= 0:
                raise DeepSeekModelError(
                    "DeepSeek structured request deadline expired before fallback"
                ) from error
            log_model_fallback(
                _logger,
                primary_provider=self.provider_name,
                primary_model=self.primary_model_name,
                fallback_provider=self.fallback_provider_name or "unknown",
                fallback_model=self.fallback_model_name or "unknown",
                error=error,
            )
            return self._fallback_model.generate(
                inputs,
                schema_name=schema_name,
                result_type=result_type,
                request_timeout_seconds=fallback_timeout,
            )

    def close(self) -> None:
        if self._owns_client and not self._closed:
            self._client.close()
            self._closed = True


class DeepSeekMultimodalModel:
    """Send text and ordered images through DeepSeek's official Responses API."""

    provider_name = DEEPSEEK_PROVIDER

    def __init__(
        self,
        model: str,
        *,
        base_url: str,
        api_key: str,
        max_output_tokens: int,
        request_timeout_seconds: float,
        reasoning_effort: Literal["none", "low", "high", "max"] = "none",
        fallback_model: ArkMultimodalModel | None = None,
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
        if (
            fallback_model is not None
            and fallback_model.provider_name != ARK_PROVIDER
        ):
            raise ValueError("fallback_model must use the ark provider")

        self.model_name = model
        self.primary_model_name = model
        self.fallback_model_name = (
            fallback_model.model_name if fallback_model is not None else None
        )
        self.fallback_provider_name = (
            fallback_model.provider_name if fallback_model is not None else None
        )
        self.base_url = base_url.rstrip("/")
        self.max_output_tokens = max_output_tokens
        self.reasoning_effort = reasoning_effort
        self._request_timeout_seconds = request_timeout_seconds
        self._fallback_model = fallback_model
        self._owns_client = client is None
        self._closed = False
        self._client = (
            client
            if client is not None
            else OpenAI(
                api_key=api_key,
                base_url=self.base_url,
                timeout=request_timeout_seconds,
                max_retries=0,
            )
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

        schema = inline_schema_refs(result_type.model_json_schema())
        request_started_at = monotonic()
        try:
            with track_model_call(
                provider=self.provider_name,
                model=self.model_name,
                operation=schema_name,
            ) as call:
                try:
                    response = self._client.responses.create(
                        model=self.model_name,
                        input=[{"role": "user", "content": content}],
                        text={
                            "format": {
                                "type": "json_schema",
                                "name": schema_name,
                                "schema": schema,
                                "strict": True,
                            }
                        },
                        reasoning={"effort": self.reasoning_effort},
                        temperature=0,
                        max_output_tokens=self.max_output_tokens,
                        timeout=self._request_timeout_seconds,
                    )
                except APIError as error:
                    call.capture_response(error)
                    raise DeepSeekModelError(
                        f"DeepSeek request failed for model {self.model_name}: {error}"
                    ) from error

                call.capture_response(response)
                if getattr(response, "status", None) == "incomplete":
                    raise DeepSeekModelError(
                        "DeepSeek exhausted max_output_tokens before completing the response"
                    )
                if getattr(response, "status", None) not in {None, "completed"}:
                    raise DeepSeekModelError("DeepSeek response did not complete")
                output_text = getattr(response, "output_text", None)
                if not isinstance(output_text, str) or not output_text.strip():
                    raise DeepSeekModelError("DeepSeek returned no output text")

                try:
                    return result_type.model_validate_json(output_text)
                except (ValueError, TypeError) as error:
                    raise DeepSeekModelError(
                        f"DeepSeek output does not match schema {schema_name}"
                    ) from error
        except DeepSeekModelError as error:
            if self._fallback_model is None:
                raise
            fallback_timeout = self._request_timeout_seconds - (
                monotonic() - request_started_at
            )
            if fallback_timeout <= 0:
                raise DeepSeekModelError(
                    "DeepSeek structured request deadline expired before fallback"
                ) from error
            log_model_fallback(
                _logger,
                primary_provider=self.provider_name,
                primary_model=self.primary_model_name,
                fallback_provider=self.fallback_provider_name or "unknown",
                fallback_model=self.fallback_model_name or "unknown",
                error=error,
            )
            return self._fallback_model.generate(
                inputs,
                schema_name=schema_name,
                result_type=result_type,
                request_timeout_seconds=fallback_timeout,
            )

    def close(self) -> None:
        if self._owns_client and not self._closed:
            self._client.close()
            self._closed = True

    def stream_text(self, inputs: Sequence[MultimodalInput]) -> Iterator[str]:
        content = _text_only_inputs(inputs, input_type="input_text")
        try:
            with track_model_call(provider=self.provider_name, model=self.model_name, operation="text_generation") as call:
                yield from request_sse_text(
                    self._client.responses,
                    usage_call=call,
                    model=self.model_name,
                    input=[{"role": "user", "content": content}],
                    reasoning={"effort": self.reasoning_effort},
                    temperature=0,
                    max_output_tokens=self.max_output_tokens,
                )
        except APIError as error:
            raise DeepSeekModelError(
                f"DeepSeek request failed for model {self.model_name}: {error}"
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

        schema = inline_schema_refs(result_type.model_json_schema())
        with track_model_call(provider=self.provider_name, model=self.model_name, operation=schema_name) as call:
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
                                "schema": schema,
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
                call.capture_response(response_payload)
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

    def stream_text(self, inputs: Sequence[MultimodalInput]) -> Iterator[str]:
        yield from _stream_chat_completion(
            self._client,
            base_url=self.base_url,
            model=self.model_name,
            max_output_tokens=self.max_output_tokens,
            inputs=inputs,
            error_type=OllamaModelError,
        )


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

        schema = inline_schema_refs(result_type.model_json_schema())
        with track_model_call(provider=self.provider_name, model=self.model_name, operation=schema_name) as call:
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
                                "schema": schema,
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
                call.capture_response(response_payload)
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

    def stream_text(self, inputs: Sequence[MultimodalInput]) -> Iterator[str]:
        yield from _stream_chat_completion(
            self._client,
            base_url=self.base_url,
            model=self.model_name,
            max_output_tokens=self.max_output_tokens,
            inputs=inputs,
            error_type=LlamaCppModelError,
        )


def _text_only_inputs(
    inputs: Sequence[MultimodalInput], *, input_type: str
) -> list[dict[str, object]]:
    if not inputs or any(not isinstance(item, TextInput) for item in inputs):
        raise ValueError("agent text calls require non-empty TextInput values")
    return [{"type": input_type, "text": item.text} for item in inputs]


def _stream_chat_completion(
    client: httpx.Client,
    *,
    base_url: str,
    model: str,
    max_output_tokens: int,
    inputs: Sequence[MultimodalInput],
    error_type: type[RuntimeError],
) -> Iterator[str]:
    content = _text_only_inputs(inputs, input_type="text")
    finished = False
    output_chars = 0
    try:
        with client.stream(
            "POST",
            f"{base_url}/v1/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": content}],
                "max_tokens": max_output_tokens,
                "temperature": 0,
                "stream": True,
            },
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    if not finished or output_chars == 0:
                        raise error_type("chat stream ended without completed text")
                    return
                payload = json.loads(data)
                if payload.get("error"):
                    raise error_type("chat stream returned a provider error")
                choices = payload.get("choices", [])
                if not choices:
                    continue  # optional usage-only envelope
                choice = choices[0]
                finish_reason = choice.get("finish_reason")
                if finish_reason is not None and finish_reason != "stop":
                    raise error_type(f"chat stream did not complete: {finish_reason}")
                delta = choice.get("delta", {}).get("content")
                if delta:
                    if finished or not isinstance(delta, str):
                        raise error_type("chat stream returned an invalid text delta")
                    output_chars += len(delta)
                    yield delta
                if finish_reason == "stop":
                    finished = True
    except httpx.HTTPError as error:
        raise error_type(f"chat stream request failed for model {model}") from error
    except (ValueError, TypeError, AttributeError, IndexError) as error:
        raise error_type("chat stream returned an invalid envelope") from error
    raise error_type("chat stream ended without a terminal event")


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
