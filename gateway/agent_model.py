from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from time import monotonic
from typing import Any, Protocol, TypeVar

from arkruntime import Ark
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, ModelCallLimitMiddleware
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage, message_to_dict
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from openai import APIError, OpenAI
from pydantic import BaseModel

from .schema_utils import inline_schema_refs
from .sse_proxy import ContentFilterError
from .usage import ModelCallTracker, track_model_call

_ResultModel = TypeVar("_ResultModel", bound=BaseModel)


class ModelInputTooLargeError(RuntimeError):
    """The complete model message state exceeded an application character budget."""


class QueryAgentModel(Protocol):
    provider_name: str
    model_name: str

    def run_query(
        self,
        prompt: str,
        *,
        tools: Sequence[BaseTool],
        result_type: type[_ResultModel],
        max_model_calls: int,
        check_control: Callable[[], None] | None = None,
    ) -> _ResultModel: ...


class AgentLoopModel(Protocol):
    provider_name: str
    model_name: str

    def run_agent_loop(
        self,
        prompt: str,
        *,
        function_name: str,
        function_description: str,
        result_type: type[_ResultModel],
        max_model_calls: int,
        handle_text: Callable[[str], None],
        handle_tool_call: Callable[[_ResultModel], str],
        request_timeout_seconds: float | None = None,
        check_control: Callable[[], None] | None = None,
    ) -> None: ...


class DeepSeekFunctionCallingError(RuntimeError):
    """DeepSeek failed or returned an invalid agent-loop response."""


