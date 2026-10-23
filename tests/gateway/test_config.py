from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from gateway.config import (
    ArkModelSourceConfig,
    ClaudeCodeModelSourceConfig,
    CodexModelSourceConfig,
    OpenAIAudioSourceConfig,
    OpenAIModelSourceConfig,
    PiHostConfig,
    TracingConfig,
    agent_api_key_env_names,
    load_config,
)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:18842/v1/", "http://localhost:18842/v1/",
    "http://[::1]:18842/v1/", "https://speech.example/v1/",
])
def test_audio_source_accepts_https_and_local_http(url: str) -> None:
    source = OpenAIAudioSourceConfig(
        provider="speech", base_url=url, api_key_env="SPEECH_KEY",
    )
    assert source.base_url == url.rstrip("/")


@pytest.mark.parametrize("url", [
    "http://speech.example/v1", "http://127.0.0.1.example/v1",
    "http://user:secret@127.0.0.1/v1", "http://127.0.0.1/v1?key=secret",
    "http://127.0.0.1/v1#fragment", "http://127.0.0.1 /v1",
])
def test_audio_source_rejects_remote_http_and_unsafe_local_urls(url: str) -> None:
    with pytest.raises(ValidationError, match="base_url"):
        OpenAIAudioSourceConfig(provider="speech", base_url=url, api_key_env="SPEECH_KEY")


def test_tracing_is_disabled_by_default_and_accepts_local_backend() -> None:
    assert not TracingConfig().enabled
    assert not load_config().tracing.enabled
    settings = TracingConfig(enabled=True, base_url="http://127.0.0.1:3000/")
    assert settings.base_url == "http://127.0.0.1:3000"
    assert settings.public_key_env == "LANGFUSE_PUBLIC_KEY"
    assert settings.secret_key_env == "LANGFUSE_SECRET_KEY"


@pytest.mark.parametrize("configuration", [
    {"base_url": "https://user:password@example.test"},
    {"base_url": "https://example.test?secret=value"},
    {"base_url": "file:///tmp/traces"},
    {"queue_size": 0},
    {"export_timeout_seconds": 0},
    {"close_timeout_seconds": 0.01},
    {"public_key_env": ""},
    {"input_export": True},
])
def test_tracing_configuration_rejects_invalid_bounds_and_payload_export(configuration) -> None:
    with pytest.raises(ValidationError):
        TracingConfig.model_validate(configuration)


def _config_payload() -> dict[str, object]:
    return {
        "model_sources": {
            "primary": {
                "adapter": "openai",
                "provider": "deepseek",
                "profile": "deepseek",
                "base_url": "https://api.deepseek.test",
                "api_key_env": "TEST_DEEPSEEK_API_KEY",
            },
            "fallback": {
                "adapter": "ark",
                "provider": "ark",
                "api_key_env": "TEST_ARK_API_KEY",
            },
        },
        "agent_model": {
            "source": "primary",
            "model": "test-agent",
            "max_output_tokens": 2048,
            "request_timeout_seconds": 30,
            "fallback": {"source": "fallback", "model": "test-ark"},
        },
        "video_parser": {
            "transcript_model": "test-transcript",
            "video_model": "test-video",
            "transcript_fps": 1.0,
            "video_fps": 1.0,
            "fallback": {
                "enabled": True,
                "frame_fps": 0.5,
                "group_duration_seconds": 30,
                "max_frames_per_group": 15,
                "max_height": 720,
                "deepseek": {
                    "provider": "deepseek",
                    "model": "test-deepseek",
                    "base_url": "https://api.deepseek.test",
                    "api_key_env": "TEST_PARSE_DEEPSEEK_API_KEY",
                    "reasoning_effort": "none",
                    "max_output_tokens": 4096,
                    "request_timeout_seconds": 120,
                },
            },
        },
        "multimodal_embedding": {
            "model": "test-embedding",
            "dimensions": 1024,
            "fps": 1.0,
        },
    }


def _legacy_agent() -> dict[str, object]:
    return {
        "provider": "deepseek",
        "model": "legacy-agent",
        "base_url": "https://api.deepseek.test",
        "api_key_env": "LEGACY_DEEPSEEK_API_KEY",
        "reasoning_effort": "none",
        "max_output_tokens": 2048,
        "request_timeout_seconds": 30,
    }


