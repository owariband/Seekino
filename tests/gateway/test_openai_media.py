# Complete-response contract cases explicitly select stream=False; see test_model_streaming.py for default SSE integration.
import json

import httpx
import pytest
from openai import OpenAI
from pydantic import BaseModel, ValidationError

from agent.utils.tool import build_agent_tool
from agent.utils.tool_result import JsonToolResult
from gateway.errors import ContentFilterError
from gateway.multimodal import ImageInput, TextInput
from gateway.openai.audio import transcribe_audio
from gateway.openai.vision import OpenAIVisionModel
from gateway.usage import ModelCallCancelledError, capture_model_usage, control_model_calls


class Result(BaseModel):
    summary: str


class CanvasArguments(BaseModel):
    value: str


def _vision_response(*, tool=False):
    message = {"role": "assistant", "content": '{"summary":"scene"}'}
    if tool:
        message.update(content=None, tool_calls=[{
            "id": "write-1", "type": "function", "function": {
                "name": "write_context_canvas", "arguments": '{"value":"scene"}',
            },
        }])
    return {
        "id": "vision-result", "object": "chat.completion", "created": 0,
        "model": "custom-vision", "choices": [{
            "index": 0, "finish_reason": "tool_calls" if tool else "stop", "message": message,
        }], "usage": {"prompt_tokens": 120, "completion_tokens": 20, "total_tokens": 140},
    }


def _write_tool(calls):
    def write(value):
        calls.append(value)
        return JsonToolResult(success=True, payload={"written": value})
    return build_agent_tool(
        func=write, name="write_context_canvas", description="Write the analysis Canvas.",
        args_schema=CanvasArguments, maximum_result_bytes=1024,
    ).as_langchain_tool()


def install_transport(monkeypatch, module, handler):
    clients = []

    def create_client(**kwargs):
        http = httpx.Client(transport=httpx.MockTransport(handler))
        clients.append(http)
        return OpenAI(**kwargs, http_client=http)

    monkeypatch.setattr(module + ".OpenAI", create_client)
    return clients


@pytest.mark.parametrize(
    "output_format,token_parameter",
    [("json_schema", "max_tokens"), ("json_object", "max_completion_tokens")],
)
def test_vision_sends_images_to_selected_endpoint_and_validates_json(
    monkeypatch, tmp_path, output_format, token_parameter
):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "result",
                "object": "chat.completion",
                "created": 0,
                "model": "custom-vision",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": '{"summary":"scene"}',
                        },
                    }
                ],
            },
        )

    clients = install_transport(monkeypatch, "gateway.openai.vision", handler)
    image = tmp_path / "frame.png"
    image.write_bytes(b"test-image")
    model = OpenAIVisionModel(
        "custom-vision", stream=False,
        provider="custom",
        base_url="https://vision.example/v1",
        api_key="fake-test-key",
        token_parameter=token_parameter,
        response_format=output_format,
    )
    result = model.generate(
        [TextInput("describe"), ImageInput(image)],
        schema_name="scene",
        result_type=Result,
    )
    assert result.summary == "scene"
    request = requests[0]
    assert str(request.url) == "https://vision.example/v1/chat/completions"
    body = json.loads(request.content)
    assert body["model"] == "custom-vision"
    assert body[token_parameter] == 8192
    assert body["response_format"]["type"] == output_format
    assert body["messages"][0]["content"][1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize(
    "output_format,token_parameter",
    [("json_schema", "max_tokens"), ("json_object", "max_completion_tokens")],
)
def test_vision_tools_keep_selected_protocol_budget_and_real_usage(
    monkeypatch, tmp_path, output_format, token_parameter,
):
    requests, writes, usage = [], [], []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=_vision_response(tool=len(requests) == 1))

    clients = install_transport(monkeypatch, "gateway.openai.vision", handler)
    frame = tmp_path / "frame.png"
    frame.write_bytes(b"test-image")
    model = OpenAIVisionModel(
        "custom-vision", stream=False, provider="portable", base_url="https://vision.example/v1",
        api_key="offline", max_output_tokens=131072, token_parameter=token_parameter,
        response_format=output_format, request_timeout_seconds=19,
    )
    with capture_model_usage(usage.append):
        result = model.generate(
            [TextInput("describe"), ImageInput(frame, detail="low")],
            schema_name="scene", result_type=Result, tools=[_write_tool(writes)],
            max_model_calls=2, context_window_policy=None, max_input_chars=400000,
        )
    assert result.summary == "scene" and writes == ["scene"]
    assert len(requests) == 2 and all(body[token_parameter] == 131072 for body in requests)
    assert all(body["response_format"]["type"] == output_format for body in requests)
    assert requests[0]["tool_choice"] == "required"
    assert requests[1]["tool_choice"] == "auto"
    assert requests[0]["tools"][0]["function"]["name"] == "write_context_canvas"
    content = requests[0]["messages"][0]["content"]
    assert content[1]["image_url"]["detail"] == "low"
    if output_format == "json_object":
        assert content[-1]["text"].startswith("Return only JSON matching this schema:")
    assert requests[1]["messages"][-1]["tool_call_id"] == "write-1"
    assert json.loads(requests[1]["messages"][-1]["content"])["written"] == "scene"
    assert [(record.provider, record.usage.input_tokens, record.status) for record in usage] == [
        ("portable", 120, "completed"), ("portable", 120, "completed"),
    ]
    assert all(client.is_closed for client in clients)


