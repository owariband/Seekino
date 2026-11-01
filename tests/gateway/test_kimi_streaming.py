import json
from pathlib import Path

import httpx
from openai import OpenAI
import pytest
from pydantic import BaseModel, ValidationError

from agent.utils.tool import build_agent_tool
from agent.utils.tool_result import JsonToolResult
from gateway.agent_model.errors import ModelFailureError, ModelFailureReason
from gateway.config import ModelBinding, OpenAIModelSourceConfig, load_config
from gateway.errors import ContentFilterError
from gateway.model_factory import GatewayClient
from gateway.multimodal import ImageInput, TextInput
from gateway.usage import ModelCallCancelledError, capture_model_usage, control_model_calls, model_call_context


class Result(BaseModel):
    summary: str


class CanvasArguments(BaseModel):
    value: str


def _write_tool(writes):
    def write(value):
        writes.append(value)
        return JsonToolResult(success=True, payload={"written": value})
    return build_agent_tool(
        func=write, name="write_context_canvas", description="Write the analysis Canvas.",
        args_schema=CanvasArguments, maximum_result_bytes=1024,
    ).as_langchain_tool()


def _chunk(delta=None, *, finish=None, usage=None):
    return {
        "id": "stream-result", "object": "chat.completion.chunk", "created": 0,
        "model": "k3.1-pro-returned", "choices": ([] if usage is not None else [{
            "index": 0, "delta": delta or {}, "finish_reason": finish,
        }]), "usage": usage,
    }


def _json_chunks(*, finish="stop", text='{"summary":"scene"}'):
    return [
        _chunk({"role": "assistant", "content": None, "reasoning_content": "inspect "}),
        _chunk({"reasoning_content": "frames"}),
        _chunk({"content": text[:9]}), _chunk({"content": text[9:]}),
        _chunk(finish=finish),
        _chunk(usage={
            "prompt_tokens": 120, "completion_tokens": 20, "total_tokens": 140,
            "prompt_tokens_details": {"cached_tokens": 10},
            "completion_tokens_details": {"reasoning_tokens": 8},
        }),
    ]


