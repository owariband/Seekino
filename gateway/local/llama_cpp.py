from __future__ import annotations

import base64
from collections.abc import Iterator, Mapping, Sequence
from typing import TypeVar

import httpx
from openai import OpenAI, APIError, APIStatusError, omit
from gateway.streaming.chat import request_chat_completion
from gateway.agent_model.errors import ModelFailureError
from pydantic import BaseModel

from gateway.schema_normalization import inline_schema_refs
from gateway.local.ollama import stream_openai_compatible_chat
from gateway.media import encode_media_file, read_media_file
from gateway.multimodal import (
    ImageInput,
    MultimodalInput,
    TextInput,
    validate_generate_request,
)
from gateway.usage import track_model_call


_ResultModel = TypeVar("_ResultModel", bound=BaseModel)


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
        stream: bool = True,
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

        self._stream = stream
        self._request_timeout_seconds = request_timeout_seconds
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
        validate_generate_request(inputs, schema_name)

        content: list[dict[str, object]] = []
        for item in inputs:
            if isinstance(item, TextInput):
                content.append({"type": "text", "text": item.text})
                continue
            if isinstance(item, ImageInput):
                media_url, _ = encode_media_file(
                    item.path, expected_kind="image"
                )
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
            _, media_bytes = read_media_file(item.path, expected_kind="video")
            content.append(
                {
                    "type": "input_video",
                    "input_video": {
                        "data": base64.b64encode(media_bytes).decode("ascii")
                    },
                }
            )

        schema = inline_schema_refs(result_type.model_json_schema())
        with track_model_call(
            provider=self.provider_name,
            model=self.model_name,
            operation=schema_name,
        ) as call:
            try:
                response = request_chat_completion(
                    OpenAI(base_url=f"{self.base_url}/v1", api_key="local",
                           http_client=self._client, max_retries=0).chat.completions,
                    stream=self._stream, usage_call=call,
                    model=self.model_name, messages=[{"role": "user", "content": content}],
                    response_format={"type": "json_schema", "json_schema": {
                        "name": schema_name, "strict": True, "schema": schema,
                    }}, temperature=0, max_tokens=self.max_output_tokens,
                    timeout=self._request_timeout_seconds, extra_headers={"Authorization": omit},
                )
            except APIStatusError as error:
                detail = _llama_cpp_error_detail(error.response)
                raise LlamaCppModelError(f"llama.cpp request failed for model {self.model_name}: HTTP {error.status_code}; server detail: {detail}") from error
            except (APIError, ModelFailureError) as error:
                raise LlamaCppModelError(f"llama.cpp request failed for model {self.model_name}: {error}") from error
            try:
                choice = response.choices[0]
                output_text = choice.message.content
                finish_reason = choice.finish_reason
            except (AttributeError, IndexError, TypeError) as error:
                raise LlamaCppModelError("llama.cpp returned an invalid chat completion envelope") from error
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
        yield from stream_openai_compatible_chat(
            self._client,
            base_url=self.base_url,
            model=self.model_name,
            max_output_tokens=self.max_output_tokens,
            inputs=inputs,
            error_type=LlamaCppModelError,
            stream=self._stream, timeout=self._request_timeout_seconds, provider=self.provider_name,
        )


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
