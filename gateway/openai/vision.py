from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TypeVar

from openai import APIError, OpenAI
from pydantic import BaseModel

from gateway.errors import ContentFilterError
from gateway.media import encode_media_file
from gateway.multimodal import (
    MultimodalInput,
    TextInput,
    VideoInput,
    validate_generate_request,
)
from gateway.usage import track_model_call

_Result = TypeVar("_Result", bound=BaseModel)


class OpenAIVisionModel:
    """Portable image Chat Completions adapter; video is sampled by the Engine."""

    def __init__(
        self,
        model: str,
        *,
        provider: str,
        base_url: str,
        api_key: str,
        request_timeout_seconds: float = 600,
        max_output_tokens: int = 8192,
        token_parameter: str = "max_tokens",
        response_format: str = "json_schema",
    ) -> None:
        self.model_name = model
        self.provider_name = provider
        self._base_url = base_url
        self._api_key = api_key
        self._timeout = request_timeout_seconds
        self._max_tokens = max_output_tokens
        self._token_parameter = token_parameter
        self._format = response_format

    def generate(
        self,
        inputs: Sequence[MultimodalInput],
        *,
        schema_name: str,
        result_type: type[_Result],
        request_timeout_seconds: float | None = None,
    ) -> _Result:
        validate_generate_request(inputs, schema_name)
        content = []
        for item in inputs:
            if isinstance(item, VideoInput):
                raise ValueError(
                    "openai_vision requires ordered images, not VideoInput"
                )
            if isinstance(item, TextInput):
                content.append({"type": "text", "text": item.text})
            else:
                url, _ = encode_media_file(item.path, expected_kind="image")
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": url, "detail": item.detail},
                    }
                )
        schema = result_type.model_json_schema()
        if self._format == "json_schema":
            output_format = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            }
        else:
            output_format = {"type": "json_object"}
            content.append(
                {
                    "type": "text",
                    "text": "Return only JSON matching this schema: "
                    + json.dumps(schema, ensure_ascii=False),
                }
            )
        timeout = min(self._timeout, request_timeout_seconds or self._timeout)
        with OpenAI(
            base_url=self._base_url,
            api_key=self._api_key,
            timeout=timeout,
            max_retries=0,
        ) as client:
            with track_model_call(
                provider=self.provider_name,
                model=self.model_name,
                operation=schema_name,
            ) as call:
                try:
                    response = client.chat.completions.create(
                        model=self.model_name,
                        messages=[{"role": "user", "content": content}],
                        response_format=output_format,
                        stream=False,
                        **{self._token_parameter: self._max_tokens},
                    )
                except APIError as error:
                    call.capture_response(error)
                    if getattr(error, "code", None) == "content_filter":
                        raise ContentFilterError(
                            "Vision output rejected", code="content_filter"
                        ) from error
                    raise
                call.capture_response(response)
                if not response.choices:
                    raise RuntimeError("Vision API returned no choices")
                choice = response.choices[0]
                if choice.finish_reason == "content_filter":
                    raise ContentFilterError(
                        "Vision output rejected", code="content_filter"
                    )
                if choice.finish_reason != "stop":
                    raise RuntimeError(
                        f"Vision response did not complete: {choice.finish_reason}"
                    )
                return result_type.model_validate_json(choice.message.content or "")
