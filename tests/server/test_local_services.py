import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit

import pytest
import yaml
from fastapi.testclient import TestClient

from gateway.config import (
    ArkModelSourceConfig,
    ClaudeCodeModelSourceConfig,
    CodexModelSourceConfig,
    OpenAIModelSourceConfig,
)
from scripts import local_services
from scripts.local_services import api_ready, config_digest, dotenv_variable_names
from server.api.app import create_app
from server.config.auth import AuthConfig


def _model_config(
    *selected_sources,
    fallback_enabled: bool = False,
) -> SimpleNamespace:
    fallback = SimpleNamespace(
        enabled=fallback_enabled,
        deepseek=SimpleNamespace(api_key_env="PARSE_DEEPSEEK_API_KEY"),
    )
    return SimpleNamespace(
        stage_source=lambda _stage: ArkModelSourceConfig(),
        selected_agent_sources=lambda: selected_sources,
        normalized_models={ref: SimpleNamespace(
            source="ark", fallback=SimpleNamespace(source="deepseek", fallback=None)
            if ref == "vision" else None,
        ) for ref in ("transcription", "vision", "embedding", "rerank")},
        source=lambda name: ArkModelSourceConfig() if name == "ark" else OpenAIModelSourceConfig(
            provider="deepseek", profile="deepseek", base_url="https://api.deepseek.com",
            api_key_env=fallback.deepseek.api_key_env,
        ),
        video_parser=SimpleNamespace(fallback=fallback),
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
        "stage_timeout_seconds": 180,
        "maximum_concurrent_kicks": 4,
    }
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.setenv("SEEKINO_MODEL_CONFIG", str(config_path))
    first = config_digest()

    config["agent_runtime"]["stage_timeout_seconds"] = 240
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


@pytest.mark.parametrize("tenant_id", ["local-default", "local & test/+库"])
def test_library_ready_uses_current_tenant_route(monkeypatch, tenant_id) -> None:
    monkeypatch.setenv("SEEKINO_AUTH_MODE", "local")
    monkeypatch.setenv("SEEKINO_LOCAL_TENANT_ID", tenant_id)
    scopes = []

    def list_series(**scope):
        scopes.append(scope)
        return []

    services = SimpleNamespace(
        library=SimpleNamespace(list_series=list_series, count_series=lambda **_scope: 0),
        media=None,
    )
    app = create_app(services)
    app.state.auth_config = AuthConfig.from_environment()
    requested = []
    with TestClient(app) as client:
        assert client.get("/api/series/list?workspaceId=local-default").status_code == 422

        def get(url: str) -> bytes | None:
            requested.append(url)
            parsed = urlsplit(url)
            response = client.get(parsed.path + "?" + parsed.query)
            return response.content if response.status_code == 200 else None

        monkeypatch.setattr(local_services, "get", get)

        assert local_services.library_ready("http://127.0.0.1:9621")

    assert requested == [
        "http://127.0.0.1:9621/api/series/list?" + urlencode({"tenantId": tenant_id})
    ]
    assert scopes[0]["tenant_id"] == tenant_id


def test_start_requires_model_key_before_launching_server(monkeypatch) -> None:
    monkeypatch.setenv("SEEKINO_HTTP_PORT", "9621")
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.setattr(local_services, "listening", lambda _port: False)
    monkeypatch.setattr(local_services, "owned_pid", lambda _name: None)

    with pytest.raises(RuntimeError, match="ARK_API_KEY"):
        local_services.start_applications()


def test_start_requires_deepseek_key_before_launching_server(monkeypatch) -> None:
    monkeypatch.setenv("SEEKINO_HTTP_PORT", "9621")
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
    monkeypatch.setenv("SEEKINO_HTTP_PORT", "9621")
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

    monkeypatch.setenv("SEEKINO_HTTP_PORT", "9621")
    monkeypatch.setenv("ARK_API_KEY", "ark-test")
    monkeypatch.setattr(
        local_services,
        "load_config",
        lambda: _model_config(source),
    )
    monkeypatch.setattr(local_services, "listening", lambda _port: False)
    monkeypatch.setattr(local_services, "owned_pid", lambda _name: None)
    monkeypatch.setattr(local_services, "refresh_applications", lambda: None)
    monkeypatch.setattr(local_services, "launch", stop_at_launch)

    with pytest.raises(LaunchReached):
        local_services.start_applications()


