from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
import json
import logging
from time import monotonic
from types import SimpleNamespace
from typing import Any, Generic, TypeVar
from uuid import uuid4

from arkruntime._exceptions import ArkError
from langchain_core.tools import BaseTool
from openai import APIError
from pydantic import BaseModel

from gateway.streaming.responses import request_response
from gateway.constants import ARK_PROVIDER, DEEPSEEK_PROVIDER
from gateway.errors import ContentFilterError, SSEProxyError
from gateway.media import encode_media_file
from gateway.multimodal import ImageInput, MultimodalInput, TextInput, VideoInput
from gateway.model_diagnostics import capture_model_diagnostics, record_model_event
from gateway.streaming.chat import request_chat_completion
from gateway.usage import ModelCallControlError, model_call_context, track_model_call

from .context import ContextCheckpoint, ContextWindowOverflow, ContextWindowPolicy, preflight_context
from .errors import ModelFailureReason, ModelInputTooLargeError, QueryModelIncompleteError
from .fallback import log_model_fallback
from .tool_loop import (
    _failure_reason_for_exception, _response_input_tokens,
    _run_function_calling_loop, _validated_agent_turn,
)

_Result = TypeVar("_Result", bound=BaseModel)
_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _Endpoint:
    adapter: Any
    client: Any
    model: str
    content: list[dict[str, Any]]
    protocol: str = "responses"
    media_message_id: str | None = None

    @property
    def provider(self) -> str:
        return self.adapter.provider_name


@dataclass(slots=True)
class ToolModelSession(Generic[_Result]):
    content: list[dict[str, Any]]
    timeout: float
    check_run: Callable[[], None]
    request_turn: Callable[..., Any]
    result: _Result | None = None


def generate_with_tools(
    adapter: Any, content: list[dict[str, Any]], *,
    schema_name: str, result_type: type[_Result], tools: Sequence[BaseTool],
    max_model_calls: int | None,
    request_timeout_seconds: float | None,
    check_control: Callable[[], None] | None,
    context_checkpoint: ContextCheckpoint | None,
    context_window_policy: ContextWindowPolicy | None,
    max_input_chars: int | None,
    fallback_model: Any | None,
    fallback_inputs: Sequence[MultimodalInput] | None,
    fallback_on_error: Callable[[BaseException], bool] | None,
) -> _Result:
    """Adapt Responses turns to the existing function loop and usage control."""
    with tool_model_session(
        adapter, content, schema_name=schema_name, result_type=result_type, tools=tools,
        request_timeout_seconds=request_timeout_seconds, check_control=check_control,
        context_window_policy=context_window_policy, max_input_chars=max_input_chars,
        fallback_model=fallback_model, fallback_inputs=fallback_inputs,
        fallback_on_error=fallback_on_error,
    ) as session:
        _run_function_calling_loop(
            prompt="Complete the requested structured analysis using the available tools.",
            tools=tools, max_model_calls=max_model_calls, handle_text=lambda text: None,
            require_tool_call=True, stop_feedback=None, request_timeout_seconds=session.timeout,
            check_control=session.check_run, max_input_chars=max_input_chars,
            context_window_policy=context_window_policy, context_checkpoint=context_checkpoint,
            initial_messages=[{"role": "user", "content": session.content}], supports_vision_input=True,
            provider_label=adapter.provider_name, error_type=QueryModelIncompleteError,
            request_turn=session.request_turn,
        )
        if session.result is None:
            raise QueryModelIncompleteError("Multimodal tool loop returned no structured result")
        return session.result


