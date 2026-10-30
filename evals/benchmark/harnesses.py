"""Evaluation-only Pi/Codex bindings; production remains on its configured harness."""
from __future__ import annotations

import ast
from dataclasses import replace
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType

from gateway.agent_model.context import ContextWindowPolicy
from gateway.agent_model.contracts import AgentModelProfile
from gateway.codex.agent import CodexAgentModel
from gateway.codex.client import CodexSession
from gateway.codex.supervisor import CodexSupervisorModel

ROOT = Path(__file__).resolve().parents[2]
PI_REVISION = "798307a"


def prepare_pi(directory: Path) -> None:
    """Extract the exact retired runtime, adjusting only its removed config import."""
    if (directory / "manifest.json").exists():
        return
    revision = subprocess.check_output(["git", "rev-parse", PI_REVISION], cwd=ROOT, text=True).strip()
    paths = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", revision, "gateway/pi"], cwd=ROOT, text=True).splitlines()
    hashes = {}
    for name in paths:
        data = subprocess.check_output(["git", "show", f"{revision}:{name}"], cwd=ROOT)
        target = directory / name.removeprefix("gateway/pi/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        hashes[name] = hashlib.sha256(data).hexdigest()
    source = subprocess.check_output(["git", "show", f"{revision}:gateway/config.py"], cwd=ROOT, text=True)
    node = next(n for n in ast.parse(source).body if isinstance(n, ast.ClassDef) and n.name == "PiHostConfig")
    (directory / "config.py").write_text(
        "from __future__ import annotations\nfrom pathlib import Path\n"
        "from pydantic import BaseModel, ConfigDict, Field, model_validator\n"
        "from gateway.config import _NonEmptyString\n\n" + ast.get_source_segment(source, node) + "\n")
    client = directory / "client.py"
    client.write_text(client.read_text().replace("from gateway.config import PiHostConfig", "from .config import PiHostConfig"))
    models = directory / "host/src/models.ts"
    models.write_text(models.read_text().replace(
        "models: [{ id: profile.model, name: profile.model, api, baseUrl,",
        "models: [{ id: profile.model, name: profile.model, api, baseUrl,\n"
        "        ...(profile.provider === \"kimi\" ? { compat: { supportsDeveloperRole: false } } : {}),"))
    (directory / "manifest.json").write_text(json.dumps({
        "git_revision": revision, "original_sha256": hashes,
        "compatibility_change": "Extract original PiHostConfig and redirect its import; for Kimi set SDK supportsDeveloperRole=false after unmodified Pi received HTTP 400. Business prompt/tools unchanged.",
    }, indent=2) + "\n")


def validate_kimi(config) -> None:
    for name, binding in config.normalized_models.items():
        if name in {"embedding", "speaker", "transcription"}:
            continue
        source = config.source(binding.source)
        if binding.model != "k3.1-pro" or source.provider != "kimi" or binding.fallback is not None:
            raise ValueError(f"{name} must use Kimi k3.1-pro without fallback")
    if config.supervisor_harness != "deepagents":
        raise ValueError("Use the shared API configuration; the experimental adapter replaces only the Supervisor")


def alternative_supervisor(arm: str, config, output: Path, legacy_pi: Path):
    binding = config.resolve_model_binding("supervisor")
    source = config.source(binding.source)
    policy = ContextWindowPolicy(
        context_window_tokens=binding.context_window_tokens,
        output_reserve_tokens=binding.max_output_tokens,
        safety_margin_tokens=binding.context_safety_margin_tokens,
        auto_compact_tokens=binding.auto_compact_tokens,
        compact_target_tokens=binding.compact_target_tokens,
    )
    if arm == "pi":
        legacy_pi = legacy_pi.resolve()
        package = ModuleType("benchmark_legacy_pi")
        package.__path__ = [str(legacy_pi)]
        sys.modules[package.__name__] = package
        client_type = importlib.import_module("benchmark_legacy_pi.client").PiClient
        model_type = importlib.import_module("benchmark_legacy_pi.supervisor").PiSupervisorModel
        client = client_type(config={"sessionDir": str(output / "pi-sessions")})
        profile = AgentModelProfile(
            id="benchmark-kimi", revision=hashlib.sha256(binding.model_dump_json().encode()).hexdigest(),
            provider="kimi", model=binding.model, api_key=os.environ[source.api_key_env], base_url=source.base_url,
            supports_vision_input=True, max_output_tokens=binding.max_output_tokens,
            context_window_tokens=binding.context_window_tokens, reasoning_effort=binding.reasoning_effort,
        )
        model = model_type(client=client, profile=profile, request_timeout_seconds=binding.request_timeout_seconds,
                           max_input_chars=binding.max_input_chars, total_timeout_enabled=True)
        return model, client.close
    if arm != "codex":
        raise ValueError(f"Unknown alternative harness: {arm}")

    catalog = {"slug": binding.model, "display_name": binding.model, "description": "Kimi benchmark",
        "default_reasoning_level": binding.reasoning_effort, "supported_reasoning_levels": [
            {"effort": e, "description": e} for e in ("low", "high", "max")],
        "shell_type": "shell_command", "visibility": "list", "supported_in_api": True, "priority": 0,
        "base_instructions": "", "supports_reasoning_summaries": True, "default_reasoning_summary": "none",
        "support_verbosity": False, "truncation_policy": {"mode": "bytes", "limit": 32000},
        "context_window": binding.context_window_tokens, "max_context_window": 1048576,
        "effective_context_window_percent": 95, "supports_parallel_tool_calls": True,
        "experimental_supported_tools": [], "input_modalities": ["text", "image"]}
    catalog_path = output / "codex-models.json"
    catalog_path.write_text(json.dumps({"models": [catalog]}, indent=2) + "\n")
    overrides = (
        f"model_catalog_json={json.dumps(str(catalog_path))}",
        'model_provider="kimi_benchmark"', 'model_providers.kimi_benchmark.name="Kimi"',
        f"model_providers.kimi_benchmark.base_url={json.dumps(source.base_url)}",
        f"model_providers.kimi_benchmark.env_key={json.dumps(source.api_key_env)}",
        'model_providers.kimi_benchmark.wire_api="responses"',
        'model_providers.kimi_benchmark.requires_openai_auth=false',
        'model_providers.kimi_benchmark.supports_websockets=false',
        'model_providers.kimi_benchmark.request_max_retries=0',
        'model_providers.kimi_benchmark.stream_max_retries=0',
        'web_search="disabled"', f"model_auto_compact_token_limit={binding.auto_compact_tokens}",
    )
    (output / "codex-overrides.json").write_text(json.dumps(overrides, indent=2) + "\n")

    class KimiSession(CodexSession):
        def __init__(self, **kwargs):
            if kwargs["model"] != "k3.1-pro":
                raise ValueError("Codex benchmark must use Kimi")
            super().__init__(**kwargs)
            self._config = replace(self._config, config_overrides=self._config.config_overrides + overrides)

    adapter = CodexAgentModel(
        model=binding.model, reasoning_effort=binding.reasoning_effort,
        request_timeout_seconds=binding.request_timeout_seconds, max_output_tokens=binding.max_output_tokens,
        max_input_chars=binding.max_input_chars, context_window_policy=policy,
        max_concurrency=1, cwd=str(ROOT), session_factory=KimiSession, total_timeout_enabled=True, mcp=False,
    )
    return CodexSupervisorModel(agent_model=adapter), adapter.close