def test_vision_tool_control_cancels_transport_before_canvas_write(monkeypatch):
    admitted, released, writes, usage = [], [], [], []

    class Control:
        def acquire(self, call):
            admitted.append(call)

        def release(self, call):
            released.append(call.call_id)

    def handler(request):
        admitted[-1].cancel()
        return httpx.Response(200, json=_vision_response(tool=True))

    clients = install_transport(monkeypatch, "gateway.openai.vision", handler)
    model = OpenAIVisionModel(
        "custom-vision", stream=False, provider="portable", base_url="https://vision.example/v1", api_key="offline",
    )
    with control_model_calls(Control()), capture_model_usage(usage.append), pytest.raises(ModelCallCancelledError):
        model.generate(
            [TextInput("describe")], schema_name="scene", result_type=Result, tools=[_write_tool(writes)],
        )
    assert writes == [] and released == [admitted[0].call_id]
    assert len(usage) == 1 and usage[0].status == "failed"
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize(
    "reason,content,error",
    [
        ("length", "{}", RuntimeError),
        ("content_filter", "", ContentFilterError),
        ("stop", "{}", ValidationError),
    ],
)
def test_vision_rejects_incomplete_filtered_or_invalid_output(
    monkeypatch, reason, content, error
):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "id": "result",
                "object": "chat.completion",
                "created": 0,
                "model": "vision",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": reason,
                        "message": {"role": "assistant", "content": content},
                    }
                ],
            },
        )

    clients = install_transport(monkeypatch, "gateway.openai.vision", handler)
    model = OpenAIVisionModel(
        "vision", stream=False,
        provider="custom",
        base_url="https://vision.example/v1",
        api_key="fake-test-key",
    )
    with pytest.raises(error):
        model.generate([TextInput("describe")], schema_name="scene", result_type=Result)
    assert all(client.is_closed for client in clients)


def test_audio_uses_transcriptions_multipart_protocol(monkeypatch, tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "task": "transcribe",
                "language": "zh",
                "duration": 1,
                "text": "你好",
                "segments": [{"start": 0, "end": 1, "text": "你好"}],
            },
        )

    clients = install_transport(monkeypatch, "gateway.openai.audio", handler)
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"fake-wav")
    result = transcribe_audio(
        audio,
        model="third-party-asr",
        provider="speech",
        base_url="https://speech.example/v1",
        api_key="fake-test-key",
        timeout=15,
        response_format="verbose_json",
    )
    assert result["text"] == "你好"
    assert str(requests[0].url) == "https://speech.example/v1/audio/transcriptions"
    assert b"third-party-asr" in requests[0].content
    assert b"fake-wav" in requests[0].content
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("failure,reason", [
    ("disconnect", "model_connection_error"),
    ("connect", "model_connection_error"),
    ("timeout", "model_timeout"),
    (504, "model_gateway_timeout"),
    (429, "model_service_unavailable"),
    (500, "model_service_unavailable"),
    (502, "model_service_unavailable"),
    (503, "model_service_unavailable"),
    (400, None), (401, None),
])
def test_direct_vision_classifies_real_sdk_transport_errors(monkeypatch, tmp_path, failure, reason):
    from gateway.usage import model_call_context
    monkeypatch.setenv("SEEKINO_MODEL_TRACE_DIR", str(tmp_path / "trace"))
    from openai import APIError
    from gateway.agent_model.errors import ModelFailureError
    from agent.video_parse_v2.understanding.workflow import _retryable_node_error
    requests = []
    def handler(request):
        requests.append(request)
        if failure == "disconnect":
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        if failure == "connect":
            raise httpx.ConnectError("Connection refused")
        if failure == "timeout":
            raise httpx.ReadTimeout("read timed out")
        return httpx.Response(failure, json={"error": {"message": "provider failure"}})
    clients = install_transport(monkeypatch, "gateway.openai.vision", handler)
    model = OpenAIVisionModel("vision", stream=False, provider="test", base_url="https://test.invalid/v1", api_key="offline")
    with model_call_context({"video_parse_task_id": "parse-network-test"}), pytest.raises(ModelFailureError if reason else APIError) as raised:
        model.generate([TextInput("describe")], schema_name="scene", result_type=Result)
    events_path = next((tmp_path / "trace").glob("*/events.jsonl"))
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    error_event = next(event for event in events if event["event"] == "error")
    chain = error_event["payload"]["chain"]
    assert chain[0]["type"] == type(raised.value).__name__
    if failure == "disconnect":
        assert chain[-1]["type"] == "RemoteProtocolError"
    if failure == 504:
        assert any(item.get("status_code") == 504 for item in chain)
    assert "offline" not in events_path.read_text()
    assert len(requests) == 1  # Retries belong to the node, not an unseen SDK loop.
    assert all(client.is_closed for client in clients)
    if reason:
        assert raised.value.reason == reason
        assert isinstance(raised.value.__cause__, APIError)
        assert _retryable_node_error(raised.value)
    else:
        assert not _retryable_node_error(raised.value)


def test_direct_vision_cancellation_is_not_retried_as_connection_failure(monkeypatch):
    admitted = []
    class Control:
        def acquire(self, call):
            admitted.append(call)
        def release(self, call):
            pass
    def handler(request):
        admitted[-1].cancel()
        raise httpx.RemoteProtocolError("closed by cancellation")
    install_transport(monkeypatch, "gateway.openai.vision", handler)
    model = OpenAIVisionModel("vision", stream=False, provider="test", base_url="https://test.invalid/v1", api_key="offline")
    with control_model_calls(Control()), pytest.raises(ModelCallCancelledError):
        model.generate([TextInput("describe")], schema_name="scene", result_type=Result)
