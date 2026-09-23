from __future__ import annotations

from types import SimpleNamespace

import pytest

import gateway.harness.factory as agent_factory
from gateway.config import (
    AgentFallbackConfig,
    AgentModelConfig,
    ArkModelSourceConfig,
    ClaudeCodeModelSourceConfig,
    CodexModelSourceConfig,
    OpenAIModelSourceConfig,
)
from gateway.harness.context import ContextWindowPolicy


def _context_window_policy() -> ContextWindowPolicy:
    return ContextWindowPolicy(
        context_window_tokens=64_000,
        output_reserve_tokens=4_096,
        safety_margin_tokens=4_096,
        auto_compact_tokens=48_000,
        compact_target_tokens=24_000,
    )


def _build_arguments() -> dict[str, object]:
    return {
        "model": "role-model",
        "reasoning_effort": "high",
        "request_timeout_seconds": 60,
        "max_output_tokens": 4_096,
        "max_input_chars": 120_000,
        "context_window_policy": _context_window_policy(),
    }


def _agent_config(*, fallback: bool = False) -> AgentModelConfig:
    return AgentModelConfig(
        source="primary",
        model="configured-model",
        max_output_tokens=8_192,
        request_timeout_seconds=90,
        fallback=(
            AgentFallbackConfig(source="fallback", model="fallback-model")
            if fallback else None
        ),
    )


def _openai_source(
    *, api_key_env: str = "DEEPSEEK_API_KEY"
) -> OpenAIModelSourceConfig:
    return OpenAIModelSourceConfig(
        provider="deepseek",
        profile="deepseek",
        base_url="https://api.deepseek.com",
        api_key_env=api_key_env,
    )


def test_factory_requires_only_the_selected_source_credentials() -> None:
    sources = {
        "primary": CodexModelSourceConfig(),
        "unused": _openai_source(api_key_env="UNUSED_API_KEY"),
    }

    factory = agent_factory.AgentModelFactory(
        _agent_config(),
        sources,
        environ={},
    )

    assert factory.provider_name == "codex"
    factory.close()


def test_factory_fails_fast_when_selected_key_is_missing() -> None:
    with pytest.raises(RuntimeError, match="MISSING_DEEPSEEK_KEY"):
        agent_factory.AgentModelFactory(
            _agent_config(),
            {"primary": _openai_source(api_key_env="MISSING_DEEPSEEK_KEY")},
            environ={},
        )


def test_openai_factory_composes_ark_fallback_with_explicit_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    structured_calls: list[dict[str, object]] = []
    loop_calls: list[dict[str, object]] = []

    def structured(**kwargs: object) -> object:
        structured_calls.append(kwargs)
        return SimpleNamespace(provider_name="deepseek")

    def loop(*_args: object, **kwargs: object) -> object:
        loop_calls.append(kwargs)
        return SimpleNamespace(provider_name="deepseek")

    monkeypatch.setattr(agent_factory, "OpenAIStructuredToolAgentModel", structured)
    monkeypatch.setattr(agent_factory, "OpenAIAgentModel", loop)
    factory = agent_factory.AgentModelFactory(
        _agent_config(fallback=True),
        {
            "primary": _openai_source(),
            "fallback": ArkModelSourceConfig(api_key_env="CUSTOM_ARK_KEY"),
        },
        fallback_model="override-fallback",
        environ={
            "DEEPSEEK_API_KEY": "deepseek-secret",
            "CUSTOM_ARK_KEY": "ark-secret",
        },
    )

    factory.build_structured(**_build_arguments())
    factory.build_loop(**_build_arguments())

    structured_kwargs = structured_calls[0]
    assert structured_kwargs["provider_name"] == "deepseek"
    assert structured_kwargs["api_profile"] == "deepseek"
    assert structured_kwargs["api_key"] == "deepseek-secret"
    structured_fallback = structured_kwargs["fallback_adapter"]
    assert structured_fallback.model_name == "override-fallback"
    assert structured_fallback._api_key == "ark-secret"
    loop_kwargs = loop_calls[0]
    assert loop_kwargs["provider_name"] == "deepseek"
    assert loop_kwargs["profile"] == "deepseek"
    loop_fallback = loop_kwargs["fallback_adapter"]
    assert loop_fallback.model_name == "override-fallback"
    assert loop_fallback._api_key == "ark-secret"


