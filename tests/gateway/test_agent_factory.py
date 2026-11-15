from __future__ import annotations

from threading import Event
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.base import empty_checkpoint

import gateway.agent_model.factory as agent_factory
from gateway.agent_model.errors import AgentModelTimeoutError
from gateway.config import (
    AgentFallbackConfig,
    AgentModelConfig,
    ArkModelSourceConfig,
    ClaudeCodeModelSourceConfig,
    CodexModelSourceConfig,
    ModelBinding,
    OpenAIModelSourceConfig,
)
from gateway.agent_model.context import ContextWindowPolicy
from gateway.codex.supervisor import CodexSupervisorModel


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


@pytest.mark.parametrize("primary", [_openai_source(), ArkModelSourceConfig(), CodexModelSourceConfig(), ClaudeCodeModelSourceConfig()])
@pytest.mark.parametrize("supports_vision_input", [True, False])
def test_factory_preserves_vision_capability_for_every_agent_transport(
    primary, supports_vision_input,
) -> None:
    factory = agent_factory.AgentModelFactory(
        _agent_config().model_copy(update={"supports_vision_input": supports_vision_input}),
        {"primary": primary},
        environ={"DEEPSEEK_API_KEY": "test-key", "ARK_API_KEY": "test-key"},
    )
    try:
        for builder in (factory.build_structured, factory.build_loop):
            adapter = builder(**_build_arguments())
            assert adapter.supports_vision_input is supports_vision_input
    finally:
        factory.close()


@pytest.mark.parametrize(("source", "adapter_name"), [
    (CodexModelSourceConfig(), "CodexAgentModel"),
    (ClaudeCodeModelSourceConfig(), "ClaudeAgentModel"),
])
@pytest.mark.parametrize(("builder_name", "run_name"), [
    ("build_structured", "run_structured"), ("build_loop", "run_agent_loop"),
])
def test_native_factory_does_not_restore_role_or_binding_context_limits(
    monkeypatch, source, adapter_name, builder_name, run_name,
):
    adapters = []

    class NativeModel:
        def __init__(self, **options):
            self.options = options
            adapters.append(self)

        def run_structured(self, _prompt, **options):
            return options

        run_agent_loop = run_structured

    monkeypatch.setattr(agent_factory, adapter_name, NativeModel)
    binding = ModelBinding(
        source="primary", model="configured-model", request_timeout_seconds=7,
        max_input_chars=5_000, max_output_tokens=1_024,
    )
    factory = agent_factory.AgentModelFactory(
        _agent_config(), {"primary": source}, binding=binding, environ={},
    )
    try:
        model = getattr(factory, builder_name)(**_build_arguments())
        assert adapters[0].options["max_input_chars"] is None
        assert adapters[0].options["context_window_policy"] is None
        options = getattr(model, run_name)("prompt", max_input_chars=1_000, request_timeout_seconds=60)
        assert options["max_input_chars"] is None
        assert options["request_timeout_seconds"] == 7
        assert adapters[0].options["max_output_tokens"] == 1_024
    finally:
        factory.close()


@pytest.mark.parametrize("primary", [_openai_source(), ArkModelSourceConfig()])
@pytest.mark.parametrize("supports_vision_input", [True, False])
def test_structured_fallback_uses_its_own_vision_capability(primary, supports_vision_input) -> None:
    config = _agent_config(fallback=True).model_copy(update={
        "supports_vision_input": not supports_vision_input,
        "fallback": AgentFallbackConfig(
            source="fallback", model="fallback-model", supports_vision_input=supports_vision_input,
        ),
    })
    factory = agent_factory.AgentModelFactory(
        config, {"primary": primary, "fallback": ArkModelSourceConfig()},
        environ={"DEEPSEEK_API_KEY": "test-key", "ARK_API_KEY": "test-key"},
    )
    try:
        adapter = factory.build_structured(**_build_arguments())
        assert adapter.supports_vision_input is not supports_vision_input
        assert adapter._fallback_adapter.supports_vision_input is supports_vision_input
    finally:
        factory.close()


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
    assert structured_fallback.max_output_tokens == 4_096
    assert structured_fallback.request_timeout_seconds == 60
    loop_kwargs = loop_calls[0]
    assert loop_kwargs["provider_name"] == "deepseek"
    assert loop_kwargs["profile"] == "deepseek"
    loop_fallback = loop_kwargs["fallback_adapter"]
    assert loop_fallback.model_name == "override-fallback"
    assert loop_fallback._api_key == "ark-secret"
    assert loop_fallback.max_output_tokens == 4_096
    assert loop_fallback.request_timeout_seconds == 60


