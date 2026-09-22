from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from copy import deepcopy
from time import monotonic
from typing import Any, Literal, Protocol, TypeVar

from arkruntime import Ark
from arkruntime._exceptions import ArkError
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, ModelCallLimitMiddleware
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    RemoveMessage,
    convert_to_messages,
    convert_to_openai_messages,
)
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from openai import APIError, OpenAI, OpenAIError
from pydantic import BaseModel, PrivateAttr, ValidationError

from .constants import ARK_OPENAI_BASE_URL, ARK_PROVIDER, DEEPSEEK_PROVIDER
from .context_window import (
    ContextCheckpoint,
    ContextWindowController,
    ContextWindowOverflow,
    ContextWindowPolicy,
    ContextWindowUpdate,
    measure_context,
)
from .model_utils import deepseek_non_thinking_extra_body, log_model_fallback
from .schema_utils import inline_schema_refs
from .sse_proxy import ContentFilterError
from .usage import ModelCallTracker, track_model_call

_ResultModel = TypeVar("_ResultModel", bound=BaseModel)
_logger = logging.getLogger(__name__)


class ModelInputTooLargeError(RuntimeError):
    """The complete model message state exceeded an application context budget."""


class QueryModelIncompleteError(RuntimeError):
    """A Query completion returned an invalid or non-terminal response."""


class MissingStructuredQueryToolCallError(RuntimeError):
    """A Query completion stopped without any required tool call."""


class MissingStructuredQueryDraftError(RuntimeError):
    """The Query loop ended without the required structured draft."""


class StructuredToolAgentModel(Protocol):
    provider_name: str
    model_name: str

    def run_structured(
        self,
        prompt: str,
        *,
        tools: Sequence[BaseTool],
        result_type: type[_ResultModel],
        max_model_calls: int | None,
        request_timeout_seconds: float | None = None,
        check_control: Callable[[], None] | None = None,
        max_input_chars: int | None = None,
        context_checkpoint: ContextCheckpoint | None = None,
        initial_messages: Sequence[dict[str, Any]] | None = None,
    ) -> _ResultModel: ...


# Kept while callers migrate to the role-neutral protocol.
QueryAgentModel = StructuredToolAgentModel


class AgentLoopModel(Protocol):
    provider_name: str
    model_name: str

    def run_agent_loop(
        self,
        prompt: str,
        *,
        tools: Sequence[BaseTool],
        max_model_calls: int | None,
        handle_text: Callable[[str], None],
        require_tool_call: bool = False,
        request_timeout_seconds: float | None = None,
        check_control: Callable[[], None] | None = None,
        max_input_chars: int | None = None,
        context_checkpoint: ContextCheckpoint | None = None,
        initial_messages: Sequence[dict[str, Any]] | None = None,
    ) -> None: ...


class DeepSeekFunctionCallingError(RuntimeError):
    """DeepSeek failed or returned an invalid agent-loop response."""


class ArkFunctionCallingError(RuntimeError):
    """Ark failed or returned an invalid agent-loop response."""