@contextmanager
def tool_model_session(
    adapter: Any, content: list[dict[str, Any]], *,
    schema_name: str, result_type: type[_Result], tools: Sequence[BaseTool],
    request_timeout_seconds: float | None,
    check_control: Callable[[], None] | None,
    context_window_policy: ContextWindowPolicy | None,
    max_input_chars: int | None,
    fallback_model: Any | None,
    fallback_inputs: Sequence[MultimodalInput] | None,
    fallback_on_error: Callable[[BaseException], bool] | None,
    enforce_final_schema: bool = True,
    media_message_id: str | None = None,
) -> Iterator[ToolModelSession[_Result]]:
    """Share provider requests and controls across structured tool harnesses."""
    timeout = request_timeout_seconds if request_timeout_seconds is not None else adapter._request_timeout_seconds
    if timeout is None:
        raise ValueError("multimodal tools require request_timeout_seconds")
    if request_timeout_seconds is not None and request_timeout_seconds <= 0:
        raise ValueError("request_timeout_seconds must be greater than zero")
    if adapter._request_timeout_seconds is not None:
        timeout = min(timeout, adapter._request_timeout_seconds)
    control_failure: BaseException | None = None

    def check_run() -> None:
        nonlocal control_failure
        if check_control is not None:
            try:
                check_control()
            except BaseException as error:
                control_failure = error
                raise

    check_run()
    with ExitStack() as cleanup:
        cleanup.enter_context(model_call_context({"agent_run_id": uuid4().hex}))
        round_no = 0
        schema = result_type.model_json_schema()
        primary = _endpoint(
            adapter, content, cleanup, schema,
            enforce_final_schema=enforce_final_schema, media_message_id=media_message_id,
        )
        fallback_adapter = fallback_model or getattr(adapter, "_fallback_model", None)
        if fallback_adapter is not None:
            fallback_content = (
                _encode_inputs(fallback_inputs, fallback_adapter.provider_name)
                if fallback_inputs is not None else deepcopy(content)
            )
            if fallback_adapter.provider_name != ARK_PROVIDER and any(
                item.get("type") == "input_video" for item in fallback_content
            ):
                raise ValueError("video fallback requires ordered ImageInput values")
            fallback = _endpoint(
                fallback_adapter, fallback_content, cleanup, schema,
                enforce_final_schema=enforce_final_schema, media_message_id=media_message_id,
            )
        else:
            fallback_name = getattr(adapter, "fallback_model_name", None)
            fallback = (
                replace(primary, model=fallback_name)
                if fallback_name is not None and primary.provider == ARK_PROVIDER
                else None
            )
        active = primary
        if context_window_policy is not None:
            budgets = [endpoint.adapter.max_output_tokens for endpoint in (primary, fallback) if endpoint is not None]
            reserve = max((budget for budget in budgets if budget is not None), default=0)
            if context_window_policy.output_reserve_tokens < reserve:
                raise ValueError("context output reserve must cover primary and fallback output budgets")

        def request_turn(messages, specs, choice, remaining, seen, visible, *, bound_tools=None):
            nonlocal active, round_no
            round_no += 1
            check_run()
            started = monotonic()
            request_tools = tools if bound_tools is None else bound_tools
            choice = "required" if choice == "any" else choice
            contract = {"tools": specs}
            if enforce_final_schema:
                contract["output_schema"] = schema
            if round_no == 1:
                try:
                    preflight_context(
                        messages, contract, policy=context_window_policy,
                        maximum_input_characters=max_input_chars, label="Multimodal initial request",
                    )
                except ContextWindowOverflow as overflow:
                    raise ModelInputTooLargeError(str(overflow)) from overflow
            try:
                turn, parsed, input_tokens = _request_turn(
                    active, messages, specs, choice, remaining, seen, visible,
                    tools=request_tools, schema_name=schema_name, result_type=result_type,
                    check_control=check_run,
                    round_no=round_no, enforce_final_schema=enforce_final_schema,
                )
            except (ArkError, APIError, TimeoutError, SSEProxyError, QueryModelIncompleteError) as error:
                if control_failure is not None and (error is control_failure or error.__cause__ is control_failure):
                    raise control_failure
                if isinstance(error, ModelCallControlError):
                    raise
                if active is not primary or fallback is None:
                    raise
                if fallback_on_error is not None and not fallback_on_error(error):
                    raise
                check_run()
                remaining -= monotonic() - started
                if remaining <= 0:
                    raise QueryModelIncompleteError(
                        "Multimodal deadline expired before fallback",
                        reason=ModelFailureReason.MODEL_TIMEOUT,
                    ) from error
                log_model_fallback(
                    _logger, primary_provider=primary.provider, primary_model=primary.model,
                    fallback_provider=fallback.provider, fallback_model=fallback.model,
                    error=error,
                )
                active = fallback
                session.content = fallback.content
                _media_message(messages, fallback)["content"] = deepcopy(fallback.content)
                try:
                    preflight_context(
                        messages, contract, policy=context_window_policy,
                        maximum_input_characters=max_input_chars, label="Multimodal fallback",
                    )
                except ContextWindowOverflow as overflow:
                    raise ModelInputTooLargeError(str(overflow)) from overflow
                turn, parsed, input_tokens = _request_turn(
                    active, messages, specs, choice, remaining, seen, visible,
                    tools=request_tools, schema_name=schema_name, result_type=result_type,
                    check_control=check_run,
                    round_no=round_no, enforce_final_schema=enforce_final_schema,
                )
            if parsed is not None:
                session.result = parsed
            return (*turn, input_tokens)

        session: ToolModelSession[_Result] = ToolModelSession(primary.content, timeout, check_run, request_turn)
        yield session