def test_factory_preserves_fallback_request_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    structured_calls: list[dict[str, object]] = []
    loop_calls: list[dict[str, object]] = []
    config = _agent_config(fallback=True).model_copy(update={
        "fallback": AgentFallbackConfig(
            source="fallback",
            model="fallback-model",
            request_timeout_seconds=17,
            max_output_tokens=2_048,
            reasoning_effort="none",
        ),
    })

    monkeypatch.setattr(
        agent_factory,
        "OpenAIStructuredToolAgentModel",
        lambda **kwargs: structured_calls.append(kwargs)
        or SimpleNamespace(provider_name="deepseek"),
    )
    monkeypatch.setattr(
        agent_factory,
        "OpenAIAgentModel",
        lambda *_args, **kwargs: loop_calls.append(kwargs)
        or SimpleNamespace(provider_name="deepseek"),
    )
    factory = agent_factory.AgentModelFactory(
        config,
        {
            "primary": _openai_source(),
            "fallback": ArkModelSourceConfig(api_key_env="CUSTOM_ARK_KEY"),
        },
        environ={
            "DEEPSEEK_API_KEY": "deepseek-secret",
            "CUSTOM_ARK_KEY": "ark-secret",
        },
    )

    factory.build_structured(**_build_arguments())
    factory.build_loop(**_build_arguments())

    structured_fallback = structured_calls[0]["fallback_adapter"]
    assert structured_fallback.request_timeout_seconds == 17
    assert structured_fallback.max_output_tokens == 2_048
    loop_fallback = loop_calls[0]["fallback_adapter"]
    assert loop_fallback.request_timeout_seconds == 17
    assert loop_fallback.max_output_tokens == 2_048


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

    assert "fallback_model" not in received
    fallback_adapter = received["fallback_adapter"]
    assert fallback_adapter.model_name == "fallback-model"
    assert fallback_adapter.request_timeout_seconds == 60
    assert fallback_adapter.max_output_tokens == 4_096
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