def test_ark_factory_uses_same_adapter_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: dict[str, object] = {}

    def build(**kwargs: object) -> object:
        received.update(kwargs)
        return SimpleNamespace(provider_name="ark")

    monkeypatch.setattr(agent_factory, "ArkStructuredToolAgentModel", build)
    factory = agent_factory.AgentModelFactory(
        _agent_config(fallback=True),
        {
            "primary": ArkModelSourceConfig(),
            "fallback": ArkModelSourceConfig(),
        },
        environ={"ARK_API_KEY": "ark-secret"},
    )

    factory.build_structured(**_build_arguments())

    assert received["fallback_model"] == "fallback-model"
    assert received["api_key"] == "ark-secret"


def test_ark_factory_rejects_fallback_with_different_credentials() -> None:
    with pytest.raises(ValueError, match="same api_key_env"):
        agent_factory.AgentModelFactory(
            _agent_config(fallback=True),
            {
                "primary": ArkModelSourceConfig(api_key_env="PRIMARY_ARK_KEY"),
                "fallback": ArkModelSourceConfig(api_key_env="FALLBACK_ARK_KEY"),
            },
            environ={},
        )


def test_factory_rejects_primary_source_reused_as_fallback() -> None:
    config = _agent_config(fallback=True).model_copy(
        update={
            "fallback": AgentFallbackConfig(
                source="primary",
                model="fallback-model",
            )
        }
    )

    with pytest.raises(ValueError, match="must differ"):
        agent_factory.AgentModelFactory(
            config,
            {"primary": ArkModelSourceConfig()},
            environ={"ARK_API_KEY": "ark-secret"},
        )


def test_factory_rejects_unsupported_cross_adapter_fallback() -> None:
    with pytest.raises(ValueError, match="codex_app_server -> ark"):
        agent_factory.AgentModelFactory(
            _agent_config(fallback=True),
            {
                "primary": CodexModelSourceConfig(),
                "fallback": ArkModelSourceConfig(),
            },
            environ={"ARK_API_KEY": "ark-secret"},
        )


def test_fallback_override_requires_a_configured_source() -> None:
    with pytest.raises(ValueError, match="fallback.source"):
        agent_factory.AgentModelFactory(
            _agent_config(),
            {"primary": CodexModelSourceConfig()},
            fallback_model="orphan-model",
            environ={},
        )


def test_codex_factory_shares_one_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def build(**kwargs: object) -> object:
        calls.append(kwargs)
        return SimpleNamespace(provider_name="codex")

    monkeypatch.setattr(agent_factory, "CodexAgentModel", build)
    source = CodexModelSourceConfig(
        codex_bin="/opt/codex/bin/codex",
        max_concurrency=3,
    )
    factory = agent_factory.AgentModelFactory(
        _agent_config(), {"primary": source}, environ={}
    )

    factory.build_structured(**_build_arguments())
    factory.build_loop(**_build_arguments())

    assert calls[0]["codex_bin"] == "/opt/codex/bin/codex"
    assert calls[0]["max_concurrency"] == 3
    assert calls[0]["executor"] is calls[1]["executor"]
    assert calls[0]["executor"] is not None
    factory.close()
    factory.close()


def test_claude_local_subscription_needs_no_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[dict[str, object]] = []

    def build(**kwargs: object) -> object:
        received.append(kwargs)
        return SimpleNamespace(provider_name="claude_code")

    monkeypatch.setattr(agent_factory, "ClaudeAgentModel", build)
    source = ClaudeCodeModelSourceConfig(
        cli_path="/opt/claude/bin/claude",
        max_concurrency=2,
    )
    factory = agent_factory.AgentModelFactory(
        _agent_config(), {"primary": source}, environ={}
    )

    factory.build_structured(**_build_arguments())
    factory.build_loop(**_build_arguments())

    assert received[0]["api_key"] is None
    assert received[0]["cli_path"] == "/opt/claude/bin/claude"
    assert received[0]["concurrency_gate"] is received[1]["concurrency_gate"]


def test_claude_api_key_mode_resolves_named_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: dict[str, object] = {}

    def build(**kwargs: object) -> object:
        received.update(kwargs)
        return SimpleNamespace(provider_name="claude_code")

    monkeypatch.setattr(agent_factory, "ClaudeAgentModel", build)
    source = ClaudeCodeModelSourceConfig(
        auth_mode="api_key",
        api_key_env="CUSTOM_ANTHROPIC_KEY",
    )
    factory = agent_factory.AgentModelFactory(
        _agent_config(),
        {"primary": source},
        environ={"CUSTOM_ANTHROPIC_KEY": "  secret-value  "},
    )

    factory.build_structured(**_build_arguments())

    assert received["api_key"] == "secret-value"
