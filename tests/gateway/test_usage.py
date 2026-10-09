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
from gateway.usage import (
    ModelCallCancelledError,
    TokenUsage,
    capture_model_usage,
    control_model_calls,
    fanout_model_usage,
    model_call_context,
    track_model_call,
)


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
    DoubaoMultimodalEmbedder("embedding", request_timeout_seconds=12.5)
    injected = SimpleNamespace()
    assert DoubaoMultimodalEmbedder("embedding", client=injected)._client is injected
    assert options == [
        {"max_retries": 0},
        {"max_retries": 0, "timeout": 12.5},
    ]


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
    {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12, "input_tokens_details": {"cached_tokens": 5, "cache_write_tokens": 6}},
    {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12, "input_tokens_details": {"cache_write_tokens": 3, "cache_write_5m_tokens": 4}},
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


def test_reported_cache_write_and_ttl_are_preserved_without_changing_inclusive_totals():
    calls = []
    response = _response()
    response.usage = {
        "input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
        "input_tokens_details": {"cached_tokens": 2, "cache_write_tokens": 3,
                                 "cache_write_5m_tokens": 1, "cache_write_1h_tokens": 2},
    }
    with capture_model_usage(calls.append):
        request_sse_json(Responses(response=response), model="model", input=[])
    usage = calls[0].usage
    assert usage.input_tokens == 10 and usage.total_tokens == 12
    assert usage.cache_write_input_tokens == 3
    assert usage.cache_write_5m_input_tokens == 1 and usage.cache_write_1h_input_tokens == 2


@pytest.mark.parametrize("cost", [True, -0.1, float("nan"), float("inf"), "0.1"])
def test_sdk_cost_rejects_values_that_cannot_be_usd_estimates(cost):
    from gateway.usage import SDKModelUsage

    with pytest.raises(ValueError, match="finite nonnegative"):
        SDKModelUsage("model", None, cost)


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


def test_host_control_reserves_before_request_and_releases_same_call_id():
    events = []

    class Control:
        def acquire(self, call):
            events.append(("acquire", call.call_id))

        def release(self, call):
            events.append(("release", call.call_id))

    class Embeddings:
        def create(self, **request):
            events.append(("request", None))
            return SimpleNamespace(
                model="embedding",
                data=SimpleNamespace(embedding=[1.0, 0.0]),
                usage=SimpleNamespace(prompt_tokens=1, total_tokens=1),
            )

    calls = []
    embedder = DoubaoMultimodalEmbedder(
        "embedding",
        client=SimpleNamespace(multimodal_embeddings=Embeddings()),
    )
    with control_model_calls(Control()), capture_model_usage(calls.append):
        embedder.embed_text("query")

    assert [event[0] for event in events] == ["acquire", "request", "release"]
    assert events[0][1] == events[2][1] == calls[0].call_id


def test_usage_start_excludes_admission_wait_and_observation_follows_release(
    monkeypatch,
):
    timestamps = iter([1_000_000, 9_000_000, 12_000_000])
    monkeypatch.setattr("gateway.usage.time.time_ns", lambda: next(timestamps))
    events = []
    calls = []

    class Control:
        def acquire(self, call):
            events.append("admitted")

        def release(self, call):
            events.append("released")

    def observe(call):
        events.append("observed")
        calls.append(call)

    with control_model_calls(Control()), capture_model_usage(observe):
        with track_model_call(provider="ark", model="m", operation="embedding"):
            events.append("request")

    assert events == ["admitted", "request", "released", "observed"]
    assert calls[0].started_at == 9
    assert calls[0].ended_at == 12


def test_rejected_admission_is_not_recorded_as_a_provider_request():
    calls = []

    class Control:
        def acquire(self, call):
            raise RuntimeError("budget exhausted")

        def release(self, call):
            raise AssertionError("a rejected call has no reservation")

    provider = Responses(response=_response())
    with (
        control_model_calls(Control()),
        capture_model_usage(calls.append),
        pytest.raises(RuntimeError, match="budget exhausted"),
    ):
        request_sse_json(provider, model="model", input=[])

    assert provider.calls == []
    assert calls == []


def test_control_cancellation_closes_stream_and_rejects_late_result():
    holder = {}

    class Control:
        def acquire(self, call):
            holder["call"] = call

        def release(self, call):
            pass

    class CancellingStream(Stream):
        def __iter__(self):
            holder["call"].cancel()
            return super().__iter__()

    provider = Responses(response=_response())
    provider.events = [
        SimpleNamespace(type="response.completed", response=_response())
    ]
    provider_stream = CancellingStream(provider.events)

    def create(**request):
        provider.calls.append(request)
        provider.stream = provider_stream
        return provider_stream

    provider.create = create
    with control_model_calls(Control()), pytest.raises(ModelCallCancelledError):
        request_sse_json(provider, model="model", input=[])
    assert provider_stream.closed


