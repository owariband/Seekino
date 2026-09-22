from __future__ import annotations

import math
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from ddgs import DDGS

_MAX_QUERY_CHARS = 500
_MAX_RESULTS = 20
_MAX_TITLE_CHARS = 200
_MAX_SNIPPET_CHARS = 600
_MAX_URL_CHARS = 2048
_MAX_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class WebSearchHit:
    title: str
    url: str
    snippet: str


class WebSearchError(RuntimeError):
    """The external search provider could not complete a search."""


class WebSearcher:
    def search(
        self,
        query: str,
        *,
        max_results: int,
        timeout_seconds: float,
    ) -> list[WebSearchHit]:
        normalized_query = query.strip()
        if not normalized_query or len(normalized_query) > _MAX_QUERY_CHARS:
            raise ValueError(f"query must contain 1–{_MAX_QUERY_CHARS} characters")
        if not 1 <= max_results <= _MAX_RESULTS:
            raise ValueError(f"max_results must be between 1 and {_MAX_RESULTS}")
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= _MAX_TIMEOUT_SECONDS:
            raise ValueError(f"timeout_seconds must be between 0 and {_MAX_TIMEOUT_SECONDS}")

        try:
            rows = DDGS(timeout=math.ceil(timeout_seconds)).text(
                normalized_query,
                max_results=max_results,
            )
        except Exception as error:
            raise WebSearchError("web search provider failed") from error

        hits: list[WebSearchHit] = []
        seen_urls: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            raw_url = row.get("href")
            if not isinstance(raw_url, str) or len(raw_url) > _MAX_URL_CHARS:
                continue
            url = raw_url.strip()
            try:
                parsed = urlsplit(url)
                hostname = parsed.hostname
                parsed.port  # Reject malformed ports that urlsplit otherwise accepts.
            except ValueError:
                continue
            if (
                parsed.scheme not in {"http", "https"}
                or not hostname
                or parsed.username is not None
                or parsed.password is not None
                or any(character.isspace() for character in url)
                or url in seen_urls
            ):
                continue
            title = _short_text(row.get("title"), _MAX_TITLE_CHARS)
            snippet = _short_text(row.get("body"), _MAX_SNIPPET_CHARS)
            if not title:
                continue
            seen_urls.add(url)
            hits.append(WebSearchHit(title=title, url=url, snippet=snippet))
            if len(hits) >= max_results:
                break
        return hits


def _short_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()[:limit]