def _tool_chunks():
    return [
        _chunk({"role": "assistant", "content": None, "reasoning_content": "inspect "}),
        _chunk({"reasoning_content": "frames", "tool_calls": [
            {"index": i, "id": f"write-{i}", "type": "function", "function": {
                "name": "write_context_canvas", "arguments": '{"value":"',
            }} for i in range(2)
        ]}),
        _chunk({"tool_calls": [
            {"index": 0, "function": {"arguments": 'scene"}'}},
            {"index": 1, "function": {"arguments": 'detail"}'}},
        ]}),
        _chunk(finish="tool_calls"),
        _chunk(usage={"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130}),
    ]


def _structured_finish_chunks():
    return [
        _chunk({"role": "assistant", "content": None, "tool_calls": [{
            "index": 0, "id": "finish", "type": "function", "function": {
                "name": "Result", "arguments": '{"summary":"scene"}',
            },
        }]}),
        _chunk(finish="tool_calls"),
        _json_chunks()[-1],
    ]


class SSEBody(httpx.SyncByteStream):
    def __init__(self, chunks, *, before_chunk=lambda: None):
        self.chunks = chunks
        self.before_chunk = before_chunk
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            self.before_chunk()
            if isinstance(chunk, BaseException):
                raise chunk
            yield b"data: " + json.dumps(chunk).encode() + b"\n\n"
        yield b"data: [DONE]\n\n"

    def close(self):
        self.closed = True


def _transport(monkeypatch, handler):
    clients = []

    def create_client(**kwargs):
        assert kwargs["max_retries"] == 0
        http = httpx.Client(transport=httpx.MockTransport(handler))
        clients.append(http)
        return OpenAI(**kwargs, http_client=http)

    monkeypatch.setattr("gateway.openai.vision.OpenAI", create_client)
    return clients


def _gateway(profile="kimi"):
    config = load_config(Path(__file__).parent / "fixtures" / "legacy-config.yaml")
    config.model_sources["selected"] = OpenAIModelSourceConfig(
        profile=profile, provider="configured-provider",
        base_url="https://kimi.example/coding/v1", api_key_env="KIMI_TEST_KEY",
    )
    return GatewayClient(config, environ={"KIMI_TEST_KEY": "offline"})


def _model(gateway):
    return gateway.create_generative_model(ModelBinding(
        source="selected", model="k3.1-pro", request_timeout_seconds=10,
        max_output_tokens=30000, supports_vision_input=True,
    ))


def _response(body):
    return httpx.Response(
        200, headers={"content-type": "text/event-stream", "x-request-id": "kimi-request"},
        stream=body,
    )


@pytest.mark.parametrize("harness", [None, "legacy", "deepagents"])
def test_factory_kimi_streams_json_and_tools_with_usage_and_reasoning(monkeypatch, tmp_path, harness):
    requests, bodies, writes, usage = [], [], [], []

    def handler(request):
        requests.append(json.loads(request.content))
        is_tool_turn = harness is not None and len(requests) == 1

        def before_chunk():
            if is_tool_turn:
                assert writes == []  # No tool execution, even after complete argument fragments.

        chunks = (_tool_chunks() if is_tool_turn else
                  _structured_finish_chunks() if harness == "deepagents" else _json_chunks())
        body = SSEBody(chunks, before_chunk=before_chunk)
        bodies.append(body)
        return _response(body)

    clients = _transport(monkeypatch, handler)
    image = tmp_path / "frame.png"
    image.write_bytes(b"test-image")
    gateway = _gateway()
    try:
        with capture_model_usage(usage.append):
            result = _model(gateway).generate(
                [TextInput("describe"), ImageInput(image, detail="low")],
                schema_name="scene", result_type=Result,
                tools=[_write_tool(writes)] if harness else [], max_model_calls=2,
                **({"tool_harness": harness} if harness else {}),
            )
    finally:
        gateway.close()
    assert result.summary == "scene"
    assert len(requests) == (2 if harness else 1)
    assert all(body["stream"] is True and body["stream_options"] == {"include_usage": True} for body in requests)
    assert all(body["model"] == "k3.1-pro" and body["max_tokens"] == 30000 for body in requests)
    if harness == "deepagents":
        assert all("response_format" not in body for body in requests)
    else:
        assert all(body["response_format"]["type"] == "json_schema" for body in requests)
    user = next(item for item in requests[0]["messages"] if item["role"] == "user")
    assert user["content"][1]["image_url"]["detail"] == "low"
    if harness:
        assert writes == ["scene", "detail"]
        assistant = next(item for item in requests[1]["messages"] if item["role"] == "assistant")
        assert assistant["reasoning_content"] == "inspect frames"
        assert [json.loads(tool["function"]["arguments"]) for tool in assistant["tool_calls"]] == [
            {"value": "scene"}, {"value": "detail"},
        ]
    assert [record.usage.input_tokens for record in usage] == ([100, 120] if harness else [120])
    assert all(record.model == "k3.1-pro-returned" and record.request_id == "kimi-request" for record in usage)
    assert all(record.status == "completed" for record in usage)
    assert usage[-1].usage.output_tokens == 20
    assert usage[-1].usage.reasoning_tokens == 8
    assert usage[-1].usage.cached_input_tokens == 10
    assert all(body.closed for body in bodies) and all(client.is_closed for client in clients)


@pytest.mark.parametrize("with_tools", [False, True])
@pytest.mark.parametrize("failure,reason", [
    (None, ModelFailureReason.MODEL_CONNECTION_ERROR),
    (httpx.ReadError("disconnected"), ModelFailureReason.MODEL_CONNECTION_ERROR),
    (httpx.RemoteProtocolError("incomplete body"), ModelFailureReason.MODEL_CONNECTION_ERROR),
    (httpx.ReadTimeout("idle timeout"), ModelFailureReason.MODEL_TIMEOUT),
])
def test_stream_interruption_never_returns_partial_json_or_runs_tools(monkeypatch, with_tools, failure, reason):
    chunks = (_tool_chunks()[:3] if with_tools else _json_chunks()[:4])
    body = SSEBody(chunks + ([failure] if failure is not None else []))
    requests, writes, usage = [], [], []

    def handler(request):
        requests.append(request)
        return _response(body)

    clients = _transport(monkeypatch, handler)
    gateway = _gateway()
    try:
        with capture_model_usage(usage.append), pytest.raises(ModelFailureError) as caught:
            _model(gateway).generate(
                [TextInput("describe")], schema_name="scene", result_type=Result,
                tools=[_write_tool(writes)] if with_tools else [],
            )
    finally:
        gateway.close()
    assert caught.value.reason == reason
    assert writes == [] and len(requests) == 1
    assert usage[0].status == "failed"
    assert body.closed and all(client.is_closed for client in clients)


@pytest.mark.parametrize("finish,text,error", [
    ("length", '{"summary":"scene"}', ModelFailureError),
    ("content_filter", "", ContentFilterError),
    ("stop", "{}", ValidationError),
])
def test_stream_keeps_finish_reason_and_schema_validation(monkeypatch, finish, text, error):
    body = SSEBody(_json_chunks(finish=finish, text=text))
    _transport(monkeypatch, lambda request: _response(body))
    gateway = _gateway()
    try:
        with pytest.raises(error):
            _model(gateway).generate([TextInput("describe")], schema_name="scene", result_type=Result)
    finally:
        gateway.close()
    assert body.closed


@pytest.mark.parametrize("with_tools", [False, True])
def test_stream_cancellation_closes_transport_before_any_result(monkeypatch, with_tools):
    admitted, writes = [], []

    class Control:
        def acquire(self, call):
            admitted.append(call)

        def release(self, call):
            pass

    body = SSEBody(_tool_chunks() if with_tools else _json_chunks(), before_chunk=lambda: admitted[0].cancel())
    clients = _transport(monkeypatch, lambda request: _response(body))
    gateway = _gateway()
    try:
        with control_model_calls(Control()), pytest.raises(ModelCallCancelledError):
            _model(gateway).generate(
                [TextInput("describe")], schema_name="scene", result_type=Result,
                tools=[_write_tool(writes)] if with_tools else [],
            )
    finally:
        gateway.close()
    assert writes == [] and body.closed and all(client.is_closed for client in clients)


def test_stream_checks_total_budget_even_while_chunks_continue(monkeypatch):
    clock = [0]
    monkeypatch.setattr("gateway.openai.streaming.monotonic", lambda: clock[0])

    def advance():
        clock[0] += 4

    body = SSEBody(_json_chunks(), before_chunk=advance)
    _transport(monkeypatch, lambda request: _response(body))
    gateway = _gateway()
    try:
        with pytest.raises(ModelFailureError) as caught:
            _model(gateway).generate([TextInput("describe")], schema_name="scene", result_type=Result)
    finally:
        gateway.close()
    assert caught.value.reason == ModelFailureReason.MODEL_TIMEOUT and body.closed


def test_other_profile_keeps_non_streaming_even_if_using_same_endpoint(monkeypatch):
    def handler(request):
        body = json.loads(request.content)
        assert body["stream"] is False and "stream_options" not in body
        return httpx.Response(200, json={
            "id": "result", "object": "chat.completion", "created": 0, "model": "k3.1-pro",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": '{"summary":"scene"}',
            }}],
        })

    _transport(monkeypatch, handler)
    gateway = _gateway("standard")
    try:
        assert _model(gateway).generate(
            [TextInput("describe")], schema_name="scene", result_type=Result,
        ).summary == "scene"
    finally:
        gateway.close()


