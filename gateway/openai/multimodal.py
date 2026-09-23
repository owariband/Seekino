from __future__ import annotations

import logging
from collections.abc import Sequence
from time import monotonic
from typing import Any, Literal, TypeVar

from openai import APIError, OpenAI
from pydantic import BaseModel

from gateway.constants import ARK_PROVIDER, DEEPSEEK_PROVIDER
from gateway.harness.fallback import log_model_fallback
from gateway.schema_normalization import inline_schema_refs
from gateway.media import encode_media_file
from gateway.multimodal import (
    DeadlineAwareMultimodalModel,
    MultimodalInput,
    TextInput,
    VideoInput,
    validate_generate_request,
)
from gateway.usage import track_model_call


_MAX_DEEPSEEK_INLINE_IMAGE_BYTES = 32 * 1024 * 1024
_MAX_DEEPSEEK_INLINE_IMAGE_TOTAL_BYTES = 35 * 1024 * 1024
_ResultModel = TypeVar("_ResultModel", bound=BaseModel)
_logger = logging.getLogger(__name__)


class DeepSeekModelError(RuntimeError):
    """The official DeepSeek API failed or returned invalid structured output."""


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
        fallback_model: DeadlineAwareMultimodalModel | None = None,
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
        validate_generate_request(inputs, schema_name)

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

            media_url, image_bytes = encode_media_file(
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
