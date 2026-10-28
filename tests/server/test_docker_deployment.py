import os
from pathlib import Path
import subprocess

import pytest
import yaml

from server.config.global_agent_settings import patch_global_agent_settings, read_global_agent_settings


ROOT = Path(__file__).resolve().parents[2]


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
