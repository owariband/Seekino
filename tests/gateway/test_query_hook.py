import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from gateway.config import QueryHookConfig, load_config
from gateway.query_hook import QueryHookModelError, QueryHookModels
from gateway.usage import (
    ModelCallControlError,
    capture_model_usage,
    control_model_calls,
)


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


def _invoke(models, *, timeout=10):
    return models.route({"query": "private-user-text"}, {"type": "choice"}, timeout_seconds=timeout)


def test_video_question_validates_its_own_labels(config):
    from agent.video_parse_v2.content_route import TYPE_QUESTION

    def ask(choice):
        models = QueryHookModels(config, transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=_decision(choice))))
        return models.route({"transcript": []}, TYPE_QUESTION, timeout_seconds=2,
                            choices=tuple(TYPE_QUESTION["criteria"]), operation="video_content_route")

    assert ask("clip") == "clip"
    with pytest.raises(QueryHookModelError, match="invalid decision"):
        ask("retrieve")


def test_query_hook_defaults_are_disabled_and_survive_gateway_loading():
    config = QueryHookConfig()
    assert not config.enabled
    assert config.rewriter_enabled
    assert config.router_endpoint == "https://openrouter.ai/api/alpha/decisions"
    assert config.router_model == "typesafe/jev-1.14"
    assert config.router_api_key_env == "OPENROUTER_JEV_API_KEY"
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
    {"router_model": ""},
    {"router_api_key_env": " "},
    {"router_timeout_seconds": 0},
    {"rewriter_timeout_seconds": float("inf")},
    {"total_timeout_seconds": float("nan")},
    {"rewriter_enabled": "true"},
    {"history_message_limit": True},
    {"history_max_chars": 0},
    {"query_max_chars": -1},
    {"max_output_tokens": 0},
    {"unexpected": True},
])
def test_query_hook_configuration_rejects_invalid_values(values):
    with pytest.raises(ValidationError):
        QueryHookConfig.model_validate(values)


@pytest.mark.parametrize("values", [
    {"rewriter_base_url": "http://127.0.0.1:18841"},
    {"rewriter_model": "seekino-qwen3-text-8b-rewriter"},
    {"rewriter_stream": False},
    {"rewriter_temperature": 0.0},
])
def test_query_hook_configuration_rejects_removed_llama_cpp_fields(values):
    with pytest.raises(ValidationError):
        QueryHookConfig.model_validate(values)


def test_query_hook_configuration_normalizes_valid_urls():
    config = QueryHookConfig(
        router_endpoint="https://example.test/decisions/",
    )
    assert config.router_endpoint == "https://example.test/decisions"


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
        assert _invoke(models) == choice
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
        assert _invoke(models) == "retrieve"
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
        _invoke(models)


def test_route_rejects_missing_key_without_request(config, monkeypatch):
    monkeypatch.delenv("TEST_JEV_KEY")
    models = QueryHookModels(config, transport=httpx.MockTransport(
        lambda request: pytest.fail("missing credentials must not make a request"),
    ))
    with pytest.raises(QueryHookModelError, match="API key is not configured"):
        _invoke(models)


@pytest.mark.parametrize("remaining", [1.25, 10])
def test_requests_clamp_timeout_and_disable_environment_proxies(config, monkeypatch, remaining):
    client_type = httpx.Client
    options = []

    class client(client_type):
        def __init__(self, **kwargs):
            options.append(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr("gateway.query_hook.httpx.Client", client)
    models = QueryHookModels(config, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=_decision()),
    ))
    _invoke(models, timeout=remaining)
    assert len(options) == 1
    assert options[0]["timeout"] == min(remaining, 3)
    assert options[0]["trust_env"] is False


@pytest.mark.parametrize("status", [429, 503])
def test_http_errors_preserve_status_without_response_body_or_retry(config, status):
    requests, usages = [], []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, json={"error": "private-test-key private-user-text"})

    models = QueryHookModels(config, transport=httpx.MockTransport(respond))
    with capture_model_usage(usages.append), pytest.raises(QueryHookModelError) as caught:
        _invoke(models)
    assert f"HTTP {status}" in str(caught.value)
    assert "private-" not in str(caught.value)
    assert len(requests) == len(usages) == 1
    assert usages[0].status == "failed"


def test_transport_errors_preserve_type_without_error_detail_or_retry(config):
    requests = []

    def timeout(request):
        requests.append(request)
        raise httpx.ReadTimeout("private-test-key private-user-text", request=request)

    models = QueryHookModels(config, transport=httpx.MockTransport(timeout))
    with pytest.raises(QueryHookModelError, match="ReadTimeout") as caught:
        _invoke(models)
    assert "private-" not in str(caught.value)
    assert len(requests) == 1


def test_model_call_control_errors_propagate_unchanged(config):
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
        _invoke(models)
    assert caught.value is rejection


@pytest.mark.parametrize("timeout", [0, -1, True, float("inf"), float("nan")])
def test_invalid_remaining_timeout_does_not_start_request(config, timeout):
    models = QueryHookModels(config, transport=httpx.MockTransport(
        lambda request: pytest.fail("invalid timeout must not reach HTTP"),
    ))
    with pytest.raises(ValueError, match="positive and finite"):
        _invoke(models, timeout=timeout)
