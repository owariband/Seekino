import json
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from gateway.config import QueryHookConfig, load_config
from gateway.query_hook import QueryHookModelError, QueryHookModels
from gateway.usage import (
    ModelCallControlError,
    capture_model_usage,
    control_model_calls,
)


class RetrievalSlot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    retrieval_query: str | None


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("TEST_JEV_KEY", "private-test-key")
    return QueryHookConfig(router_api_key_env="TEST_JEV_KEY")


def _decision(choice="retrieve"):
    return {
        "model": "typesafe/jev-1.14",
        "answers": {"route": {"type": "choice", "choice": choice}},
        "usage": {"input_tokens": 12, "output_tokens": 0, "cost": 0.0001},
    }


def _completion(content='{"retrieval_query":"天台争吵"}'):
    return {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
    }


def _invoke(models, method, *, timeout=10):
    if method == "route":
        return models.route({"query": "private-user-text"}, {"type": "choice"}, timeout_seconds=timeout)
    return models.rewrite("private-user-text", RetrievalSlot, timeout_seconds=timeout)


def test_query_hook_defaults_are_disabled_and_survive_gateway_loading():
    config = QueryHookConfig()
    assert not config.enabled
    assert config.router_endpoint == "https://openrouter.ai/api/alpha/decisions"
    assert config.router_model == "typesafe/jev-1.14"
    assert config.router_api_key_env == "OPENROUTER_JEV_API_KEY"
    assert config.rewriter_base_url == "http://127.0.0.1:18841"
    assert config.rewriter_model == "seekino-qwen3-text-8b-rewriter"
    assert (config.router_timeout_seconds, config.rewriter_timeout_seconds, config.total_timeout_seconds) == (3, 4, 8)
    assert (config.max_output_tokens, config.history_message_limit, config.history_max_chars, config.query_max_chars) == (256, 6, 2000, 2000)
    loaded = load_config(Path(__file__).parent / "fixtures" / "legacy-config.yaml")
    assert loaded.query_hook == config


@pytest.mark.parametrize("values", [
    {"router_endpoint": "http://example.test/decisions"},
    {"router_endpoint": "https://user:password@example.test/decisions"},
    {"router_endpoint": "https://example.test/decisions?token=value"},
    {"router_endpoint": "https://example.test/decisions#fragment"},
    {"router_endpoint": "https://example.test:wrong/decisions"},
    {"rewriter_base_url": "ftp://localhost"},
    {"rewriter_base_url": "http://user:password@localhost"},
    {"rewriter_base_url": "http://localhost?key=value"},
    {"rewriter_base_url": "http://localhost#fragment"},
    {"rewriter_base_url": "http://localhost:65536"},
    {"rewriter_base_url": "http://local host"},
    {"router_model": ""},
    {"router_api_key_env": " "},
    {"router_timeout_seconds": 0},
    {"rewriter_timeout_seconds": float("inf")},
    {"total_timeout_seconds": float("nan")},
    {"history_message_limit": True},
    {"history_max_chars": 0},
    {"query_max_chars": -1},
    {"max_output_tokens": 0},
    {"unexpected": True},
])
def test_query_hook_configuration_rejects_invalid_values(values):
    with pytest.raises(ValidationError):
        QueryHookConfig.model_validate(values)


def test_query_hook_configuration_normalizes_valid_urls():
    config = QueryHookConfig(
        router_endpoint="https://example.test/decisions/",
        rewriter_base_url="https://rewriter.example.test/",
    )
    assert config.router_endpoint == "https://example.test/decisions"
    assert config.rewriter_base_url == "https://rewriter.example.test"


@pytest.mark.parametrize("choice", ["respond", "retrieve", "mixed"])
def test_route_transmits_state_and_records_only_usage(config, choice):
    requests, usages = [], []

    def respond(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer private-test-key"
        assert json.loads(request.content) == {
            "model": config.router_model, "state": {"query": "private-user-text"},
            "questions": {"route": {"type": "choice"}},
        }
        return httpx.Response(200, json=_decision(choice))

    models = QueryHookModels(config, transport=httpx.MockTransport(respond))
    with capture_model_usage(usages.append):
        assert _invoke(models, "route") == choice
    assert len(requests) == len(usages) == 1
    assert str(requests[0].url) == config.router_endpoint
    assert usages[0].provider == "openrouter"
    assert usages[0].operation == "query_hook_route"
    assert usages[0].usage.input_tokens == 12
    assert usages[0].usage.output_tokens == 0
    assert usages[0].usage.total_tokens == 12
    assert usages[0].status == "completed"
    assert "private-user-text" not in repr(usages[0])
    assert "private-test-key" not in repr(usages[0])


@pytest.mark.parametrize("usage", [
    None,
    {"input_tokens": True, "output_tokens": 0},
    {"input_tokens": -1, "output_tokens": 0},
    {"input_tokens": "12", "output_tokens": 0},
    {"input_tokens": 12.5, "output_tokens": 0},
    {"input_tokens": 12},
    {"input_tokens": 12, "output_tokens": -1},
    {"input_tokens": 12, "output_tokens": 0, "total_tokens": "invalid"},
])
def test_route_keeps_invalid_usage_unavailable_without_rejecting_decision(config, usage):
    payload = {**_decision(), "usage": usage}
    models = QueryHookModels(config, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload),
    ))
    usages = []
    with capture_model_usage(usages.append):
        assert _invoke(models, "route") == "retrieve"
    assert len(usages) == 1
    assert usages[0].status == "completed"
    assert usages[0].usage is None