def _write_config(path: Path, payload: dict[str, object]) -> None:
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def test_repository_parse_defaults_use_three_fps_without_changing_asr_or_embedding() -> None:
    config = load_config(Path(__file__).resolve().parents[2] / "config.yaml")
    assert config.video_parser.video_fps == 3.0
    assert config.video_parser.transcript_fps == 1.0
    assert config.multimodal_embedding.fps == 1.0


def test_default_config_uses_canonical_agent_sources() -> None:
    config = load_config()

    assert config.agent_model.source == "deepseek_api"
    assert config.agent_model.model == "deepseek-flash"
    assert config.agent_model.max_output_tokens == 8192
    assert config.agent_model.request_timeout_seconds == 180
    assert config.agent_model.fallback is not None
    assert config.agent_model.fallback.source == "ark_default"
    assert config.agent_model.fallback.model == "doubao-seed-2-0-lite-260428"

    primary = config.model_sources["deepseek_api"]
    assert isinstance(primary, OpenAIModelSourceConfig)
    assert primary.provider == "deepseek"
    assert primary.profile == "deepseek"
    assert primary.base_url == "https://api.deepseek.com"
    assert primary.api_key_env == "DEEPSEEK_API_KEY"
    assert isinstance(config.model_sources["ark_default"], ArkModelSourceConfig)
    assert agent_api_key_env_names(config) == ("ARK_API_KEY", "DEEPSEEK_API_KEY")

    assert config.video_parser.transcript_model == "doubao-seed-2-0-lite-260428"
    assert config.video_parser.vision_source == "codex_local"
    assert config.video_parser.video_model == "gpt-6-astra"
    assert config.video_parser.vision_max_output_tokens == 8192
    assert config.video_parser.vision_reasoning_effort == "low"
    resolved = config.resolve_model_binding("vision", environ={})
    assert resolved.supports_vision_input
    assert resolved.context_window_tokens == 128000
    assert resolved.request_timeout_seconds == 600
    assert config.multimodal_embedding.model == "doubao-embedding-vision-250615"
    assert config.agent_runtime["stage_timeout_seconds"] == 480


def test_parse_only_config_gets_legacy_compatible_agent_defaults(tmp_path: Path) -> None:
    payload = _config_payload()
    del payload["model_sources"]
    del payload["agent_model"]
    config_path = tmp_path / "parse-only.yaml"
    _write_config(config_path, payload)

    config = load_config(config_path)

    assert config.agent_model.model == "deepseek-flash"
    assert config.agent_model.fallback is not None
    assert config.agent_model.fallback.model == "test-transcript"
    assert isinstance(config.model_sources["legacy_agent"], OpenAIModelSourceConfig)


def test_gateway_accepts_agent_runtime_in_shared_yaml(tmp_path: Path) -> None:
    payload = _config_payload()
    payload["agent_runtime"] = {"max_tool_result_bytes": 8192}
    config_path = tmp_path / "with-agent-runtime.yaml"
    _write_config(config_path, payload)

    assert load_config(config_path).agent_runtime == {"max_tool_result_bytes": 8192}


def test_legacy_deepseek_agent_chat_is_normalized_once(tmp_path: Path) -> None:
    payload = _config_payload()
    del payload["model_sources"]
    payload["agent_chat"] = _legacy_agent()
    del payload["agent_model"]
    config_path = tmp_path / "legacy-agent-chat.yaml"
    _write_config(config_path, payload)

    config = load_config(config_path)

    assert config.agent_model.source == "legacy_agent"
    assert config.agent_model.model == "legacy-agent"
    assert config.agent_model.fallback is not None
    assert config.agent_model.fallback.model == "test-transcript"
    primary = config.model_sources["legacy_agent"]
    assert isinstance(primary, OpenAIModelSourceConfig)
    assert primary.provider == "deepseek"
    assert primary.profile == "deepseek"
    assert primary.api_key_env == "LEGACY_DEEPSEEK_API_KEY"
    assert isinstance(
        config.model_sources["legacy_ark_fallback"], ArkModelSourceConfig
    )