def test_start_claude_api_key_mode_requires_configured_key(monkeypatch) -> None:
    source = ClaudeCodeModelSourceConfig(
        auth_mode="api_key",
        api_key_env="CUSTOM_ANTHROPIC_API_KEY",
    )
    monkeypatch.setenv("SEEKINO_HTTP_PORT", "9621")
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
            url="http://127.0.0.1:9200", index_name="seekino_video_chunks_dev",
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
        database_url="mysql+pymysql://root@127.0.0.1:3306/seekino_development",
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
    monkeypatch.setenv("SEEKINO_HTTP_PORT", "9621")
    monkeypatch.setenv("SEEKINO_WEB_PORT", "5173")

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
        database_url="mysql+pymysql://root@127.0.0.1:3306/seekino_development",
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

    assert events[0].startswith("CREATE DATABASE IF NOT EXISTS `seekino_development`")
    assert events[-1] == "application closed"
    assert events.count("admin closed") == 1
    assert events.count("application closed") == 1
    assert events.index("admin closed") < events.index("application closed")
    assert ("schema" in events) is (failure != "admin")


@pytest.fixture
def main_worktree(tmp_path):
    root = tmp_path / "main checkout"
    subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "core.hooksPath=/dev/null",
         "-c", "user.name=Test", "-c", "user.email=test@example.com",
         "commit", "--allow-empty", "-m", "initial"],
        check=True, capture_output=True,
    )
    return root


@pytest.mark.parametrize("arguments", [[], ["start"], ["status"], ["stop"]])
@pytest.mark.parametrize("source_branch", ["main", "dev"])
def test_shell_routes_to_selected_branch_before_loading_environment(
    main_worktree, tmp_path, arguments, source_branch,
):
    root = main_worktree
    if source_branch != "main":
        subprocess.run(["git", "-C", str(root), "switch", "-c", source_branch],
                       check=True, capture_output=True)
    launcher = Path(local_services.__file__).resolve().parent.parent / "start.sh"
    (root / "start.sh").write_bytes(launcher.read_bytes())
    subprocess.run(["git", "-C", str(root), "add", "start.sh"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "core.hooksPath=/dev/null",
         "-c", "user.name=Test", "-c", "user.email=test@example.com",
         "commit", "-m", "launcher"],
        check=True, capture_output=True,
    )
    other = tmp_path / "experiment checkout"
    subprocess.run(
        ["git", "-C", str(root), "worktree", "add", "--detach", str(other)],
        check=True, capture_output=True,
    )
    (root / ".env.local").write_text("LAUNCH_SOURCE=main\n", encoding="utf-8")
    (other / ".env.local").write_text("LAUNCH_SOURCE=experiment\n", encoding="utf-8")
    (root / ".venv/bin").mkdir(parents=True)
    (root / ".venv/bin/python").symlink_to(sys.executable)
    (root / "scripts").mkdir()
    (root / "scripts/local_services.py").write_text(
        "from pathlib import Path\n"
        "assert Path('.env.local').read_text() == 'LAUNCH_SOURCE=main\\n'\n"
        "print('LAUNCH_SOURCE')\n",
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\nfrom pathlib import Path\n"
        "Path(os.environ['LAUNCH_RECORD']).write_text(json.dumps({"
        "'cwd': str(Path.cwd()), 'args': sys.argv[1:], "
        "'inherited': os.environ.get('LAUNCH_SOURCE'), "
        "'dotenv': Path(sys.argv[sys.argv.index('--env-file') + 1]).read_text()}))\n",
        encoding="utf-8",
    )
    uv.chmod(0o755)
    record = tmp_path / "launch.json"
    branch_args = [] if source_branch == "main" else ["--branch", source_branch]
    result = subprocess.run(
        ["bash", str(other / "start.sh"), *branch_args, *arguments], cwd=other,
        env={**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
             "LAUNCH_SOURCE": "inherited", "LAUNCH_RECORD": str(record)},
        capture_output=True, text=True, check=True,
    )

    assert f"[{source_branch}]" in result.stdout
    assert json.loads(record.read_text()) == {
        "cwd": str(root.resolve()),
        "args": ["run", "--no-sync", "--env-file", ".env.local", "python",
                 "scripts/local_services.py", *branch_args, *arguments],
        "inherited": None, "dotenv": "LAUNCH_SOURCE=main\n",
    }
    assert not (other / ".venv").exists()