class DeepSeekFunctionCallingModel:
    """Run a bounded DeepSeek tool loop and deliver ordered assistant events."""

    provider_name = "deepseek"

    def __init__(
        self,
        model: str,
        *,
        base_url: str,
        api_key: str,
        max_output_tokens: int,
        request_timeout_seconds: float,
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

        self.model_name = model
        self.base_url = base_url.rstrip("/")
        self.max_output_tokens = max_output_tokens
        self._api_key = api_key
        self._request_timeout_seconds = request_timeout_seconds
        self._client = client

    def run_agent_loop(
        self,
        prompt: str,
        *,
        function_name: str,
        function_description: str,
        result_type: type[_ResultModel],
        max_model_calls: int,
        handle_text: Callable[[str], None],
        handle_tool_call: Callable[[_ResultModel], str],
        request_timeout_seconds: float | None = None,
        check_control: Callable[[], None] | None = None,
    ) -> None:
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        if not function_name.strip():
            raise ValueError("function_name must not be empty")
        if not function_description.strip():
            raise ValueError("function_description must not be empty")
        if (
            isinstance(max_model_calls, bool)
            or not isinstance(max_model_calls, int)
            or max_model_calls <= 0
        ):
            raise ValueError("max_model_calls must be a positive integer")
        if request_timeout_seconds is not None and request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be greater than zero")
        effective_timeout = min(
            self._request_timeout_seconds,
            request_timeout_seconds or self._request_timeout_seconds,
        )

        deadline = monotonic() + effective_timeout
        if check_control is not None:
            check_control()
        client = self._client or OpenAI(
            api_key=self._api_key,
            base_url=self.base_url,
            timeout=self._request_timeout_seconds,
            max_retries=0,
        )
        try:
            messages: list[dict[str, Any]] = [
                {"role": "user", "content": prompt}
            ]
            tool = {
                "type": "function",
                "function": {
                    "name": function_name,
                    "description": function_description,
                    "parameters": inline_schema_refs(
                        result_type.model_json_schema()
                    ),
                },
            }
            seen_tool_call_ids: set[str] = set()
            emitted_text = False

            for model_call_index in range(max_model_calls):
                if model_call_index > 0 and check_control is not None:
                    check_control()
                remaining_timeout = deadline - monotonic()
                if remaining_timeout <= 0:
                    raise DeepSeekFunctionCallingError(
                        "DeepSeek agent loop request deadline expired"
                    )
                with track_model_call(
                    provider=self.provider_name,
                    model=self.model_name,
                    operation=function_name,
                ) as call:
                    response = self._create_completion(
                        client,
                        call,
                        messages=messages,
                        tools=[tool],
                        tool_choice="auto",
                        timeout=remaining_timeout,
                    )
                    (
                        finish_reason,
                        content,
                        assistant_message,
                        validated_calls,
                    ) = _validated_agent_turn(
                        response,
                        function_name=function_name,
                        result_type=result_type,
                        seen_tool_call_ids=seen_tool_call_ids,
                    )

                if check_control is not None:
                    check_control()
                visible_content = (
                    content
                    if isinstance(content, str) and content.strip()
                    else None
                )
                if visible_content is not None:
                    handle_text(visible_content)
                    emitted_text = True

                if finish_reason == "stop":
                    if not emitted_text:
                        raise DeepSeekFunctionCallingError(
                            "DeepSeek agent loop completed without visible text"
                        )
                    return

                messages.append(assistant_message)
                for tool_call_id, result in validated_calls:
                    if check_control is not None:
                        check_control()
                    output = handle_tool_call(result)
                    if not isinstance(output, str):
                        raise TypeError("handle_tool_call must return str")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": output,
                    })
            raise DeepSeekFunctionCallingError(
                "DeepSeek agent loop exhausted max_model_calls before stop"
            )
        finally:
            if self._client is None:
                client.close()

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
            response = client.chat.completions.create(
                model=self.model_name,
                messages=messages,
                stream=False,
                temperature=0,
                max_tokens=self.max_output_tokens,
                timeout=timeout,
                extra_body={"thinking": {"type": "disabled"}},
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


def _validated_agent_turn(
    response: Any,
    *,
    function_name: str,
    result_type: type[_ResultModel],
    seen_tool_call_ids: set[str],
) -> tuple[
    str,
    str | None,
    dict[str, Any],
    list[tuple[str, _ResultModel]],
]:
    choice = _single_completion_choice(response)
    finish_reason = getattr(choice, "finish_reason", None)
    if finish_reason == "content_filter":
        raise ContentFilterError(
            "DeepSeek filtered agent-loop output",
            code="content_filter",
            response_id=getattr(response, "id", None),
        )
    if finish_reason not in {"stop", "tool_calls"}:
        raise DeepSeekFunctionCallingError(
            "DeepSeek agent-loop turn did not complete: "
            f"{finish_reason!r}"
        )

    message = getattr(choice, "message", None)
    if getattr(message, "role", None) != "assistant":
        raise DeepSeekFunctionCallingError(
            "DeepSeek returned an invalid assistant message"
        )
    content = getattr(message, "content", None)
    if content is not None and not isinstance(content, str):
        raise DeepSeekFunctionCallingError(
            "DeepSeek assistant content must be text or null"
        )
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls is None:
        tool_calls = []
    if not isinstance(tool_calls, list):
        raise DeepSeekFunctionCallingError(
            "DeepSeek returned invalid tool calls"
        )
    if finish_reason == "stop":
        if tool_calls:
            raise DeepSeekFunctionCallingError(
                "DeepSeek stop response must not contain tool calls"
            )
        return (
            finish_reason,
            content,
            {"role": "assistant", "content": content},
            [],
        )
    if not tool_calls:
        raise DeepSeekFunctionCallingError(
            "DeepSeek tool_calls response contained no tool calls"
        )

    new_tool_call_ids: set[str] = set()
    rendered_calls: list[dict[str, Any]] = []
    validated_calls: list[tuple[str, _ResultModel]] = []
    for tool_call in tool_calls:
        tool_call_id = getattr(tool_call, "id", None)
        if (
            not isinstance(tool_call_id, str)
            or not tool_call_id.strip()
            or tool_call_id in seen_tool_call_ids
            or tool_call_id in new_tool_call_ids
        ):
            raise DeepSeekFunctionCallingError(
                "DeepSeek function calls must have unique nonempty IDs"
            )
        new_tool_call_ids.add(tool_call_id)

        function = getattr(tool_call, "function", None)
        if (
            getattr(tool_call, "type", None) != "function"
            or getattr(function, "name", None) != function_name
        ):
            raise DeepSeekFunctionCallingError(
                "DeepSeek called an unexpected function"
            )
        arguments = getattr(function, "arguments", None)
        if not isinstance(arguments, str) or not arguments.strip():
            raise DeepSeekFunctionCallingError(
                "DeepSeek returned empty function arguments"
            )
        try:
            result = result_type.model_validate_json(arguments)
        except (ValueError, TypeError) as error:
            raise DeepSeekFunctionCallingError(
                "DeepSeek function arguments do not match schema "
                f"{function_name}"
            ) from error

        rendered_calls.append({
            "id": tool_call_id,
            "type": "function",
            "function": {
                "name": function_name,
                "arguments": arguments,
            },
        })
        validated_calls.append((tool_call_id, result))

    seen_tool_call_ids.update(new_tool_call_ids)
    return (
        finish_reason,
        content,
        {
            "role": "assistant",
            "content": content,
            "tool_calls": rendered_calls,
        },
        validated_calls,
    )


def _single_completion_choice(response: Any) -> Any:
    choices = getattr(response, "choices", None)
    if not isinstance(choices, list) or len(choices) != 1:
        raise DeepSeekFunctionCallingError(
            "DeepSeek returned an invalid chat completion envelope"
        )
    return choices[0]


class _AgentChatOpenAI(ChatOpenAI):
    """Adapt Ark's typed completion envelope to the maintained OpenAI adapter."""

    use_legacy_token_parameter: bool = False

    def _get_request_payload(self, input_: Any, *, stop: Any = None, **kwargs: Any):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        if self.use_legacy_token_parameter and "max_completion_tokens" in payload:
            payload["max_tokens"] = payload.pop("max_completion_tokens")
        return payload

    def _create_chat_result(self, response: Any, generation_info: Any = None):
        if isinstance(response, BaseModel):
            response = response.model_dump()
        for choice in response.get("choices", []):
            reason = choice.get("finish_reason")
            if reason not in {"stop", "tool_calls"}:
                raise RuntimeError(f"Query model did not complete: {reason}")
        return super()._create_chat_result(response, generation_info)


class _ExecutionControlMiddleware(AgentMiddleware):
    def __init__(self, check: Callable[[], None]) -> None:
        super().__init__()
        self._check = check

    def before_model(self, state: Any, runtime: Any) -> None:
        self._check()

    def after_model(self, state: Any, runtime: Any) -> None:
        self._check()


class _InputCharacterLimitMiddleware(AgentMiddleware):
    def __init__(self, maximum_chars: int, contract_chars: int) -> None:
        super().__init__()
        self._maximum_chars = maximum_chars
        self._contract_chars = contract_chars

    def before_model(self, state: Any, runtime: Any) -> None:
        messages = state.get("messages", [])
        payload = [
            message_to_dict(message) if isinstance(message, BaseMessage) else message
            for message in messages
        ]
        message_chars = len(json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), default=str,
        ))
        total_chars = message_chars + self._contract_chars
        if total_chars > self._maximum_chars:
            raise ModelInputTooLargeError(
                "Query model message character budget exceeded: "
                f"{total_chars} > {self._maximum_chars}"
            )