@pytest.mark.parametrize(
    ("provider", "fields", "source_type"),
    [
        (
            "codex",
            {"codex_bin": "/opt/codex/bin/codex", "max_concurrency": 6},
            CodexModelSourceConfig,
        ),
        (
            "claude-code",
            {"cli_path": "/opt/claude/bin/claude", "max_concurrency": 3},
            ClaudeCodeModelSourceConfig,
        ),
    ],
)
def test_legacy_local_agent_providers_are_normalized(
    tmp_path: Path,
    provider: str,
    fields: dict[str, object],
    source_type: type,
) -> None:
    payload = _config_payload()
    del payload["model_sources"]
    payload["agent_model"] = {
        "provider": provider,
        "model": "local-model",
        "max_output_tokens": 4096,
        "request_timeout_seconds": 90,
        **fields,
    }
    config_path = tmp_path / "legacy-local-agent.yaml"
    _write_config(config_path, payload)

    config = load_config(config_path)

    assert isinstance(config.model_sources["legacy_agent"], source_type)
    assert config.agent_model.model == "local-model"
    assert config.agent_model.fallback is None


def test_canonical_sources_parse_all_supported_adapters(tmp_path: Path) -> None:
    payload = _config_payload()
    sources = payload["model_sources"]
    assert isinstance(sources, dict)
    sources.update({
        "codex": {
            "adapter": "codex_app_server",
            "provider": "codex",
            "codex_bin": "/opt/codex",
        },
        "claude": {
            "adapter": "claude_sdk",
            "provider": "claude_code",
            "auth_mode": "local_subscription",
        },
    })
    config_path = tmp_path / "all-sources.yaml"
    _write_config(config_path, payload)

    config = load_config(config_path)

    assert isinstance(config.model_sources["codex"], CodexModelSourceConfig)
    assert isinstance(config.model_sources["claude"], ClaudeCodeModelSourceConfig)


def test_unselected_source_credentials_are_not_required(tmp_path: Path) -> None:
    payload = _config_payload()
    sources = payload["model_sources"]
    assert isinstance(sources, dict)
    sources["unused"] = {
        "adapter": "openai",
        "provider": "unused",
        "base_url": "https://unused.example.com",
        "api_key_env": "UNUSED_API_KEY",
    }
    config_path = tmp_path / "unused-source.yaml"
    _write_config(config_path, payload)

    config = load_config(config_path)

    assert agent_api_key_env_names(config) == (
        "TEST_ARK_API_KEY",
        "TEST_DEEPSEEK_API_KEY",
    )


def test_claude_api_key_auth_requires_environment_name(tmp_path: Path) -> None:
    payload = _config_payload()
    sources = payload["model_sources"]
    assert isinstance(sources, dict)
    sources["claude"] = {
        "adapter": "claude_sdk",
        "provider": "claude_code",
        "auth_mode": "api_key",
    }
    config_path = tmp_path / "claude-api-key-agent.yaml"
    _write_config(config_path, payload)

    with pytest.raises(ValidationError, match="api_key_env"):
        load_config(config_path)


def test_config_rejects_legacy_provider_mixed_with_model_sources(
    tmp_path: Path,
) -> None:
    payload = _config_payload()
    payload["agent_model"] = _legacy_agent()
    config_path = tmp_path / "ambiguous-agent.yaml"
    _write_config(config_path, payload)

    with pytest.raises(ValidationError, match="cannot be combined"):
        load_config(config_path)


def test_config_rejects_both_agent_model_and_legacy_alias(tmp_path: Path) -> None:
    payload = _config_payload()
    payload["agent_chat"] = _legacy_agent()
    config_path = tmp_path / "duplicate-agent.yaml"
    _write_config(config_path, payload)

    with pytest.raises(ValidationError, match="legacy alias"):
        load_config(config_path)


@pytest.mark.parametrize("path", [("source",), ("fallback", "source")])
def test_agent_model_rejects_unknown_source(
    tmp_path: Path,
    path: tuple[str, ...],
) -> None:
    payload = _config_payload()
    target = payload["agent_model"]
    assert isinstance(target, dict)
    for part in path[:-1]:
        target = target[part]
        assert isinstance(target, dict)
    target[path[-1]] = "missing"
    config_path = tmp_path / "unknown-source.yaml"
    _write_config(config_path, payload)

    with pytest.raises(ValidationError, match="not present"):
        load_config(config_path)


def test_agent_model_rejects_same_source_as_fallback(tmp_path: Path) -> None:
    payload = _config_payload()
    agent_model = payload["agent_model"]
    assert isinstance(agent_model, dict)
    fallback = agent_model["fallback"]
    assert isinstance(fallback, dict)
    fallback["source"] = agent_model["source"]
    config_path = tmp_path / "same-fallback-source.yaml"
    _write_config(config_path, payload)

    with pytest.raises(ValidationError, match="must differ"):
        load_config(config_path)