def _endpoint(
    adapter: Any, content: list[dict[str, Any]], cleanup: ExitStack, schema: dict, *,
    enforce_final_schema: bool = True, media_message_id: str | None = None,
) -> _Endpoint:
    protocol = getattr(adapter, "_tool_protocol", "responses")
    if protocol != "chat" and adapter.provider_name not in {ARK_PROVIDER, DEEPSEEK_PROVIDER}:
        raise TypeError("multimodal tools require a supported Responses or Vision adapter")
    client = getattr(adapter, "_client", None)
    if client is None:
        client = adapter._new_client()
        cleanup.callback(client.close)
    if protocol == "chat":
        content = _chat_content(content)
        if enforce_final_schema and adapter._format == "json_object":
            content.append({"type": "text", "text": "Return only JSON matching this schema: " + json.dumps(schema, ensure_ascii=False)})
    return _Endpoint(adapter, client, adapter.model_name, content, protocol, media_message_id)


def _log_multimodal_request(endpoint, call, request, *, schema_name, result_type):
    if not _logger.isEnabledFor(logging.INFO):
        return
    inputs = request.get("input", request.get("messages", []))
    blocks = [block for item in inputs if isinstance(item.get("content"), list)
              for block in item["content"] if isinstance(block, Mapping)]
    output_format = request.get("text", {}).get("format", request.get("response_format", {}))
    summary = {
        "provider": endpoint.provider[:128], "model": endpoint.model[:256],
        "schema_name": schema_name[:128], "result_type": result_type.__name__[:128],
        "response_format_type": output_format.get("type"),
        "response_format_strict": output_format.get("json_schema", output_format).get("strict"),
        "call_id": call.call_id, "protocol": endpoint.protocol,
        "tool_choice": str(request["tool_choice"])[:128],
        "tool_count": len(request["tools"]),
        "tool_names": [str(_field(tool.get("function", tool), "name", ""))[:128]
                       for tool in request["tools"][:32]],
        "max_output_tokens": endpoint.adapter.max_output_tokens,
        "token_parameter": (endpoint.adapter._token_parameter
                            if endpoint.protocol == "chat" else "max_output_tokens"),
        "thinking": _field(request.get("thinking"), "type"),
        "reasoning_effort": _field(request.get("reasoning"), "effort"),
        "timeout": request["timeout"], "input_item_count": len(inputs),
        "content_block_count": len(blocks),
        "text_chars": sum(len(item[key]) for item in inputs
                          for key in ("content", "output", "arguments")
                          if isinstance(item.get(key), str))
                      + sum(len(block["text"]) for block in blocks
                            if isinstance(block.get("text"), str)),
        "media_item_count": sum(block.get("type") in {"input_image", "input_video", "image_url"}
                                for block in blocks),
        **{key: str(call.context[key])[:256] for key in (
            "run_id", "video_parse_task_id", "chunk_parse_task_id",
            "interval_parse_task_id", "video_chunk_id",
        ) if isinstance(call.context.get(key), (str, int))},
        **{key: call.context[key] for key in ("sequence_no", "lease_version")
           if type(call.context.get(key)) is int},
    }
    _logger.info("event=multimodal_model_request summary=%s",
                 json.dumps(summary, ensure_ascii=True, separators=(",", ":")))


