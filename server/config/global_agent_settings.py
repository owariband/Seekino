"""Validated, versioned Agent settings for local and cloud deployments."""

from __future__ import annotations

import math
import os
from dataclasses import asdict
from pathlib import Path
from typing import Mapping

import yaml
from pydantic import ValidationError
from yaml.nodes import MappingNode, ScalarNode

from gateway.config import GatewayConfig, resolve_agent_runtime_path, resolve_config_path, standalone_agent_runtime
from server.constants.agent import CLOUD_TURN_CALL_LIMIT

from .agent import AgentServiceConfig
from .yaml_document import (
    GlobalAgentSettingsConflictError, document_lock, replace_document as _replace_document, revision as _revision,
)


_DEFAULTS = AgentServiceConfig()
_FIELD_NAMES = frozenset(asdict(_DEFAULTS))
_RETIRED_FIELDS = frozenset({
    "evaluator_reasoning_effort", "chat_reasoning_effort",
    "evaluator_stage_timeout_seconds", "chat_timeout_seconds",
    "single_pass_result", "result_max_loops", "evaluator_policy",
    "evaluator_low_confidence_rank",
})
_ENV_NAMES = {
    name: "SEEKINO_AGENT_" + name.upper()
    for name in _FIELD_NAMES
}
_ENV_NAMES.update({
    "maximum_top_k": "SEEKINO_AGENT_MAX_TOP_K",
    "maximum_concurrent_kicks": "SEEKINO_AGENT_MAX_CONCURRENT_KICKS",
})
_AUTO_MAXIMUMS = (
    ("max_model_calls_per_turn", "maximum_model_calls_per_turn"),
    ("max_tool_calls_per_turn", "maximum_tool_calls_per_turn"),
)

_TURN_BUDGET_FIELDS = frozenset(field for pair in _AUTO_MAXIMUMS for field in pair)


def _settings_path(config_path: str | Path | None) -> tuple[Path, bool]:
    """Seed a private runtime file once when upgrading a cloud deployment."""
    model_path = resolve_config_path(config_path)
    path = resolve_agent_runtime_path(model_path)
    if path is None:
        return model_path, False
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with document_lock(path):
            if not path.exists():
                legacy, _, _ = _read_document(model_path)
                # Only Server tuning migrates. The new defaults replace the old call budgets.
                tuning = {
                    key: value for key, value in legacy["agent_runtime"].items()
                    if key in _FIELD_NAMES and key not in _TURN_BUDGET_FIELDS
                }
                raw = yaml.safe_dump({"agent_runtime": tuning}, allow_unicode=True, sort_keys=False).encode()
                _replace_document(path, raw, None)
    return path, True


def read_global_agent_settings(
    config_path: str | Path | None = None,
) -> tuple[AgentServiceConfig, str]:
    """Read the effective Agent settings and their document revision."""
    path, standalone = _settings_path(config_path)
    payload, revision, _ = _read_document(path, standalone=standalone)
    return _effective_config(payload["agent_runtime"], standalone=standalone), revision


def patch_global_agent_settings(
    values: Mapping[str, object],
    revision: str,
    config_path: str | Path | None = None,
) -> tuple[AgentServiceConfig, str]:
    """Atomically apply a partial update without losing other YAML sections."""
    if not isinstance(values, Mapping):
        raise ValueError("agent settings values must be a mapping")
    path, standalone = _settings_path(config_path)
    with document_lock(path):
        payload, current_revision, raw = _read_document(path, standalone=standalone)
        if revision != current_revision:
            raise GlobalAgentSettingsConflictError(
                "config.yaml changed; reload global settings before saving"
            )
        current_values = payload["agent_runtime"]
        merged = {**current_values, **_validate_values(values)}
        effective = _effective_config(merged, standalone=standalone)
        if merged == current_values:
            return effective, current_revision
        payload["agent_runtime"] = merged
        serialized = _serialize_runtime_section(raw, merged)
        try:
            rewritten = yaml.safe_load(serialized)
        except yaml.YAMLError as exc:
            raise ValueError("agent_runtime cannot be safely edited") from exc
        if rewritten != payload:
            raise ValueError("agent_runtime cannot be safely edited")
        if standalone:
            model_document = yaml.safe_load(resolve_config_path(config_path).read_bytes())
            GatewayConfig.model_validate({**model_document, "agent_runtime": merged})
        else:
            GatewayConfig.model_validate(rewritten)
        _replace_document(path, serialized, current_revision)
        return effective, _revision(serialized)


def _read_document(path: Path, *, standalone: bool = False) -> tuple[dict[str, object], str, bytes]:
    if not path.is_file():
        raise FileNotFoundError(f"gateway config does not exist: {path}")
    raw = path.read_bytes()
    try:
        payload = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML in gateway config: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"gateway config must be a YAML mapping: {path}")
    if standalone:
        standalone_agent_runtime(payload)
    else:
        try:
            GatewayConfig.model_validate(payload)
        except ValidationError:
            raise ValueError("服务 YAML 配置无效，请检查模型和来源设置") from None
    runtime_values = payload.get("agent_runtime", {})
    if not isinstance(runtime_values, dict):
        raise ValueError("agent_runtime must be a YAML mapping")
    # Old YAML remains readable; only active settings can be changed by the API.
    payload["agent_runtime"] = {
        **{name: value for name, value in runtime_values.items() if name in _RETIRED_FIELDS},
        **_validate_values({name: value for name, value in runtime_values.items() if name not in _RETIRED_FIELDS}),
    }
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


def _effective_config(yaml_values: Mapping[str, object], *, standalone: bool = False) -> AgentServiceConfig:
    yaml_values = {name: value for name, value in yaml_values.items() if name not in _RETIRED_FIELDS}
    defaults = asdict(_DEFAULTS)
    if standalone:
        defaults.update(dict.fromkeys(_TURN_BUDGET_FIELDS, CLOUD_TURN_CALL_LIMIT))
    environment_values: dict[str, object] = {}
    env_fields = _FIELD_NAMES - yaml_values.keys()
    if standalone:
        env_fields -= _TURN_BUDGET_FIELDS
    for name in env_fields:
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
    return AgentServiceConfig(**{**defaults, **combined})


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
