from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from typing import Any, Protocol, TypeVar

from arkruntime import Ark
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, ModelCallLimitMiddleware
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage, message_to_dict
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from openai import OpenAI
from pydantic import BaseModel

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