def test_shell_rejects_missing_main_checkout_before_uv(main_worktree, tmp_path, monkeypatch):
    root = main_worktree
    launcher = Path(local_services.__file__).resolve().parent.parent / "start.sh"
    (root / "start.sh").write_bytes(launcher.read_bytes())
    subprocess.run(["git", "-C", str(root), "switch", "-c", "experiment"], check=True,
                   capture_output=True)
    uv = tmp_path / "uv"
    uv.write_text("#!/bin/sh\necho unexpected-uv-call >&2\nexit 99\n", encoding="utf-8")
    uv.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    result = subprocess.run(["bash", str(root / "start.sh")], cwd=tmp_path,
                            capture_output=True, text=True)

    assert result.returncode == 1
    assert "未找到已检出 main" in result.stderr
    assert "unexpected-uv-call" not in result.stderr
    assert not (root / ".venv").exists()


@pytest.mark.parametrize("checkout", [["switch", "-c", "experiment"], ["checkout", "--detach"]])
def test_direct_entry_rejects_non_main_before_creating_state(monkeypatch, main_worktree, checkout):
    root = main_worktree
    monkeypatch.setattr(local_services, "ROOT", root)
    state = root / "state"
    monkeypatch.setattr(local_services, "STATE", state)
    monkeypatch.setattr(sys, "argv", ["local_services.py", "start"])
    local_services.require_worktree_branch("main")
    subprocess.run(["git", "-C", str(root), *checkout], check=True, capture_output=True)

    with pytest.raises(RuntimeError, match="只允许 main"):
        local_services.main()
    assert not state.exists()


def test_direct_entry_accepts_explicit_dev_branch(monkeypatch, main_worktree):
    root = main_worktree
    subprocess.run(["git", "-C", str(root), "switch", "-c", "dev"],
                   check=True, capture_output=True)
    monkeypatch.setattr(local_services, "ROOT", root)
    monkeypatch.setattr(local_services, "STATE", root / "state")
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys, "argv", ["local_services.py", "--branch", "dev", "status"])
    monkeypatch.setattr(local_services, "configure", lambda: None)
    monkeypatch.setattr(local_services, "status", lambda settings: True)

    assert local_services.main() == 0
    assert (root / "state/startup.lock").exists()


@pytest.mark.parametrize(("name", "directory"), [("api", "server"), ("web", "web")])
def test_source_digest_tracks_working_files_and_ignores_generated_files(
    monkeypatch, main_worktree, name, directory,
):
    root = main_worktree
    monkeypatch.setattr(local_services, "ROOT", root)
    source_dir = root / directory
    source_dir.mkdir()
    tracked = source_dir / "source.py"
    tracked.write_text("first\n", encoding="utf-8")
    (root / ".gitignore").write_text("__pycache__/\nnode_modules/\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", directory, ".gitignore"], check=True)
    first = local_services.source_digest(name)
    tracked.write_text("edited without commit\n", encoding="utf-8")
    edited = local_services.source_digest(name)
    (source_dir / "new.py").write_text("untracked\n", encoding="utf-8")
    added = local_services.source_digest(name)
    tracked.unlink()
    deleted = local_services.source_digest(name)

    assert len({first, edited, added, deleted}) == 4
    for directory_name in ("__pycache__", "node_modules"):
        generated = source_dir / directory_name
        generated.mkdir()
        (generated / "generated.py").write_text("ignored\n", encoding="utf-8")
    assert local_services.source_digest(name) == deleted


@pytest.mark.parametrize("name", ["api", "web"])
def test_launch_records_current_source(monkeypatch, tmp_path, name):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(local_services, "ROOT", tmp_path)
    monkeypatch.setattr(local_services, "STATE", state)
    monkeypatch.setattr(local_services, "CHILDREN", {})
    monkeypatch.setattr(local_services, "config_digest", lambda: "config")
    monkeypatch.setattr(local_services, "source_digest", lambda service: f"{service}-source")
    monkeypatch.setattr(local_services, "signature", lambda _pid: "started now")
    monkeypatch.setattr(local_services.subprocess, "Popen", lambda *_args, **_kwargs: SimpleNamespace(pid=123))

    local_services.launch(name, ["unused"], cwd=tmp_path)

    assert json.loads((state / f"{name}.pid.json").read_text()) == {
        "pid": 123, "signature": "started now", "config": "config",
        "source_root": str(tmp_path), "source_digest": f"{name}-source",
    }