def test_lifecycle_finisher_keeps_failed_usage_and_runs_once():
    events = []

    class Observer:
        def __call__(self, call):
            pytest.fail("start hook's finisher must own the terminal event")

        def on_start(self, tracker):
            events.append(("start", tracker.call_id, tracker.started_at))
            return lambda call: events.append(("end", call))

    with capture_model_usage(Observer()), pytest.raises(ValueError, match="invalid result"):
        with track_model_call(
            provider="codex", model="m", operation="query",
            granularity="sdk_turn_run", usage_source="codex_thread_total",
        ) as tracker:
            tracker.capture_response(_response())
            raise ValueError("invalid result")
    assert [event[0] for event in events] == ["start", "end"]
    call = events[1][1]
    assert call.call_id == events[0][1]
    assert call.ended_at >= call.started_at == events[0][2]
    assert call.status == "failed" and call.usage.total_tokens == 130
    assert (call.granularity, call.usage_source) == ("sdk_turn_run", "codex_thread_total")


def test_context_snapshot_merges_restores_and_ignores_later_caller_mutation():
    calls = []
    context = {"tenant_id": "w", "metadata": {"attempt": 1}}
    with capture_model_usage(calls.append), model_call_context(context):
        context["metadata"]["attempt"] = 99
        with model_call_context({"task_id": "t"}):
            with track_model_call(provider="ark", model="m", operation="query"):
                pass
        with track_model_call(provider="ark", model="m", operation="query"):
            pass
    with capture_model_usage(calls.append):
        with track_model_call(provider="ark", model="m", operation="query"):
            pass
    assert dict(calls[0].context) == {
        "tenant_id": "w", "metadata": {"attempt": 1}, "task_id": "t",
    }
    assert "task_id" not in calls[1].context
    assert not calls[2].context
    with pytest.raises(TypeError):
        calls[0].context["tenant_id"] = "other"


def test_explicit_fanout_isolates_start_and_finish_failures(caplog):
    ledger, traced = [], []

    class BrokenStart:
        def on_start(self, tracker):
            raise RuntimeError("secret exporter detail")

        def __call__(self, call):
            raise RuntimeError("secret exporter detail")

    class Tracing:
        def on_start(self, tracker):
            return traced.append

        def __call__(self, call):
            pytest.fail("finisher expected")

    with capture_model_usage(fanout_model_usage(BrokenStart(), ledger.append, Tracing())):
        with track_model_call(provider="ark", model="m", operation="query"):
            pass
    assert len(ledger) == len(traced) == 1
    assert ledger[0] is traced[0]
    assert "start observer failed" in caplog.text
    assert "observer failed" in caplog.text
    assert "secret exporter detail" not in caplog.text


def test_explicit_combine_deduplicates_nested_subscriber():
    outer, inner = [], []
    subscriber = outer.append
    with capture_model_usage(subscriber):
        with capture_model_usage(inner.append, combine=True):
            with capture_model_usage(subscriber, combine=True):
                with track_model_call(provider="ark", model="m", operation="query"):
                    pass
    assert len(outer) == len(inner) == 1
    assert outer[0] is inner[0]


def test_lifecycle_start_is_after_admission_and_rejection_emits_nothing():
    events = []

    class Control:
        def acquire(self, tracker):
            events.append("reject")
            raise RuntimeError("no admission")

        def release(self, tracker):
            pytest.fail("not admitted")

    class Observer:
        def on_start(self, tracker):
            events.append("start")

        def __call__(self, call):
            events.append("finish")

    with control_model_calls(Control()), capture_model_usage(Observer()):
        with pytest.raises(RuntimeError, match="no admission"):
            with track_model_call(provider="ark", model="m", operation="query"):
                pytest.fail("provider must not run")
    assert events == ["reject"]


def test_concurrent_host_scopes_and_finisher_restore_identity():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    barrier = Barrier(2)

    def run(identity):
        calls = []
        with model_call_context({"task_id": identity}), capture_model_usage(calls.append):
            with track_model_call(provider="ark", model="m", operation="query"):
                barrier.wait(timeout=5)
        return calls[0]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(run, ("a", "b")))
    assert [call.context["task_id"] for call in results] == ["a", "b"]
    assert results[0].call_id != results[1].call_id
    assert all(call.ended_at is not None and call.usage_source is None for call in results)


def test_old_record_constructor_remains_compatible():
    from gateway.usage import ModelCallUsage

    record = ModelCallUsage("id", "ark", "m", "query", 1, None, None, "completed")
    assert record.ended_at is None
    assert record.granularity == "provider_request"
    assert record.usage_source is None and not record.context
