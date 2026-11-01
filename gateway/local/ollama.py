from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import TypeVar

import httpx
from openai import OpenAI, APIError, omit
from gateway.streaming.chat import request_chat_completion, stream_chat_text
from gateway.agent_model.errors import ModelFailureError
from pydantic import BaseModel

from gateway.schema_normalization import inline_schema_refs
from gateway.media import encode_media_file
from gateway.multimodal import (
    MultimodalInput,
    TextInput,
    VideoInput,
    text_only_inputs,
    validate_generate_request,
)
from gateway.usage import track_model_call


_ResultModel = TypeVar("_ResultModel", bound=BaseModel)


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
        stream: bool = True,
        temperature: float = 0.0,
    ) -> None:
        if not model.strip():
            raise ValueError("model must not be empty")
        if not base_url.strip():
            raise ValueError("base_url must not be empty")
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be greater than zero")
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be greater than zero")

        self._stream = stream
        self.temperature = temperature
        self._request_timeout_seconds = request_timeout_seconds
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
        validate_generate_request(inputs, schema_name)

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

            media_url, _ = encode_media_file(item.path, expected_kind="image")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": media_url},
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
                    }}, temperature=self.temperature, max_tokens=self.max_output_tokens,
                    timeout=self._request_timeout_seconds, extra_headers={"Authorization": omit},
                )
            except (APIError, ModelFailureError) as error:
                raise OllamaModelError(f"Ollama request failed for model {self.model_name}: {error}") from error
            try:
                choice = response.choices[0]
                output_text = choice.message.content
                finish_reason = choice.finish_reason
            except (AttributeError, IndexError, TypeError) as error:
                raise OllamaModelError("Ollama returned an invalid chat completion envelope") from error
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
        yield from stream_openai_compatible_chat(
            self._client,
            base_url=self.base_url,
            model=self.model_name,
            max_output_tokens=self.max_output_tokens,
            inputs=inputs,
            error_type=OllamaModelError,
            stream=self._stream, timeout=self._request_timeout_seconds, provider=self.provider_name,
            temperature=self.temperature,
        )


def stream_openai_compatible_chat(
    client: httpx.Client, *, base_url: str, model: str, max_output_tokens: int,
    inputs: Sequence[MultimodalInput], error_type: type[RuntimeError],
    stream: bool = True, timeout: float = 600, provider: str = "local",
    temperature: float = 0.0,
) -> Iterator[str]:
    content = text_only_inputs(inputs, input_type="text")
    completions = OpenAI(base_url=f"{base_url}/v1", api_key="local",
                         http_client=client, max_retries=0).chat.completions
    request = dict(model=model, messages=[{"role": "user", "content": content}],
                   max_tokens=max_output_tokens, temperature=temperature, timeout=timeout,
                   extra_headers={"Authorization": omit})
    with track_model_call(provider=provider, model=model, operation="text_generation") as call:
        try:
            if stream:
                yield from stream_chat_text(completions, usage_call=call, **request)
            else:
                response = request_chat_completion(completions, stream=False, usage_call=call, **request)
                choice = response.choices[0]
                if choice.finish_reason != "stop" or not choice.message.content:
                    raise error_type("chat request ended without completed text")
                yield choice.message.content
        except (APIError, ModelFailureError, ValueError, TypeError, AttributeError, IndexError) as error:
            raise error_type(f"chat stream request failed for model {model}: {error}") from error
