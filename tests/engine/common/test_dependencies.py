from __future__ import annotations

import ast
import json
import subprocess
import sys
from importlib.util import resolve_name
from pathlib import Path

import pytest


_ROOT = Path(__file__).parents[3]
_V2_SERVICES = (
    "server/service/video_parse.py",
    "server/service/video_parse_media.py",
    "server/service/video_parse_index_coordinator.py",
)


@pytest.mark.parametrize("shared", [True, False], ids=["common", "v2"])
def test_shared_and_v2_modules_do_not_import_v1(shared: bool) -> None:
    if shared:
        paths = sorted((_ROOT / "engine/common").rglob("*.py"))
        forbidden = ("engine.parsev1", "engine.parsev2", "agent", "server")
    else:
        paths = sorted((_ROOT / "engine/parsev2").rglob("*.py"))
        paths += sorted((_ROOT / "agent/video_parse_v2").rglob("*.py"))
        paths += [_ROOT / path for path in _V2_SERVICES]
        forbidden = ("engine.parsev1",)

    violations: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        package = ".".join(path.relative_to(_ROOT).with_suffix("").parts[:-1])
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level:
                    module = resolve_name("." * node.level + module, package)
                modules = [module] + [
                    f"{module}.{alias.name}" for alias in node.names if alias.name != "*"
                ]
            else:
                continue
            for module in modules:
                if any(module == name or module.startswith(name + ".") for name in forbidden):
                    violations.append(f"{path.relative_to(_ROOT)}:{node.lineno}:{module}")

    assert violations == []


@pytest.mark.parametrize("modules", [
    [
        "engine.common.config", "engine.common.media_probe", "engine.common.background",
        "engine.common.frame_sampler", "engine.common.usage", "engine.common.transcription",
        "engine.common.model_factory",
    ],
    ["engine.parsev2", "agent.video_parse_v2"],
    [
        "server.config.settings", "server.config.parse_profile",
        "server.service.video_parse", "server.service.video_parse_media",
        "server.service.video_parse_index_coordinator", "server.service.upload",
        "server.service.library", "server.service.keyframe_backfill",
        "server.bootstrap", "server.cli",
    ],
], ids=["common", "v2-agent", "server-imports"])
def test_imports_do_not_load_v1_in_a_fresh_process(modules: list[str]) -> None:
    script = """
import importlib
import json
import sys

for name in json.loads(sys.argv[1]):
    importlib.import_module(name)
loaded = [name for name in sys.modules if name == "engine.parsev1" or name.startswith("engine.parsev1.")]
assert not loaded, loaded
"""
    result = subprocess.run(
        [sys.executable, "-c", script, json.dumps(modules)],
        cwd=_ROOT, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_v1_and_common_share_transcript_and_config_class_identity() -> None:
    from engine.common.config import VideoParseRuntimeConfig
    from engine.common.transcription import TranscriptAnalysis, TranscriptCue
    from engine.parsev1 import runtime, schemas

    assert schemas.TranscriptCue is TranscriptCue
    assert schemas.TranscriptAnalysis is TranscriptAnalysis
    assert runtime.VideoParseRuntimeConfig is VideoParseRuntimeConfig
