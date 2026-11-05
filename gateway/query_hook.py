"""Provider transport for turn routing."""

from __future__ import annotations

import math
import os

import httpx

from gateway.config import QueryHookConfig
from gateway.usage import track_model_call


class QueryHookModelError(RuntimeError):
    """A routing or rewriting provider could not return a usable result."""


def _request_timeout(configured: float, remaining: float) -> float:
    if (
        isinstance(remaining, bool)
        or not isinstance(remaining, (int, float))
        or not math.isfinite(remaining)
        or remaining <= 0
    ):
        raise ValueError("timeout_seconds must be positive and finite")
    return min(configured, remaining)


class QueryHookModels:
    def __init__(
        self, config: QueryHookConfig, *, transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._config = config
        self._transport = transport

    def route(self, state: dict, question: dict, *, timeout_seconds: float) -> str:
        timeout = _request_timeout(self._config.router_timeout_seconds, timeout_seconds)
        api_key = os.getenv(self._config.router_api_key_env, "").strip()
        if not api_key:
            raise QueryHookModelError("Jev API key is not configured")
        with httpx.Client(timeout=timeout, trust_env=False, transport=self._transport) as client:
            with track_model_call(
                provider="openrouter", model=self._config.router_model, operation="query_hook_route",
            ) as call:
                try:
                    response = client.post(
                        self._config.router_endpoint,
                        headers={"Authorization": f"Bearer {api_key}"},
                        json={
                            "model": self._config.router_model, "state": state,
                            "questions": {"route": question},
                        },
                    )
                except httpx.HTTPError as error:
                    raise QueryHookModelError(f"Jev transport failed ({type(error).__name__})") from None
                if not response.is_success:
                    raise QueryHookModelError(f"Jev HTTP {response.status_code}")
                try:
                    result = response.json()
                    answer = result["answers"]["route"]
                    if not isinstance(answer, dict) or answer.get("type") != "choice":
                        raise ValueError("Expected a choice answer")
                    choice = answer.get("choice")
                    if type(choice) is not str or choice not in ("respond", "retrieve", "mixed"):
                        raise ValueError("Unknown routing choice")
                except (ValueError, KeyError, TypeError):
                    raise QueryHookModelError("Jev returned an invalid decision response") from None
                usage = result.get("usage")
                if isinstance(usage, dict) and usage.get("total_tokens") is None:
                    counts = (usage.get("input_tokens"), usage.get("output_tokens"))
                    if all(type(value) is int and value >= 0 for value in counts):
                        result = {**result, "usage": {**usage, "total_tokens": sum(counts)}}
                call.capture_response(result)
                return choice

