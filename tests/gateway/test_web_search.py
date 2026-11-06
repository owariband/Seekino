from __future__ import annotations

import json
import logging

import httpx
import pytest

from gateway.config import WebSearchConfig
from gateway.web_search import WebSearchError, WebSearcher


def searcher(handler, *, config=None, environ=None):
    return WebSearcher(config or WebSearchConfig(provider="deepseek"), environ={"DEEPSEEK_API_KEY": "private-test-key"} if environ is None else environ,
                       transport=httpx.MockTransport(handler))


def test_search_sends_official_contract_and_keeps_bounded_unique_http_results(caplog):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, headers={"x-request-id": "deepseek-123"}, json={"results": [
            {"title": "  Course\nintro  ", "url": "https://example.com/1", "summary": " First  \n lesson ", "snippet": "short"},
            {"title": "duplicate", "url": "https://example.com/1", "summary": "duplicate"},
            {"title": "unsafe", "url": "javascript:alert(1)", "summary": "unsafe"},
            {"title": "no credentials", "url": "https://user:pass@example.com/2", "summary": "unsafe"},
            {"title": "invalid port", "url": "https://example.com:invalid/2", "summary": "unsafe"},
            {"title": " Another title ", "url": "http://example.org/2", "summary": "", "snippet": "x" * 1000},
            {"title": "ignored", "url": "https://example.com/3", "summary": "not needed"},
        ]})

    with caplog.at_level(logging.INFO, logger="gateway.web_search"):
        hits = searcher(handle).search('  course "ep1" site:example.com  ', max_results=2, timeout_seconds=2.1)

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST" and str(request.url) == "https://api.deepseek.com/v1/tools/search"
    assert request.headers["authorization"] == "Bearer private-test-key"
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.content) == {"query": 'course "ep1" site:example.com', "count": 2, "need_content": False}
    assert all(value == 2.1 for value in request.extensions["timeout"].values())
    assert [(hit.title, hit.url) for hit in hits] == [
        ("Course intro", "https://example.com/1"), ("Another title", "http://example.org/2"),
    ]
    assert hits[0].snippet == "First lesson" and len(hits[1].snippet) == 600
    assert "provider=deepseek" in caplog.text and "provider_request_id=deepseek-123" in caplog.text
    assert "result_count=2" in caplog.text and "status_code=200" in caplog.text
    assert all(value not in caplog.text for value in ("private-test-key", "ep1", "First lesson"))


@pytest.mark.parametrize(("query", "max_results", "timeout_seconds"), [
    (" ", 1, 5), ("界" * 129, 1, 5), ("ok", 0, 5), ("ok", 21, 5),
    ("ok", True, 5), ("ok", 1, 0), ("ok", 1, float("nan")), ("ok", 1, 61),
])
def test_invalid_parameters_never_reach_provider(query, max_results, timeout_seconds):
    def unexpected(request):
        pytest.fail("invalid input must not be sent")
    with pytest.raises(ValueError):
        searcher(unexpected).search(query, max_results=max_results, timeout_seconds=timeout_seconds)


def test_128_unicode_characters_preserve_query_and_configuration():
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"results": []})
    config = WebSearchConfig(provider="deepseek", base_url="https://search.example.com/", api_key_env="SEARCH_KEY")
    client = searcher(handle, config=config, environ={"SEARCH_KEY": "custom-key"})
    assert client.search("界" * 128, max_results=20, timeout_seconds=12) == []
    assert json.loads(requests[0].content)["query"] == "界" * 128
    assert requests[0].headers["authorization"] == "Bearer custom-key"
    assert str(requests[0].url) == "https://search.example.com/v1/tools/search"


def test_missing_credential_fails_only_when_search_is_called():
    def unexpected(request):
        pytest.fail("missing credentials must not send a request")
    client = searcher(unexpected, environ={})
    with pytest.raises(WebSearchError, match="not configured") as error:
        client.search("topic", max_results=3, timeout_seconds=5)
    assert error.value.retryable is False


@pytest.mark.parametrize(("status", "retryable"), [
    (302, False), (400, False), (401, False), (402, False), (403, False),
    (408, True), (429, True), (500, True), (504, True),
])
def test_provider_status_has_retry_semantics_and_no_automatic_retry(status, retryable, caplog):
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(status, json={"error": {"message": "secret echoed by upstream"}})
    with caplog.at_level(logging.INFO, logger="gateway.web_search"):
        with pytest.raises(WebSearchError, match=f"HTTP {status}") as error:
            searcher(handle).search("topic", max_results=3, timeout_seconds=5)
    assert error.value.retryable is retryable and len(requests) == 1
    assert "secret echoed" not in str(error.value) + caplog.text


