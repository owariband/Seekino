import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from gateway.config import (
    ArkModelSourceConfig,
    ClaudeCodeModelSourceConfig,
    CodexModelSourceConfig,
    OpenAIModelSourceConfig,
)
from scripts import local_services
from scripts.local_services import api_ready, config_digest, dotenv_variable_names


def _model_config(
    *selected_sources,
    fallback_enabled: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        stage_source=lambda _stage: ArkModelSourceConfig(),
        selected_agent_sources=lambda: selected_sources,
        video_parser=SimpleNamespace(
            fallback=SimpleNamespace(
                enabled=fallback_enabled,
                deepseek=SimpleNamespace(api_key_env="PARSE_DEEPSEEK_API_KEY"),
            ),
        ),
        model_dump=lambda mode: {
            "model_sources": [source.model_dump() for source in selected_sources],
            "agent_runtime": {},
        },
    )


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
    config = _model_config(
        CodexModelSourceConfig(),
        fallback_enabled=True,
    )
    config.video_parser.fallback.deepseek.api_key_env = "CUSTOM_DEEPSEEK_API_KEY"
    monkeypatch.setattr(local_services, "load_config", lambda: config)
    monkeypatch.setenv("CUSTOM_DEEPSEEK_API_KEY", "first")
    initial = config_digest()
    monkeypatch.setenv("CUSTOM_DEEPSEEK_API_KEY", "second")

    assert config_digest() != initial


@pytest.mark.parametrize(
    "source",
    [
        CodexModelSourceConfig(),
        ClaudeCodeModelSourceConfig(),
    ],
)
def test_config_digest_local_cli_providers_do_not_require_agent_api_key(
    monkeypatch,
    source,
) -> None:
    monkeypatch.setattr(
        local_services,
        "load_config",
        lambda: _model_config(source),
    )
    monkeypatch.setenv("ARK_API_KEY", "ark-test")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "first")
    initial = config_digest()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "second")

    assert config_digest() == initial


def test_config_digest_tracks_claude_api_key_mode(monkeypatch) -> None:
    source = ClaudeCodeModelSourceConfig(
        auth_mode="api_key",
        api_key_env="CUSTOM_ANTHROPIC_API_KEY",
    )
    monkeypatch.setattr(
        local_services,
        "load_config",
        lambda: _model_config(source),
    )
    monkeypatch.setenv("ARK_API_KEY", "ark-test")
    monkeypatch.setenv("CUSTOM_ANTHROPIC_API_KEY", "first")
    initial = config_digest()
    monkeypatch.setenv("CUSTOM_ANTHROPIC_API_KEY", "second")

    assert config_digest() != initial