class DeepSeekFunctionCallingModel:
    """Run a DeepSeek tool loop with an optional in-place Ark fallback."""

    provider_name = DEEPSEEK_PROVIDER
    supports_context_checkpoints = True
    supports_context_messages = True

    def __init__(
        self,
        model: str,
        *,
        base_url: str,
        api_key: str,
        max_output_tokens: int,
        request_timeout_seconds: float,
        reasoning_effort: Literal["none", "low", "high", "max"] = "none",
        fallback_model: str | None = None,
        context_window_policy: ContextWindowPolicy | None = None,
        client: Any | None = None,
        fallback_client: Any | None = None,
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
        if fallback_model is not None and not fallback_model.strip():
            raise ValueError("fallback_model must not be empty")
        if reasoning_effort not in {"none", "low", "high", "max"}:
            raise ValueError("unsupported reasoning_effort")

        self.model_name = model
        self.primary_model_name = model
        self.fallback_provider_name = (
            ARK_PROVIDER if fallback_model is not None else None
        )
        self.fallback_model_name = fallback_model
        self.base_url = base_url.rstrip("/")
        self.max_output_tokens = max_output_tokens
        self.reasoning_effort = reasoning_effort
        self.context_window_policy = context_window_policy
        self._api_key = api_key
        self._request_timeout_seconds = request_timeout_seconds
        self._client = client
        self._fallback_client = fallback_client

    def run_agent_loop(
        self,
        prompt: str,
        *,
        tools: Sequence[BaseTool],
        max_model_calls: int | None,
        handle_text: Callable[[str], None],
        require_tool_call: bool = False,
        request_timeout_seconds: float | None = None,
        check_control: Callable[[], None] | None = None,
        max_input_chars: int | None = None,
        context_checkpoint: ContextCheckpoint | None = None,
        initial_messages: Sequence[dict[str, Any]] | None = None,
    ) -> None:
        effective_timeout = min(
            self._request_timeout_seconds,
            request_timeout_seconds or self._request_timeout_seconds,
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
            fallback_client = self._fallback_client
            if fallback_client is None and self.fallback_model_name is not None:
                fallback_client = Ark.volc(
                    timeout=self._request_timeout_seconds,
                    max_retries=0,
                )
                cleanup.callback(fallback_client.close)
            active_provider = DEEPSEEK_PROVIDER

            def request_turn(
                messages: list[dict[str, Any]],
                tool_specs: list[dict[str, Any]],
                tool_choice: str,
                timeout: float,
                seen_tool_call_ids: set[str],
                require_visible_text_on_stop: bool,
            ):
                nonlocal active_provider
                if active_provider == ARK_PROVIDER:
                    return _request_ark_function_turn(
                        fallback_client,
                        model_name=self.fallback_model_name,
                        max_output_tokens=self.max_output_tokens,
                        tools=tools,
                        messages=messages,
                        tool_specs=tool_specs,
                        tool_choice=tool_choice,
                        timeout=timeout,
                        seen_tool_call_ids=seen_tool_call_ids,
                        require_visible_text_on_stop=require_visible_text_on_stop,
                    )

                attempt_started_at = (
                    monotonic() if self.fallback_model_name is not None else None
                )
                try:
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
                except (DeepSeekFunctionCallingError, ContentFilterError) as exc:
                    if self.fallback_model_name is None:
                        raise
                    assert attempt_started_at is not None
                    fallback_timeout = timeout - (monotonic() - attempt_started_at)
                    if fallback_timeout <= 0:
                        raise DeepSeekFunctionCallingError(
                            "DeepSeek agent loop request deadline expired before fallback"
                        ) from exc
                    log_model_fallback(
                        _logger,
                        primary_provider=DEEPSEEK_PROVIDER,
                        primary_model=self.model_name,
                        fallback_provider=ARK_PROVIDER,
                        fallback_model=self.fallback_model_name,
                        error=exc,
                    )
                    active_provider = ARK_PROVIDER
                    return _request_ark_function_turn(
                        fallback_client,
                        model_name=self.fallback_model_name,
                        max_output_tokens=self.max_output_tokens,
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
                request_timeout_seconds=effective_timeout,
                check_control=check_control,
                max_input_chars=max_input_chars,
                context_window_policy=self.context_window_policy,
                context_checkpoint=context_checkpoint,
                initial_messages=initial_messages,
                provider_label="Agent",
                error_type=DeepSeekFunctionCallingError,
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
                "auto" if self.reasoning_effort != "none" else tool_choice
            )
            _logger.info(
                "event=chat_model_request_contract provider=%s model=%s "
                "tool_choice=%s tool_count=%s tool_names=%s",
                self.provider_name,
                model_name,
                effective_tool_choice,
                len(tools),
                ",".join(tool.name for tool in tools),
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
                provider_label="DeepSeek",
                error_type=DeepSeekFunctionCallingError,
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
            thinking_enabled = self.reasoning_effort != "none"
            response = client.chat.completions.create(
                model=self.model_name,
                messages=messages,
                stream=False,
                temperature=0,
                max_tokens=self.max_output_tokens,
                timeout=timeout,
                extra_body=(
                    {"thinking": {"type": "enabled"}}
                    if thinking_enabled else deepseek_non_thinking_extra_body()
                ),
                **(
                    {"reasoning_effort": self.reasoning_effort}
                    if thinking_enabled else {}
                ),
                **tool_options,
            )
        except APIError as error:
            usage_call.capture_response(error)
            if getattr(error, "code", None) == "content_filter":
                raise ContentFilterError(
                    "DeepSeek filtered generated output",
                    code="content_filter",
                    response_id=getattr(error, "request_id", None),
                ) from error
            raise DeepSeekFunctionCallingError(
                f"DeepSeek request failed for model {self.model_name}"
            ) from error
        usage_call.capture_response(response)
        return response


def _response_input_tokens(response: Any) -> int | None:
    usage = (
        response.get("usage")
        if isinstance(response, Mapping)
        else getattr(response, "usage", None)
    )
    if usage is None:
        return None
    value = (
        usage.get("input_tokens", usage.get("prompt_tokens"))
        if isinstance(usage, Mapping)
        else getattr(
            usage,
            "input_tokens",
            getattr(usage, "prompt_tokens", None),
        )
    )
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _request_ark_function_turn(
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
                "tool_choice=%s tool_count=%s tool_names=%s",
                ARK_PROVIDER,
                model_name,
                tool_choice,
                len(tools),
                ",".join(tool.name for tool in tools),
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
            raise ArkFunctionCallingError(
                f"Ark request failed for model {model_name!r}"
            ) from exc
        call.capture_response(response)
        turn = _validated_agent_turn(
            response,
            tools=tools,
            seen_tool_call_ids=seen_tool_call_ids,
            provider_label="Ark",
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

    def __init__(
        self,
        model: str,
        *,
        fallback_model: str | None,
        max_output_tokens: int,
        request_timeout_seconds: float,
        context_window_policy: ContextWindowPolicy | None = None,
        client: Any | None = None,
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

    def run_agent_loop(
        self,
        prompt: str,
        *,
        tools: Sequence[BaseTool],
        max_model_calls: int | None,
        handle_text: Callable[[str], None],
        require_tool_call: bool = False,
        request_timeout_seconds: float | None = None,
        check_control: Callable[[], None] | None = None,
        max_input_chars: int | None = None,
        context_checkpoint: ContextCheckpoint | None = None,
        initial_messages: Sequence[dict[str, Any]] | None = None,
    ) -> None:
        effective_timeout = min(
            self._request_timeout_seconds,
            request_timeout_seconds or self._request_timeout_seconds,
        )
        client = self._client or Ark.volc(
            timeout=self._request_timeout_seconds,
            max_retries=0,
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

                def invoke(model_name: str, request_timeout: float):
                    return _request_ark_function_turn(
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
                except (ArkFunctionCallingError, ContentFilterError) as exc:
                    if active_model != self.model_name or self.fallback_model_name is None:
                        raise
                    fallback_timeout = timeout - (monotonic() - attempt_started_at)
                    if fallback_timeout <= 0:
                        raise ArkFunctionCallingError(
                            "Ark agent loop request deadline expired before fallback"
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


def _function_tool_spec(tool: BaseTool) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": inline_schema_refs(
                tool.get_input_schema().model_json_schema()
            ),
        },
    }


def _tool_output_text(output: object) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, BaseModel):
        output = output.model_dump(mode="json")
    return json.dumps(
        output,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def _copy_assistant_reasoning(
    messages: Sequence[Any], rendered: list[dict[str, Any]],
) -> None:
    for source, target in zip(messages, rendered):
        if isinstance(source, AIMessage) and target.get("role") == "assistant":
            reasoning = source.additional_kwargs.get("reasoning_content")
            if isinstance(reasoning, str):
                target["reasoning_content"] = reasoning


def _log_context_window_update(
    role: str,
    update: ContextWindowUpdate,
) -> None:
    _logger.info(
        "event=agent_context_window_compacted role=%s reason=%s "
        "window_number=%s window_id=%s previous_window_id=%s "
        "characters_before=%s characters_after=%s "
        "estimated_tokens_before=%s estimated_tokens_after=%s "
        "cleared_tool_results=%s",
        role,
        update.reason,
        update.window_number,
        update.window_id,
        update.previous_window_id,
        update.before.characters,
        update.after.characters,
        update.before.estimated_tokens,
        update.after.estimated_tokens,
        update.cleared_tool_results,
    )


def _run_function_calling_loop(
    *,
    prompt: str,
    tools: Sequence[BaseTool],
    max_model_calls: int | None,
    handle_text: Callable[[str], None],
    require_tool_call: bool,
    request_timeout_seconds: float,
    check_control: Callable[[], None] | None,
    max_input_chars: int | None,
    context_window_policy: ContextWindowPolicy | None,
    context_checkpoint: ContextCheckpoint | None,
    initial_messages: Sequence[dict[str, Any]] | None,
    provider_label: str,
    error_type: type[RuntimeError],
    request_turn: Callable[
        ...,
        tuple[str, str | None, dict[str, Any], list, int | None],
    ],
) -> None:
    if not prompt.strip():
        raise ValueError("prompt must not be empty")
    if not tools:
        raise ValueError("tools must not be empty")
    tool_names = [tool.name for tool in tools]
    if any(not name.strip() for name in tool_names):
        raise ValueError("tool names must not be empty")
    if len(tool_names) != len(set(tool_names)):
        raise ValueError("tool names must be unique")
    if any(not tool.description.strip() for tool in tools):
        raise ValueError("tool descriptions must not be empty")
    if max_model_calls is not None and (
        isinstance(max_model_calls, bool)
        or not isinstance(max_model_calls, int)
        or max_model_calls <= 0
    ):
        raise ValueError("max_model_calls must be a positive integer")
    if request_timeout_seconds <= 0:
        raise ValueError("request_timeout_seconds must be greater than zero")
    if max_input_chars is not None and (
        isinstance(max_input_chars, bool)
        or not isinstance(max_input_chars, int)
        or max_input_chars <= 0
    ):
        raise ValueError("max_input_chars must be positive when provided")

    deadline = monotonic() + request_timeout_seconds
    if check_control is not None:
        check_control()
    messages: list[dict[str, Any]] = (
        deepcopy(list(initial_messages))
        if initial_messages is not None
        else [{"role": "user", "content": prompt}]
    )
    if not messages:
        raise ValueError("initial_messages must not be empty when provided")
    tool_specs = [_function_tool_spec(tool) for tool in tools]
    context_window = ContextWindowController(
        policy=context_window_policy,
        maximum_input_characters=max_input_chars,
        label=f"{provider_label} agent-loop",
    )
    seen_tool_call_ids: set[str] = set()
    emitted_text = False
    submitted_tool = False

    model_call_index = 0
    while max_model_calls is None or model_call_index < max_model_calls:
        if model_call_index > 0 and check_control is not None:
            check_control()
        remaining_timeout = deadline - monotonic()
        if remaining_timeout <= 0:
            raise error_type(f"{provider_label} agent loop request deadline expired")
        try:
            context_update = context_window.prepare(
                messages,
                contract=tool_specs,
                checkpoint=context_checkpoint,
            )
        except ContextWindowOverflow as exc:
            raise ModelInputTooLargeError(str(exc)) from exc
        if context_update.compacted:
            messages = context_update.messages
            _log_context_window_update(provider_label, context_update)
        tool_choice = "required" if require_tool_call and not submitted_tool else "auto"
        (
            finish_reason,
            content,
            assistant_message,
            validated_calls,
            provider_input_tokens,
        ) = request_turn(
            messages,
            tool_specs,
            tool_choice,
            remaining_timeout,
            seen_tool_call_ids,
            not emitted_text,
        )
        model_call_index += 1
        context_window.record_provider_input_tokens(
            provider_input_tokens,
            request_estimated_tokens=measure_context(
                messages,
                tool_specs,
            ).estimated_tokens,
        )
        if check_control is not None:
            check_control()
        visible_content = content if isinstance(content, str) and content.strip() else None
        if visible_content is not None:
            handle_text(visible_content)
            emitted_text = True

        if finish_reason == "stop":
            if not emitted_text:
                raise error_type(
                    f"{provider_label} agent loop completed without visible text"
                )
            return

        messages.append(assistant_message)
        for tool_call_id, tool, arguments_or_error in validated_calls:
            if check_control is not None:
                check_control()
            if tool is None:
                output = arguments_or_error
            else:
                assert isinstance(arguments_or_error, dict)
                output = tool.invoke(arguments_or_error)
                submitted_tool = True
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": _tool_output_text(output),
            })
    raise error_type(
        f"{provider_label} agent loop exhausted max_model_calls before stop"
    )


def _validated_agent_turn(
    response: Any,
    *,
    tools: Sequence[BaseTool],
    seen_tool_call_ids: set[str],
    provider_label: str = "DeepSeek",
    error_type: type[RuntimeError] = DeepSeekFunctionCallingError,
    require_tool_call: bool = False,
    require_visible_text_on_stop: bool = False,
) -> tuple[
    str,
    str | None,
    dict[str, Any],
    list[tuple[str, BaseTool | None, dict[str, Any] | str]],
]:
    tools_by_name = {tool.name: tool for tool in tools}
    choice = _single_completion_choice(
        response, provider_label=provider_label, error_type=error_type,
    )
    finish_reason = getattr(choice, "finish_reason", None)
    if finish_reason == "content_filter":
        raise ContentFilterError(
            f"{provider_label} filtered agent-loop output",
            code="content_filter",
            response_id=getattr(response, "id", None),
        )
    if finish_reason not in {"stop", "tool_calls"}:
        raise error_type(
            f"{provider_label} agent-loop turn did not complete: "
            f"{finish_reason!r}"
        )

    message = getattr(choice, "message", None)
    if getattr(message, "role", None) != "assistant":
        raise error_type(
            f"{provider_label} returned an invalid assistant message"
        )
    content = getattr(message, "content", None)
    if content is not None and not isinstance(content, str):
        raise error_type(
            f"{provider_label} assistant content must be text or null"
        )
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls is None:
        tool_calls = []
    if not isinstance(tool_calls, list):
        raise error_type(
            f"{provider_label} returned invalid tool calls"
        )
    if finish_reason == "stop":
        if tool_calls:
            raise error_type(
                f"{provider_label} stop response must not contain tool calls"
            )
        if require_tool_call:
            raise error_type(
                f"{provider_label} stopped before the required function call"
            )
        if require_visible_text_on_stop and not (
            isinstance(content, str) and content.strip()
        ):
            raise error_type(
                f"{provider_label} stopped without visible text"
            )
        return (
            finish_reason,
            content,
            {"role": "assistant", "content": content},
            [],
        )
    if not tool_calls:
        raise error_type(
            f"{provider_label} tool_calls response contained no tool calls"
        )

    reasoning_content = getattr(message, "reasoning_content", None)
    new_tool_call_ids: set[str] = set()
    rendered_calls: list[dict[str, Any]] = []
    validated_calls: list[tuple[str, BaseTool | None, dict[str, Any] | str]] = []
    for tool_call in tool_calls:
        tool_call_id = getattr(tool_call, "id", None)
        if (
            not isinstance(tool_call_id, str)
            or not tool_call_id.strip()
            or tool_call_id in seen_tool_call_ids
            or tool_call_id in new_tool_call_ids
        ):
            raise error_type(
                f"{provider_label} function calls must have unique nonempty IDs"
            )
        new_tool_call_ids.add(tool_call_id)

        function = getattr(tool_call, "function", None)
        function_name = getattr(function, "name", None)
        if (
            getattr(tool_call, "type", None) != "function"
            or not isinstance(function_name, str)
        ):
            raise error_type(
                f"{provider_label} called an unexpected function"
            )
        arguments = getattr(function, "arguments", None)
        if not isinstance(arguments, str):
            raise error_type(
                f"{provider_label} returned invalid function arguments"
            )
        rendered_calls.append({
            "id": tool_call_id,
            "type": "function",
            "function": {
                "name": function_name,
                "arguments": arguments,
            },
        })
        tool = tools_by_name.get(function_name)
        if tool is None:
            available = ", ".join(tools_by_name)[:400]
            validated_calls.append((
                tool_call_id,
                None,
                f"Unknown tool {function_name[:80]!r}. Use one of: {available}.",
            ))
            continue
        if not arguments.strip():
            validated_calls.append((
                tool_call_id,
                None,
                f"Invalid arguments for {function_name}: provide a JSON object "
                "matching the tool schema.",
            ))
            continue
        try:
            result = tool.get_input_schema().model_validate_json(arguments)
        except ValidationError as error:
            details = "; ".join(
                f"{'.'.join(map(str, issue['loc'])) or 'arguments'}: {issue['msg']}"
                for issue in error.errors(include_input=False, include_url=False)[:3]
            )[:400]
            validated_calls.append((
                tool_call_id,
                None,
                f"Invalid arguments for {function_name}: {details}. "
                "Fix them and call the tool again.",
            ))
            continue
        validated_calls.append((
            tool_call_id,
            tool,
            result.model_dump(mode="python"),
        ))

    seen_tool_call_ids.update(new_tool_call_ids)
    assistant_message = {
        "role": "assistant", "content": content, "tool_calls": rendered_calls,
    }
    if provider_label == "DeepSeek" and isinstance(reasoning_content, str):
        assistant_message["reasoning_content"] = reasoning_content
    return finish_reason, content, assistant_message, validated_calls


def _single_completion_choice(
    response: Any,
    *,
    provider_label: str = "DeepSeek",
    error_type: type[RuntimeError] = DeepSeekFunctionCallingError,
) -> Any:
    choices = getattr(response, "choices", None)
    if not isinstance(choices, list) or len(choices) != 1:
        raise error_type(
            f"{provider_label} returned an invalid chat completion envelope"
        )
    return choices[0]


class _AgentChatOpenAI(ChatOpenAI):
    """Validate Query tool responses and keep fallback at the model-call boundary."""

    provider_name: str = ARK_PROVIDER
    request_timeout_seconds: float
    use_legacy_token_parameter: bool = False
    fallback_model_name: str | None = None
    fallback_provider_name: str | None = None
    _fallback_active: bool = PrivateAttr(default=False)
    _fallback_chat_model: BaseChatModel | None = PrivateAttr(default=None)

    def set_cross_provider_fallback(self, fallback: BaseChatModel) -> None:
        self._fallback_chat_model = fallback
        self.fallback_provider_name = str(
            getattr(fallback, "provider_name", "unknown")
        )
        self.fallback_model_name = str(getattr(fallback, "model_name", "unknown"))

    def _generate(self, *args: Any, **kwargs: Any):
        requested_timeout = kwargs.get("timeout")
        request_timeout = self.request_timeout_seconds
        if (
            not isinstance(requested_timeout, bool)
            and isinstance(requested_timeout, (int, float))
            and requested_timeout > 0
        ):
            request_timeout = min(request_timeout, float(requested_timeout))
        request_kwargs = dict(kwargs)
        request_kwargs["timeout"] = request_timeout

        if self._fallback_active and self._fallback_chat_model is not None:
            return self._fallback_chat_model._generate(*args, **request_kwargs)
        attempt_started_at = (
            monotonic() if self.fallback_model_name is not None else None
        )
        try:
            return super()._generate(*args, **request_kwargs)
        except (
            OpenAIError,
            ArkError,
            QueryModelIncompleteError,
            MissingStructuredQueryToolCallError,
        ) as exc:
            if self._fallback_active or self.fallback_model_name is None:
                raise
            assert attempt_started_at is not None
            fallback_timeout = request_timeout - (
                monotonic() - attempt_started_at
            )
            if fallback_timeout <= 0:
                raise
            primary_model = self.model_name
            self._fallback_active = True
            log_model_fallback(
                _logger,
                primary_provider=self.provider_name,
                primary_model=primary_model,
                fallback_provider=(
                    self.fallback_provider_name or self.provider_name
                ),
                fallback_model=self.fallback_model_name,
                error=exc,
            )
            fallback_kwargs = dict(kwargs)
            fallback_kwargs["timeout"] = fallback_timeout
            if self._fallback_chat_model is not None:
                return self._fallback_chat_model._generate(
                    *args, **fallback_kwargs
                )
            self.model_name = self.fallback_model_name
            return super()._generate(*args, **fallback_kwargs)

    def _get_request_payload(self, input_: Any, *, stop: Any = None, **kwargs: Any):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        if self.use_legacy_token_parameter and "max_completion_tokens" in payload:
            payload["max_tokens"] = payload.pop("max_completion_tokens")
        if (
            self.provider_name == DEEPSEEK_PROVIDER
            and self.reasoning_effort in {"low", "high", "max"}
        ):
            if payload.get("tool_choice") not in (None, "auto", "none"):
                payload["tool_choice"] = "auto"
            _copy_assistant_reasoning(
                self._convert_input(input_).to_messages(), payload["messages"],
            )
        tools = payload.get("tools")
        tool_names = [
            str(tool.get("function", {}).get("name", ""))
            for tool in tools
            if isinstance(tool, dict)
        ] if isinstance(tools, list) else []
        _logger.info(
            "event=query_model_request_contract provider=%s model=%s "
            "tool_choice=%s tool_count=%s tool_names=%s",
            self.provider_name,
            self.model_name,
            payload.get("tool_choice"),
            len(tool_names),
            ",".join(tool_names),
        )
        return payload

    def _create_chat_result(self, response: Any, generation_info: Any = None):
        if isinstance(response, BaseModel):
            response = response.model_dump()
        for choice in response.get("choices", []):
            reason = choice.get("finish_reason")
            if reason not in {"stop", "tool_calls"}:
                raise QueryModelIncompleteError(
                    f"Query model did not complete: {reason}"
                )
        result = super()._create_chat_result(response, generation_info)
        if (
            self.provider_name == DEEPSEEK_PROVIDER
            and self.reasoning_effort in {"low", "high", "max"}
        ):
            for choice, generation in zip(
                response.get("choices", []), result.generations,
            ):
                reasoning = choice.get("message", {}).get("reasoning_content")
                if isinstance(reasoning, str):
                    generation.message.additional_kwargs["reasoning_content"] = reasoning
        valid_tool_call_count = sum(
            len(getattr(generation.message, "tool_calls", None) or [])
            for generation in result.generations
        )
        if valid_tool_call_count == 0:
            invalid_tool_call_count = sum(
                len(getattr(generation.message, "invalid_tool_calls", None) or [])
                for generation in result.generations
            )
            _logger.warning(
                "event=query_model_invalid_tool_response model=%s "
                "valid_tool_call_count=0 invalid_tool_call_count=%s",
                self.model_name,
                invalid_tool_call_count,
            )
            raise MissingStructuredQueryToolCallError(
                "Query model returned no valid retrieval or structured-draft tool call"
            )
        return result


class _ExecutionControlMiddleware(AgentMiddleware):
    def __init__(
        self,
        check: Callable[[], None],
        remaining_seconds: Callable[[], float],
    ) -> None:
        super().__init__()
        self._check = check
        self._remaining_seconds = remaining_seconds

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        self._check()
        settings = dict(request.model_settings or {})
        timeout = self._remaining_seconds()
        configured = settings.get("timeout")
        if (
            not isinstance(configured, bool)
            and isinstance(configured, (int, float))
            and configured > 0
        ):
            timeout = min(timeout, float(configured))
        try:
            return handler(request.override(model_settings={**settings, "timeout": timeout}))
        finally:
            self._check()

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        self._check()
        try:
            return handler(request)
        finally:
            self._check()


class _ContextWindowMiddleware(AgentMiddleware):
    """Apply the same replacement-window policy to LangChain Agent state."""

    def __init__(
        self,
        *,
        policy: ContextWindowPolicy | None,
        maximum_input_characters: int | None,
        contract: object,
        checkpoint: ContextCheckpoint | None,
        label: str,
    ) -> None:
        super().__init__()
        self._controller = ContextWindowController(
            policy=policy,
            maximum_input_characters=maximum_input_characters,
            label=label,
        )
        self._contract = contract
        self._checkpoint = checkpoint
        self._label = label
        self._last_request_estimated_tokens: int | None = None

    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        messages = state.get("messages", [])
        rendered = convert_to_openai_messages(messages)
        payload = [rendered] if isinstance(rendered, dict) else rendered
        _copy_assistant_reasoning(messages, payload)
        try:
            update = self._controller.prepare(
                payload,
                contract=self._contract,
                checkpoint=self._checkpoint,
            )
        except ContextWindowOverflow as exc:
            raise ModelInputTooLargeError(str(exc)) from exc
        self._last_request_estimated_tokens = measure_context(
            update.messages,
            self._contract,
        ).estimated_tokens
        if not update.compacted:
            return None
        _log_context_window_update(self._label, update)
        replacement = convert_to_messages(update.messages)
        return {
            "messages": [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                *replacement,
            ]
        }

    def after_model(self, state: Any, runtime: Any) -> None:
        if self._last_request_estimated_tokens is None:
            return
        for message in reversed(state.get("messages", [])):
            if not isinstance(message, AIMessage):
                continue
            usage = message.usage_metadata
            if not isinstance(usage, Mapping):
                return
            self._controller.record_provider_input_tokens(
                usage.get("input_tokens"),
                request_estimated_tokens=self._last_request_estimated_tokens,
            )
            return


def _new_query_client(
    provider: str,
    *,
    base_url: str | None,
    api_key: str | None,
    request_timeout_seconds: float,
) -> Any:
    if provider == ARK_PROVIDER:
        # Ark documents its client as thread unsafe: own one per Query invocation.
        return Ark.volc(timeout=request_timeout_seconds, max_retries=0)
    if not base_url or not api_key:
        raise ValueError(f"{provider} Query provider requires base_url and api_key")
    return OpenAI(
        base_url=base_url,
        api_key=api_key,
        timeout=request_timeout_seconds,
        max_retries=0,
    )


def _build_query_chat_model(
    *,
    provider: str,
    model_name: str,
    client: Any,
    base_url: str | None,
    max_output_tokens: int,
    request_timeout_seconds: float,
    fallback_model_name: str | None,
    reasoning_effort: Literal["none", "low", "high", "max"] = "none",
) -> _AgentChatOpenAI:
    options: dict[str, Any] = {}
    if provider == DEEPSEEK_PROVIDER:
        options["extra_body"] = (
            {"thinking": {"type": "enabled"}}
            if reasoning_effort != "none" else deepseek_non_thinking_extra_body()
        )
        if reasoning_effort != "none":
            options["reasoning_effort"] = reasoning_effort
    return _AgentChatOpenAI(
        model=model_name,
        provider_name=provider,
        client=client.chat.completions,
        async_client=client.chat.completions,
        api_key="injected-sdk-auth",
        base_url=base_url or ARK_OPENAI_BASE_URL,
        max_completion_tokens=max_output_tokens,
        request_timeout_seconds=request_timeout_seconds,
        fallback_model_name=fallback_model_name,
        fallback_provider_name=(provider if fallback_model_name else None),
        use_legacy_token_parameter=provider != ARK_PROVIDER,
        temperature=0,
        max_retries=0,
        timeout=request_timeout_seconds,
        use_responses_api=False,
        streaming=False,
        **options,
    )


class SDKStructuredToolAgentModel:
    """Run one isolated LangChain Tool loop ending in structured output."""

    supports_context_checkpoints = True
    supports_context_messages = True

    def __init__(
        self,
        model: str,
        *,
        request_timeout_seconds: float,
        max_output_tokens: int,
        max_input_chars: int | None = None,
        context_window_policy: ContextWindowPolicy | None = None,
        fallback_model: str | None = None,
        fallback_provider: str | None = None,
        provider: str = "ark",
        base_url: str | None = None,
        api_key: str | None = None,
        reasoning_effort: Literal["none", "low", "high", "max"] = "none",
        client: Any | None = None,
        fallback_client: Any | None = None,
        chat_model: BaseChatModel | None = None,
    ) -> None:
        if not model.strip():
            raise ValueError("model must not be empty")
        if request_timeout_seconds <= 0 or max_output_tokens <= 0:
            raise ValueError("model timeout and max_output_tokens must be positive")
        if max_input_chars is not None and (
            isinstance(max_input_chars, bool)
            or not isinstance(max_input_chars, int)
            or max_input_chars <= 0
        ):
            raise ValueError("max_input_chars must be positive when provided")
        if fallback_model is not None and not fallback_model.strip():
            raise ValueError("fallback_model must not be empty")
        if provider not in {"ark", "deepseek", "ollama", "llama_cpp"}:
            raise ValueError(f"unsupported Query provider: {provider}")
        if fallback_provider is not None and fallback_provider not in {
            "ark", "deepseek", "ollama", "llama_cpp",
        }:
            raise ValueError(f"unsupported Query fallback provider: {fallback_provider}")
        if fallback_provider is not None and fallback_model is None:
            raise ValueError("fallback_provider requires fallback_model")
        if reasoning_effort not in {"none", "low", "high", "max"}:
            raise ValueError("unsupported reasoning_effort")
        if (
            fallback_provider is not None
            and fallback_provider not in {provider, ARK_PROVIDER}
        ):
            raise ValueError(
                "cross-provider Query fallback currently supports Ark only"
            )
        self.model_name = model
        self.primary_model_name = model
        self.provider_name = provider
        self.request_timeout_seconds = request_timeout_seconds
        self.max_output_tokens = max_output_tokens
        self.max_input_chars = max_input_chars
        self.context_window_policy = context_window_policy
        self.fallback_model_name = (
            fallback_model if fallback_model != model else None
        )
        self.fallback_provider_name = (
            (fallback_provider or provider)
            if self.fallback_model_name is not None
            else None
        )
        self.base_url = base_url
        self.api_key = api_key
        self.reasoning_effort = reasoning_effort
        self._client = client
        self._fallback_client = fallback_client
        self._chat_model = chat_model

    def run_structured(
        self,
        prompt: str,
        *,
        tools: Sequence[BaseTool],
        result_type: type[_ResultModel],
        max_model_calls: int | None,
        request_timeout_seconds: float | None = None,
        check_control: Callable[[], None] | None = None,
        max_input_chars: int | None = None,
        context_checkpoint: ContextCheckpoint | None = None,
        initial_messages: Sequence[dict[str, Any]] | None = None,
    ) -> _ResultModel:
        if not prompt.strip():
            raise ValueError("Agent prompt must not be empty")
        if max_model_calls is not None and (
            isinstance(max_model_calls, bool)
            or not isinstance(max_model_calls, int)
            or max_model_calls <= 0
        ):
            raise ValueError("max_model_calls must be a positive integer")
        effective_timeout = (
            self.request_timeout_seconds
            if request_timeout_seconds is None else request_timeout_seconds
        )
        if effective_timeout <= 0:
            raise ValueError("request_timeout_seconds must be greater than zero")
        if initial_messages is not None and not initial_messages:
            raise ValueError("initial_messages must not be empty when provided")
        if max_input_chars is not None and (
            isinstance(max_input_chars, bool)
            or not isinstance(max_input_chars, int)
            or max_input_chars <= 0
        ):
            raise ValueError("max_input_chars must be positive when provided")
        deadline = monotonic() + effective_timeout

        def check_run() -> None:
            if check_control is not None:
                check_control()
            if monotonic() >= deadline:
                raise TimeoutError("Structured Agent execution deadline expired")

        def remaining_seconds() -> float:
            check_run()
            return deadline - monotonic()

        check_run()
        with ExitStack() as cleanup:
            if self._chat_model is not None:
                model = self._chat_model
            else:
                client = self._client
                if client is None:
                    client = _new_query_client(
                        self.provider_name,
                        base_url=self.base_url,
                        api_key=self.api_key,
                        request_timeout_seconds=self.request_timeout_seconds,
                    )
                    cleanup.callback(client.close)
                model = _build_query_chat_model(
                    provider=self.provider_name,
                    model_name=self.model_name,
                    client=client,
                    base_url=self.base_url,
                    max_output_tokens=self.max_output_tokens,
                    request_timeout_seconds=self.request_timeout_seconds,
                    fallback_model_name=(
                        self.fallback_model_name
                        if self.fallback_provider_name == self.provider_name
                        else None
                    ),
                    reasoning_effort=self.reasoning_effort,
                )
                if (
                    self.fallback_model_name is not None
                    and self.fallback_provider_name != self.provider_name
                ):
                    fallback_client = self._fallback_client
                    if fallback_client is None:
                        fallback_client = _new_query_client(
                            self.fallback_provider_name or ARK_PROVIDER,
                            base_url=None,
                            api_key=None,
                            request_timeout_seconds=self.request_timeout_seconds,
                        )
                        cleanup.callback(fallback_client.close)
                    fallback = _build_query_chat_model(
                        provider=self.fallback_provider_name or ARK_PROVIDER,
                        model_name=self.fallback_model_name,
                        client=fallback_client,
                        base_url=None,
                        max_output_tokens=self.max_output_tokens,
                        request_timeout_seconds=self.request_timeout_seconds,
                        fallback_model_name=None,
                    )
                    model.set_cross_provider_fallback(fallback)
            middleware = []
            if max_model_calls is not None:
                middleware.append(ModelCallLimitMiddleware(
                    run_limit=max_model_calls,
                    exit_behavior="error",
                ))
            middleware.append(_ExecutionControlMiddleware(check_run, remaining_seconds))
            effective_input_chars = (
                min(self.max_input_chars, max_input_chars)
                if self.max_input_chars is not None and max_input_chars is not None
                else self.max_input_chars or max_input_chars
            )
            if (
                effective_input_chars is not None
                or self.context_window_policy is not None
            ):
                middleware.append(_ContextWindowMiddleware(
                    policy=self.context_window_policy,
                    maximum_input_characters=effective_input_chars,
                    contract=_model_contract(tools, result_type),
                    checkpoint=context_checkpoint,
                    label=f"{self.provider_name}:{self.model_name}",
                ))
            agent = create_agent(
                model,
                tools=list(tools),
                response_format=ToolStrategy(result_type, handle_errors=False),
                middleware=middleware,
            )
            state = agent.invoke(
                {
                    "messages": (
                        deepcopy(list(initial_messages))
                        if initial_messages is not None
                        else [{"role": "user", "content": prompt}]
                    )
                },
                config={
                    "recursion_limit": (
                        max_model_calls * 6 + 10
                        if max_model_calls is not None else sys.maxsize
                    ),
                    "max_concurrency": 1,
                },
            )
            check_run()
            result = state.get("structured_response")
            if not isinstance(result, result_type):
                raise MissingStructuredQueryDraftError(
                    "Agent ended without its required structured result"
                )
            return result

    def run_query(
        self,
        prompt: str,
        *,
        tools: Sequence[BaseTool],
        result_type: type[_ResultModel],
        max_model_calls: int | None,
        request_timeout_seconds: float | None = None,
        check_control: Callable[[], None] | None = None,
        max_input_chars: int | None = None,
        context_checkpoint: ContextCheckpoint | None = None,
        initial_messages: Sequence[dict[str, Any]] | None = None,
    ) -> _ResultModel:
        """Compatibility entry point for callers not yet migrated."""

        return self.run_structured(
            prompt,
            tools=tools,
            result_type=result_type,
            max_model_calls=max_model_calls,
            request_timeout_seconds=request_timeout_seconds,
            check_control=check_control,
            max_input_chars=max_input_chars,
            context_checkpoint=context_checkpoint,
            initial_messages=initial_messages,
        )


SDKQueryAgentModel = SDKStructuredToolAgentModel


def _model_contract(
    tools: Sequence[BaseTool],
    result_type: type[BaseModel],
) -> dict[str, Any]:
    return {
        "tools": [{
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.get_input_schema().model_json_schema(),
        } for tool in tools],
        "structured_output": result_type.model_json_schema(),
    }