def test_factory_rejects_unsupported_ark_fallback_reasoning() -> None:
    config = _agent_config(fallback=True).model_copy(update={
        "fallback": AgentFallbackConfig(
            source="fallback",
            model="fallback-model",
            reasoning_effort="high",
        ),
    })

    with pytest.raises(ValueError, match="does not support reasoning_effort"):
        agent_factory.AgentModelFactory(
            config,
            {
                "primary": _openai_source(),
                "fallback": ArkModelSourceConfig(),
            },
            environ={
                "DEEPSEEK_API_KEY": "deepseek-secret",
                "ARK_API_KEY": "ark-secret",
            },
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


@pytest.mark.parametrize("builder", ["build_structured", "build_loop", "build_supervisor"])
def test_codex_factory_mcp_control_is_per_model_and_independent_of_builder(monkeypatch, builder):
    calls = []

    def build(**options):
        calls.append(options)
        return SimpleNamespace(
            model_name=options["model"], provider_name="codex", supports_vision_input=True,
            close=lambda: None,
        )

    monkeypatch.setattr(agent_factory, "CodexAgentModel", build)
    factory = agent_factory.AgentModelFactory(
        _agent_config(), {"primary": CodexModelSourceConfig()}, environ={},
    )
    options = {"harness": "codex"} if builder == "build_supervisor" else {}
    models = []
    try:
        for mcp_options in ({}, {"mcp": True}, {"mcp": False}):
            models.append(getattr(factory, builder)(**_build_arguments(), **options, **mcp_options))
        assert [call["mcp"] for call in calls] == [False, True, False]
        assert all(call["executor"] is calls[0]["executor"] for call in calls)
    finally:
        for model in models:
            model.close()
        factory.close()


@pytest.mark.parametrize("builder", ["build_structured", "build_loop", "build_deep_agent", "build_supervisor"])
@pytest.mark.parametrize("mcp", [None, 0, 1, "false", []])
def test_factory_rejects_non_boolean_mcp_before_adapter_construction(monkeypatch, builder, mcp):
    monkeypatch.setattr(
        agent_factory, "OpenAIStructuredToolAgentModel",
        lambda **_options: pytest.fail("invalid mcp created a structured adapter"),
    )
    monkeypatch.setattr(
        agent_factory, "OpenAIAgentModel",
        lambda *_args, **_options: pytest.fail("invalid mcp created a loop adapter"),
    )
    factory = agent_factory.AgentModelFactory(
        _agent_config(), {"primary": _openai_source()},
        environ={"DEEPSEEK_API_KEY": "test-key"},
    )
    options = {"harness": "deepagents"} if builder == "build_supervisor" else {}
    try:
        with pytest.raises(ValueError, match="mcp must be a boolean"):
            getattr(factory, builder)(**_build_arguments(), **options, mcp=mcp)
    finally:
        factory.close()


@pytest.mark.parametrize("builder", ["build_structured", "build_loop", "build_deep_agent", "build_supervisor"])
def test_api_factory_accepts_mcp_without_adding_native_adapter_arguments(builder):
    factory = agent_factory.AgentModelFactory(
        _agent_config(), {"primary": _openai_source()},
        environ={"DEEPSEEK_API_KEY": "test-key"},
    )
    options = {"harness": "deepagents"} if builder == "build_supervisor" else {}
    try:
        model = getattr(factory, builder)(**_build_arguments(), **options, mcp=True)
        assert model.provider_name == "deepseek"
    finally:
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


@pytest.mark.parametrize("primary", [_openai_source(), ArkModelSourceConfig()])
def test_deep_agent_factory_preserves_provider_controls_and_shares_sessions(
    monkeypatch: pytest.MonkeyPatch,
    primary: OpenAIModelSourceConfig | ArkModelSourceConfig,
) -> None:
    calls: list[dict[str, object]] = []

    def build(**kwargs: object) -> object:
        calls.append(kwargs)
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr(agent_factory, "DeepAgentsSupervisorModel", build)
    config = _agent_config(fallback=True).model_copy(update={
        "supports_vision_input": True,
        "fallback": AgentFallbackConfig(
            source="fallback", model="fallback-model",
            supports_vision_input=True,
            request_timeout_seconds=17, max_output_tokens=2_048,
        ),
    })
    factory = agent_factory.AgentModelFactory(
        config,
        {"primary": primary, "fallback": ArkModelSourceConfig()},
        environ={"DEEPSEEK_API_KEY": "deepseek-secret", "ARK_API_KEY": "ark-secret"},
    )
    try:
        first = factory.build_deep_agent(**_build_arguments())
        replacement = factory.build_deep_agent(**{
            **_build_arguments(), "request_timeout_seconds": 45,
        })

        assert first.session_store is replacement.session_store
        assert calls[0]["provider_name"] == primary.provider
        assert calls[0]["model"] == "role-model"
        assert calls[0]["request_timeout_seconds"] == 60
        assert calls[1]["request_timeout_seconds"] == 45
        assert calls[0]["max_input_chars"] == 120_000
        assert calls[0]["supports_vision_input"] is True
        adapter = calls[0]["agent_model"]
        assert adapter.model_name == "role-model"
        assert adapter.max_output_tokens == 4_096
        assert adapter._request_timeout_seconds == 60
        assert adapter.supports_vision_input is True
        assert adapter.fallback_model_name == "fallback-model"
        assert adapter.context_window_policy is calls[0]["context_window_policy"]
        if isinstance(primary, OpenAIModelSourceConfig):
            assert isinstance(adapter, agent_factory.OpenAIAgentModel)
            assert adapter.profile == "deepseek"
            assert adapter.reasoning_effort == "high"
            assert adapter.base_url == "https://api.deepseek.com"
            assert adapter._api_key == "deepseek-secret"
            fallback = adapter._fallback_adapter
            assert fallback.max_output_tokens == 2_048
            assert fallback.request_timeout_seconds == 17
            assert fallback.supports_vision_input is True
            assert fallback._api_key == "ark-secret"
        else:
            assert isinstance(adapter, agent_factory.ArkFunctionCallingModel)
            assert adapter._api_key == "ark-secret"
            assert adapter._fallback_max_output_tokens == 2_048
            assert adapter._fallback_request_timeout_seconds == 17
            assert adapter._fallback_supports_vision_input is True
    finally:
        factory.close()


def _ratio_binding(**updates: object) -> ModelBinding:
    values = dict(
        source="primary", model="configured-model", request_timeout_seconds=90,
        max_output_tokens=8_192, max_input_chars=120_000,
        context_window_tokens=262_144, context_safety_margin_tokens=4_096,
        auto_compact_tokens=200_000, compact_target_tokens=150_000,
        auto_compact_ratio=0.85,
    )
    values.update(updates)
    return ModelBinding(**values)


def _install_deep_agent_capture(monkeypatch):
    calls: list[dict[str, object]] = []

    def build(**kwargs: object) -> object:
        calls.append(kwargs)
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr(agent_factory, "DeepAgentsSupervisorModel", build)
    return calls


def test_deep_agent_uses_discovered_capacity_for_adapter_and_supervisor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_deep_agent_capture(monkeypatch)
    discovered: list[tuple[str, str, str | None, bool]] = []

    def discover(protocol, base_url, api_key, *, allow_local):
        discovered.append((protocol, base_url, api_key, allow_local))
        return {"role-model": 1_048_576}

    monkeypatch.setattr(agent_factory, "discover_model_context_windows", discover)
    factory = agent_factory.AgentModelFactory(
        _agent_config(), {"primary": _openai_source()}, binding=_ratio_binding(),
        environ={"DEEPSEEK_API_KEY": "deepseek-secret"},
    )
    try:
        factory.build_deep_agent(**{**_build_arguments(), "max_output_tokens": 8_192})
        policy = calls[0]["context_window_policy"]
        assert discovered == [
            ("openai_chat", "https://api.deepseek.com", "deepseek-secret", False),
        ]
        assert policy.context_window_tokens == 1_048_576
        assert policy.output_reserve_tokens == 8_192
        assert policy.safety_margin_tokens == 4_096
        assert policy.auto_compact_tokens == 891_289
        assert policy.compact_target_tokens == 24_000
        # The SDK adapter and the DeepAgents supervisor must share one effective policy.
        assert calls[0]["agent_model"].context_window_policy is policy
    finally:
        factory.close()


def test_deep_agent_context_metadata_is_cached_per_source_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_deep_agent_capture(monkeypatch)
    discovered: list[str] = []

    def discover(protocol, base_url, api_key, *, allow_local):
        discovered.append(base_url)
        return {"model-a": 1_048_576, "model-b": 200_000}

    monkeypatch.setattr(agent_factory, "discover_model_context_windows", discover)
    factory = agent_factory.AgentModelFactory(
        _agent_config(), {"primary": _openai_source()}, binding=_ratio_binding(),
        environ={"DEEPSEEK_API_KEY": "deepseek-secret"},
    )
    try:
        for model in ("model-a", "model-a", "model-b"):
            factory.build_deep_agent(**{**_build_arguments(), "model": model})
        # One metadata request per source/model; rebuilding the same role is cached.
        assert discovered == ["https://api.deepseek.com", "https://api.deepseek.com"]
        first, repeated, switched = (call["context_window_policy"] for call in calls)
        assert first == repeated
        assert first.auto_compact_tokens == 891_289
        assert switched.context_window_tokens == 200_000
        assert switched.auto_compact_tokens == 170_000
        assert switched.compact_target_tokens == 24_000
    finally:
        factory.close()


def test_ratio_none_keeps_the_previous_shared_policy_without_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_deep_agent_capture(monkeypatch)

    def discover(*args, **kwargs):
        raise AssertionError("metadata must not be requested without a ratio")

    monkeypatch.setattr(agent_factory, "discover_model_context_windows", discover)
    factory = agent_factory.AgentModelFactory(
        _agent_config(), {"primary": _openai_source()},
        binding=_ratio_binding(auto_compact_ratio=None),
        environ={"DEEPSEEK_API_KEY": "deepseek-secret"},
    )
    try:
        factory.build_deep_agent(**_build_arguments())
        policy = calls[0]["context_window_policy"]
        assert policy.context_window_tokens == 64_000
        assert policy.auto_compact_tokens == 48_000
        assert policy.compact_target_tokens == 24_000
    finally:
        factory.close()


def test_ark_deep_agent_uses_declared_capacity_without_model_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_deep_agent_capture(monkeypatch)

    def discover(*args, **kwargs):
        raise AssertionError("Ark has no model-list endpoint")

    monkeypatch.setattr(agent_factory, "discover_model_context_windows", discover)
    factory = agent_factory.AgentModelFactory(
        _agent_config(), {"primary": ArkModelSourceConfig()}, binding=_ratio_binding(),
        environ={"ARK_API_KEY": "ark-secret"},
    )
    try:
        factory.build_deep_agent(**_build_arguments())
        policy = calls[0]["context_window_policy"]
        assert policy.context_window_tokens == 262_144
        assert policy.auto_compact_tokens == 222_822
        assert policy.compact_target_tokens == 24_000
    finally:
        factory.close()


def test_deep_agent_without_declared_capacity_keeps_the_shared_static_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_deep_agent_capture(monkeypatch)

    def discover(*args, **kwargs):
        return None

    monkeypatch.setattr(agent_factory, "discover_model_context_windows", discover)
    factory = agent_factory.AgentModelFactory(
        _agent_config(), {"primary": _openai_source()},
        binding=_ratio_binding(
            context_window_tokens=None, context_safety_margin_tokens=None,
            auto_compact_tokens=None, compact_target_tokens=None,
        ),
        environ={"DEEPSEEK_API_KEY": "deepseek-secret"},
    )
    try:
        factory.build_deep_agent(**_build_arguments())
        policy = calls[0]["context_window_policy"]
        assert policy.context_window_tokens == 64_000
        assert policy.auto_compact_tokens == 48_000
        assert policy.compact_target_tokens == 24_000
    finally:
        factory.close()


def test_deep_agent_metadata_failure_uses_declared_binding_capacity_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    calls = _install_deep_agent_capture(monkeypatch)
    discovered: list[str] = []

    def discover(protocol, base_url, api_key, *, allow_local):
        discovered.append(base_url)
        raise ValueError("模型列表接口返回 HTTP 404，可手动填写模型 ID")

    monkeypatch.setattr(agent_factory, "discover_model_context_windows", discover)
    factory = agent_factory.AgentModelFactory(
        _agent_config(), {"primary": _openai_source()},
        binding=_ratio_binding(
            context_window_tokens=131_072,
            auto_compact_tokens=100_000, compact_target_tokens=60_000,
        ),
        environ={"DEEPSEEK_API_KEY": "deepseek-secret"},
    )
    try:
        factory.build_deep_agent(**_build_arguments())
        factory.build_deep_agent(**_build_arguments())
        assert len(discovered) == 1
        policy = calls[0]["context_window_policy"]
        assert calls[1]["context_window_policy"].context_window_tokens == 131_072
        assert policy.context_window_tokens == 131_072
        assert policy.auto_compact_tokens == 111_411
        assert policy.compact_target_tokens == 24_000
        assert caplog.text.count("event=agent_model_context_capacity_unavailable") == 1
    finally:
        factory.close()


def test_deep_agent_uses_the_smaller_configured_fallback_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_deep_agent_capture(monkeypatch)
    discovered: list[str] = []

    def discover(protocol, base_url, api_key, *, allow_local):
        discovered.append(base_url)
        return {"role-model": 1_048_576}

    monkeypatch.setattr(agent_factory, "discover_model_context_windows", discover)
    fallback = ModelBinding(
        source="fallback", model="fallback-model", request_timeout_seconds=90,
        max_output_tokens=8_192, max_input_chars=120_000,
        context_window_tokens=65_536, context_safety_margin_tokens=4_096,
        auto_compact_tokens=50_000, compact_target_tokens=24_000,
    )
    factory = agent_factory.AgentModelFactory(
        _agent_config(fallback=True),
        {"primary": _openai_source(), "fallback": ArkModelSourceConfig()},
        binding=_ratio_binding(fallback=fallback),
        environ={"DEEPSEEK_API_KEY": "deepseek-secret", "ARK_API_KEY": "ark-secret"},
    )
    try:
        factory.build_deep_agent(**_build_arguments())
        policy = calls[0]["context_window_policy"]
        assert discovered == ["https://api.deepseek.com"]
        assert policy.context_window_tokens == 65_536
        assert policy.auto_compact_tokens == 55_705
        assert policy.compact_target_tokens == 24_000
        assert policy.auto_compact_tokens < policy.maximum_input_tokens
    finally:
        factory.close()


def test_structured_and_loop_match_deep_agent_dynamic_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_deep_agent_capture(monkeypatch)
    discovered: list[str] = []

    def discover(protocol, base_url, api_key, *, allow_local):
        discovered.append(base_url)
        return {"role-model": 1_048_576}

    monkeypatch.setattr(agent_factory, "discover_model_context_windows", discover)
    factory = agent_factory.AgentModelFactory(
        _agent_config(), {"primary": _openai_source()}, binding=_ratio_binding(),
        environ={"DEEPSEEK_API_KEY": "deepseek-secret"},
    )
    arguments = {**_build_arguments(), "max_output_tokens": 8_192}
    try:
        factory.build_deep_agent(**arguments)
        structured = factory.build_structured(**arguments)
        loop = factory.build_loop(**arguments)

        parent = calls[0]["context_window_policy"]
        assert parent.context_window_tokens == 1_048_576
        assert parent.auto_compact_tokens == 891_289
        assert parent.compact_target_tokens == 24_000
        # Same binding and factory: one metadata lookup, identical API controls.
        assert discovered == ["https://api.deepseek.com"]
        assert structured.context_window_policy == parent
        assert loop.context_window_policy == parent
    finally:
        factory.close()


@pytest.mark.parametrize("builder", ["build_structured", "build_loop"])
def test_api_builder_capacity_switches_with_the_selected_model(
    monkeypatch: pytest.MonkeyPatch, builder: str,
) -> None:
    discovered: list[str] = []

    def discover(protocol, base_url, api_key, *, allow_local):
        discovered.append(base_url)
        return {"model-a": 1_048_576, "model-b": 200_000}

    monkeypatch.setattr(agent_factory, "discover_model_context_windows", discover)
    factory = agent_factory.AgentModelFactory(
        _agent_config(), {"primary": _openai_source()}, binding=_ratio_binding(),
        environ={"DEEPSEEK_API_KEY": "deepseek-secret"},
    )
    try:
        first = getattr(factory, builder)(**{**_build_arguments(), "model": "model-a"})
        repeated = getattr(factory, builder)(**{**_build_arguments(), "model": "model-a"})
        switched = getattr(factory, builder)(**{**_build_arguments(), "model": "model-b"})

        assert discovered == ["https://api.deepseek.com", "https://api.deepseek.com"]
        assert first.context_window_policy == repeated.context_window_policy
        assert first.context_window_policy.context_window_tokens == 1_048_576
        assert first.context_window_policy.auto_compact_tokens == 891_289
        assert switched.context_window_policy.context_window_tokens == 200_000
        assert switched.context_window_policy.auto_compact_tokens == 170_000
        assert switched.context_window_policy.compact_target_tokens == 24_000
    finally:
        factory.close()


@pytest.mark.parametrize("builder", ["build_structured", "build_loop"])
def test_api_builder_uses_the_smaller_configured_fallback_capacity(
    monkeypatch: pytest.MonkeyPatch, builder: str,
) -> None:
    discovered: list[str] = []

    def discover(protocol, base_url, api_key, *, allow_local):
        discovered.append(base_url)
        return {"role-model": 1_048_576}

    monkeypatch.setattr(agent_factory, "discover_model_context_windows", discover)
    fallback = ModelBinding(
        source="fallback", model="fallback-model", request_timeout_seconds=90,
        max_output_tokens=8_192, max_input_chars=120_000,
        context_window_tokens=65_536, context_safety_margin_tokens=4_096,
        auto_compact_tokens=50_000, compact_target_tokens=24_000,
    )
    factory = agent_factory.AgentModelFactory(
        _agent_config(fallback=True),
        {"primary": _openai_source(), "fallback": ArkModelSourceConfig()},
        binding=_ratio_binding(fallback=fallback),
        environ={"DEEPSEEK_API_KEY": "deepseek-secret", "ARK_API_KEY": "ark-secret"},
    )
    try:
        adapter = getattr(factory, builder)(**_build_arguments())
        policy = adapter.context_window_policy
        assert discovered == ["https://api.deepseek.com"]
        assert policy.context_window_tokens == 65_536
        assert policy.auto_compact_tokens == 55_705
        assert policy.compact_target_tokens == 24_000
    finally:
        factory.close()


@pytest.mark.parametrize("builder", ["build_structured", "build_loop"])
def test_api_builder_metadata_failure_uses_declared_capacity_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, builder: str,
) -> None:
    discovered: list[str] = []

    def discover(protocol, base_url, api_key, *, allow_local):
        discovered.append(base_url)
        raise ValueError("模型列表接口返回 HTTP 404，可手动填写模型 ID")

    monkeypatch.setattr(agent_factory, "discover_model_context_windows", discover)
    factory = agent_factory.AgentModelFactory(
        _agent_config(), {"primary": _openai_source()},
        binding=_ratio_binding(
            context_window_tokens=131_072,
            auto_compact_tokens=100_000, compact_target_tokens=60_000,
        ),
        environ={"DEEPSEEK_API_KEY": "deepseek-secret"},
    )
    try:
        first = getattr(factory, builder)(**_build_arguments())
        repeated = getattr(factory, builder)(**_build_arguments())
        assert len(discovered) == 1
        assert first.context_window_policy.context_window_tokens == 131_072
        assert repeated.context_window_policy.context_window_tokens == 131_072
        assert first.context_window_policy.auto_compact_tokens == 111_411
        assert caplog.text.count("event=agent_model_context_capacity_unavailable") == 1
    finally:
        factory.close()


