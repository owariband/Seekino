import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

from server.config.global_agent_settings import patch_global_agent_settings, read_global_agent_settings


ROOT = Path(__file__).resolve().parents[2]


def test_cloud_compose_enforces_host_budget_and_loads_gpu_bindings(tmp_path, monkeypatch):
    docker = shutil.which("docker")
    if not docker or subprocess.run([docker, "compose", "version"], capture_output=True).returncode:
        pytest.skip("Docker Compose CLI is required to render the production overlay")
    cloud = tmp_path / "deploy/cloud"
    cloud.mkdir(parents=True)
    shutil.copy(ROOT / "compose.yaml", tmp_path)
    shutil.copy(ROOT / "deploy/cloud/compose.prod.yaml", cloud)
    env = cloud / ".env.cloud"
    template = (ROOT / "deploy/cloud/env.cloud.example").read_text()
    env.write_text(
        template
        .replace("MYSQL_ROOT_PASSWORD=\n", "MYSQL_ROOT_PASSWORD=fixture-root\n")
        .replace("MYSQL_PASSWORD=\n", "MYSQL_PASSWORD=fixture-db\n")
        .replace("SEEKINO_AUTH_SECRET_KEY=\n", "SEEKINO_AUTH_SECRET_KEY=" + "f" * 40 + "\n")
        + "\nSEEKINO_VIDEO_CONCURRENCY=3\nSEEKINO_PARSE_CONCURRENCY=10\n"
          "SEEKINO_REFINE_CONCURRENCY=10\nSEEKINO_MODEL_TRACE_DIR=/debug\n"
    )
    command = [docker, "compose", "--env-file", str(env), "-f", "compose.yaml"]
    output = subprocess.check_output(
        [*command, "-f", "deploy/cloud/compose.prod.yaml", "config", "--format", "json"],
        cwd=tmp_path, text=True, env={k: v for k, v in os.environ.items() if not k.startswith(("SEEKINO_", "MYSQL_"))},
    )
    services = json.loads(output)["services"]
    steady = [services[name] for name in ("api", "mysql", "elasticsearch", "redis", "web")]
    assert sum(float(service["cpus"]) for service in steady) == 3.75
    assert sum(int(service["mem_limit"]) for service in steady) == 5824 * 1024**2
    assert int(services["initialize"]["mem_limit"]) == 512 * 1024**2
    assert all(service["memswap_limit"] == service["mem_limit"] for service in [*steady, services["initialize"]])
    assert services["elasticsearch"]["environment"]["ES_JAVA_OPTS"] == "-Xms1g -Xmx1g"
    assert not any(services[name].get("ports") for name in ("mysql", "elasticsearch", "redis"))

    from server.bootstrap import prepare_service_configuration
    from server.config import ServerSettings

    for name, value in services["api"]["environment"].items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("SEEKINO_MODEL_CONFIG", str(ROOT / "deploy/cloud/models.example.yaml"))
    monkeypatch.setenv("SEEKINO_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    settings = ServerSettings.from_environment()
    models, profile = prepare_service_configuration(settings)
    assert settings.parse.video_concurrency == profile.asr.concurrency == 1
    execution = profile.execution
    assert (execution.parse_concurrency, execution.refine_concurrency, execution.index_concurrency) == (1, 1, 1)
    assert settings.mq.max_running_children == 2 and not os.environ["SEEKINO_MODEL_TRACE_DIR"]
    assert models.source(profile.asr.source).adapter == "openai_audio"
    assert models.source(profile.embedding.source).adapter == "llama_cpp_embedding"
    embedding = profile.embedding
    assert (embedding.model, embedding.dimensions, embedding.input_mode) == ("seekino-qwen3-embedding-0.6b", 1024, "text")
    assert models.query_hook.router_provider == "startlux"


@pytest.fixture
def initializer(tmp_path):
    services = yaml.safe_load((ROOT / "compose.yaml").read_text())["services"]
    command = services["initialize"]["command"]
    template = tmp_path / "model template.yaml"
    template.write_bytes((ROOT / "config.yaml").read_bytes())
    data = tmp_path / "persistent data"
    config = data / "config" / "config.yaml"
    calls = tmp_path / "calls"
    binary = tmp_path / "bin"
    binary.mkdir()
    python = binary / "python"
    python.write_text(
        '#!/bin/sh\n'
        'printf "%s\\n" "$*" >> "$CALL_LOG"\n'
        'case "$*" in\n'
        '  -c*) exit "${PREFLIGHT_EXIT:-0}" ;;\n'
        '  *init-db) exit "${DATABASE_EXIT:-0}" ;;\n'
        '  *init-es-index) exit "${INDEX_EXIT:-0}" ;;\n'
        'esac\n'
    )
    python.chmod(0o755)
    script = command[-1].replace("$$", "$").replace("/app/model-template.yaml", f'"{template}"')
    # Keep the production shell and quoting; substitute only the container mount paths.
    script = script.replace("/data/config", f'"{data}/config"')

    def run(**overrides):
        calls.write_text("")
        environment = {
            **os.environ,
            "PATH": str(binary) + os.pathsep + os.environ.get("PATH", ""),
            "SEEKINO_MODEL_CONFIG": str(config),
            "CALL_LOG": str(calls),
            **overrides,
        }
        result = subprocess.run(command[:-1] + [script], env=environment, text=True, capture_output=True)
        return result, calls.read_text().splitlines()

    return run, config, template, services


def test_docker_first_launch_initializes_checkpoint_schema_before_index(initializer):
    run, config, template, services = initializer
    result, calls = run()
    assert result.returncode == 0, result.stderr
    assert config.read_bytes() == template.read_bytes()
    assert len(calls) == 3
    assert calls[-2:] == ["-m server.cli init-db", "-m server.cli init-es-index"]
    assert services["initialize"]["depends_on"] == {
        "mysql": {"condition": "service_healthy"},
        "elasticsearch": {"condition": "service_healthy"},
    }
    assert services["api"]["depends_on"]["initialize"]["condition"] == "service_completed_successfully"
    assert services["web"]["depends_on"]["api"]["condition"] == "service_healthy"


def test_docker_restart_preserves_settings_saved_atomically(initializer):
    run, config, template, _ = initializer
    result, _ = run()
    assert result.returncode == 0, result.stderr
    _, revision = read_global_agent_settings(config)
    patch_global_agent_settings({"maximum_concurrent_kicks": 2}, revision, config)
    saved = config.read_bytes()
    template.write_text("changed template must not overwrite user configuration\n")
    result, _ = run()
    assert result.returncode == 0, result.stderr
    assert config.read_bytes() == saved
    assert read_global_agent_settings(config)[0].maximum_concurrent_kicks == 2


@pytest.mark.parametrize("failed_stage,expected_calls", [
    ("PREFLIGHT_EXIT", 1), ("DATABASE_EXIT", 2), ("INDEX_EXIT", 3),
])
def test_docker_initialization_fails_closed(initializer, failed_stage, expected_calls):
    run, _, _, _ = initializer
    result, calls = run(**{failed_stage: "17"})
    assert result.returncode == 17
    assert len(calls) == expected_calls


def test_docker_missing_first_configuration_does_not_initialize_storage(initializer):
    run, config, template, _ = initializer
    template.unlink()
    result, calls = run()
    assert result.returncode != 0
    assert not config.exists()
    assert calls == []