def _request_turn(
    endpoint: _Endpoint, messages, specs, choice, remaining, seen, visible, *,
    tools: Sequence[BaseTool], schema_name: str, result_type: type[_Result],
    check_control: Callable[[], None] | None,
    round_no: int = 1,
    enforce_final_schema: bool = True,
):
    if endpoint.protocol == "chat":
        return _request_chat_turn(
            endpoint, messages, specs, choice, remaining, seen, visible,
            tools=tools, schema_name=schema_name, result_type=result_type,
            check_control=check_control,
            round_no=round_no, enforce_final_schema=enforce_final_schema,
        )
    adapter = endpoint.adapter
    timeout = min(remaining, adapter._request_timeout_seconds or remaining)
    request = {
        "model": endpoint.model, "input": _response_input(messages, endpoint),
        "tools": [{"type": "function", **spec["function"]} for spec in specs],
        "tool_choice": choice, "timeout": timeout,
        "temperature": adapter.temperature,
    }
    if enforce_final_schema:
        schema = result_type.model_json_schema()
        if endpoint.provider == DEEPSEEK_PROVIDER:
            from gateway.openai.multimodal import _deepseek_response_schema
            from gateway.schema_normalization import inline_schema_refs
            schema = _deepseek_response_schema(inline_schema_refs(schema))
        request["text"] = {"format": {
            "type": "json_schema", "name": schema_name, "schema": schema, "strict": True,
        }}
    if adapter.max_output_tokens is not None:
        request["max_output_tokens"] = adapter.max_output_tokens
    if endpoint.provider == ARK_PROVIDER and adapter.reasoning_effort == "none":
        request["thinking"] = {"type": "disabled"}
    elif adapter.reasoning_effort is not None:
        request["reasoning"] = {"effort": adapter.reasoning_effort}
    with model_call_context({"agent_round": round_no}), track_model_call(
        provider=endpoint.provider, model=endpoint.model, operation=schema_name,
    ) as call, capture_model_diagnostics(call, {**request, "stream": adapter._stream}):
        close = getattr(endpoint.client, "close", None)
        unregister = call.add_cancel_callback(close) if callable(close) else lambda: None
        try:
            call.raise_if_cancelled()
            if check_control is not None:
                check_control()
            _log_multimodal_request(endpoint, call, request, schema_name=schema_name, result_type=result_type)
            try:
                response, text = request_response(
                    endpoint.client.responses, stream=adapter._stream, usage_call=call,
                    check_control=check_control, **request,
                )
                record_model_event("response", response)
            except Exception as error:
                call.capture_response(error, include_usage=False)
                if getattr(error, "code", None) in {"content_filter", "OutputTextSensitiveContentDetected"} and not isinstance(error, ContentFilterError):
                    raise ContentFilterError(
                        "Responses filtered multimodal output", code="content_filter",
                        response_id=getattr(error, "request_id", None),
                    ) from error
                if isinstance(error, (ArkError, APIError, TimeoutError, SSEProxyError)) and not isinstance(error, ContentFilterError):
                    raise QueryModelIncompleteError(
                        "Multimodal Responses request failed",
                        reason=(ModelFailureReason.OUTPUT_TOKEN_LIMIT
                                if getattr(error, "code", None) == "max_output_tokens"
                                else _failure_reason_for_exception(error)),
                    ) from error
                raise
            call.raise_if_cancelled()
            if check_control is not None:
                check_control()
            calls = [item for item in _field(response, "output", []) or []
                     if _field(item, "type") == "function_call"]
            if any(_field(item, "status") not in {None, "completed"} for item in calls):
                raise QueryModelIncompleteError("Multimodal function call did not complete")
            status = _field(response, "status", "completed")
            reason = _field(_field(response, "incomplete_details"), "reason")
            if status not in {None, "completed"}:
                if reason == "content_filter" or _field(_field(response, "error"), "code") in {"content_filter", "OutputTextSensitiveContentDetected"}:
                    raise ContentFilterError("Responses filtered multimodal output", code="content_filter")
                raise QueryModelIncompleteError(
                    f"Multimodal Responses turn did not complete: {status!r}",
                    reason=(ModelFailureReason.OUTPUT_TOKEN_LIMIT if reason == "max_output_tokens"
                            else ModelFailureReason.INVALID_MODEL_RESPONSE),
                )
            completion = SimpleNamespace(
                id=_field(response, "id"), usage=_field(response, "usage"),
                choices=[SimpleNamespace(
                    finish_reason="tool_calls" if calls else "stop",
                    message=SimpleNamespace(
                        role="assistant", content=text or None,
                        tool_calls=[SimpleNamespace(
                            id=_field(item, "call_id"), type="function",
                            function=SimpleNamespace(name=_field(item, "name"), arguments=_field(item, "arguments")),
                        ) for item in calls],
                    ),
                )],
            )
            turn = _validated_agent_turn(
                completion, tools=tools, seen_tool_call_ids=seen,
                provider_label=endpoint.provider, model_name=endpoint.model,
                max_output_tokens=adapter.max_output_tokens, error_type=QueryModelIncompleteError,
                require_tool_call=enforce_final_schema and choice == "required",
                require_visible_text_on_stop=enforce_final_schema and visible,
            )
            parsed = None
            if enforce_final_schema and turn[0] == "stop":
                try:
                    parsed = result_type.model_validate_json(text)
                except ValueError as error:
                    raise QueryModelIncompleteError("Multimodal final output does not match its schema") from error
            turn[2]["responses_output"] = [_plain(item) for item in _field(response, "output", []) or []]
            turn[2]["responses_provider"] = (endpoint.provider, endpoint.model)
            return turn, parsed, _response_input_tokens(response)
        finally:
            unregister()