@pytest.mark.parametrize("builder", ["build_structured", "build_loop"])
def test_api_builder_without_ratio_keeps_the_previous_shared_policy(
    monkeypatch: pytest.MonkeyPatch, builder: str,
) -> None:
    def discover(*args, **kwargs):
        raise AssertionError("metadata must not be requested without a ratio")

    monkeypatch.setattr(agent_factory, "discover_model_context_windows", discover)
    factory = agent_factory.AgentModelFactory(
        _agent_config(), {"primary": _openai_source()},
        binding=_ratio_binding(auto_compact_ratio=None),
        environ={"DEEPSEEK_API_KEY": "deepseek-secret"},
    )
    try:
        adapter = getattr(factory, builder)(**_build_arguments())
        policy = adapter.context_window_policy
        assert policy.context_window_tokens == 64_000
        assert policy.auto_compact_tokens == 48_000
        assert policy.compact_target_tokens == 24_000
    finally:
        factory.close()


@pytest.mark.parametrize("source", [CodexModelSourceConfig(), ClaudeCodeModelSourceConfig()])
def test_deep_agent_rejects_native_runtime_sources_without_changing_role_models(
    monkeypatch: pytest.MonkeyPatch,
    source: CodexModelSourceConfig | ClaudeCodeModelSourceConfig,
) -> None:
    monkeypatch.setattr(
        agent_factory, "CodexAgentModel",
        lambda **kwargs: SimpleNamespace(provider_name="codex", **kwargs),
    )
    monkeypatch.setattr(
        agent_factory, "ClaudeAgentModel",
        lambda **kwargs: SimpleNamespace(provider_name="claude_code", **kwargs),
    )
    factory = agent_factory.AgentModelFactory(
        _agent_config(), {"primary": source}, environ={},
    )
    try:
        assert factory.build_loop(**_build_arguments()).provider_name == source.provider
        assert factory.build_structured(**_build_arguments()).provider_name == source.provider
        with pytest.raises(ValueError, match="select native Codex/Claude through Bridge"):
            factory.build_deep_agent(**_build_arguments())
    finally:
        factory.close()


