"""Validated, versioned access to the Agent section of the shared YAML config."""

from __future__ import annotations

import hashlib
import math
import os
import stat
import threading
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Mapping

import yaml
from yaml.nodes import MappingNode, ScalarNode

from gateway.config import GatewayConfig, resolve_config_path

from .agent import AgentServiceConfig


class GlobalAgentSettingsConflictError(Exception):
    """The YAML file changed since the caller last read it."""


_WRITE_LOCK = threading.Lock()
_DEFAULTS = AgentServiceConfig()
_FIELD_NAMES = frozenset(asdict(_DEFAULTS))
_ENV_NAMES = {
    name: "ANIVAG_AGENT_" + name.upper()
    for name in _FIELD_NAMES
}
_ENV_NAMES.update({
    "maximum_top_k": "ANIVAG_AGENT_MAX_TOP_K",
    "maximum_concurrent_kicks": "ANIVAG_AGENT_MAX_CONCURRENT_KICKS",
})
_AUTO_MAXIMUMS = (
    ("max_model_calls_per_turn", "maximum_model_calls_per_turn"),
    ("max_tool_calls_per_turn", "maximum_tool_calls_per_turn"),
)


def read_global_agent_settings(
    config_path: str | Path | None = None,
) -> tuple[AgentServiceConfig, str]:
    """Read effective values and a revision covering the entire YAML file.

    Explicit YAML values take precedence. Legacy environment variables fill
    fields absent from the YAML section, then dataclass defaults fill the rest.
    Reading the file afresh also observes direct edits made outside the API.
    """
    path = resolve_config_path(config_path)
    payload, revision, _ = _read_document(path)
    return _effective_config(payload["agent_runtime"]), revision


def patch_global_agent_settings(
    values: Mapping[str, object],
    revision: str,
    config_path: str | Path | None = None,
) -> tuple[AgentServiceConfig, str]:
    """Atomically apply a partial update without losing other YAML sections."""
    if not isinstance(values, Mapping):
        raise ValueError("agent settings values must be a mapping")
    path = resolve_config_path(config_path)
    with _WRITE_LOCK:
        payload, current_revision, raw = _read_document(path)
        if revision != current_revision:
            raise GlobalAgentSettingsConflictError(
                "config.yaml changed; reload global settings before saving"
            )
        current_values = payload["agent_runtime"]
        merged = {**current_values, **_validate_values(values)}
        effective = _effective_config(merged)
        if merged == current_values:
            return effective, current_revision
        payload["agent_runtime"] = merged
        GatewayConfig.model_validate(payload)
        serialized = _serialize_runtime_section(raw, merged)
        try:
            rewritten = yaml.safe_load(serialized)
        except yaml.YAMLError as exc:
            raise ValueError("agent_runtime cannot be safely edited") from exc
        if rewritten != payload:
            raise ValueError("agent_runtime cannot be safely edited")
        GatewayConfig.model_validate(rewritten)
        _replace_document(path, serialized, current_revision)
        return effective, _revision(serialized)


def _read_document(path: Path) -> tuple[dict[str, object], str, bytes]:
    if not path.is_file():
        raise FileNotFoundError(f"gateway config does not exist: {path}")
    raw = path.read_bytes()
    try:
        payload = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML in gateway config: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"gateway config must be a YAML mapping: {path}")
    GatewayConfig.model_validate(payload)
    runtime_values = payload.get("agent_runtime", {})
    if not isinstance(runtime_values, dict):
        raise ValueError("agent_runtime must be a YAML mapping")
    payload["agent_runtime"] = _validate_values(runtime_values)
    return payload, _revision(raw), raw


def _serialize_runtime_section(raw: bytes, values: Mapping[str, object]) -> bytes:
    """Replace only the runtime mapping, leaving other YAML text untouched."""
    document = raw.decode("utf-8")
    newline = "\r\n" if "\r\n" in document else "\n"
    section = yaml.safe_dump(
        {"agent_runtime": dict(values)}, allow_unicode=True, sort_keys=False,
    ).replace("\n", newline)
    root = yaml.compose(document)
    if not isinstance(root, MappingNode):
        raise ValueError("gateway config must be a YAML mapping")
    for key, value in root.value:
        if not isinstance(key, ScalarNode) or key.value != "agent_runtime":
            continue
        if not isinstance(value, MappingNode):
            raise ValueError("agent_runtime must be a YAML mapping")
        last_value = value.value[-1][1] if value.value else value
        if last_value.end_mark.index < key.start_mark.index:
            raise ValueError("agent_runtime aliases cannot be edited")
        line_end = document.find("\n", last_value.end_mark.index)
        end = len(document) if line_end < 0 else line_end + 1
        return (document[:key.start_mark.index] + section + document[end:]).encode("utf-8")
    separator = "" if not document or document.endswith(newline * 2) else (
        newline if document.endswith(newline) else newline * 2
    )
    return (document + separator + section).encode("utf-8")


def _effective_config(yaml_values: Mapping[str, object]) -> AgentServiceConfig:
    defaults = asdict(_DEFAULTS)
    environment_values: dict[str, object] = {}
    for name in _FIELD_NAMES - yaml_values.keys():
        env_name = _ENV_NAMES[name]
        raw = os.getenv(env_name)
        if raw is None:
            continue
        default = defaults[name]
        try:
            if isinstance(default, bool):
                normalized = raw.strip().casefold()
                if normalized in {"1", "true", "yes", "on"}:
                    environment_values[name] = True
                elif normalized in {"0", "false", "no", "off"}:
                    environment_values[name] = False
                else:
                    raise ValueError("must be a boolean")
            elif isinstance(default, int):
                environment_values[name] = int(raw)
            elif isinstance(default, str):
                environment_values[name] = raw.strip().casefold()
            else:
                environment_values[name] = float(raw)
        except ValueError as exc:
            raise ValueError(f"{env_name} has an invalid value") from exc
    combined = {**environment_values, **yaml_values}
    # Preserve the legacy environment behavior: if only a per-turn default is
    # raised, its maximum follows it unless explicitly configured elsewhere.
    for default_name, maximum_name in _AUTO_MAXIMUMS:
        if maximum_name not in combined and default_name in combined:
            combined[maximum_name] = max(
                defaults[maximum_name], combined[default_name]
            )
    return AgentServiceConfig(**combined)


def _validate_values(values: Mapping[str, object]) -> dict[str, object]:
    defaults = asdict(_DEFAULTS)
    checked: dict[str, object] = {}
    for name, value in values.items():
        if name not in _FIELD_NAMES:
            raise ValueError(f"unknown agent_runtime setting: {name}")
        default = defaults[name]
        if isinstance(default, bool):
            valid = isinstance(value, bool)
        elif isinstance(default, int):
            valid = isinstance(value, int) and not isinstance(value, bool)
        elif isinstance(default, str):
            valid = isinstance(value, str)
        else:
            valid = (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
            )
            if valid:
                value = float(value)
        if not valid:
            raise ValueError(f"agent_runtime.{name} has an invalid type")
        checked[name] = value
    return checked


def _revision(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _replace_document(path: Path, content: bytes, expected_revision: str) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{path.name}.", suffix=".tmp",
            dir=path.parent, delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_path, stat.S_IMODE(path.stat().st_mode))
        if _revision(path.read_bytes()) != expected_revision:
            raise GlobalAgentSettingsConflictError(
                "config.yaml changed while saving; reload global settings"
            )
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
