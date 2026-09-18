import json
import sys
from types import SimpleNamespace

import pytest

from scripts import local_services
from scripts.local_services import api_ready, config_digest, dotenv_variable_names


def test_dotenv_variable_names_only_returns_safe_assignment_names(
    tmp_path,
) -> None:
    marker = tmp_path / "must-not-run"
    env_file = tmp_path / ".env.local"
    env_file.write_text(
        "\n".join([
            "# comment",
            "ARK_API_KEY=private-value",
            " export CUSTOM_CHAT_TOKEN = 'another-private-value'",
            "DUPLICATE=first",
            "DUPLICATE=second",
            "INVALID-NAME=ignored",
            "1INVALID=ignored",
            f"COMMAND=$(touch {marker})",
        ]),
        encoding="utf-8",
    )

    assert dotenv_variable_names(env_file) == (
        "ARK_API_KEY",
        "CUSTOM_CHAT_TOKEN",
        "DUPLICATE",
        "COMMAND",
    )
    assert not marker.exists()


def test_env_name_command_outputs_names_without_creating_runtime_state(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    env_file = tmp_path / ".env.local"
    env_file.write_text("CUSTOM_CHAT_TOKEN=private-value\n", encoding="utf-8")
    state = tmp_path / "state"
    monkeypatch.setattr(local_services, "STATE", state)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "local_services.py",
            "_env-names",
            "--env-file",
            str(env_file),
        ],
    )

    assert local_services.main() == 0
    assert capsys.readouterr().out == "CUSTOM_CHAT_TOKEN\n"
    assert not state.exists()


def test_config_digest_tracks_model_api_key_changes(monkeypatch) -> None:
    monkeypatch.setenv("ARK_API_KEY", "ark-first")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-first")
    initial = config_digest()

    monkeypatch.setenv("ARK_API_KEY", "ark-second")
    ark_changed = config_digest()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-second")
    both_changed = config_digest()

    assert initial != ark_changed != both_changed
    assert len(initial) == len(ark_changed) == len(both_changed) == 64


def test_config_digest_tracks_configured_parse_fallback_api_key(monkeypatch) -> None:
    config = SimpleNamespace(
        agent_model=SimpleNamespace(api_key_env="DEEPSEEK_API_KEY"),
        video_parser=SimpleNamespace(
            fallback=SimpleNamespace(
                enabled=True,
                deepseek=SimpleNamespace(api_key_env="CUSTOM_DEEPSEEK_API_KEY"),
            ),
        ),
    )
    monkeypatch.setattr(local_services, "load_config", lambda: config)
    monkeypatch.setenv("CUSTOM_DEEPSEEK_API_KEY", "first")
    initial = config_digest()
    monkeypatch.setenv("CUSTOM_DEEPSEEK_API_KEY", "second")

    assert config_digest() != initial


def test_launch_validates_digest_before_spawning(monkeypatch) -> None:
    def fail_digest() -> str:
        raise ValueError("invalid model config")

    monkeypatch.setattr(local_services, "CHILDREN", {})
    monkeypatch.setattr(local_services, "config_digest", fail_digest)
    monkeypatch.setattr(
        local_services.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("Popen must not run"),
    )

    with pytest.raises(ValueError, match="invalid model config"):
        local_services.launch("digest-failure", ["unused"])

    assert local_services.CHILDREN == {}


@pytest.mark.parametrize(
    ("health", "expected"),
    [
        (
            {
                "status": "ok",
                "components": {"parseConsumer": {"status": "running"}},
            },
            True,
        ),
        (
            {
                "status": "degraded",
                "components": {"parseConsumer": {"status": "failed"}},
            },
            False,
        ),
        (
            {
                "status": "ok",
                "components": {"parseConsumer": {"status": "stopped"}},
            },
            False,
        ),
        ({"status": "ok"}, False),
    ],
)
def test_api_ready_requires_running_parse_consumer(monkeypatch, health, expected) -> None:
    requested = []

    def get(url: str) -> bytes:
        requested.append(url)
        return json.dumps(health).encode()

    monkeypatch.setattr(local_services, "get", get)

    assert api_ready("http://127.0.0.1:9621") is expected
    assert requested == ["http://127.0.0.1:9621/healthz"]


@pytest.mark.parametrize("payload", [None, b"not-json", b"[]"])
def test_api_ready_rejects_invalid_health_payload(monkeypatch, payload) -> None:
    monkeypatch.setattr(local_services, "get", lambda _url: payload)

    assert not api_ready("http://127.0.0.1:9621")


def test_start_requires_model_key_before_launching_server(monkeypatch) -> None:
    monkeypatch.setenv("ANIVAG_HTTP_PORT", "9621")
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.setattr(local_services, "listening", lambda _port: False)
    monkeypatch.setattr(local_services, "owned_pid", lambda _name: None)

    with pytest.raises(RuntimeError, match="ARK_API_KEY"):
        local_services.start_applications()


def test_start_requires_deepseek_key_before_launching_server(monkeypatch) -> None:
    monkeypatch.setenv("ANIVAG_HTTP_PORT", "9621")
    monkeypatch.setenv("ARK_API_KEY", "ark-test")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(local_services, "listening", lambda _port: False)
    monkeypatch.setattr(local_services, "owned_pid", lambda _name: None)

    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        local_services.start_applications()


def test_start_requires_configured_parse_fallback_key_name(monkeypatch) -> None:
    config = SimpleNamespace(
        agent_model=SimpleNamespace(api_key_env="CUSTOM_DEEPSEEK_API_KEY"),
        video_parser=SimpleNamespace(
            fallback=SimpleNamespace(
                enabled=True,
                deepseek=SimpleNamespace(api_key_env="CUSTOM_DEEPSEEK_API_KEY"),
            ),
        ),
    )
    monkeypatch.setenv("ANIVAG_HTTP_PORT", "9621")
    monkeypatch.setenv("ARK_API_KEY", "ark-test")
    monkeypatch.delenv("CUSTOM_DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(local_services, "load_config", lambda: config)
    monkeypatch.setattr(local_services, "listening", lambda _port: False)
    monkeypatch.setattr(local_services, "owned_pid", lambda _name: None)

    with pytest.raises(RuntimeError, match="CUSTOM_DEEPSEEK_API_KEY"):
        local_services.start_applications()