@pytest.mark.parametrize("builder", ["build_structured", "build_loop", "build_deep_agent", "build_supervisor"])
def test_factory_closes_shared_supervisor_resources_once_and_rejects_new_models(
    monkeypatch: pytest.MonkeyPatch,
    builder: str,
) -> None:
    closed: list[str] = []
    session_store = SimpleNamespace(close=lambda: closed.append("sessions"))
    monkeypatch.setattr(agent_factory, "DeepAgentSessionStore", lambda: session_store)
    factory = agent_factory.AgentModelFactory(
        _agent_config(), {"primary": _openai_source()},
        environ={"DEEPSEEK_API_KEY": "deepseek-secret"},
    )

    factory.close()
    factory.close()

    assert closed == ["sessions"]
    with pytest.raises(RuntimeError, match="factory is closed"):
        options = {"harness": "deepagents"} if builder == "build_supervisor" else {}
        getattr(factory, builder)(**options, **_build_arguments())


def test_supervisor_close_preserves_checkpoint_after_factory_shutdown() -> None:
    factory = agent_factory.AgentModelFactory(
        _agent_config(), {"primary": _openai_source()},
        environ={"DEEPSEEK_API_KEY": "deepseek-secret"},
    )
    first = factory.build_deep_agent(**_build_arguments())
    saver = first.session_store.checkpointer
    thread = {"configurable": {"thread_id": "conversation", "checkpoint_ns": ""}}
    saver.put(thread, empty_checkpoint(), {}, {})
    try:
        first.close()
        replacement = factory.build_deep_agent(**_build_arguments())
        assert replacement.session_store is first.session_store
        assert saver.get(thread) is not None
        replacement.close()
        assert saver.get(thread) is not None
    finally:
        factory.close()
    assert saver.get(thread) is not None


