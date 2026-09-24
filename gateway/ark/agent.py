from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from time import monotonic
from typing import Any

from arkruntime import Ark
from arkruntime._exceptions import ArkError
from langchain_core.tools import BaseTool
from openai import OpenAIError

from gateway.constants import ARK_PROVIDER
from gateway.constants import ARK_OPENAI_BASE_URL
from gateway.errors import ContentFilterError
from gateway.agent_model.context import ContextCheckpoint, ContextWindowPolicy
from gateway.agent_model.errors import ArkFunctionCallingError, ModelFailureReason
from gateway.agent_model.fallback import log_model_fallback
from gateway.agent_model.tool_loop import (
    _failure_reason_for_exception,
    _response_input_tokens,
    _run_function_calling_loop,
    _validated_agent_turn,
)
from gateway.openai.structured import OpenAIStructuredToolAgentModel
from gateway.usage import track_model_call

_logger = logging.getLogger(__name__)


def new_ark_client(
    *,
    request_timeout_seconds: float,
    api_key: str | None = None,
) -> Any:
    """Create one Ark client owned by a single Agent invocation."""

    options: dict[str, Any] = {
        "timeout": request_timeout_seconds,
        "max_retries": 0,
    }
    if api_key is not None:
        options["api_key"] = api_key
    return Ark.volc(**options)

def request_ark_function_turn(
    client: Any,
    *,
    model_name: str | None,
    max_output_tokens: int,
    tools: Sequence[BaseTool],
    messages: list[dict[str, Any]],
    tool_specs: list[dict[str, Any]],
    tool_choice: str,
    timeout: float,
    seen_tool_call_ids: set[str],
    require_visible_text_on_stop: bool,
):
    if client is None or model_name is None:
        raise ArkFunctionCallingError("Ark fallback is not configured")
    with track_model_call(
        provider=ARK_PROVIDER,
        model=model_name,
        operation=",".join(tool.name for tool in tools),
    ) as call:
        try:
            _logger.info(
                "event=chat_model_request_contract provider=%s model=%s "
                "tool_choice=%s tool_count=%s tool_names=%s "
                "max_output_tokens=%s token_parameter=max_completion_tokens",
                ARK_PROVIDER,
                model_name,
                tool_choice,
                len(tools),
                ",".join(tool.name for tool in tools),
                max_output_tokens,
            )
            response = client.chat.completions.create(
                model=model_name,
                messages=[
                    {
                        key: value for key, value in message.items()
                        if key != "reasoning_content"
                    }
                    for message in messages
                ],
                stream=False,
                temperature=0,
                max_completion_tokens=max_output_tokens,
                timeout=timeout,
                tools=tool_specs,
                tool_choice=tool_choice,
            )
        except Exception as exc:
            call.capture_response(exc)
            if isinstance(exc, ContentFilterError) or getattr(exc, "code", None) in {
                "content_filter", "OutputTextSensitiveContentDetected",
            }:
                raise ContentFilterError(
                    "Ark filtered generated output",
                    code="content_filter",
                    response_id=getattr(exc, "request_id", None),
                ) from exc
            if not isinstance(exc, (ArkError, OpenAIError, TimeoutError)):
                raise
            raise ArkFunctionCallingError(
                f"Ark request failed for model {model_name!r}",
                reason=_failure_reason_for_exception(exc),
            ) from exc
        call.capture_response(response)
        turn = _validated_agent_turn(
            response,
            tools=tools,
            seen_tool_call_ids=seen_tool_call_ids,
            provider_label="Ark",
            model_name=model_name,
            max_output_tokens=max_output_tokens,
            error_type=ArkFunctionCallingError,
            require_tool_call=tool_choice == "required",
            require_visible_text_on_stop=require_visible_text_on_stop,
        )
        return (*turn, _response_input_tokens(response))