def test_legacy_agent_rejects_reasoning_effort_that_was_never_supported(
    tmp_path: Path,
) -> None:
    payload = _config_payload()
    del payload["model_sources"]
    legacy_agent = _legacy_agent()
    legacy_agent["reasoning_effort"] = "high"
    payload["agent_model"] = legacy_agent
    config_path = tmp_path / "unsupported-legacy-reasoning.yaml"
    _write_config(config_path, payload)

    with pytest.raises(ValidationError, match="only supports 'none'"):
        load_config(config_path)


def test_environment_can_select_config(monkeypatch, tmp_path: Path) -> None:
    payload = _config_payload()
    agent_model = payload["agent_model"]
    assert isinstance(agent_model, dict)
    agent_model["model"] = "custom-agent"
    sources = payload["model_sources"]
    assert isinstance(sources, dict)
    primary = sources["primary"]
    assert isinstance(primary, dict)
    primary["api_key_env"] = "CUSTOM_AGENT_API_KEY"
    video_parser = payload["video_parser"]
    assert isinstance(video_parser, dict)
    video_parser["transcript_model"] = "custom-transcript"
    fallback = video_parser["fallback"]
    assert isinstance(fallback, dict)
    fallback["group_duration_seconds"] = 15
    embedding = payload["multimodal_embedding"]
    assert isinstance(embedding, dict)
    embedding["dimensions"] = 512
    config_path = tmp_path / "custom.yaml"
    _write_config(config_path, payload)
    monkeypatch.setenv("ANIVAG_MODEL_CONFIG", str(config_path))

    config = load_config()

    assert config.agent_model.model == "custom-agent"
    assert isinstance(config.model_sources["primary"], OpenAIModelSourceConfig)
    assert config.model_sources["primary"].api_key_env == "CUSTOM_AGENT_API_KEY"
    assert config.video_parser.transcript_model == "custom-transcript"
    assert config.video_parser.fallback.group_duration_seconds == 15
    assert config.multimodal_embedding.dimensions == 512


@pytest.mark.parametrize(
    ("section", "missing_field"),
    [
        ("fallback", "group_duration_seconds"),
        ("fallback", "max_frames_per_group"),
        ("deepseek", "model"),
        ("deepseek", "base_url"),
        ("deepseek", "api_key_env"),
    ],
)
def test_parse_fallback_requires_pipeline_and_model_fields(
    section: str,
    missing_field: str,
    tmp_path: Path,
) -> None:
    payload = _config_payload()
    fallback = payload["video_parser"]["fallback"]
    target = fallback if section == "fallback" else fallback["deepseek"]
    del target[missing_field]
    config_path = tmp_path / "missing-parse-fallback.yaml"
    _write_config(config_path, payload)

    with pytest.raises(ValidationError):
        load_config(config_path)


@pytest.mark.parametrize(
    "base_url",
    [
        "api.deepseek.com",
        "http://api.deepseek.com",
        "https://user:secret@api.deepseek.com",
        "https://api.deepseek.com?tenant=other",
        "https://api.deepseek.com#other",
        "https://api.deepseek.com /v1",
    ],
)
@pytest.mark.parametrize("section", ["model_source", "video_fallback"])
def test_openai_compatible_base_urls_require_safe_https_endpoints(
    tmp_path: Path,
    section: str,
    base_url: str,
) -> None:
    payload = _config_payload()
    if section == "model_source":
        target = payload["model_sources"]["primary"]
    else:
        target = payload["video_parser"]["fallback"]["deepseek"]
    target["base_url"] = base_url
    config_path = tmp_path / f"unsafe-{section}.yaml"
    _write_config(config_path, payload)

    with pytest.raises(ValidationError, match="base_url"):
        load_config(config_path)


def test_base_urls_are_trimmed_and_canonicalized(tmp_path: Path) -> None:
    payload = _config_payload()
    payload["model_sources"]["primary"]["base_url"] = (
        "  https://api.deepseek.test/v1/  "
    )
    config_path = tmp_path / "canonical-base-url.yaml"
    _write_config(config_path, payload)

    primary = load_config(config_path).model_sources["primary"]
    assert isinstance(primary, OpenAIModelSourceConfig)
    assert primary.base_url == "https://api.deepseek.test/v1"


