from contextlib import nullcontext
from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError, APIStatusError, APITimeoutError
from pydantic import BaseModel, ValidationError

from gateway.agent_model.errors import ModelFailureError, ModelFailureReason
from gateway.multimodal import TextInput
from gateway.openai.multimodal import DeepSeekModelError, DeepSeekMultimodalModel
from gateway.openai.vision import OpenAIVisionModel


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