def test_config_digest_distinguishes_hot_settings_from_restart_settings(
    monkeypatch, tmp_path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config = yaml.safe_load((Path(local_services.ROOT) / "config.yaml").read_text())
    config["agent_runtime"] = {
        "evaluator_stage_timeout_seconds": 180,
        "maximum_concurrent_kicks": 4,
    }
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.setenv("ANIVAG_MODEL_CONFIG", str(config_path))
    first = config_digest()

    config["agent_runtime"]["evaluator_stage_timeout_seconds"] = 240
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    assert config_digest() == first

    config["agent_runtime"]["maximum_concurrent_kicks"] = 6
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    assert config_digest() != first


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
    config = _model_config(
        OpenAIModelSourceConfig(
            provider="deepseek",
            profile="deepseek",
            base_url="https://api.deepseek.com",
            api_key_env="CUSTOM_DEEPSEEK_API_KEY",
        ),
        fallback_enabled=True,
    )
    config.video_parser.fallback.deepseek.api_key_env = "CUSTOM_DEEPSEEK_API_KEY"
    monkeypatch.setenv("ANIVAG_HTTP_PORT", "9621")
    monkeypatch.setenv("ARK_API_KEY", "ark-test")
    monkeypatch.delenv("CUSTOM_DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(local_services, "load_config", lambda: config)
    monkeypatch.setattr(local_services, "listening", lambda _port: False)
    monkeypatch.setattr(local_services, "owned_pid", lambda _name: None)

    with pytest.raises(RuntimeError, match="CUSTOM_DEEPSEEK_API_KEY"):
        local_services.start_applications()


@pytest.mark.parametrize(
    "source",
    [
        CodexModelSourceConfig(),
        ClaudeCodeModelSourceConfig(),
    ],
)
def test_start_local_cli_providers_reach_launch_without_agent_api_key(
    monkeypatch,
    source,
) -> None:
    class LaunchReached(RuntimeError):
        pass

    def stop_at_launch(*_args, **_kwargs) -> None:
        raise LaunchReached

    monkeypatch.setenv("ANIVAG_HTTP_PORT", "9621")
    monkeypatch.setenv("ARK_API_KEY", "ark-test")
    monkeypatch.setattr(
        local_services,
        "load_config",
        lambda: _model_config(source),
    )
    monkeypatch.setattr(local_services, "listening", lambda _port: False)
    monkeypatch.setattr(local_services, "owned_pid", lambda _name: None)
    monkeypatch.setattr(local_services, "launch", stop_at_launch)

    with pytest.raises(LaunchReached):
        local_services.start_applications()


def test_start_claude_api_key_mode_requires_configured_key(monkeypatch) -> None:
    source = ClaudeCodeModelSourceConfig(
        auth_mode="api_key",
        api_key_env="CUSTOM_ANTHROPIC_API_KEY",
    )
    monkeypatch.setenv("ANIVAG_HTTP_PORT", "9621")
    monkeypatch.setenv("ARK_API_KEY", "ark-test")
    monkeypatch.delenv("CUSTOM_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(
        local_services,
        "load_config",
        lambda: _model_config(source),
    )
    monkeypatch.setattr(local_services, "listening", lambda _port: False)
    monkeypatch.setattr(local_services, "owned_pid", lambda _name: None)

    with pytest.raises(RuntimeError, match="CUSTOM_ANTHROPIC_API_KEY"):
        local_services.start_applications()


def test_local_start_validates_explicit_elasticsearch_before_applications(monkeypatch) -> None:
    events: list[str] = []
    requests = []
    settings = SimpleNamespace(
        elasticsearch=SimpleNamespace(
            url="http://127.0.0.1:9200", index_name="anivag_video_chunks_dev",
            api_key=None,
        ),
        environment="development", text_vector_dimensions=3,
        vision_vector_dimensions=3,
    )

    class FakeClient:
        cluster = SimpleNamespace(health=lambda **_kwargs: {"status": "yellow"})

        def close(self) -> None:
            events.append("closed")

    class FakeStore:
        def __init__(self, _client, **_kwargs) -> None:
            events.append("store")

        def ensure_index(self) -> None:
            events.append("schema")

    def create_client(config, **options):
        requests.append((config, options))
        return FakeClient()

    monkeypatch.setattr(local_services, "create_elasticsearch_client", create_client)
    monkeypatch.setattr(local_services, "ElasticsearchVideoChunkStore", FakeStore)

    local_services.start_elasticsearch(settings)

    assert events == ["store", "schema", "closed"]
    assert requests == [(settings.elasticsearch, {"request_timeout_seconds": 5})]


def test_local_start_rejects_unready_elasticsearch_without_schema_write(monkeypatch) -> None:
    settings = SimpleNamespace(
        elasticsearch=SimpleNamespace(url="http://127.0.0.1:9200", api_key=None),
    )
    closed = []

    class FakeClient:
        cluster = SimpleNamespace(health=lambda **_kwargs: {"status": "red"})

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(local_services, "create_elasticsearch_client", lambda *_args, **_kwargs: FakeClient())
    monkeypatch.setattr(
        local_services, "ElasticsearchVideoChunkStore",
        lambda *_args, **_kwargs: pytest.fail("schema must not be touched"),
    )

    with pytest.raises(RuntimeError, match="Elasticsearch 未就绪"):
        local_services.start_elasticsearch(settings)
    assert closed == [True]


def test_local_start_rejects_missing_elasticsearch_configuration(monkeypatch) -> None:
    monkeypatch.setattr(
        local_services, "create_elasticsearch_client",
        lambda *_args, **_kwargs: pytest.fail("ES client must not be opened"),
    )

    with pytest.raises(RuntimeError, match="显式配置 Elasticsearch"):
        local_services.start_elasticsearch(SimpleNamespace(elasticsearch=None))


@pytest.mark.parametrize(("health", "expected"), [
    ({"status": "green", "timed_out": False}, True),
    ({"status": "red", "timed_out": True}, False),
])
def test_local_status_includes_explicit_elasticsearch_health(
    monkeypatch, capsys, health, expected,
) -> None:
    requests = []
    closed = []
    settings = SimpleNamespace(
        database_url="mysql+pymysql://root@127.0.0.1:3306/anivag_development",
        elasticsearch=SimpleNamespace(url="http://127.0.0.1:9200", api_key=None),
    )

    class FakeClient:
        cluster = SimpleNamespace(health=lambda **_kwargs: health)

        def close(self) -> None:
            closed.append(True)

    def create_client(config, **options):
        requests.append((config, options))
        return FakeClient()

    monkeypatch.setattr(local_services, "create_elasticsearch_client", create_client)
    monkeypatch.setattr(local_services, "listening", lambda _port: True)
    monkeypatch.setattr(local_services, "get", lambda _url: b"ready")
    monkeypatch.setattr(local_services, "api_ready", lambda _url: True)
    monkeypatch.setenv("ANIVAG_HTTP_PORT", "9621")
    monkeypatch.setenv("ANIVAG_WEB_PORT", "5173")

    assert local_services.status(settings) is expected
    assert "Elasticsearch health" in capsys.readouterr().out
    assert requests == [(settings.elasticsearch, {"request_timeout_seconds": 3})]
    assert closed == [True]


@pytest.mark.parametrize("failure", [None, "admin", "schema"])
def test_local_mysql_missing_database_closes_admin_and_application_engines(
    monkeypatch, failure,
) -> None:
    from contextlib import contextmanager
    from sqlalchemy.exc import OperationalError

    settings = SimpleNamespace(
        database_url="mysql+pymysql://root@127.0.0.1:3306/anivag_development",
        environment="development",
    )
    events = []

    @contextmanager
    def application_connection():
        raise OperationalError("SELECT 1", None, Exception(1049, "unknown database"))
        yield  # pragma: no cover

    def execute(statement):
        events.append(str(statement))
        if failure == "admin":
            raise RuntimeError("admin failed")

    @contextmanager
    def admin_connection():
        yield SimpleNamespace(execute=execute)

    engine = SimpleNamespace(
        connect=application_connection, dispose=lambda: events.append("application closed"),
    )
    admin = SimpleNamespace(
        connect=admin_connection, dispose=lambda: events.append("admin closed"),
    )

    def create_admin(database_url, *, environment):
        assert database_url == settings.database_url
        assert environment == settings.environment
        return admin

    def create_schema(received):
        assert received is engine
        events.append("schema")
        if failure == "schema":
            raise RuntimeError("schema failed")

    monkeypatch.setattr(local_services, "listening", lambda _port: True)
    monkeypatch.setattr(local_services, "create_database_engine", lambda *_a, **_k: engine)
    monkeypatch.setattr(local_services, "create_admin_database_engine", create_admin)
    monkeypatch.setattr(local_services, "create_schema", create_schema)

    if failure:
        with pytest.raises(RuntimeError, match=f"{failure} failed"):
            local_services.start_mysql(settings)
    else:
        local_services.start_mysql(settings)

    assert events[0].startswith("CREATE DATABASE IF NOT EXISTS `anivag_development`")
    assert events[-1] == "application closed"
    assert events.count("admin closed") == 1
    assert events.count("application closed") == 1
    assert events.index("admin closed") < events.index("application closed")
    assert ("schema" in events) is (failure != "admin")