class SDKQueryAgentModel:
    """Let LangChain own the inner model/tool loop, with a fresh state per call."""

    def __init__(
        self,
        model: str,
        *,
        request_timeout_seconds: float,
        max_output_tokens: int,
        max_input_chars: int | None = None,
        provider: str = "ark",
        base_url: str | None = None,
        api_key: str | None = None,
        client: Any | None = None,
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
        if provider not in {"ark", "deepseek", "ollama", "llama_cpp"}:
            raise ValueError(f"unsupported Query provider: {provider}")
        self.model_name = model
        self.provider_name = provider
        self.request_timeout_seconds = request_timeout_seconds
        self.max_output_tokens = max_output_tokens
        self.max_input_chars = max_input_chars
        self.base_url = base_url
        self.api_key = api_key
        self._client = client
        self._chat_model = chat_model

    def run_query(
        self,
        prompt: str,
        *,
        tools: Sequence[BaseTool],
        result_type: type[_ResultModel],
        max_model_calls: int,
        check_control: Callable[[], None] | None = None,
    ) -> _ResultModel:
        if not prompt.strip() or max_model_calls <= 0:
            raise ValueError("Query prompt and model call budget must be positive")
        if check_control is not None:
            check_control()
        with ExitStack() as cleanup:
            if self._chat_model is not None:
                model = self._chat_model
            else:
                client = self._client
                if client is None:
                    # Ark's client is thread unsafe: own one per Query invocation.
                    if self.provider_name == "ark":
                        client = Ark.volc(
                            timeout=self.request_timeout_seconds,
                            max_retries=0,
                        )
                    else:
                        client = OpenAI(
                            base_url=self.base_url,
                            api_key=self.api_key or "local-server",
                            timeout=self.request_timeout_seconds,
                            max_retries=0,
                        )
                    cleanup.callback(client.close)
                model = _AgentChatOpenAI(
                    model=self.model_name,
                    client=client.chat.completions,
                    async_client=client.chat.completions,
                    api_key="injected-sdk-auth",
                    base_url=self.base_url or "https://ark.cn-beijing.volces.com/api/v3",
                    max_completion_tokens=self.max_output_tokens,
                    use_legacy_token_parameter=self.provider_name != "ark",
                    temperature=0,
                    max_retries=0,
                    timeout=self.request_timeout_seconds,
                    use_responses_api=False,
                    streaming=False,
                )
            middleware = [
                ModelCallLimitMiddleware(
                    run_limit=max_model_calls,
                    exit_behavior="error",
                )
            ]
            if check_control is not None:
                middleware.append(_ExecutionControlMiddleware(check_control))
            if self.max_input_chars is not None:
                middleware.append(_InputCharacterLimitMiddleware(
                    self.max_input_chars,
                    _model_contract_chars(tools, result_type),
                ))
            agent = create_agent(
                model,
                tools=list(tools),
                response_format=ToolStrategy(result_type, handle_errors=False),
                middleware=middleware,
            )
            state = agent.invoke(
                {"messages": [{"role": "user", "content": prompt}]},
                config={
                    "recursion_limit": max_model_calls * 6 + 10,
                    "max_concurrency": 1,
                },
            )
            result = state.get("structured_response")
            if not isinstance(result, result_type):
                raise RuntimeError("Query Agent ended without its structured draft")
            return result


def _model_contract_chars(
    tools: Sequence[BaseTool],
    result_type: type[BaseModel],
) -> int:
    contracts = {
        "tools": [{
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.get_input_schema().model_json_schema(),
        } for tool in tools],
        "structured_output": result_type.model_json_schema(),
    }
    return len(json.dumps(
        contracts, ensure_ascii=False, separators=(",", ":"), default=str,
    ))