@pytest.mark.parametrize("primary", [_openai_source(), ArkModelSourceConfig()])
@pytest.mark.parametrize("total_timeout_enabled", [True, False])
def test_explicit_deepagents_supervisor_retains_shared_sessions_and_provider_controls(primary, total_timeout_enabled):
    factory = agent_factory.AgentModelFactory(
        _agent_config(), {"primary": primary},
        environ={"DEEPSEEK_API_KEY": "deepseek-secret", "ARK_API_KEY": "ark-secret"},
    )
    first = factory.build_supervisor(
        harness="deepagents", **_build_arguments(), total_timeout_enabled=total_timeout_enabled,
    )
    try:
        first.close()
        replacement = factory.build_supervisor(
            harness="deepagents", **_build_arguments(), total_timeout_enabled=total_timeout_enabled,
        )
        assert replacement.session_store is first.session_store
        assert replacement.provider_name == primary.provider
        assert replacement.model_name == "role-model"
        assert replacement._request_timeout_seconds == 60
        assert replacement._max_input_chars == 120_000
        assert replacement.total_timeout_enabled is total_timeout_enabled
        replacement.close()
    finally:
        factory.close()


@pytest.mark.parametrize("source,harness,reason", [
    (_openai_source(), "codex", "codex_app_server source"),
    (CodexModelSourceConfig(), "deepagents", "OpenAI-compatible or Ark"),
    (CodexModelSourceConfig(), "unknown", "must be deepagents or codex"),
])
def test_supervisor_rejects_harness_source_mismatch(source, harness, reason):
    factory = agent_factory.AgentModelFactory(
        _agent_config(), {"primary": source}, environ={"DEEPSEEK_API_KEY": "deepseek-secret"},
    )
    try:
        with pytest.raises(ValueError, match=reason):
            factory.build_supervisor(harness=harness, **_build_arguments())
    finally:
        factory.close()


