import json
from collections.abc import Callable
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from pydantic import BaseModel, PydanticInvalidForJsonSchema, ValidationError

from gateway.ark.embedding import DoubaoMultimodalEmbedder
from gateway.ark.multimodal import ArkMultimodalModel
from gateway.ark.sse import request_sse_json, request_sse_text
from gateway.errors import SSEProxyError
from gateway.local.llama_cpp import LlamaCppMultimodalModel
from gateway.local.ollama import OllamaMultimodalModel
from gateway.multimodal import TextInput
from gateway.openai.multimodal import DeepSeekModelError, DeepSeekMultimodalModel
from gateway.usage import TokenUsage, capture_model_usage


class Answer(BaseModel):
    summary: str


def _response(text='{"summary":"ok"}', **fields):
    return SimpleNamespace(
        id="response-id", model="actual-model", status="completed",
        output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", text=text)])],
        output_text=text,
        usage=SimpleNamespace(
            input_tokens=100, output_tokens=30, total_tokens=130,
            input_tokens_details=SimpleNamespace(cached_tokens=40, audio_tokens=10, audio_cached_tokens=5),
            output_tokens_details=SimpleNamespace(reasoning_tokens=20),
        ),
        **fields,
    )


class Stream:
    def __init__(self, events):
        self.events = events
        self.closed = False
        self.response = httpx.Response(200, headers={"X-Client-Request-Id": "request-123"})

    def __iter__(self):
        return iter(self.events)

    def close(self):
        self.closed = True


class Responses:
    def __init__(self, response=None, events=None, error=None):
        self.response = response
        self.events = events
        self.error = error
        self.calls = []
        self.stream = None

    def create(self, **request):
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        if request.get("stream"):
            events = self.events or [SimpleNamespace(type="response.completed", response=self.response)]
            self.stream = Stream(events)
            return self.stream
        return self.response


def test_ark_captures_one_raw_usage_before_result_validation():
    calls = []
    provider = Responses(response=_response())
    model = ArkMultimodalModel("requested-model", client=SimpleNamespace(responses=provider))
    with capture_model_usage(calls.append):
        assert model.generate([TextInput("question")], schema_name="video_timeline", result_type=Answer).summary == "ok"
    assert len(provider.calls) == len(calls) == 1
    call = calls[0]
    assert UUID(call.call_id)
    assert isinstance(call.started_at, int) and call.started_at >= 0
    assert (call.provider, call.model, call.operation, call.request_id, call.status) == (
        "ark", "actual-model", "video_timeline", "request-123", "completed",
    )
    assert call.usage == TokenUsage(100, 30, 130, 40, 20, audio_input_tokens=10, cached_audio_input_tokens=5)
    assert "usage_call" not in provider.calls[0]
    assert provider.stream.closed


@pytest.mark.parametrize("text,error", [("invalid JSON", SSEProxyError), ('{"wrong":"field"}', ValidationError)])
def test_invalid_output_keeps_raw_provider_usage(text, error):
    calls = []
    provider = Responses(response=_response(text))
    model = ArkMultimodalModel("model", client=SimpleNamespace(responses=provider))
    with capture_model_usage(calls.append), pytest.raises(error):
        model.generate([TextInput("question")], schema_name="video_timeline", result_type=Answer)
    assert len(calls) == len(provider.calls) == 1
    assert calls[0].status == "failed"
    assert calls[0].usage.total_tokens == 130


@pytest.mark.parametrize("terminal", ["response.failed", "response.incomplete"])
def test_non_completed_sse_captures_terminal_usage(terminal):
    calls = []
    response = _response(error=SimpleNamespace(code="InternalError", message="failed"),
                         incomplete_details=SimpleNamespace(reason="max_output_tokens"))
    response.status = terminal.split(".")[1]
    provider = Responses(events=[SimpleNamespace(type=terminal, response=response)])
    with capture_model_usage(calls.append), pytest.raises(SSEProxyError):
        request_sse_json(provider, model="model", input=[])
    assert len(calls) == 1
    assert calls[0].status == "failed"
    assert calls[0].usage.total_tokens == 130
    assert provider.stream.closed


def test_stream_close_records_missing_usage_once_and_preserves_delta():
    calls = []
    provider = Responses(events=[SimpleNamespace(type="response.output_text.delta", delta="first")])
    with capture_model_usage(calls.append):
        stream = request_sse_text(provider, model="model", input=[])
        assert next(stream) == "first"
        assert not calls
        stream.close()
    assert len(calls) == 1
    assert calls[0].usage is None
    assert calls[0].status == "failed"
    assert provider.stream.closed


