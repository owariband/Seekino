from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from typing import Any, TypeVar

from arkruntime import Ark
from pydantic import BaseModel

from gateway.ark.sse import request_sse_json, request_sse_text
from gateway.constants import ARK_PROVIDER
from gateway.harness.fallback import log_model_fallback
from gateway.media import encode_media_file
from gateway.multimodal import (
    ImageInput,
    MultimodalInput,
    TextInput,
    text_only_inputs,
    validate_generate_request,
)
from gateway.usage import track_model_call


_MAX_ARK_INLINE_MEDIA_BYTES = 45 * 1024 * 1024
_ResultModel = TypeVar("_ResultModel", bound=BaseModel)
_logger = logging.getLogger(__name__)


class ArkMultimodalModel:
    """Send typed multimodal inputs through Ark Responses SSE."""

    provider_name = ARK_PROVIDER

    def __init__(
        self,
        model: str,
        *,
        request_timeout_seconds: float | None = None,
        max_output_tokens: int | None = None,
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
        validate_generate_request(inputs, schema_name)
        if request_timeout_seconds is not None and request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be greater than zero")

        content: list[dict[str, object]] = []
        total_media_bytes = 0
        for item in inputs:
            if isinstance(item, TextInput):
                content.append({"type": "input_text", "text": item.text})
                continue

            media_url, media_bytes = encode_media_file(
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

        request: dict[str, Any] = {
            "model": self.model_name,
            "input": [{"role": "user", "content": content}],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "schema": result_type.model_json_schema(),
                    "strict": True,
                }
            },
        }
        if self.max_output_tokens is not None:
            request["max_output_tokens"] = self.max_output_tokens
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
                        client.responses, usage_call=call, **request
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
        content = text_only_inputs(inputs, input_type="input_text")
        request: dict[str, Any] = {
            "model": self.model_name,
            "input": [{"role": "user", "content": content}],
        }
        if self.max_output_tokens is not None:
            request["max_output_tokens"] = self.max_output_tokens
        client = self._client or self._new_client()
        try:
            with track_model_call(
                provider=self.provider_name,
                model=self.model_name,
                operation="text_generation",
            ) as call:
                yield from request_sse_text(
                    client.responses, usage_call=call, **request
                )
        finally:
            if self._client is None:
                client.close()