@pytest.mark.parametrize("total_timeout_enabled", [True, False])
def test_codex_supervisor_factory_reuses_executor_and_native_context_controls(monkeypatch, total_timeout_enabled):
    adapters = []

    class NativeModel:
        provider_name = "codex"

        def __init__(self, **options):
            self.options = options
            self.model_name = options["model"]
            self.calls = []
            self.closed = 0
            adapters.append(self)

        def run_agent_loop(self, prompt, **options):
            self.calls.append((prompt, options))
            options["handle_text"]("answer")

        def close(self):
            self.closed += 1

    monkeypatch.setattr(agent_factory, "CodexAgentModel", NativeModel)
    binding = ModelBinding(
        source="primary", model="configured-model", supports_vision_input=True,
        request_timeout_seconds=7, max_output_tokens=1_024, max_input_chars=5_000,
        context_window_tokens=32_000, context_safety_margin_tokens=1_024,
        auto_compact_tokens=24_000, compact_target_tokens=12_000,
    )
    config = _agent_config().model_copy(update={"supports_vision_input": True})
    factory = agent_factory.AgentModelFactory(
        config, {"primary": CodexModelSourceConfig(max_concurrency=2)}, binding=binding, environ={},
    )
    model = factory.build_supervisor(
        harness="codex", **_build_arguments(), total_timeout_enabled=total_timeout_enabled,
    )
    replacement = factory.build_supervisor(
        harness="codex", **_build_arguments(), total_timeout_enabled=total_timeout_enabled,
    )
    try:
        assert isinstance(model, CodexSupervisorModel)
        assert model.model_name == "role-model" and model.provider_name == "codex"
        assert model.supports_vision_input is True
        assert adapters[0].options["executor"] is adapters[1].options["executor"]
        assert adapters[0].options["request_timeout_seconds"] == 7
        assert adapters[0].options["max_output_tokens"] == 1_024
        assert adapters[0].options["max_input_chars"] is None
        assert adapters[0].options["context_window_policy"] is None
        assert all(adapter.options["total_timeout_enabled"] is total_timeout_enabled for adapter in adapters)
        texts = []
        model.run_agent(
            "Supervisor instruction", thread_id="conversation", message_id="answer",
            user_message="user question", history=[], tools=[], max_model_calls=2,
            handle_text=texts.append, request_timeout_seconds=60, max_input_chars=120_000,
        )
        assert texts == ["answer"]
        assert adapters[0].calls[0][1]["request_timeout_seconds"] == 7
        assert adapters[0].calls[0][1]["max_input_chars"] is None
        assert adapters[0].calls[0][1]["max_model_calls"] == 2
        model.confirm_turn("conversation", "answer", "SQL answer")
        model.run_agent(
            "Supervisor instruction", thread_id="conversation", message_id="smaller",
            user_message="user question", history=[], tools=[], max_model_calls=1,
            handle_text=texts.append, request_timeout_seconds=1, max_input_chars=1_000,
        )
        assert adapters[0].calls[1][1]["request_timeout_seconds"] == 1
        assert adapters[0].calls[1][1]["max_input_chars"] is None
    finally:
        model.close()
        model.close()
        replacement.close()
        factory.close()
        factory.close()
    assert [adapter.closed for adapter in adapters] == [1, 1]
    assert adapters[0].options["executor"]._shutdown is True
    with pytest.raises(RuntimeError, match="factory is closed"):
        factory.build_supervisor(harness="codex", **_build_arguments())