@pytest.mark.parametrize("mode", ["create_error", "iteration_error", "missing_terminal"])
def test_failed_request_without_usage_is_unknown_not_zero(mode):
    calls = []
    provider = Responses(events=[SimpleNamespace(type="response.created", response=SimpleNamespace(id="response-only"))])
    if mode == "create_error":
        provider.error = RuntimeError("network")
    elif mode == "iteration_error":
        def events():
            yield SimpleNamespace(type="response.created", response=SimpleNamespace(id="response-only"))
            raise RuntimeError("network")
        provider.events = events()
    with capture_model_usage(calls.append), pytest.raises((RuntimeError, SSEProxyError)):
        request_sse_json(provider, model="model", input=[])
    assert len(calls) == len(provider.calls) == 1
    assert calls[0].status == "failed"
    assert calls[0].usage is None
    assert calls[0].request_id == (None if mode == "create_error" else "request-123")


def test_observer_failure_does_not_change_result_or_retry(caplog):
    provider = Responses(response=_response())
    def observer(call):
        raise RuntimeError("storage failure")
    with capture_model_usage(observer):
        assert request_sse_json(provider, model="model", input=[]) == {"summary": "ok"}
    assert len(provider.calls) == 1
    assert "Model usage observer failed" in caplog.text
    assert "storage failure" not in caplog.text


@pytest.mark.parametrize("status,text", [("completed", '{"summary":"ok"}'), ("incomplete", "partial"), ("completed", "invalid")])
def test_deepseek_captures_usage_even_on_invalid_or_incomplete_output(status, text):
    calls = []
    response = _response(text, _request_id="deepseek-request")
    response.status = status
    provider = Responses(response=response)
    model = DeepSeekMultimodalModel("model", base_url="https://example.test", api_key="test-key",
                                   max_output_tokens=1000, request_timeout_seconds=10,
                                   client=SimpleNamespace(responses=provider))
    with capture_model_usage(calls.append):
        if status == "completed" and text.startswith("{"):
            assert model.generate([TextInput("question")], schema_name="neutral_visual_evidence", result_type=Answer).summary == "ok"
        else:
            with pytest.raises(DeepSeekModelError):
                model.generate([TextInput("question")], schema_name="neutral_visual_evidence", result_type=Answer)
    assert len(calls) == len(provider.calls) == 1
    assert calls[0].provider == "deepseek"
    assert calls[0].request_id == "deepseek-request"
    assert calls[0].usage.reasoning_tokens == 20
    assert calls[0].status == ("completed" if status == "completed" and text.startswith("{") else "failed")


def test_embedding_uses_sdk_text_image_details_without_double_counting_query_alias(tmp_path):
    calls = []
    class Embeddings:
        requests = []
        def create(self, **request):
            self.requests.append(request)
            video = request["input"][0]["type"] == "video_url"
            return SimpleNamespace(
                data=SimpleNamespace(embedding=[0.1, 0.2]), model="embedding-actual", _request_id="embedding-request",
                usage=SimpleNamespace(prompt_tokens=30, total_tokens=30,
                                      prompt_tokens_details=SimpleNamespace(text_tokens=0 if video else 30,
                                                                           image_tokens=30 if video else 0)),
            )
    provider = Embeddings()
    embedder = DoubaoMultimodalEmbedder("embedding", client=SimpleNamespace(multimodal_embeddings=provider))
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake-video")
    with capture_model_usage(calls.append):
        embedder.embed_video(video)
        embedder.embed_query("query")
    assert len(calls) == len(provider.requests) == 2
    assert [call.operation for call in calls] == ["video_embedding", "text_embedding"]
    assert calls[0].usage == TokenUsage(30, 0, 30, None, None, text_input_tokens=0, image_input_tokens=30)
    assert calls[1].usage == TokenUsage(30, 0, 30, None, None, text_input_tokens=30, image_input_tokens=0)
    assert calls[0].model == "embedding-actual"


def test_embedding_default_client_disables_hidden_sdk_retries(monkeypatch):
    options = []
    def client(**kwargs):
        options.append(kwargs)
        return SimpleNamespace()
    monkeypatch.setattr("gateway.ark.embedding.Ark.volc", client)
    DoubaoMultimodalEmbedder("embedding")
    injected = SimpleNamespace()
    assert DoubaoMultimodalEmbedder("embedding", client=injected)._client is injected
    assert options == [{"max_retries": 0}]


