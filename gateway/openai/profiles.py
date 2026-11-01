from __future__ import annotations

from typing import Literal

from gateway.constants import KIMI_FIXED_TEMPERATURE

OpenAIProfile = Literal["standard", "deepseek", "kimi"]
ReasoningEffort = Literal["none", "low", "high", "max"]

_DEEPSEEK_NON_THINKING_EXTRA_BODY = {"thinking": {"type": "disabled"}}


def deepseek_non_thinking_extra_body() -> dict[str, dict[str, str]]:
    """Return an isolated request body for DeepSeek non-thinking calls."""

    return {
        key: dict(value)
        for key, value in _DEEPSEEK_NON_THINKING_EXTRA_BODY.items()
    }


def effective_temperature(profile: OpenAIProfile, temperature: float) -> float:
    """Kimi Coding's fixed sampling contract overrides the configured value."""
    return KIMI_FIXED_TEMPERATURE if profile == "kimi" else temperature


def completion_options(
    profile: OpenAIProfile,
    reasoning_effort: ReasoningEffort,
    *,
    temperature: float = 0.0,
) -> dict[str, object]:
    """Return profile-specific Chat Completions request options."""

    if profile == "standard":
        return {"temperature": temperature}
    if profile == "kimi":
        options: dict[str, object] = {"temperature": effective_temperature(profile, temperature)}
        if reasoning_effort != "none":
            options["reasoning_effort"] = reasoning_effort
        return options
    if profile != "deepseek":
        raise ValueError(f"unsupported OpenAI API profile: {profile}")
    if reasoning_effort == "none":
        return {"temperature": temperature, "extra_body": deepseek_non_thinking_extra_body()}
    return {
        "temperature": temperature,
        "extra_body": {"thinking": {"type": "enabled"}},
        "reasoning_effort": reasoning_effort,
    }


def uses_legacy_token_parameter(profile: OpenAIProfile) -> bool:
    """Whether this profile expects max_tokens for Chat Completions."""

    if profile not in {"standard", "deepseek", "kimi"}:
        raise ValueError(f"unsupported OpenAI API profile: {profile}")
    return profile == "deepseek"


__all__ = [
    "OpenAIProfile",
    "ReasoningEffort",
    "completion_options",
    "effective_temperature",
    "deepseek_non_thinking_extra_body",
    "uses_legacy_token_parameter",
]
