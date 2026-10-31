"""Creation-time stream and temperature settings through real SDKs and mock HTTP."""
import json
from pathlib import Path

import httpx
import pytest
from arkruntime import Ark
from openai import OpenAI
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ValidationError

from gateway.agent_model.context import ContextWindowPolicy
from gateway.config import ArkModelSourceConfig, ModelBinding, OpenAIModelSourceConfig, QueryHookConfig, load_config
from gateway.local.llama_cpp import LlamaCppMultimodalModel
from gateway.local.ollama import OllamaMultimodalModel
from gateway.model_factory import GatewayClient
from gateway.multimodal import TextInput
from gateway.query_hook import QueryHookModels
from gateway.usage import capture_model_usage


class Answer(BaseModel):
    answer: str


def _wire(body, *, structured=False, tool_call=False):
    text = '{"answer":"ok"}'
    usage = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}
    if "input" in body:
        response = {"id": "response-1", "object": "response", "created_at": 0,
                    "model": body["model"], "status": "completed", "output": [{
                        "type": "message", "id": "msg-1", "status": "completed", "role": "assistant",
                        "content": [{"type": "output_text", "text": text, "annotations": []}],
                    }], "usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}}
        events = [{"type": "response.output_text.delta", "sequence_number": 0,
                   "output_index": 0, "content_index": 0, "item_id": "msg-1", "delta": text},
                  {"type": "response.completed", "sequence_number": 1, "response": response}]
        if tool_call:
            response["output"] = [{"type": "function_call", "id": "item-search", "call_id": "call-search",
                                   "name": "search_evidence", "arguments": '{"query":"scene"}', "status": "completed"}]
            events = [{"type": "response.completed", "sequence_number": 0, "response": response}]
    else:
        message = {"role": "assistant", "content": text}
        if structured or tool_call:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "finish-1", "type": "function", "function": {
                    "name": "search_evidence" if tool_call else "Answer",
                    "arguments": '{"query":"scene"}' if tool_call else text},
            }]}
        finish = "tool_calls" if structured or tool_call else "stop"
        response = {"id": "chat-1", "object": "chat.completion", "created": 0,
                    "model": body["model"], "choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": usage}
        delta = {**message}
        if structured or tool_call:
            delta["tool_calls"] = [{**message["tool_calls"][0], "index": 0}]
        base = {"id": "chat-1", "object": "chat.completion.chunk", "created": 0, "model": body["model"]}
        events = [{**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                  {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
                  {**base, "choices": [], "usage": usage}]
    if not body["stream"]:
        return httpx.Response(200, json=response)
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          text="".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n")


def _setup(monkeypatch, profile, *, structured=False, fail_primary=False, visual_tools=False):
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        if fail_primary and body["model"] == "primary" and len(requests) == 1:
            code = "content_filter" if fail_primary == "filter" else "temporary_failure"
            return httpx.Response(400 if code == "content_filter" else 502, json={"error": {"message": "temporary failure", "code": code}})
        return _wire(body, structured=structured, tool_call=visual_tools and len(requests) == 1)

    def openai(**kwargs):
        return OpenAI(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(respond)))

    def ark(**kwargs):
        return Ark(api_key="offline", base_url="https://ark.example/v1", max_retries=0,
                   http_client=httpx.Client(transport=httpx.MockTransport(respond)))

    for module in ("gateway.openai.vision", "gateway.openai.agent", "gateway.openai.structured",
                   "gateway.openai.multimodal", "gateway.agent_model.deepagents"):
        monkeypatch.setattr(module + ".OpenAI", openai)
    monkeypatch.setattr("gateway.ark.multimodal.ArkMultimodalModel._new_client", lambda self: ark())
    monkeypatch.setattr("gateway.ark.agent.new_ark_client", ark)
    monkeypatch.setattr("gateway.agent_model.deepagents.new_ark_client", ark)
    config = load_config(Path(__file__).parent / "fixtures/legacy-config.yaml")
    config.model_sources["selected"] = (ArkModelSourceConfig(api_key_env="KEY") if profile == "ark" else
        OpenAIModelSourceConfig(profile=profile, provider="arbitrary-display-name",
                                base_url="https://model.example/v1", api_key_env="KEY"))
    config.model_sources["backup"] = ArkModelSourceConfig(api_key_env="KEY")
    return GatewayClient(config, environ={"KEY": "offline"}), requests


def _run(gateway, binding, entry):
    if entry in {"json", "transcription", "vision_tools"}:
        create = gateway.create_transcription_model if entry == "transcription" else gateway.create_generative_model
        return create(binding).generate(
            [TextInput("answer")], schema_name="answer", result_type=Answer,
            **({"tools": [StructuredTool.from_function(lambda query: "evidence", name="search_evidence", description="Read evidence")],
                "max_model_calls": 3} if entry == "vision_tools" else {}),
        ).answer
    factory = gateway.create_agent_factory(binding)
    tools = [StructuredTool.from_function(lambda query: "evidence", name="search_evidence", description="Read evidence")]
    controls = dict(model=binding.model, reasoning_effort="none", request_timeout_seconds=10,
                    max_output_tokens=1024, max_input_chars=10000,
                    context_window_policy=ContextWindowPolicy(128000, 1024, 2048, 60000, 40000))
    if entry == "structured":
        return factory.build_structured(**controls).run_structured(
            "answer", tools=tools, result_type=Answer, max_model_calls=2,
        ).answer
    texts = []
    if entry == "loop":
        factory.build_loop(**controls).run_agent_loop("answer", tools=tools, max_model_calls=2, handle_text=texts.append)
    else:
        factory.build_supervisor(harness="deepagents", **controls).run_agent(
            "answer", thread_id="conversation", message_id="message", user_message="answer",
            history=[], tools=[], max_model_calls=2, handle_text=texts.append,
        )
    assert texts
    return json.loads(texts[-1])["answer"]


@pytest.mark.parametrize("profile", ["standard", "kimi", "deepseek", "ark"])
@pytest.mark.parametrize("setting", [None, True, False], ids=["default", "enabled", "disabled"])
@pytest.mark.parametrize("temperature", [None, 0.2, 0.8], ids=["default-temperature", "temperature-0.2", "temperature-0.8"])
@pytest.mark.parametrize("entry", ["json", "loop", "structured", "supervisor", "vision_tools"])
def test_all_factory_entries_execute_the_configured_transport(monkeypatch, profile, setting, temperature, entry):
    gateway, requests = _setup(monkeypatch, profile, structured=entry == "structured", visual_tools=entry == "vision_tools")
    binding = ModelBinding(source="selected", model="test-model", max_output_tokens=1024,
                           **({} if temperature is None else {"temperature": temperature}),
                           **({} if setting is None else {"stream": setting}))
    usage = []
    try:
        with capture_model_usage(usage.append):
            assert _run(gateway, binding, entry) == "ok"
    finally:
        gateway.close()
    expected = setting is not False
    assert requests and all(request["stream"] is expected for request in requests)
    for request in requests:
        assert request["temperature"] == (1 if profile == "kimi" else temperature or 0)
        if "messages" in request:
            assert request.get("stream_options") == ({"include_usage": True} if expected else None)
    assert any(item.usage.input_tokens == 100 and item.usage.output_tokens == 10 for item in usage)


@pytest.mark.parametrize("primary", [True, False])
@pytest.mark.parametrize("entry", ["json", "loop", "structured", "supervisor"])
def test_fallback_uses_its_own_stream_setting(monkeypatch, primary, entry):
    gateway, requests = _setup(monkeypatch, "kimi", structured=entry == "structured", fail_primary="filter" if entry == "json" else True)
    binding = ModelBinding(source="selected", model="primary", stream=primary, max_output_tokens=1024,
                           temperature=0.2,
                           fallback=ModelBinding(source="backup", model="fallback", stream=not primary, temperature=0.8, max_output_tokens=1024))
    try:
        assert _run(gateway, binding, entry) == "ok"
    finally:
        gateway.close()
    assert [(request["model"], request["stream"]) for request in requests] == [("primary", primary), ("fallback", not primary)]
    assert [request["temperature"] for request in requests] == [1, 0.8]


@pytest.mark.parametrize("primary", [True, False])
@pytest.mark.parametrize("distinct_transport", [True, False])
@pytest.mark.parametrize("entry", ["loop", "structured", "supervisor"])
def test_same_ark_model_can_have_a_distinct_fallback_transport(monkeypatch, primary, distinct_transport, entry):
    gateway, requests = _setup(monkeypatch, "ark", structured=entry == "structured", fail_primary=True)
    binding = ModelBinding(
        source="selected", model="primary", stream=primary, temperature=0.2, max_output_tokens=1024,
        fallback=ModelBinding(source="backup", model="primary", stream=not primary if distinct_transport else primary,
                              temperature=0.8, max_output_tokens=1024),
    )
    try:
        assert _run(gateway, binding, entry) == "ok"
    finally:
        gateway.close()
    assert [request["stream"] for request in requests] == [primary, not primary if distinct_transport else primary]
    assert [request["temperature"] for request in requests] == [0.2, 0.8]


@pytest.mark.parametrize("setting", [None, True, False])
def test_video_transcription_generation_uses_its_binding_stream_setting(monkeypatch, setting):
    gateway, requests = _setup(monkeypatch, "ark")
    binding = ModelBinding(source="selected", model="video-asr", kind="transcription", max_output_tokens=1024,
                           temperature=0.2,
                           **({} if setting is None else {"stream": setting}))
    try:
        assert _run(gateway, binding, "transcription") == "ok"
    finally:
        gateway.close()
    assert len(requests) == 1 and requests[0]["stream"] is (setting is not False)
    assert requests[0]["temperature"] == 0.2


@pytest.mark.parametrize("setting", [None, True, False])
@pytest.mark.parametrize("temperature", [None, 0.8])
@pytest.mark.parametrize("model_type", [OllamaMultimodalModel, LlamaCppMultimodalModel])
def test_local_compatible_models_use_the_same_default_and_override(setting, temperature, model_type):
    requests = []
    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        return _wire(body)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        model = model_type("local", base_url="http://localhost:9000", max_output_tokens=1024,
                           request_timeout_seconds=10, client=client,
                           **({} if temperature is None else {"temperature": temperature}),
                           **({"expected_server_video_fps": 1} if model_type is LlamaCppMultimodalModel else {}),
                           **({} if setting is None else {"stream": setting}))
        assert model.generate([TextInput("answer")], schema_name="answer", result_type=Answer).answer == "ok"
        assert json.loads("".join(model.stream_text([TextInput("answer")])))["answer"] == "ok"
    assert len(requests) == 2 and all(body["stream"] is (setting is not False) for body in requests)
    assert all(body["temperature"] == (temperature or 0) for body in requests)


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None])
def test_stream_configuration_rejects_non_boolean_values(value):
    with pytest.raises(ValidationError):
        ModelBinding(source="selected", model="model", stream=value)


@pytest.mark.parametrize("setting", [None, True, False])
@pytest.mark.parametrize("temperature", [None, 0.2])
def test_query_rewriter_uses_its_creation_time_stream_setting(setting, temperature):
    requests = []
    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        return _wire(body)
    config = QueryHookConfig(**({} if setting is None else {"rewriter_stream": setting}),
                             **({} if temperature is None else {"rewriter_temperature": temperature}))
    model = QueryHookModels(config, transport=httpx.MockTransport(respond))
    assert model.rewrite("answer", Answer, timeout_seconds=3).answer == "ok"
    assert len(requests) == 1 and requests[0]["stream"] is (setting is not False)
    assert requests[0]["temperature"] == (temperature or 0)