@pytest.mark.parametrize(("pid", "listeners", "returncode", "expected"), [
    (None, "123\n", 0, False), (123, "123\n", 0, True),
    (123, "124\n", 0, False), (123, "123\n124\n", 0, False),
    (123, "", 1, False),
])
def test_owned_listener_requires_exact_managed_pid(monkeypatch, pid, listeners, returncode, expected):
    monkeypatch.setattr(local_services, "owned_pid", lambda _name: pid)
    calls = []

    def run(args, **_kwargs):
        calls.append(args)
        return SimpleNamespace(stdout=listeners, returncode=returncode)

    monkeypatch.setattr(local_services.subprocess, "run", run)

    assert local_services.owned_listener("api", 9621) is expected
    assert calls == ([] if pid is None else [
        ["lsof", "-nP", "-t", "-iTCP:9621", "-sTCP:LISTEN"]
    ])


@pytest.mark.parametrize("name", ["api", "web"])
@pytest.mark.parametrize("change", [None, "legacy", "source_root", "source_digest", "config"])
def test_refresh_only_stale_managed_applications(monkeypatch, tmp_path, name, change):
    monkeypatch.setattr(local_services, "ROOT", tmp_path)
    monkeypatch.setattr(local_services, "STATE", tmp_path)
    monkeypatch.setenv("SEEKINO_HTTP_PORT", "9621")
    monkeypatch.setenv("SEEKINO_WEB_PORT", "5173")
    monkeypatch.setattr(local_services, "listening", lambda _port: True)
    monkeypatch.setattr(local_services, "owned_listener", lambda _name, _port: True)
    monkeypatch.setattr(local_services, "owned_pid", lambda _name: 123)
    monkeypatch.setattr(local_services, "config_digest", lambda: "current-config")
    monkeypatch.setattr(local_services, "source_digest", lambda _name: "current-source")
    stopped = []
    monkeypatch.setattr(local_services, "stop", lambda names: stopped.append(names))
    for service in ("api", "web"):
        record = {"config": "current-config", "source_root": str(tmp_path),
                  "source_digest": "current-source"}
        if service == name and change == "legacy":
            record = {"config": "current-config"}
        elif service == name and change:
            record[change] = "outdated"
        (tmp_path / f"{service}.pid.json").write_text(json.dumps(record), encoding="utf-8")

    local_services.refresh_applications()

    assert stopped == ([(name,)] if change else [])


@pytest.mark.parametrize("unknown", ["api", "web"])
def test_refresh_rejects_any_unknown_listener_before_stopping(monkeypatch, tmp_path, unknown):
    monkeypatch.setattr(local_services, "ROOT", tmp_path)
    monkeypatch.setattr(local_services, "STATE", tmp_path)
    monkeypatch.setenv("SEEKINO_HTTP_PORT", "9621")
    monkeypatch.setenv("SEEKINO_WEB_PORT", "5173")
    monkeypatch.setattr(local_services, "listening", lambda _port: True)
    monkeypatch.setattr(local_services, "owned_listener", lambda name, _port: name != unknown)
    monkeypatch.setattr(local_services, "config_digest", lambda: pytest.fail("must preflight both ports"))
    monkeypatch.setattr(local_services, "stop", lambda *_args: pytest.fail("must not stop any service"))

    with pytest.raises(RuntimeError, match=f"{unknown} 端口 .*非本启动器"):
        local_services.refresh_applications()


def test_stop_does_not_signal_reused_pid(monkeypatch, tmp_path):
    monkeypatch.setattr(local_services, "ROOT", tmp_path)
    monkeypatch.setattr(local_services, "STATE", tmp_path)
    (tmp_path / "api.pid.json").write_text(json.dumps({"pid": 123, "signature": "old"}))
    monkeypatch.setattr(local_services, "signature", lambda _pid: "new process")
    monkeypatch.setattr(local_services.os, "killpg", lambda *_args: pytest.fail("must not signal reused PID"))

    local_services.stop(("api",))

    assert (tmp_path / "api.pid.json").exists()