@pytest.mark.parametrize("configuration", [
    {"max_active_runs": 0}, {"max_active_runs": 65}, {"max_active_runs": True},
    {"max_model_requests": 0}, {"max_model_requests": 129},
    {"max_loaded_sessions": 0}, {"max_loaded_sessions": 4097},
    {"max_session_bytes": 0}, {"max_session_bytes": 1023},
    {"max_session_bytes": True}, {"max_session_bytes": 1024.5},
    {"max_cache_bytes": 0}, {"max_cache_bytes": 1023}, {"max_cache_bytes": True},
    {"max_session_bytes": 4096, "max_cache_bytes": 2048},
    {"max_transient_tasks": 0}, {"max_transient_tasks": 129},
    {"session_ttl_seconds": 0}, {"startup_timeout_seconds": 0},
    {"cancel_timeout_seconds": 0}, {"shutdown_timeout_seconds": 0},
    {"session_ttl_seconds": float("inf")}, {"startup_timeout_seconds": float("inf")},
    {"cancel_timeout_seconds": float("nan")}, {"shutdown_timeout_seconds": float("inf")},
    {"node_bin": ""}, {"session_dir": ""}, {"max_line_bytes": 1023},
    {"max_queue_bytes": 1024}, {"max_tool_output_bytes": 16 * 1024 * 1024},
    {"unknown_setting": True},
])
def test_pi_host_rejects_invalid_resource_and_transport_bounds(configuration):
    with pytest.raises(ValidationError):
        PiHostConfig.model_validate(configuration)


def test_pi_host_defaults_allow_bounded_media_transport():
    settings = PiHostConfig()
    assert settings.max_active_runs == 2
    assert settings.max_session_bytes == 64 * 1024 * 1024
    assert settings.max_cache_bytes == 256 * 1024 * 1024
    assert settings.max_queue_bytes >= settings.max_line_bytes
    assert settings.max_line_bytes > settings.max_tool_output_bytes >= 12 * 1024 * 1024


def test_pi_host_runtime_options_preserve_cache_budgets_and_resolve_session_path(tmp_path):
    session_dir = tmp_path / "work" / ".." / "sessions"
    settings = PiHostConfig(
        session_dir=str(session_dir), max_session_bytes=2048, max_cache_bytes=4096,
    )
    options = settings.runtime_options()
    assert options["sessionDir"] == str(tmp_path / "sessions")
    assert options["maxSessionBytes"] == 2048
    assert options["maxCacheBytes"] == 4096


def test_pi_host_cache_budget_can_equal_one_session_budget():
    settings = PiHostConfig(max_session_bytes=1024, max_cache_bytes=1024)
    assert settings.runtime_options()["maxCacheBytes"] == 1024


@pytest.mark.parametrize("source", ["primary", "fallback"])
def test_pi_harness_accepts_one_api_model_profile(tmp_path, source):
    payload = _config_payload()
    payload["supervisor_harness"] = "pi"
    payload["agent_model"] = {
        "source": source, "model": "cloud-agent", "max_output_tokens": 2048,
        "request_timeout_seconds": 30,
    }
    config_path = tmp_path / "pi.yaml"
    _write_config(config_path, payload)

    config = load_config(config_path)
    assert config.supervisor_harness == "pi"
    assert config.resolve_model_binding("agent").source == source


@pytest.mark.parametrize("native", [
    {"adapter": "codex_app_server", "provider": "codex"},
    {"adapter": "claude_sdk", "provider": "claude_code"},
])
def test_pi_harness_rejects_native_model_sources(tmp_path, native):
    payload = _config_payload()
    payload["supervisor_harness"] = "pi"
    payload["model_sources"]["primary"] = native
    payload["agent_model"].pop("fallback")
    config_path = tmp_path / "pi-native.yaml"
    _write_config(config_path, payload)
    with pytest.raises(ValidationError, match="pi supervisor_harness requires.*API source"):
        load_config(config_path)


def test_pi_harness_rejects_implicit_api_fallback(tmp_path):
    payload = _config_payload()
    payload["supervisor_harness"] = "pi"
    config_path = tmp_path / "pi-fallback.yaml"
    _write_config(config_path, payload)
    with pytest.raises(ValidationError, match="pi supervisor_harness does not support fallback"):
        load_config(config_path)