def _request_chat_turn(
    endpoint: _Endpoint, messages, specs, choice, remaining, seen, visible, *,
    tools: Sequence[BaseTool], schema_name: str, result_type: type[_Result],
    check_control: Callable[[], None] | None,
    round_no: int = 1,
    enforce_final_schema: bool = True,
):
    adapter = endpoint.adapter
    request = {
        "model": endpoint.model, "messages": _chat_input(messages, endpoint),
        "tools": specs, "tool_choice": choice,
        "stream": adapter._stream, "timeout": min(remaining, adapter._request_timeout_seconds),
        "temperature": adapter.temperature,
        adapter._token_parameter: adapter.max_output_tokens,
    }
    if adapter._stream:
        request["stream_options"] = {"include_usage": True}
    if enforce_final_schema:
        request["response_format"] = ({"type": "json_schema", "json_schema": {
            "name": schema_name, "strict": True, "schema": result_type.model_json_schema(),
        }} if adapter._format == "json_schema" else {"type": "json_object"})
    with model_call_context({"agent_round": round_no}), track_model_call(
        provider=endpoint.provider, model=endpoint.model, operation=schema_name,
    ) as call, capture_model_diagnostics(call, request):
        unregister = call.add_cancel_callback(endpoint.client.close)
        try:
            call.raise_if_cancelled()
            if check_control is not None:
                check_control()
            _log_multimodal_request(endpoint, call, request, schema_name=schema_name, result_type=result_type)
            try:
                response = request_chat_completion(
                    endpoint.client.chat.completions, usage_call=call,
                    check_control=check_control, **request,
                )
            except APIError as error:
                call.capture_response(error)
                call.raise_if_cancelled()
                if getattr(error, "code", None) == "content_filter":
                    raise ContentFilterError("Vision output rejected", code="content_filter") from error
                raise QueryModelIncompleteError(
                    "Vision tool request failed", reason=_failure_reason_for_exception(error),
                ) from error
            call.capture_response(response)
            record_model_event("response", response)
            call.raise_if_cancelled()
            if check_control is not None:
                check_control()
            turn = _validated_agent_turn(
                response, tools=tools, seen_tool_call_ids=seen,
                provider_label=endpoint.provider, model_name=endpoint.model,
                max_output_tokens=adapter.max_output_tokens, error_type=QueryModelIncompleteError,
                require_tool_call=enforce_final_schema and choice == "required",
                require_visible_text_on_stop=enforce_final_schema and visible,
            )
            parsed = None
            if enforce_final_schema and turn[0] == "stop":
                try:
                    parsed = result_type.model_validate_json(turn[1])
                except ValueError as error:
                    raise QueryModelIncompleteError("Vision final output does not match its schema") from error
            return turn, parsed, _response_input_tokens(response)
        finally:
            unregister()


