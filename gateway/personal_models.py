"""User-owned chat endpoints, using the same Deep Agents loop and tool history as hosted models."""
from __future__ import annotations

from urllib.parse import quote

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, convert_to_openai_messages
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import PrivateAttr

from .agent_model.context import ContextWindowPolicy
from .agent_model.deepagents import DeepAgentsSupervisorModel, DeepAgentSessionStore
from .personal_endpoint import PersonalModelTarget, personal_http_client


class PersonalModelRequestError(RuntimeError):
    """Safe to expose; never retain upstream response bodies or credentials."""


def _result(message: AIMessage) -> ChatResult:
    return ChatResult(generations=[ChatGeneration(message=message)])


class _PersonalChatModel(BaseChatModel):
    _target: PersonalModelTarget = PrivateAttr()
    _http: httpx.Client = PrivateAttr()
    _max_output: int = PrivateAttr()

    def __init__(self, target: PersonalModelTarget, http: httpx.Client, max_output: int):
        super().__init__()
        self._target, self._http, self._max_output = target, http, max_output

    @property
    def _llm_type(self) -> str:
        return self._target.protocol

    def close(self) -> None:
        self._http.close()

    def _post(self, path: str, headers: dict, payload: dict, timeout: float | None) -> dict:
        response = self._http.post(self._target.base_url + path, headers=headers, json=payload,
                                   timeout=timeout if timeout is not None else self._http.timeout)
        if not response.is_success:
            raise PersonalModelRequestError(f"模型接口返回 HTTP {response.status_code}")
        return response.json()

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs) -> ChatResult:
        target = self._target
        key = target.api_key.get_secret_value()
        tools = kwargs.get("tools") or []
        timeout = kwargs.get("timeout")
        try:
            if target.protocol == "gemini":
                from google.genai.types import GenerateContentResponse
                from langchain_google_genai._function_utils import convert_to_genai_function_declarations
                from langchain_google_genai.chat_models import _parse_chat_history, _response_to_result

                def wire(obj):
                    return obj.model_dump(mode="json", by_alias=True, exclude_none=True)
                system, history = _parse_chat_history(messages, model=target.model_id)
                payload = {"contents": [wire(item) for item in history],
                           "generationConfig": {"maxOutputTokens": self._max_output}}
                if system:
                    payload["systemInstruction"] = wire(system)
                if tools:
                    payload["tools"] = [wire(tool) for tool in convert_to_genai_function_declarations(tools)]
                path = "/models/" + quote(target.model_id.removeprefix("models/"), safe="/") + ":generateContent"
                data = self._post(path, {"x-goog-api-key": key} if key else {}, payload, timeout)
                return _response_to_result(GenerateContentResponse.model_validate(data))

            if target.protocol == "anthropic_messages":
                from langchain_anthropic.chat_models import _format_messages, convert_to_anthropic_tool
                from langchain_core.messages.tool import tool_call

                system, history = _format_messages(messages, model=target.model_id)
                payload = {"model": target.model_id, "messages": history, "max_tokens": self._max_output}
                if system:
                    payload["system"] = system
                if tools:
                    payload["tools"] = [convert_to_anthropic_tool(tool) for tool in tools]
                headers = {"anthropic-version": "2023-06-01"}
                if key:
                    headers["x-api-key"] = key
                path = "/messages" if target.base_url.endswith("/v1") else "/v1/messages"
                data = self._post(path, headers, payload, timeout)
                blocks = data["content"]
                calls = [tool_call(name=b["name"], args=b["input"], id=b["id"]) for b in blocks if b.get("type") == "tool_use"]
                return _result(AIMessage(content=blocks, tool_calls=calls))

            headers = {"Authorization": f"Bearer {key}"} if key else {}
            payload = {"model": target.model_id}
            if tools:
                payload["tools"] = [convert_to_openai_tool(tool) for tool in tools]
            if target.protocol == "openai_responses":
                from langchain_openai.chat_models.base import _construct_responses_api_payload, _construct_lc_result_from_responses_api
                from openai._models import construct_type_unchecked
                from openai.types.responses import Response

                payload.update(max_output_tokens=self._max_output, store=False)
                data = self._post("/responses", headers, _construct_responses_api_payload(messages, payload), timeout)
                return _construct_lc_result_from_responses_api(construct_type_unchecked(value=data, type_=Response), output_version="responses/v1")
            if target.protocol == "openai_chat":
                from langchain_openai.chat_models.base import _convert_dict_to_message
                from .agent_model.tool_loop import _copy_assistant_reasoning

                history = convert_to_openai_messages(messages)
                _copy_assistant_reasoning(messages, history)
                payload.update(messages=history, max_tokens=self._max_output)
                data = self._post("/chat/completions", headers, payload, timeout)
                return _result(_convert_dict_to_message(data["choices"][0]["message"]))
            raise PersonalModelRequestError("未知模型接口协议")
        except PersonalModelRequestError:
            raise
        except (httpx.HTTPError, OSError):
            error = "无法连接模型接口"
        except Exception:
            error = "模型请求或回复与所选协议不匹配"
        raise PersonalModelRequestError(error)


class _PersonalSupervisor(DeepAgentsSupervisorModel):
    def close(self) -> None:
        self._chat_model.close()


def build_personal_supervisor(target: PersonalModelTarget, *, session_store: DeepAgentSessionStore,
                              request_timeout_seconds: float, max_output_tokens: int, max_input_chars: int,
                              context_window_policy: ContextWindowPolicy, total_timeout_enabled: bool,
                              allow_local: bool) -> DeepAgentsSupervisorModel:
    http = personal_http_client(target, allow_local=allow_local, timeout=request_timeout_seconds)
    try:
        return _PersonalSupervisor(
            session_store=session_store, model=target.model_id, provider_name=target.protocol,
            request_timeout_seconds=request_timeout_seconds, max_input_chars=max_input_chars,
            context_window_policy=context_window_policy, total_timeout_enabled=total_timeout_enabled,
            supports_vision_input=target.supports_vision, chat_model=_PersonalChatModel(target, http, max_output_tokens),
        )
    except BaseException:
        http.close()
        raise


def probe_personal_model(target: PersonalModelTarget, *, allow_local: bool) -> None:
    with personal_http_client(target, allow_local=allow_local, timeout=15) as http:
        result = _PersonalChatModel(target, http, 64)._generate([HumanMessage("Reply with OK.")])
        if not result.generations or not result.generations[0].message.text.strip():
            raise PersonalModelRequestError("模型没有返回文本")
