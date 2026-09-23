from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from typing import TypeVar

import httpx
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
        yield from stream_openai_compatible_chat(
            self._client,
            base_url=self.base_url,
            model=self.model_name,
            max_output_tokens=self.max_output_tokens,
            inputs=inputs,
            error_type=OllamaModelError,
        )


def stream_openai_compatible_chat(
    client: httpx.Client,
    *,
    base_url: str,
    model: str,
    max_output_tokens: int,
    inputs: Sequence[MultimodalInput],
    error_type: type[RuntimeError],
) -> Iterator[str]:
    content = text_only_inputs(inputs, input_type="text")
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
                    raise error_type(
                        f"chat stream did not complete: {finish_reason}"
                    )
                delta = choice.get("delta", {}).get("content")
                if delta:
                    if finished or not isinstance(delta, str):
                        raise error_type(
                            "chat stream returned an invalid text delta"
                        )
                    output_chars += len(delta)
                    yield delta
                if finish_reason == "stop":
                    finished = True
    except httpx.HTTPError as error:
        raise error_type(f"chat stream request failed for model {model}") from error
    except (ValueError, TypeError, AttributeError, IndexError) as error:
        raise error_type("chat stream returned an invalid envelope") from error
    raise error_type("chat stream ended without a terminal event")
