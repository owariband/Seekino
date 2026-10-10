from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from time import monotonic
from typing import Any

from langchain_core.tools import BaseTool
from openai import APIError, OpenAI

from gateway.errors import ContentFilterError
from gateway.agent_model.context import ContextCheckpoint, ContextWindowPolicy
from gateway.agent_model.errors import (
    ModelFailureReason,
    OpenAIFunctionCallingError,
)
from gateway.agent_model.fallback import log_model_fallback
from gateway.agent_model.tool_loop import (
    _failure_reason_for_exception,
    _response_input_tokens,
    _run_function_calling_loop,
    _validated_agent_turn,
)
from gateway.usage import ModelCallTracker, track_model_call
from gateway.media import text_only_messages

from .profiles import OpenAIProfile, ReasoningEffort, completion_options

_logger = logging.getLogger(__name__)

class OpenAIAgentModel:
    """Run a bounded tool loop through an OpenAI-compatible endpoint."""

    supports_context_checkpoints = True
    supports_context_messages = True
    supports_stop_feedback = True

    def __init__(
        self,
        model: str,
        *,
        base_url: str,
        api_key: str,
        max_output_tokens: int,
        request_timeout_seconds: float,
        provider_name: str = "openai",
        profile: OpenAIProfile = "standard",
        reasoning_effort: ReasoningEffort = "none",
        fallback_adapter: Any | None = None,
        context_window_policy: ContextWindowPolicy | None = None,
        client: Any | None = None,
        supports_vision_input: bool = False,
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
        if not provider_name.strip():
            raise ValueError("provider_name must not be empty")
        if profile not in {"standard", "deepseek"}:
            raise ValueError(f"unsupported OpenAI API profile: {profile}")
        if reasoning_effort not in {"none", "low", "high", "max"}:
            raise ValueError("unsupported reasoning_effort")

        self.model_name = model
        self.supports_vision_input = supports_vision_input
        self.primary_model_name = model
        self.provider_name = provider_name
        self.profile = profile
        self.fallback_provider_name = (
            str(fallback_adapter.provider_name)
            if fallback_adapter is not None else None
        )
        self.fallback_model_name = (
            str(fallback_adapter.model_name)
            if fallback_adapter is not None else None
        )
        self.base_url = base_url.rstrip("/")
        self.max_output_tokens = max_output_tokens
        self.reasoning_effort = reasoning_effort
        self.context_window_policy = context_window_policy
        self._api_key = api_key
        self._request_timeout_seconds = request_timeout_seconds
        self._client = client
        self._fallback_adapter = fallback_adapter

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
        with ExitStack() as cleanup:
            client = self._client
            if client is None:
                client = OpenAI(
                    api_key=self._api_key,
                    base_url=self.base_url,
                    timeout=self._request_timeout_seconds,
                    max_retries=0,
                )
                cleanup.callback(client.close)
            fallback_client = (
                self._fallback_adapter.acquire_client(cleanup)
                if self._fallback_adapter is not None else None
            )
            fallback_active = False

            def request_turn(
                messages: list[dict[str, Any]],
                tool_specs: list[dict[str, Any]],
                tool_choice: str,
                timeout: float,
                seen_tool_call_ids: set[str],
                require_visible_text_on_stop: bool,
            ):
                nonlocal fallback_active
                timeout = min(timeout, self._request_timeout_seconds)
                if fallback_active:
                    assert self._fallback_adapter is not None
                    return self._fallback_adapter.request_turn(
                        fallback_client,
                        tools=tools,
                        messages=(messages if getattr(self._fallback_adapter, "supports_vision_input", False) else text_only_messages(messages)),
                        tool_specs=tool_specs,
                        tool_choice=tool_choice,
                        timeout=timeout,
                        seen_tool_call_ids=seen_tool_call_ids,
                        require_visible_text_on_stop=require_visible_text_on_stop,
                    )

                attempt_started_at = (
                    monotonic() if self._fallback_adapter is not None else None
                )
                try:
                    return self._request_turn(
                        client,
                        model_name=self.model_name,
                        tools=tools,
                        messages=(messages if self.supports_vision_input else text_only_messages(messages)),
                        tool_specs=tool_specs,
                        tool_choice=tool_choice,
                        timeout=timeout,
                        seen_tool_call_ids=seen_tool_call_ids,
                        require_visible_text_on_stop=require_visible_text_on_stop,
                    )
                except OpenAIFunctionCallingError as exc:
                    if self._fallback_adapter is None:
                        raise
                    assert attempt_started_at is not None
                    fallback_timeout = timeout - (monotonic() - attempt_started_at)
                    if fallback_timeout <= 0:
                        raise OpenAIFunctionCallingError(
                            f"{self.provider_name} agent loop request deadline expired "
                            "before fallback",
                            reason=ModelFailureReason.MODEL_TIMEOUT,
                        ) from exc
                    log_model_fallback(
                        _logger,
                        primary_provider=self.provider_name,
                        primary_model=self.model_name,
                        fallback_provider=self._fallback_adapter.provider_name,
                        fallback_model=self._fallback_adapter.model_name,
                        error=exc,
                    )
                    fallback_active = True
                    return self._fallback_adapter.request_turn(
                        fallback_client,
                        tools=tools,
                        messages=(messages if getattr(self._fallback_adapter, "supports_vision_input", False) else text_only_messages(messages)),
                        tool_specs=tool_specs,
                        tool_choice=tool_choice,
                        timeout=fallback_timeout,
                        seen_tool_call_ids=seen_tool_call_ids,
                        require_visible_text_on_stop=require_visible_text_on_stop,
                    )

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
                supports_vision_input=self.supports_vision_input,
                provider_label="Agent",
                error_type=OpenAIFunctionCallingError,
                request_turn=request_turn,
            )

    def _request_turn(
        self,
        client: Any,
        *,
        model_name: str,
        tools: Sequence[BaseTool],
        messages: list[dict[str, Any]],
        tool_specs: list[dict[str, Any]],
        tool_choice: str,
        timeout: float,
        seen_tool_call_ids: set[str],
        require_visible_text_on_stop: bool,
    ):
        with track_model_call(
            provider=self.provider_name,
            model=model_name,
            operation=",".join(tool.name for tool in tools),
        ) as call:
            effective_tool_choice = (
                "auto"
                if self.profile == "deepseek" and self.reasoning_effort != "none"
                else tool_choice
            )
            token_parameter = (
                "max_tokens" if self.profile == "deepseek"
                else "max_completion_tokens"
            )
            _logger.info(
                "event=chat_model_request_contract provider=%s model=%s "
                "tool_choice=%s tool_count=%s tool_names=%s "
                "max_output_tokens=%s token_parameter=%s",
                self.provider_name,
                model_name,
                effective_tool_choice,
                len(tools),
                ",".join(tool.name for tool in tools),
                self.max_output_tokens,
                token_parameter,
            )
            response = self._create_completion(
                client,
                call,
                messages=messages,
                tools=tool_specs,
                tool_choice=effective_tool_choice,
                timeout=timeout,
            )
            turn = _validated_agent_turn(
                response,
                tools=tools,
                seen_tool_call_ids=seen_tool_call_ids,
                provider_label=(
                    "DeepSeek" if self.profile == "deepseek"
                    else self.provider_name
                ),
                model_name=model_name,
                max_output_tokens=self.max_output_tokens,
                error_type=OpenAIFunctionCallingError,
                require_tool_call=tool_choice == "required",
                require_visible_text_on_stop=require_visible_text_on_stop,
            )
            return (*turn, _response_input_tokens(response))

    def _create_completion(
        self,
        client: Any,
        usage_call: ModelCallTracker,
        *,
        messages: list[dict[str, Any]],
        timeout: float,
        **tool_options: Any,
    ) -> Any:
        try:
            token_options = (
                {"max_tokens": self.max_output_tokens}
                if self.profile == "deepseek"
                else {"max_completion_tokens": self.max_output_tokens}
            )
            response = client.chat.completions.create(
                model=self.model_name,
                messages=messages,
                stream=False,
                temperature=0,
                timeout=timeout,
                **token_options,
                **completion_options(self.profile, self.reasoning_effort),
                **tool_options,
            )
        except APIError as error:
            usage_call.capture_response(error)
            if getattr(error, "code", None) == "content_filter":
                raise ContentFilterError(
                    f"{self.provider_name} filtered generated output",
                    code="content_filter",
                    response_id=getattr(error, "request_id", None),
                ) from error
            raise OpenAIFunctionCallingError(
                f"{self.provider_name} request failed for model {self.model_name}",
                reason=_failure_reason_for_exception(error),
            ) from error
        usage_call.capture_response(response)
        return response


__all__ = [
    "OpenAIAgentModel",
    "OpenAIFunctionCallingError",
]