@pytest.mark.parametrize("payload", [
    None, [], {"answers": []}, {"answers": {}},
    {"answers": {"route": []}},
    {"answers": {"route": {"type": "noul", "choice": "respond"}}},
    {"answers": {"route": {"type": "choice", "choice": "clarify"}}},
    {"answers": {"route": {"type": "choice", "choice": ["respond"]}}},
])
def test_route_rejects_invalid_decision_contract(config, payload):
    models = QueryHookModels(config, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload),
    ))
    with pytest.raises(QueryHookModelError, match="invalid decision response"):
        _invoke(models, "route")


def test_route_rejects_missing_key_without_request(config, monkeypatch):
    monkeypatch.delenv("TEST_JEV_KEY")
    models = QueryHookModels(config, transport=httpx.MockTransport(
        lambda request: pytest.fail("missing credentials must not make a request"),
    ))
    with pytest.raises(QueryHookModelError, match="API key is not configured"):
        _invoke(models, "route")


def test_rewrite_reuses_structured_adapter(config):
    requests, usages = [], []

    def respond(request):
        requests.append(request)
        body = json.loads(request.content)
        assert str(request.url) == f"{config.rewriter_base_url}/v1/chat/completions"
        assert "Authorization" not in request.headers
        assert body["model"] == config.rewriter_model
        assert body["temperature"] == 0
        assert body["max_tokens"] == 256
        assert body["response_format"]["json_schema"]["strict"] is True
        assert body["messages"][0]["content"] == [{"type": "text", "text": "private-user-text"}]
        return httpx.Response(200, json=_completion())

    models = QueryHookModels(config, transport=httpx.MockTransport(respond))
    with capture_model_usage(usages.append):
        assert _invoke(models, "rewrite") == RetrievalSlot(retrieval_query="天台争吵")
    assert len(requests) == len(usages) == 1
    assert usages[0].provider == "llama_cpp"


@pytest.mark.parametrize("method,configured", [("route", 3), ("rewrite", 4)])
@pytest.mark.parametrize("remaining", [1.25, 10])
def test_requests_clamp_timeout_and_disable_environment_proxies(config, monkeypatch, method, configured, remaining):
    client_type = httpx.Client
    options = []

    def client(**kwargs):
        options.append(kwargs)
        return client_type(**kwargs)

    monkeypatch.setattr("gateway.query_hook.httpx.Client", client)
    models = QueryHookModels(config, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=_decision() if method == "route" else _completion()),
    ))
    _invoke(models, method, timeout=remaining)
    assert len(options) == 1
    assert options[0]["timeout"] == min(remaining, configured)
    assert options[0]["trust_env"] is False


@pytest.mark.parametrize("method", ["route", "rewrite"])
@pytest.mark.parametrize("status", [429, 503])
def test_http_errors_preserve_status_without_response_body_or_retry(config, method, status):
    requests, usages = [], []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, json={"error": "private-test-key private-user-text"})

    models = QueryHookModels(config, transport=httpx.MockTransport(respond))
    with capture_model_usage(usages.append), pytest.raises(QueryHookModelError) as caught:
        _invoke(models, method)
    assert f"HTTP {status}" in str(caught.value)
    assert "private-" not in str(caught.value)
    assert len(requests) == len(usages) == 1
    assert usages[0].status == "failed"


@pytest.mark.parametrize("method", ["route", "rewrite"])
def test_transport_errors_preserve_type_without_error_detail_or_retry(config, method):
    requests = []

    def timeout(request):
        requests.append(request)
        raise httpx.ReadTimeout("private-test-key private-user-text", request=request)

    models = QueryHookModels(config, transport=httpx.MockTransport(timeout))
    with pytest.raises(QueryHookModelError, match="ReadTimeout") as caught:
        _invoke(models, method)
    assert "private-" not in str(caught.value)
    assert len(requests) == 1


def test_rewrite_rejects_schema_mismatch_without_echoing_output(config):
    models = QueryHookModels(config, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=_completion('{"private-user-text":"private-test-key"}')),
    ))
    with pytest.raises(QueryHookModelError, match="ValidationError") as caught:
        _invoke(models, "rewrite")
    assert "private-" not in str(caught.value)


@pytest.mark.parametrize("method", ["route", "rewrite"])
def test_model_call_control_errors_propagate_unchanged(config, method):
    rejection = ModelCallControlError("quota or cancellation")

    class Control:
        def acquire(self, call):
            raise rejection

        def release(self, call):
            pytest.fail("rejected call must not be released")

    models = QueryHookModels(config, transport=httpx.MockTransport(
        lambda request: pytest.fail("rejected call must not reach HTTP"),
    ))
    with control_model_calls(Control()), pytest.raises(ModelCallControlError) as caught:
        _invoke(models, method)
    assert caught.value is rejection


@pytest.mark.parametrize("method", ["route", "rewrite"])
@pytest.mark.parametrize("timeout", [0, -1, True, float("inf"), float("nan")])
def test_invalid_remaining_timeout_does_not_start_request(config, method, timeout):
    models = QueryHookModels(config, transport=httpx.MockTransport(
        lambda request: pytest.fail("invalid timeout must not reach HTTP"),
    ))
    with pytest.raises(ValueError, match="positive and finite"):
        _invoke(models, method, timeout=timeout)