@pytest.mark.parametrize("payload", [None, [], {}, {"results": None}, {"results": {}}])
def test_invalid_success_envelope_is_not_silently_treated_as_no_matches(payload):
    with pytest.raises(WebSearchError):
        searcher(lambda request: httpx.Response(200, json=payload)).search("topic", max_results=3, timeout_seconds=5)


def test_timeout_is_transient_and_cause_is_preserved():
    def handle(request):
        raise httpx.ReadTimeout("upstream read stalled", request=request)
    with pytest.raises(WebSearchError) as error:
        searcher(handle).search("topic", max_results=3, timeout_seconds=5)
    assert error.value.retryable and isinstance(error.value.__cause__, httpx.ReadTimeout)


def test_malformed_json_is_a_provider_error():
    with pytest.raises(WebSearchError, match="invalid JSON"):
        searcher(lambda request: httpx.Response(200, text="<html>upstream failure</html>")).search(
            "topic", max_results=3, timeout_seconds=5,
        )


@pytest.mark.parametrize("backend", ["auto", "duckduckgo", "google, brave"])
def test_ddgs_works_without_reading_api_keys_and_preserves_query(backend, monkeypatch, caplog):
    class NoCredentials(dict):
        def get(self, *args):
            pytest.fail("DDGS must not read API credentials")
    calls = []
    class FakeDDGS:
        def __init__(self, **kwargs):
            calls.append(kwargs)
        def text(self, query, **kwargs):
            calls.append((query, kwargs))
            return [
                {"title": " Official ", "href": "https://example.org/page", "body": " Summary ", "summary": "wrong provider field"},
                {"title": "duplicate", "href": "https://example.org/page", "body": "duplicate"},
                {"title": "unsafe", "href": "javascript:alert(1)", "body": "unsafe"},
                {"title": "second", "href": "https://example.org/second", "body": "x" * 900},
            ]
    monkeypatch.setattr("ddgs.DDGS", FakeDDGS)
    def unexpected(request):
        pytest.fail("DDGS must not call DeepSeek")
    client = WebSearcher(WebSearchConfig(provider="ddgs", backend=backend), environ=NoCredentials(),
                         transport=httpx.MockTransport(unexpected))
    query = "界" * 500
    with caplog.at_level(logging.INFO, logger="gateway.web_search"):
        hits = client.search(query, max_results=2, timeout_seconds=2.1)
    assert calls == [{"timeout": 3}, (query, {"max_results": 2, "backend": backend.replace(" ", "")})]
    assert client.max_query_chars == 500
    assert hits[0].snippet == "Summary" and len(hits[1].snippet) == 600
    assert "provider=ddgs" in caplog.text and query not in caplog.text
    with pytest.raises(ValueError):
        client.search("界" * 501, max_results=2, timeout_seconds=2.1)
    assert len(calls) == 2


def test_default_ddgs_does_not_switch_when_deepseek_key_exists(monkeypatch):
    calls = []
    class FakeDDGS:
        def __init__(self, **kwargs):
            pass
        def text(self, query, **kwargs):
            calls.append(query)
            return []
    monkeypatch.setattr("ddgs.DDGS", FakeDDGS)
    client = WebSearcher(environ={"DEEPSEEK_API_KEY": "present-but-not-selected"})
    assert client.search("topic", max_results=2, timeout_seconds=1) == []
    assert calls == ["topic"]


def test_invalid_ddgs_backend_is_not_silently_replaced_by_auto(monkeypatch):
    monkeypatch.setattr("ddgs.DDGS", lambda **kwargs: pytest.fail("invalid backend must not send a query"))
    with pytest.raises(WebSearchError, match="Unsupported DDGS") as error:
        WebSearcher(WebSearchConfig(backend="unknown")).search("topic", max_results=2, timeout_seconds=1)
    assert error.value.retryable is False


def test_ddgs_failure_does_not_fall_back_to_paid_search(monkeypatch):
    from ddgs.exceptions import TimeoutException
    class FailingDDGS:
        def __init__(self, **kwargs):
            pass
        def text(self, *args, **kwargs):
            raise TimeoutException("timed out")
    monkeypatch.setattr("ddgs.DDGS", FailingDDGS)
    client = WebSearcher(environ={"DEEPSEEK_API_KEY": "available"},
                         transport=httpx.MockTransport(lambda request: pytest.fail("no automatic paid fallback")))
    with pytest.raises(WebSearchError) as error:
        client.search("topic", max_results=2, timeout_seconds=1)
    assert error.value.retryable and isinstance(error.value.__cause__, TimeoutException)


def test_deepseek_failure_does_not_switch_to_ddgs(monkeypatch):
    monkeypatch.setattr("ddgs.DDGS", lambda **kwargs: pytest.fail("no automatic DDGS fallback"))
    with pytest.raises(WebSearchError) as error:
        searcher(lambda request: httpx.Response(402)).search("topic", max_results=2, timeout_seconds=1)
    assert error.value.retryable is False
