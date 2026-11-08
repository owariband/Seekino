from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from time import monotonic
from typing import Any

import httpx
from langchain_core.tools import BaseTool
from openai import APIConnectionError, APIError, DefaultHttpxClient, OpenAI

from gateway.errors import ContentFilterError
from gateway.model_events import emit_model_retry
from gateway.model_diagnostics import record_model_event
from gateway.streaming.chat import request_chat_completion
from gateway.streaming.first_sse import FirstSSEDeadline
from gateway.agent_model.context import ContextCheckpoint, ContextWindowPolicy
from gateway.agent_model.errors import (
    ModelFailureReason,
    ModelFailureError,
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
from gateway.media import require_vision_input

from .profiles import OpenAIProfile, ReasoningEffort, completion_options, effective_temperature

_logger = logging.getLogger(__name__)

CHAT_FIRST_SSE_TIMEOUT_SECONDS = 15.0
CHAT_FIRST_SSE_BACKOFF_SECONDS = (1.0, 2.0, 4.0)
MAX_CHAT_FIRST_SSE_ATTEMPTS = 1 + len(CHAT_FIRST_SSE_BACKOFF_SECONDS)

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
        stream: bool = True,
        temperature: float = 0.0,
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
        if profile not in {"standard", "deepseek", "kimi"}:
            raise ValueError(f"unsupported OpenAI API profile: {profile}")
        if reasoning_effort not in {"none", "low", "high", "max"}:
            raise ValueError("unsupported reasoning_effort")

        self._stream = stream
        self.temperature = effective_temperature(profile, temperature)
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
        image_urls: Sequence[str] = (),
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
                    require_vision_input(
                        messages, supports_vision_input=getattr(self._fallback_adapter, "supports_vision_input", False),
                        model_name=self._fallback_adapter.model_name,
                    )
                    return self._fallback_adapter.request_turn(
                        fallback_client,
                        tools=tools,
                        messages=messages,
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
                    require_vision_input(messages, supports_vision_input=self.supports_vision_input, model_name=self.model_name)
                    return self._request_turn(
                        client,
                        model_name=self.model_name,
                        tools=tools,
                        messages=messages,
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
                    require_vision_input(
                        messages, supports_vision_input=getattr(self._fallback_adapter, "supports_vision_input", False),
                        model_name=self._fallback_adapter.model_name,
                    )
                    return self._fallback_adapter.request_turn(
                        fallback_client,
                        tools=tools,
                        messages=messages,
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
                image_urls=image_urls,
                supports_vision_input=self.supports_vision_input,
                model_name=self.model_name,
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
        if self._stream and self._client is None:
            try:
                return self._create_streaming_completion_with_retry(
                    usage_call, messages=messages, timeout=timeout, **tool_options,
                )
            finally:
                emit_model_retry(usage_call.call_id, 0, MAX_CHAT_FIRST_SSE_ATTEMPTS - 1)
        return self._request_completion(
            client, usage_call, first_sse_deadline=None,
            messages=messages, timeout=timeout, **tool_options,
        )

    def _create_streaming_completion_with_retry(
        self,
        usage_call: ModelCallTracker,
        *,
        messages: list[dict[str, Any]],
        timeout: float,
        **tool_options: Any,
    ) -> Any:
        started = monotonic()
        last_error: BaseException | None = None
        for attempt in range(1, MAX_CHAT_FIRST_SSE_ATTEMPTS + 1):
            remaining = timeout - (monotonic() - started)
            if remaining <= 0:
                break
            first_sse = FirstSSEDeadline(
                remaining, limit_seconds=CHAT_FIRST_SSE_TIMEOUT_SECONDS,
                on_first_event=(lambda: emit_model_retry(usage_call.call_id, 0, MAX_CHAT_FIRST_SSE_ATTEMPTS - 1)) if attempt > 1 else None,
            )
            with OpenAI(
                api_key=self._api_key,
                base_url=self.base_url,
                timeout=remaining,
                max_retries=0,
                http_client=DefaultHttpxClient(
                    http2=False, event_hooks={"request": [first_sse.on_request]},
                ),
            ) as attempt_client:
                try:
                    response = self._request_completion(
                        attempt_client, usage_call, first_sse_deadline=first_sse,
                        messages=messages, timeout=remaining, **tool_options,
                    )
                except OpenAIFunctionCallingError as error:
                    last_error = error
                    retry_reason = self._chat_first_sse_retry_reason(first_sse, error)
                    if retry_reason is None:
                        raise
                    if attempt == MAX_CHAT_FIRST_SSE_ATTEMPTS:
                        break
                    emit_model_retry(usage_call.call_id, attempt, MAX_CHAT_FIRST_SSE_ATTEMPTS - 1)
                    record_model_event("chat_first_sse_retry", {
                        "attempt": attempt,
                        "reason": retry_reason,
                        "elapsed_seconds": monotonic() - started,
                    })
                    _logger.warning(
                        "event=chat_first_sse_retry provider=%s model=%s attempt=%s "
                        "reason=%s elapsed_seconds=%.3f",
                        self.provider_name, self.model_name, attempt, retry_reason,
                        monotonic() - started,
                    )
                    if not self._wait_backoff(
                        CHAT_FIRST_SSE_BACKOFF_SECONDS[attempt - 1],
                        usage_call, timeout=timeout, started=started,
                    ):
                        break
                    continue
                first_event_ms = first_sse.first_event_ms()
                _logger.info(
                    "event=chat_first_sse provider=%s model=%s attempt=%s first_event_ms=%s",
                    self.provider_name, self.model_name, attempt,
                    "%.1f" % first_event_ms if first_event_ms is not None else "None",
                )
                return response
        raise OpenAIFunctionCallingError(
            f"{self.provider_name} request failed for model {self.model_name}",
            reason=ModelFailureReason.MODEL_NETWORK_ERROR,
        ) from last_error

    def _chat_first_sse_retry_reason(
        self, deadline: FirstSSEDeadline, error: OpenAIFunctionCallingError,
    ) -> str | None:
        if deadline.received_event():
            return None
        if deadline.expired():
            return "first_sse_timeout"
        if error.reason == ModelFailureReason.MODEL_CONNECTION_ERROR:
            return "connection_error"
        if self._is_connection_failure(error):
            return "connection_error"
        return None

    def _is_connection_failure(self, error: BaseException) -> bool:
        current: BaseException | None = error
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            if isinstance(current, (httpx.RequestError, httpx.StreamError)) and not isinstance(
                current, httpx.TimeoutException,
            ):
                return True
            if isinstance(current, APIConnectionError):
                return True
            current = current.__cause__ or current.__context__
        return False

    def _wait_backoff(
        self,
        backoff: float,
        usage_call: ModelCallTracker,
        *,
        timeout: float,
        started: float,
    ) -> bool:
        """Sleep the backoff within the total budget; False if no budget remains."""
        woke = threading.Event()
        unregister = usage_call.add_cancel_callback(woke.set)
        try:
            remaining = timeout - (monotonic() - started)
            if remaining <= 0:
                return False
            woke.wait(min(backoff, remaining))
        finally:
            unregister()
        usage_call.raise_if_cancelled()
        return timeout - (monotonic() - started) > 0

    def _request_completion(
        self,
        client: Any,
        usage_call: ModelCallTracker,
        *,
        first_sse_deadline: FirstSSEDeadline | None,
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
            profile_options = completion_options(self.profile, self.reasoning_effort, temperature=self.temperature)
            response = request_chat_completion(
                client.chat.completions, usage_call=usage_call,
                model=self.model_name,
                messages=messages,
                stream=self._stream,
                timeout=timeout,
                first_sse_deadline=first_sse_deadline,
                **token_options,
                **profile_options,
                **tool_options,
            )
        except (APIError, ModelFailureError) as error:
            usage_call.capture_response(error, include_usage=False)
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