def _chat_content(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"type": "text", "text": item["text"]} if item.get("type") == "input_text"
        else {"type": "image_url", "image_url": {"url": item["image_url"], "detail": item.get("detail", "high")}}
        if item.get("type") == "input_image" else deepcopy(item)
        for item in content
    ]


def _chat_input(messages: Sequence[dict[str, Any]], endpoint: _Endpoint) -> list[dict[str, Any]]:
    inputs = []
    media_message = _media_message(messages, endpoint)
    for message in messages:
        content = endpoint.content if message is media_message else message.get("content")
        item = {"role": message["role"], "content": _chat_content(content) if isinstance(content, list) else content}
        for key in ("tool_call_id", "tool_calls", "reasoning_content"):
            if key in message:
                item[key] = deepcopy(message[key])
        inputs.append(item)
    return inputs


def _response_input(messages: Sequence[dict[str, Any]], endpoint: _Endpoint) -> list[dict[str, Any]]:
    inputs = []
    media_message = _media_message(messages, endpoint)
    for message in messages:
        role = message["role"]
        if role == "tool":
            inputs.append({"type": "function_call_output", "call_id": message["tool_call_id"], "output": message["content"]})
            continue
        if role == "assistant" and message.get("responses_provider") == (endpoint.provider, endpoint.model):
            inputs.extend(deepcopy(message["responses_output"]))
            continue
        content = endpoint.content if message is media_message else message.get("content")
        if content:
            if isinstance(content, list):
                content = [
                    {"type": "input_text", "text": item["text"]} if item.get("type") == "text"
                    else {"type": "input_image", "image_url": item["image_url"]["url"], "detail": item["image_url"].get("detail", "high")}
                    if item.get("type") == "image_url" else deepcopy(item)
                    for item in content
                ]
            inputs.append({"role": role, "content": content})
        for call in message.get("tool_calls", []):
            inputs.append({"type": "function_call", "call_id": call["id"], **call["function"]})
    return inputs


def _media_message(messages: Sequence[dict[str, Any]], endpoint: _Endpoint) -> dict[str, Any]:
    if endpoint.media_message_id is None:
        return messages[0]
    for message in messages:
        if message.get("role") == "user" and message.get("id") == endpoint.media_message_id:
            return message
    raise ValueError("Multimodal message history lost its source media message")


def _encode_inputs(inputs: Sequence[MultimodalInput], provider: str) -> list[dict[str, Any]]:
    if not inputs:
        raise ValueError("fallback_inputs must not be empty")
    content, total = [], 0
    for item in inputs:
        if isinstance(item, TextInput):
            content.append({"type": "input_text", "text": item.text})
            continue
        if isinstance(item, VideoInput) and provider != ARK_PROVIDER:
            raise ValueError("DeepSeek tools require ordered ImageInput values")
        url, size = encode_media_file(item.path, expected_kind="image" if isinstance(item, ImageInput) else "video")
        total += size
        if provider == ARK_PROVIDER and total >= 45 * 1024 * 1024:
            raise ValueError("combined inline media must be smaller than 45 MiB")
        if provider == DEEPSEEK_PROVIDER and (size > 32 * 1024 * 1024 or total > 35 * 1024 * 1024):
            raise ValueError("DeepSeek inline images exceed their media budget")
        content.append({"type": "input_image", "image_url": url, "detail": item.detail}
                       if isinstance(item, ImageInput)
                       else {"type": "input_video", "video_url": url, "fps": item.fps})
    return content


def _field(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, SimpleNamespace):
        return _plain(vars(value))
    return value