@pytest.mark.parametrize("total_timeout_enabled", [True, False])
def test_bound_codex_supervisor_total_timeout_switch_leaves_stage_models_enabled(monkeypatch, total_timeout_enabled):
    clock = [100.0]
    started, release = Event(), Event()
    monkeypatch.setattr("gateway.codex.agent.monotonic", lambda: clock[0])

    class Session:
        closed = False

        def run_turn(self, prompt, *, output_schema=None):
            started.set()
            assert release.wait(timeout=1)
            return SimpleNamespace(final_response="Native answer", usage=None)

        def close(self):
            self.closed = True
            release.set()

    session = Session()
    monkeypatch.setattr("gateway.codex.agent.CodexSession", lambda **_options: session)
    binding = ModelBinding(
        source="primary", model="model", request_timeout_seconds=7, max_output_tokens=4_096,
        max_input_chars=120_000, context_window_tokens=64_000,
        context_safety_margin_tokens=4_096, auto_compact_tokens=48_000, compact_target_tokens=24_000,
    )
    factory = agent_factory.AgentModelFactory(
        _agent_config(), {"primary": CodexModelSourceConfig()}, binding=binding, environ={},
    )
    model = factory.build_supervisor(
        harness="codex", **_build_arguments(), total_timeout_enabled=total_timeout_enabled,
    )
    structured = factory.build_structured(**_build_arguments())
    loop = factory.build_loop(**_build_arguments())

    def check():
        if started.is_set():
            clock[0] = 140.0
            release.set()

    texts = []
    try:
        assert structured.total_timeout_enabled is True
        assert loop.total_timeout_enabled is True
        assert model._agent_model._timeout == 7
        options = dict(
            thread_id="conversation", message_id="answer", user_message="question", history=[],
            tools=[], max_model_calls=2, handle_text=texts.append, check_control=check,
            request_timeout_seconds=None,
        )
        if total_timeout_enabled:
            with pytest.raises(AgentModelTimeoutError):
                model.run_agent("instruction", **options)
        else:
            model.run_agent("instruction", **options)
            assert texts == ["Native answer"]
        assert clock[0] == 140
        assert session.closed
    finally:
        release.set()
        model.close()
        structured.close()
        loop.close()
        factory.close()
