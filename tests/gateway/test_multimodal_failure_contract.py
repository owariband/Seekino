from contextlib import nullcontext
import logging
from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError, APIStatusError, APITimeoutError
from pydantic import BaseModel, ValidationError

from gateway.agent_model.errors import ModelFailureError, ModelFailureReason, QueryModelIncompleteError
from gateway.agent_model.multimodal import tool_model_session
from gateway.multimodal import TextInput
from gateway.openai.multimodal import DeepSeekModelError, DeepSeekMultimodalModel
from gateway.openai.vision import OpenAIVisionModel
from tests.gateway.test_multimodal_agent import Completion, Responses, _model, _response, _tools


class Result(BaseModel):
    answer: str


def adapter(protocol, endpoint, monkeypatch):
    if protocol == "responses":
        return DeepSeekMultimodalModel(
            "deepseek-flash", stream=False, base_url="https://example.test",
            api_key="test", max_output_tokens=100, request_timeout_seconds=10,
            client=SimpleNamespace(responses=endpoint),
        )
    model = OpenAIVisionModel(
        "test-model", provider="test", base_url="https://example.test", api_key="test",
        stream=False, request_timeout_seconds=10,
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=endpoint), close=lambda: None)
    monkeypatch.setattr("gateway.openai.vision.OpenAI", lambda **kwargs: nullcontext(client))
    return model


@pytest.mark.parametrize("protocol", ["chat", "responses"])
@pytest.mark.parametrize("kind,reason", [
    ("connection", ModelFailureReason.MODEL_CONNECTION_ERROR),
    ("timeout", ModelFailureReason.MODEL_TIMEOUT),
    (504, ModelFailureReason.MODEL_GATEWAY_TIMEOUT),
    (429, ModelFailureReason.MODEL_SERVICE_UNAVAILABLE),
    (500, ModelFailureReason.MODEL_SERVICE_UNAVAILABLE),
    (502, ModelFailureReason.MODEL_SERVICE_UNAVAILABLE),
    (503, ModelFailureReason.MODEL_SERVICE_UNAVAILABLE),
    (400, None), (401, None), (403, None),
])
def test_parse_protocols_classify_sdk_failures_identically(protocol, kind, reason, monkeypatch):
    request = httpx.Request("POST", "https://example.test")
    error = (APIConnectionError(request=request) if kind == "connection" else
             APITimeoutError(request=request) if kind == "timeout" else
             APIStatusError("provider rejection", response=httpx.Response(kind, request=request), body=None))

    def create(**kwargs):
        raise error

    model = adapter(protocol, SimpleNamespace(create=create), monkeypatch)
    expected = ModelFailureError if reason else (APIStatusError, DeepSeekModelError)
    with pytest.raises(expected) as raised:
        model.generate([TextInput("test")], schema_name="result", result_type=Result)
    assert getattr(raised.value, "reason", None) == reason
    if reason:
        assert raised.value.__cause__ is error


@pytest.mark.parametrize("protocol", ["chat", "responses"])
def test_invalid_json_keeps_original_candidate_and_location(protocol, monkeypatch):
    raw = '{"answer":"keep this candidate",}'
    response = (SimpleNamespace(status="completed", output_text=raw, output=[]) if protocol == "responses"
                else SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=raw))]))
    model = adapter(protocol, SimpleNamespace(create=lambda **kwargs: response), monkeypatch)
    with pytest.raises(ValidationError) as raised:
        model.generate([TextInput("test")], schema_name="result", result_type=Result)
    detail = raised.value.errors()[0]
    assert detail["type"] == "json_invalid"
    assert detail["input"] == raw
    assert "line 1 column" in detail["msg"]


def _connection_error(error_type, cause):
    error = error_type(request=httpx.Request("POST", "https://example.test"))
    error.__cause__ = cause
    return error


def _connect_cause(cause_type):
    return cause_type("connection failed", request=httpx.Request("POST", "https://example.test"))


