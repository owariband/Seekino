from __future__ import annotations

import ast
from pathlib import Path


def test_retrieval_engine_has_no_application_or_infrastructure_imports() -> None:
    root = Path(__file__).parents[2]
    forbidden = {
        "agent",
        "elasticsearch",
        "gateway",
        "server",
        "sqlalchemy",
    }
    violations: list[str] = []
    for path in sorted((root / "engine" / "retrieval").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            else:
                continue
            for module in modules:
                if module.split(".", 1)[0] in forbidden:
                    violations.append(f"{path.name}:{node.lineno}:{module}")

    assert violations == []
