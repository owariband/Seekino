import httpx
import pytest

import gateway.model_discovery as discovery


def _serve(monkeypatch, handler):
    def client(base_url, *, allow_local, timeout):
        return httpx.Client(transport=httpx.MockTransport(handler), timeout=timeout)
    monkeypatch.setattr(discovery, "endpoint_http_client", client)


@pytest.mark.parametrize("status", [404, 405, 501])
def test_service_without_model_list_is_reported_as_not_provided(monkeypatch, status):
    # whisper.cpp's server answers /v1/models with 404: that means "no list", not a broken connection.
    _serve(monkeypatch, lambda request: httpx.Response(status))
    assert discovery.discover_models("openai_audio", "http://127.0.0.1:18842/v1", None, allow_local=True) is None


def test_listed_models_are_returned(monkeypatch):
    _serve(monkeypatch, lambda request: httpx.Response(200, json={"data": [{"id": "deepseek-flash"}, {"id": "deepseek-v4-pro"}]}))
    assert discovery.discover_models("openai_chat", "https://api.example.com", "key", allow_local=False) == [
        "deepseek-flash", "deepseek-v4-pro"]


def test_other_failures_still_raise(monkeypatch):
    _serve(monkeypatch, lambda request: httpx.Response(500))
    with pytest.raises(ValueError, match="HTTP 500"):
        discovery.discover_models("openai_chat", "https://api.example.com", "key", allow_local=False)


def test_missing_later_page_is_an_error_not_an_empty_list(monkeypatch):
    def handler(request):
        if "after_id" in request.url.params:
            return httpx.Response(404)
        return httpx.Response(200, json={"data": [{"id": "claude-a"}], "has_more": True, "last_id": "claude-a"})
    _serve(monkeypatch, handler)
    with pytest.raises(ValueError, match="HTTP 404"):
        discovery.discover_models("anthropic_messages", "https://api.anthropic.com", "key", allow_local=False)


def test_context_windows_are_returned_per_listed_model_with_invalid_values_absent(monkeypatch):
    _serve(monkeypatch, lambda request: httpx.Response(200, json={"data": [
        {"id": "deepseek-flash", "context_window": 1048576},
        {"id": "deepseek-v4-pro", "context_window": "1048576"},
        {"id": "deepseek-v4-lite", "context_window": True},
        {"id": "deepseek-v4-nano", "context_window": -1},
        {"id": "deepseek-v4-mini"},
    ]}))
    assert discovery.discover_model_context_windows(
        "openai_chat", "https://api.example.com", "key", allow_local=False,
    ) == {
        "deepseek-flash": 1048576,
        "deepseek-v4-pro": None,
        "deepseek-v4-lite": None,
        "deepseek-v4-nano": None,
        "deepseek-v4-mini": None,
    }


def test_context_window_discovery_preserves_list_endpoint_policies(monkeypatch):
    _serve(monkeypatch, lambda request: httpx.Response(404))
    assert discovery.discover_model_context_windows(
        "openai_chat", "https://api.example.com", "key", allow_local=False,
    ) is None

    _serve(monkeypatch, lambda request: httpx.Response(500))
    with pytest.raises(ValueError, match="HTTP 500"):
        discovery.discover_model_context_windows(
            "openai_chat", "https://api.example.com", "key", allow_local=False,
        )


def test_audio_transcription_servers_are_not_offered_discovery():
    from server.service.yaml_chat_models import DISCOVERABLE_PROTOCOLS
    assert "openai_audio" not in DISCOVERABLE_PROTOCOLS
    assert {"openai_chat", "ollama_embedding", "llama_cpp_embedding"} <= DISCOVERABLE_PROTOCOLS