def _request_turn(model, *, check_control=None, budget=30):
    tools = _tools({}, [])
    with tool_model_session(
        model, [{"type": "input_text", "text": "Analyze the video."}],
        schema_name="completion", result_type=Completion, tools=tools,
        request_timeout_seconds=None, check_control=check_control,
        context_window_policy=None, max_input_chars=None,
        fallback_model=None, fallback_inputs=None, fallback_on_error=None,
    ) as session:
        session.request_turn(
            [{"role": "user", "content": session.content}], [], "auto", budget, set(), False,
        )
        return session.result


@pytest.mark.parametrize(("error_type", "cause_type"), [
    (APIConnectionError, httpx.ConnectError),
    (APIConnectionError, httpx.ConnectTimeout),
    (APITimeoutError, httpx.ConnectTimeout),
])
def test_tool_loop_retries_a_request_that_never_reached_the_provider(error_type, cause_type, monkeypatch):
    delays = []
    monkeypatch.setattr("gateway.agent_model.multimodal.sleep", delays.append)
    responses = Responses([_connection_error(error_type, _connect_cause(cause_type)), _response()])

    assert _request_turn(_model("deepseek", responses, stream=False)).done

    assert len(responses.requests) == 2
    assert delays == [1.0]


def test_tool_loop_raises_the_existing_error_after_two_connect_retries(monkeypatch, caplog):
    delays = []
    monkeypatch.setattr("gateway.agent_model.multimodal.sleep", delays.append)
    errors = [_connection_error(APIConnectionError, _connect_cause(httpx.ConnectError)) for _ in range(3)]
    responses = Responses(list(errors))

    with caplog.at_level(logging.WARNING, logger="gateway.agent_model.multimodal"), pytest.raises(
        QueryModelIncompleteError, match="Multimodal Responses request failed",
    ) as raised:
        _request_turn(_model("deepseek", responses, stream=False))

    assert raised.value.__cause__ is errors[-1]
    assert len(responses.requests) == 3
    assert delays == [1.0, 2.0]
    retries = [record.getMessage() for record in caplog.records if "event=multimodal_connect_retry" in record.getMessage()]
    assert len(retries) == 2
    assert "attempt=1" in retries[0] and "error_type=APIConnectionError" in retries[0]
    assert "attempt=2" in retries[1]


@pytest.mark.parametrize("kind", ["http_429", "read_timeout", "bare_connection"])
def test_tool_loop_never_retries_errors_after_a_connection_was_established(kind, monkeypatch):
    delays = []
    monkeypatch.setattr("gateway.agent_model.multimodal.sleep", delays.append)
    request = httpx.Request("POST", "https://example.test")
    error = (
        APIStatusError("provider rejection", response=httpx.Response(429, request=request), body=None)
        if kind == "http_429"
        else _connection_error(APITimeoutError, httpx.ReadTimeout("read timed out", request=request))
        if kind == "read_timeout"
        else APIConnectionError(request=request)
    )
    responses = Responses([error])

    with pytest.raises(QueryModelIncompleteError, match="Multimodal Responses request failed") as raised:
        _request_turn(_model("deepseek", responses, stream=False))

    assert raised.value.__cause__ is error
    assert len(responses.requests) == 1
    assert delays == []


def test_tool_loop_does_not_retry_when_the_backoff_exceeds_the_call_budget(monkeypatch):
    delays = []
    monkeypatch.setattr("gateway.agent_model.multimodal.sleep", delays.append)
    responses = Responses([_connection_error(APIConnectionError, _connect_cause(httpx.ConnectError))])

    with pytest.raises(QueryModelIncompleteError, match="Multimodal Responses request failed"):
        _request_turn(_model("deepseek", responses, stream=False, timeout=0.5), budget=0.5)

    assert len(responses.requests) == 1
    assert delays == []


def test_tool_loop_checks_control_before_retrying_a_connect_failure(monkeypatch):
    delays = []
    monkeypatch.setattr("gateway.agent_model.multimodal.sleep", delays.append)
    responses = Responses([_connection_error(APIConnectionError, _connect_cause(httpx.ConnectError)), _response()])

    def check():
        if responses.requests:
            raise RuntimeError("execution cancelled")

    with pytest.raises(RuntimeError, match="execution cancelled"):
        _request_turn(_model("deepseek", responses, stream=False), check_control=check)

    assert len(responses.requests) == 1
    assert delays == []