class ArkFunctionCallingModel:
    """Run the final bounded tool loop on Doubao Pro with per-call Lite fallback."""

    provider_name = ARK_PROVIDER
    supports_context_checkpoints = True
    supports_context_messages = True
    supports_stop_feedback = True

    def __init__(
        self,
        model: str,
        *,
        fallback_model: str | None,
        max_output_tokens: int,
        request_timeout_seconds: float,
        context_window_policy: ContextWindowPolicy | None = None,
        client: Any | None = None,
        api_key: str | None = None,
    ) -> None:
        if not model.strip():
            raise ValueError("model must not be empty")
        if fallback_model is not None and not fallback_model.strip():
            raise ValueError("fallback_model must not be empty")
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be greater than zero")
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be greater than zero")
        self.model_name = model
        self.primary_model_name = model
        self.fallback_provider_name = (
            ARK_PROVIDER
            if fallback_model is not None and fallback_model != model
            else None
        )
        self.fallback_model_name = (
            fallback_model if fallback_model != model else None
        )
        self.max_output_tokens = max_output_tokens
        self.context_window_policy = context_window_policy
        self._request_timeout_seconds = request_timeout_seconds
        self._client = client
        self._api_key = api_key

    def run_agent_loop(
        self,
        prompt: str,
        *,
        tools: Sequence[BaseTool],
        max_model_calls: int | None,
        handle_text: Callable[[str], None],
        require_tool_call: bool = False,
        stop_feedback: Callable[[], str | None] | None = None,
        request_timeout_seconds: float | None = None,
        check_control: Callable[[], None] | None = None,
        max_input_chars: int | None = None,
        context_checkpoint: ContextCheckpoint | None = None,
        initial_messages: Sequence[dict[str, Any]] | None = None,
    ) -> None:
        effective_timeout = (
            self._request_timeout_seconds
            if request_timeout_seconds is None else request_timeout_seconds
        )
        client = self._client or new_ark_client(
            request_timeout_seconds=self._request_timeout_seconds,
            api_key=self._api_key,
        )
        active_model = self.model_name
        try:
            def request_turn(
                messages: list[dict[str, Any]],
                tool_specs: list[dict[str, Any]],
                tool_choice: str,
                timeout: float,
                seen_tool_call_ids: set[str],
                require_visible_text_on_stop: bool,
            ):
                nonlocal active_model
                timeout = min(timeout, self._request_timeout_seconds)

                def invoke(model_name: str, request_timeout: float):
                    return request_ark_function_turn(
                        client,
                        model_name=model_name,
                        max_output_tokens=self.max_output_tokens,
                        tools=tools,
                        messages=messages,
                        tool_specs=tool_specs,
                        tool_choice=tool_choice,
                        timeout=request_timeout,
                        seen_tool_call_ids=seen_tool_call_ids,
                        require_visible_text_on_stop=require_visible_text_on_stop,
                    )

                try:
                    attempt_started_at = monotonic()
                    return invoke(active_model, timeout)
                except ArkFunctionCallingError as exc:
                    if active_model != self.model_name or self.fallback_model_name is None:
                        raise
                    fallback_timeout = timeout - (monotonic() - attempt_started_at)
                    if fallback_timeout <= 0:
                        raise ArkFunctionCallingError(
                            "Ark agent loop request deadline expired before fallback",
                            reason=ModelFailureReason.MODEL_TIMEOUT,
                        ) from exc
                    log_model_fallback(
                        _logger,
                        primary_provider=ARK_PROVIDER,
                        primary_model=self.model_name,
                        fallback_provider=ARK_PROVIDER,
                        fallback_model=self.fallback_model_name,
                        error=exc,
                    )
                    active_model = self.fallback_model_name
                    return invoke(active_model, fallback_timeout)

            _run_function_calling_loop(
                prompt=prompt,
                tools=tools,
                max_model_calls=max_model_calls,
                handle_text=handle_text,
                require_tool_call=require_tool_call,
                stop_feedback=stop_feedback,
                request_timeout_seconds=effective_timeout,
                check_control=check_control,
                max_input_chars=max_input_chars,
                context_window_policy=self.context_window_policy,
                context_checkpoint=context_checkpoint,
                initial_messages=initial_messages,
                provider_label="Ark",
                error_type=ArkFunctionCallingError,
                request_turn=request_turn,
            )
        finally:
            if self._client is None:
                client.close()




