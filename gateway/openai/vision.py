from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any, TypeVar

from langchain_core.tools import BaseTool
from openai import APIConnectionError, APIError, APITimeoutError, OpenAI
from pydantic import BaseModel

from gateway.errors import ContentFilterError
from gateway.agent_model.errors import ModelFailureError, ModelFailureReason
from gateway.agent_model.context import ContextCheckpoint, ContextWindowPolicy
from gateway.media import encode_media_file
from gateway.multimodal import (
    MultimodalInput,
    TextInput,
    VideoInput,
    validate_generate_request,
)
from gateway.usage import track_model_call
from gateway.model_diagnostics import capture_model_diagnostics, record_model_event
from gateway.streaming.chat import request_chat_completion
from gateway.openai.profiles import OpenAIProfile, effective_temperature

_Result = TypeVar("_Result", bound=BaseModel)


class OpenAIVisionModel:
    """Portable image Chat Completions adapter; video is sampled by the Engine."""

    _tool_protocol = "chat"

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
        stream: bool = True,
        temperature: float = 0.0,
        profile: OpenAIProfile = "standard",
    ) -> None:
        self.model_name = model
        self.provider_name = provider
        self._base_url = base_url
        self._api_key = api_key
        self._timeout = request_timeout_seconds
        self._max_tokens = max_output_tokens
        self._token_parameter = token_parameter
        self._format = response_format
        self._stream = stream
        self.temperature = effective_temperature(profile, temperature)

    @property
    def max_output_tokens(self) -> int:
        return self._max_tokens

    @property
    def reasoning_effort(self) -> None:
        return None

    @property
    def _request_timeout_seconds(self) -> float:
        return self._timeout

    def _new_client(self) -> OpenAI:
        return OpenAI(
            base_url=self._base_url, api_key=self._api_key,
            timeout=self._timeout, max_retries=0,
        )

    def generate(
        self,
        inputs: Sequence[MultimodalInput],
        *,
        schema_name: str,
        result_type: type[_Result],
        request_timeout_seconds: float | None = None,
        tools: Sequence[BaseTool] | None = None,
        max_model_calls: int | None = None,
        check_control: Callable[[], None] | None = None,
        context_checkpoint: ContextCheckpoint | None = None,
        context_window_policy: ContextWindowPolicy | None = None,
        max_input_chars: int | None = None,
        fallback_model: Any | None = None,
        fallback_inputs: Sequence[MultimodalInput] | None = None,
        fallback_on_error: Callable[[BaseException], bool] | None = None,
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
        if tools:
            from gateway.agent_model.multimodal import generate_with_tools
            return generate_with_tools(
                self, content, schema_name=schema_name, result_type=result_type,
                tools=tools, max_model_calls=max_model_calls,
                request_timeout_seconds=request_timeout_seconds,
                check_control=check_control, context_checkpoint=context_checkpoint,
                context_window_policy=context_window_policy, max_input_chars=max_input_chars,
                fallback_model=fallback_model, fallback_inputs=fallback_inputs,
                fallback_on_error=fallback_on_error,
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
        request = dict(
            model=self.model_name,
            messages=[{"role": "user", "content": content}],
            response_format=output_format,
            stream=self._stream,
            temperature=self.temperature,
            timeout=timeout,
            **{self._token_parameter: self._max_tokens},
        )
        if self._stream:
            request["stream_options"] = {"include_usage": True}
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
                with capture_model_diagnostics(call, request):
                    unregister_cancel = call.add_cancel_callback(client.close)
                    try:
                        call.raise_if_cancelled()
                        if check_control is not None:
                            check_control()
                        response = (
                            request_chat_completion(
                                client.chat.completions, usage_call=call,
                                check_control=check_control, **request,
                            )
                        )
                        record_model_event("response", response)
                        call.raise_if_cancelled()
                        if check_control is not None:
                            check_control()
                    except APIError as error:
                        call.capture_response(error)
                        call.raise_if_cancelled()
                        if getattr(error, "code", None) == "content_filter":
                            raise ContentFilterError(
                                "Vision output rejected", code="content_filter"
                            ) from error
                        if isinstance(error, APITimeoutError):
                            reason = ModelFailureReason.MODEL_TIMEOUT
                        elif isinstance(error, APIConnectionError):
                            reason = ModelFailureReason.MODEL_CONNECTION_ERROR
                        elif getattr(error, "status_code", None) == 504:
                            reason = ModelFailureReason.MODEL_GATEWAY_TIMEOUT
                        elif getattr(error, "status_code", None) in {429, 500, 502, 503}:
                            reason = ModelFailureReason.MODEL_SERVICE_UNAVAILABLE
                        else:
                            raise
                        raise ModelFailureError(
                            "Vision model request failed", reason=reason,
                        ) from error
                    finally:
                        unregister_cancel()
                    call.capture_response(response)
                    if not response.choices:
                        raise RuntimeError("Vision API returned no choices")
                    choice = response.choices[0]
                    if choice.finish_reason == "content_filter":
                        raise ContentFilterError(
                            "Vision output rejected", code="content_filter"
                        )
                    if choice.finish_reason != "stop":
                        raise ModelFailureError(
                            f"Vision response did not complete: {choice.finish_reason}",
                            reason=(ModelFailureReason.OUTPUT_TOKEN_LIMIT if choice.finish_reason == "length"
                                    else ModelFailureReason.INVALID_MODEL_RESPONSE),
                        )
                    return result_type.model_validate_json(choice.message.content or "")