def test_stream_records_incremental_and_final_diagnostics(monkeypatch, tmp_path):
    monkeypatch.setenv("SEEKINO_MODEL_TRACE_DIR", str(tmp_path))
    body = SSEBody(_json_chunks())
    _transport(monkeypatch, lambda request: _response(body))
    gateway = _gateway()
    try:
        with model_call_context({"video_parse_task_id": "parse-offline"}):
            _model(gateway).generate([TextInput("describe")], schema_name="scene", result_type=Result)
    finally:
        gateway.close()
    path, = tmp_path.glob("*/events.jsonl")
    events = [json.loads(line) for line in path.read_text().splitlines()]
    assert events[0]["payload"]["request"]["stream"] is True
    assert [event["event"] for event in events] == ["request", *(["sse"] * 6), "response", "completed"]
    assert events[-2]["payload"]["choices"][0]["message"]["content"] == '{"summary":"scene"}'
    assert events[-2]["provider_request_id"] == "kimi-request"


@pytest.mark.parametrize("finish", ["length", "content_filter"])
def test_stream_rejected_tool_turn_never_executes_complete_arguments(monkeypatch, finish):
    chunks = _tool_chunks()
    chunks[3] = _chunk(finish=finish)
    body = SSEBody(chunks)
    _transport(monkeypatch, lambda request: _response(body))
    writes = []
    gateway = _gateway()
    try:
        with pytest.raises(ContentFilterError if finish == "content_filter" else ModelFailureError):
            _model(gateway).generate(
                [TextInput("describe")], schema_name="scene", result_type=Result, tools=[_write_tool(writes)],
            )
    finally:
        gateway.close()
    assert writes == [] and body.closed


def test_stream_honors_host_control_between_chunks(monkeypatch):
    delivered = []
    body = SSEBody(_json_chunks(), before_chunk=lambda: delivered.append(True))
    _transport(monkeypatch, lambda request: _response(body))

    def check():
        if len(delivered) >= 2:
            raise RuntimeError("task lease was revoked")

    gateway = _gateway()
    try:
        with pytest.raises(RuntimeError, match="task lease was revoked"):
            _model(gateway).generate(
                [TextInput("describe")], schema_name="scene", result_type=Result, check_control=check,
            )
    finally:
        gateway.close()
    assert len(delivered) == 2 and body.closed
