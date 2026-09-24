from __future__ import annotations

import os

import pytest
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict

from gateway.config import AgentModelConfig, OpenAIModelSourceConfig
from gateway.agent_model.context import ContextWindowPolicy
from gateway.agent_model.factory import AgentModelFactory


class _MarkerArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str


@pytest.mark.live_agent
def test_deepseek_agent_calls_a_tool_and_returns_visible_text() -> None:
    if os.getenv("ANIVAG_RUN_LIVE_AGENT_TEST") != "1":
        pytest.skip("set ANIVAG_RUN_LIVE_AGENT_TEST=1 to call the live provider")
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        pytest.fail("DEEPSEEK_API_KEY is required for the live Agent smoke test")

    invocations: list[str] = []
    visible_text: list[str] = []

    def lookup_ci_marker(name: str) -> str:
        invocations.append(name)
        return "CI_AGENT_OK"

    tool = StructuredTool.from_function(
        func=lookup_ci_marker,
        name="lookup_ci_marker",
        description=(
            "Look up the fixed AniVAG CI marker. Call this tool before answering."
        ),
        args_schema=_MarkerArguments,
    )
    config = AgentModelConfig(
        source="deepseek_ci",
        model=os.getenv("ANIVAG_LIVE_AGENT_MODEL", "deepseek-flash"),
        max_output_tokens=1_024,
        request_timeout_seconds=60,
        fallback=None,
    )
    factory = AgentModelFactory(
        config,
        {
            "deepseek_ci": OpenAIModelSourceConfig(
                provider="deepseek",
                profile="deepseek",
                base_url="https://api.deepseek.com",
                api_key_env="DEEPSEEK_API_KEY",
            )
        },
        environ={"DEEPSEEK_API_KEY": api_key},
    )
    model = factory.build_loop(
        model=config.model,
        reasoning_effort="none",
        request_timeout_seconds=config.request_timeout_seconds,
        max_output_tokens=config.max_output_tokens,
        max_input_chars=20_000,
        context_window_policy=ContextWindowPolicy(
            context_window_tokens=16_000,
            output_reserve_tokens=1_024,
            safety_margin_tokens=1_024,
            auto_compact_tokens=12_000,
            compact_target_tokens=6_000,
        ),
    )
    try:
        model.run_agent_loop(
            (
                "Call lookup_ci_marker exactly once with name 'anivag-ci'. "
                "After reading its result, answer with one short visible sentence."
            ),
            tools=[tool],
            max_model_calls=2,
            handle_text=visible_text.append,
            require_tool_call=True,
            request_timeout_seconds=60,
        )
    finally:
        factory.close()

    assert invocations == ["anivag-ci"]
    assert "".join(visible_text).strip()
