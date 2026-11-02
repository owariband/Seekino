from __future__ import annotations

import logging
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from time import monotonic
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from gateway.config import WebSearchConfig

MAX_WEB_QUERY_CHARS = 128
_MAX_RESULTS = 20
_MAX_TITLE_CHARS = 200
_MAX_SNIPPET_CHARS = 600
_MAX_URL_CHARS = 2048
_MAX_TIMEOUT_SECONDS = 60
_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WebSearchHit:
    title: str
    url: str
    snippet: str


class WebSearchError(RuntimeError):
    """Search failed; retryability distinguishes configuration from transient errors."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class WebSearcher:
    def __init__(
        self, config: WebSearchConfig | None = None, *,
        environ: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._config = config or WebSearchConfig()
        self._environ = os.environ if environ is None else environ
        self._transport = transport

    def search(
        self,
        query: str,
        *,
        max_results: int,
        timeout_seconds: float,
    ) -> list[WebSearchHit]:
        normalized_query = query.strip()
        if not normalized_query or len(normalized_query) > MAX_WEB_QUERY_CHARS:
            raise ValueError(f"query must contain 1–{MAX_WEB_QUERY_CHARS} characters")
        if type(max_results) is not int or not 1 <= max_results <= _MAX_RESULTS:
            raise ValueError(f"max_results must be between 1 and {_MAX_RESULTS}")
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= _MAX_TIMEOUT_SECONDS:
            raise ValueError(f"timeout_seconds must be between 0 and {_MAX_TIMEOUT_SECONDS}")

        started = monotonic()
        request_id = str(uuid4())
        status_code: int | None = None
        provider_request_id = None
        hits: list[WebSearchHit] = []
        outcome = "error"
        try:
            key = self._environ.get(self._config.api_key_env, "").strip()
            if not key:
                raise WebSearchError("DeepSeek search API key is not configured", retryable=False)
            with httpx.Client(timeout=timeout_seconds, transport=self._transport) as client:
                response = client.post(
                    self._config.base_url + "/v1/tools/search",
                    headers={"Authorization": f"Bearer {key}"},
                    json={"query": normalized_query, "count": max_results, "need_content": False},
                )
            status_code = response.status_code
            raw_id = response.headers.get("x-request-id", "")
            provider_request_id = re.sub(r"[^a-zA-Z0-9_.:-]", "", raw_id)[:128] or None
            if status_code != 200:
                raise WebSearchError(
                    f"DeepSeek search returned HTTP {status_code}",
                    retryable=status_code in {408, 429} or status_code >= 500,
                )
            try:
                payload = response.json()
            except ValueError as error:
                raise WebSearchError("DeepSeek search returned invalid JSON") from error
            if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
                raise WebSearchError("DeepSeek search returned an invalid results envelope")
            hits = _normalize_hits(payload["results"], max_results=max_results)
            outcome = "ok" if hits else "empty"
            return hits
        except httpx.HTTPError as error:
            raise WebSearchError("DeepSeek search transport failed") from error
        finally:
            # Do not log credentials, queries, response bodies or page contents.
            _logger.info(
                "event=web_search_completed provider=deepseek request_id=%s "
                "provider_request_id=%s status_code=%s outcome=%s result_count=%s elapsed_ms=%.1f",
                request_id, provider_request_id, status_code, outcome, len(hits),
                (monotonic() - started) * 1000,
            )


def _normalize_hits(rows: list[object], *, max_results: int) -> list[WebSearchHit]:
    hits: list[WebSearchHit] = []
    seen_urls: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_url = row.get("url")
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
        snippet = _short_text(row.get("summary"), _MAX_SNIPPET_CHARS) or _short_text(
            row.get("snippet"), _MAX_SNIPPET_CHARS,
        )
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