class ArkFunctionCallingFallback:
    """Adapt Ark turn requests for an OpenAI-compatible primary loop."""

    provider_name = ARK_PROVIDER

    def __init__(
        self,
        model: str,
        *,
        max_output_tokens: int,
        request_timeout_seconds: float,
        client: Any | None = None,
        api_key: str | None = None,
    ) -> None:
        if not model.strip():
            raise ValueError("model must not be empty")
        self.model_name = model
        self.max_output_tokens = max_output_tokens
        self.request_timeout_seconds = request_timeout_seconds
        self._client = client
        self._api_key = api_key

    def acquire_client(self, cleanup: ExitStack) -> Any:
        client = self._client
        if client is None:
            client = new_ark_client(
                request_timeout_seconds=self.request_timeout_seconds,
                api_key=self._api_key,
            )
            cleanup.callback(client.close)
        return client

    def request_turn(
        self,
        client: Any,
        *,
        tools: Sequence[BaseTool],
        messages: list[dict[str, Any]],
        tool_specs: list[dict[str, Any]],
        tool_choice: str,
        timeout: float,
        seen_tool_call_ids: set[str],
        require_visible_text_on_stop: bool,
    ) -> tuple:
        return request_ark_function_turn(
            client,
            model_name=self.model_name,
            max_output_tokens=self.max_output_tokens,
            tools=tools,
            messages=messages,
            tool_specs=tool_specs,
            tool_choice=tool_choice,
            timeout=min(timeout, self.request_timeout_seconds),
            seen_tool_call_ids=seen_tool_call_ids,
            require_visible_text_on_stop=require_visible_text_on_stop,
        )


class ArkStructuredFallback:
    """Build an isolated Ark-backed ChatModel for structured cross fallback."""

    provider_name = ARK_PROVIDER

    def __init__(
        self,
        model: str,
        *,
        max_output_tokens: int,
        request_timeout_seconds: float,
        client: Any | None = None,
        api_key: str | None = None,
    ) -> None:
        self.model_name = model
        self.max_output_tokens = max_output_tokens
        self.request_timeout_seconds = request_timeout_seconds
        self._client = client
        self._api_key = api_key

    def build_chat_model(self, cleanup: ExitStack) -> Any:
        from gateway.openai.structured import build_openai_chat_model

        client = self._client
        if client is None:
            client = new_ark_client(
                request_timeout_seconds=self.request_timeout_seconds,
                api_key=self._api_key,
            )
            cleanup.callback(client.close)
        return build_openai_chat_model(
            provider_name=ARK_PROVIDER,
            api_profile="standard",
            model_name=self.model_name,
            client=client,
            base_url=ARK_OPENAI_BASE_URL,
            max_output_tokens=self.max_output_tokens,
            request_timeout_seconds=self.request_timeout_seconds,
            fallback_model_name=None,
            provider_error_types=(ArkError, OpenAIError),
        )


class ArkStructuredToolAgentModel(OpenAIStructuredToolAgentModel):
    """Construct the shared structured loop with an Ark-owned client."""

    def __init__(
        self,
        model: str,
        *,
        request_timeout_seconds: float,
        max_output_tokens: int,
        max_input_chars: int | None = None,
        context_window_policy: ContextWindowPolicy | None = None,
        fallback_model: str | None = None,
        client: Any | None = None,
        api_key: str | None = None,
        chat_model: Any | None = None,
    ) -> None:
        super().__init__(
            model,
            request_timeout_seconds=request_timeout_seconds,
            max_output_tokens=max_output_tokens,
            max_input_chars=max_input_chars,
            context_window_policy=context_window_policy,
            fallback_model=fallback_model,
            provider_name=ARK_PROVIDER,
            api_profile="standard",
            base_url=ARK_OPENAI_BASE_URL,
            client=client,
            client_factory=(
                None
                if client is not None
                else lambda: new_ark_client(
                    request_timeout_seconds=request_timeout_seconds,
                    api_key=api_key,
                )
            ),
            provider_error_types=(ArkError, OpenAIError),
            chat_model=chat_model,
        )

__all__ = [
    "ArkFunctionCallingError",
    "ArkFunctionCallingFallback",
    "ArkFunctionCallingModel",
    "ArkStructuredFallback",
    "ArkStructuredToolAgentModel",
    "new_ark_client",
    "request_ark_function_turn",
]
