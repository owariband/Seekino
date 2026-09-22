from __future__ import annotations

import pytest

from gateway.web_search import WebSearchError, WebSearcher


def test_search_keeps_bounded_unique_http_results(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []

    class FakeDDGS:
        def __init__(self, *, timeout: int) -> None:
            calls.append(timeout)

        def text(self, query: str, *, max_results: int) -> list[dict[str, object]]:
            calls.append((query, max_results))
            return [
                {"title": "  Course\nintro  ", "href": "https://example.com/1", "body": " First  \n lesson "},
                {"title": "duplicate", "href": "https://example.com/1", "body": "duplicate"},
                {"title": "unsafe", "href": "javascript:alert(1)", "body": "unsafe"},
                {"title": "no credentials", "href": "https://user:pass@example.com/2", "body": "unsafe"},
                {"title": "invalid port", "href": "https://example.com:invalid/2", "body": "unsafe"},
                {"title": " Another title ", "href": "http://example.org/2", "body": "x" * 1000},
                {"title": "ignored", "href": "https://example.com/3", "body": "not needed"},
            ]

    monkeypatch.setattr("gateway.web_search.DDGS", FakeDDGS)

    hits = WebSearcher().search("  course ep1  ", max_results=2, timeout_seconds=2.1)

    assert calls == [3, ("course ep1", 2)]
    assert [(hit.title, hit.url) for hit in hits] == [
        ("Course intro", "https://example.com/1"),
        ("Another title", "http://example.org/2"),
    ]
    assert hits[0].snippet == "First lesson"
    assert len(hits[1].snippet) == 600


@pytest.mark.parametrize(
    ("query", "max_results", "timeout_seconds"),
    [
        (" ", 1, 5),
        ("x" * 501, 1, 5),
        ("ok", 0, 5),
        ("ok", 21, 5),
        ("ok", 1, 0),
        ("ok", 1, float("nan")),
        ("ok", 1, 61),
    ],
)
def test_search_rejects_out_of_range_parameters(
    query: str, max_results: int, timeout_seconds: float
) -> None:
    with pytest.raises(ValueError):
        WebSearcher().search(query, max_results=max_results, timeout_seconds=timeout_seconds)


def test_search_distinguishes_empty_results_from_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EmptyDDGS:
        def __init__(self, *, timeout: int) -> None:
            pass

        def text(self, query: str, *, max_results: int) -> list[object]:
            return []

    monkeypatch.setattr("gateway.web_search.DDGS", EmptyDDGS)
    assert WebSearcher().search("topic", max_results=3, timeout_seconds=5) == []

    class BrokenDDGS(EmptyDDGS):
        def text(self, query: str, *, max_results: int) -> list[object]:
            raise TimeoutError("provider timed out")

    monkeypatch.setattr("gateway.web_search.DDGS", BrokenDDGS)
    with pytest.raises(WebSearchError, match="provider failed") as error:
        WebSearcher().search("topic", max_results=3, timeout_seconds=5)
    assert isinstance(error.value.__cause__, TimeoutError)