def test_invalid_usage_metadata_is_not_used_as_a_token_estimate(caplog):
    calls = []
    response = _response()
    response.usage.input_tokens = -1
    with capture_model_usage(calls.append):
        assert request_sse_json(Responses(response=response), model="model", input=[]) == {"summary": "ok"}
    assert calls[0].usage is None
    assert "invalid token usage" in caplog.text


def test_nested_observers_restore_scope_and_missing_usage_stays_none():
    outer, inner = [], []
    response = _response()
    response.usage = None
    with capture_model_usage(outer.append):
        with capture_model_usage(inner.append):
            request_sse_json(Responses(response=response), model="model", input=[])
        request_sse_json(Responses(response=response), model="model", input=[])
    request_sse_json(Responses(response=response), model="model", input=[])
    assert len(outer) == len(inner) == 1
    assert outer[0].usage is None
    assert inner[0].usage is None
    assert outer[0].call_id != inner[0].call_id


def test_nonterminal_initial_zero_usage_does_not_turn_interrupted_call_into_zero_cost():
    calls = []
    response = _response()
    response.usage = SimpleNamespace(input_tokens=0, output_tokens=0, total_tokens=0)
    provider = Responses(events=[SimpleNamespace(type="response.created", response=response)])
    with capture_model_usage(calls.append), pytest.raises(SSEProxyError, match="without a terminal event"):
        request_sse_json(provider, model="model", input=[])
    assert len(calls) == 1
    assert calls[0].status == "failed"
    assert calls[0].usage is None


@pytest.mark.parametrize("usage", [
    {"input_tokens": 10, "output_tokens": 2, "total_tokens": 13},
    {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12, "input_tokens_details": {"cached_tokens": 11}},
    {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12, "input_tokens_details": {"audio_tokens": 11}},
    {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12, "input_tokens_details": {"audio_tokens": 3, "audio_cached_tokens": 4}},
    {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12, "output_tokens_details": {"reasoning_tokens": 3}},
    {"input_tokens": True, "output_tokens": 2, "total_tokens": 3},
])
def test_invalid_counter_totals_and_subsets_are_unknown(usage):
    calls = []
    response = _response()
    response.usage = usage
    with capture_model_usage(calls.append):
        request_sse_json(Responses(response=response), model="model", input=[])
    assert calls[0].usage is None


def test_unreported_cache_reasoning_and_modality_counts_stay_unknown():
    calls = []
    response = _response()
    response.usage = {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}
    with capture_model_usage(calls.append):
        request_sse_json(Responses(response=response), model="model", input=[])
    assert calls[0].usage == TokenUsage(10, 2, 12, None, None)


@pytest.mark.parametrize("provider", ["ark", "deepseek", "ollama", "llama_cpp"])
def test_local_schema_validation_failure_does_not_record_a_request(provider):
    class UnsupportedSchema(BaseModel):
        callback: Callable[[], None]
    responses = Responses(response=_response())
    client = SimpleNamespace(responses=responses)
    options = dict(model="model", base_url="https://example.test", max_output_tokens=100,
                   request_timeout_seconds=10, client=client)
    if provider == "ark":
        model = ArkMultimodalModel("model", client=client)
    elif provider == "deepseek":
        model = DeepSeekMultimodalModel(api_key="test-key", **options)
    elif provider == "ollama":
        model = OllamaMultimodalModel(**options)
    else:
        model = LlamaCppMultimodalModel(expected_server_video_fps=1, **options)
    calls = []
    with capture_model_usage(calls.append), pytest.raises(PydanticInvalidForJsonSchema):
        model.generate([TextInput("question")], schema_name="video_timeline", result_type=UnsupportedSchema)
    assert responses.calls == []
    assert calls == []


def test_embedding_validation_failure_retains_provider_usage():
    calls = []
    class Embeddings:
        requests = 0
        def create(self, **request):
            self.requests += 1
            return SimpleNamespace(data=SimpleNamespace(embedding=[]),
                                   usage=SimpleNamespace(prompt_tokens=8, total_tokens=8))
    provider = Embeddings()
    embedder = DoubaoMultimodalEmbedder("embedding", client=SimpleNamespace(multimodal_embeddings=provider))
    with capture_model_usage(calls.append), pytest.raises(RuntimeError, match="empty vector"):
        embedder.embed_text("text")
    assert len(calls) == provider.requests == 1
    assert calls[0].status == "failed"
    assert calls[0].usage == TokenUsage(8, 0, 8, None, None)
