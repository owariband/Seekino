"""Run a registered HTTP model through the existing structured media and tool contracts."""
from __future__ import annotations

import json

from langchain_core.messages import HumanMessage
from langchain_openai.chat_models.base import _convert_dict_to_message, _convert_message_to_dict
from openai.types.chat import ChatCompletion
from pydantic import SecretStr

from .agent_model.errors import QueryModelIncompleteError
from .agent_model.multimodal import _chat_content, _encode_inputs, generate_with_tools
from .personal_endpoint import PersonalModelTarget, personal_http_client
from .personal_models import _PersonalChatModel
from .usage import track_model_call


class RegisteredMultimodalModel:
    _tool_protocol = "registered"
    _format = "json_object"

    def __init__(self, binding, source, *, api_key: str | None, allow_local: bool):
        self.model_name = binding.model
        self.provider_name = source.protocol
        self.max_output_tokens = binding.max_output_tokens
        self._request_timeout_seconds = binding.request_timeout_seconds
        self._allow_local = allow_local
        self._target = PersonalModelTarget(source.protocol, source.base_url, binding.model,
                                           SecretStr(api_key or ""), binding.supports_vision_input)

    def _new_client(self):
        http = personal_http_client(self._target, allow_local=self._allow_local, timeout=self._request_timeout_seconds)
        return _PersonalChatModel(self._target, http, self.max_output_tokens)

    def generate(self, inputs, *, schema_name, result_type, tools=(), request_timeout_seconds=None, **options):
        content = _chat_content(_encode_inputs(inputs, self.provider_name))
        if tools:
            return generate_with_tools(self, content, schema_name=schema_name, result_type=result_type, tools=tools,
                        request_timeout_seconds=request_timeout_seconds, **options)
        content.append({"type": "text", "text": "Return only JSON matching this schema: " +
                        json.dumps(result_type.model_json_schema(), ensure_ascii=False)})
        client = self._new_client()
        try:
            with track_model_call(provider=self.provider_name, model=self.model_name, operation=schema_name) as call:
                unregister = call.add_cancel_callback(client.close)
                try:
                    call.raise_if_cancelled()
                    message = client.invoke([HumanMessage(content=content)], timeout=request_timeout_seconds)
                    client.capture_usage(call)
                    call.raise_if_cancelled()
                    return result_type.model_validate_json(message.text)
                finally:
                    unregister()
        finally:
            client.close()

    def tool_completion(self, client, messages, specs, choice, timeout):
        response = client.invoke([_convert_dict_to_message(message) for message in messages],
                                 tools=specs, tool_choice=choice, timeout=timeout)
        if response.invalid_tool_calls:
            raise QueryModelIncompleteError("模型返回了无效的工具调用")
        message = _convert_message_to_dict(response)
        message["content"] = response.text or None
        return ChatCompletion.model_validate({
            "id": response.id or "registered", "model": self.model_name, "created": 0, "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": "tool_calls" if response.tool_calls else "stop", "message": message}],
        })
