from __future__ import annotations

from typing import Literal

OpenAIProfile = Literal["standard", "deepseek", "kimi"]
ReasoningEffort = Literal["none", "low", "high", "max"]

_DEEPSEEK_NON_THINKING_EXTRA_BODY = {"thinking": {"type": "disabled"}}


def deepseek_non_thinking_extra_body() -> dict[str, dict[str, str]]:
    """Return an isolated request body for DeepSeek non-thinking calls."""

    return {
        key: dict(value)
        for key, value in _DEEPSEEK_NON_THINKING_EXTRA_BODY.items()
    }


def completion_options(
    profile: OpenAIProfile,
    reasoning_effort: ReasoningEffort,
) -> dict[str, object]:
    """Return profile-specific Chat Completions request options."""

    if profile == "standard":
        return {}
    if profile == "kimi":
        options: dict[str, object] = {"temperature": 1}
        if reasoning_effort != "none":
            options["reasoning_effort"] = reasoning_effort
        return options
    if profile != "deepseek":
        raise ValueError(f"unsupported OpenAI API profile: {profile}")
    if reasoning_effort == "none":
        return {"extra_body": deepseek_non_thinking_extra_body()}
    return {
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
    "deepseek_non_thinking_extra_body",
    "uses_legacy_token_parameter",
]
