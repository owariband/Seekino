from __future__ import annotations

import ast
from importlib.util import resolve_name
from pathlib import Path


def test_retrieval_engine_has_no_application_or_infrastructure_imports() -> None:
    root = Path(__file__).parents[2]
    forbidden = {
        "agent",
        "anthropic",
        "elasticsearch",
        "gateway",
        "openai",
        "qdrant_client",
        "redis",
        "server",
        "sqlalchemy",
        "volcenginesdkarkruntime",
    }
    violations: list[str] = []
    paths = sorted((root / "engine" / "rag").rglob("*.py"))
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        parts = path.relative_to(root).with_suffix("").parts
        package = ".".join(parts[:-1])
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
                if module.split(".", 1)[0] in forbidden or (
                    module.split(".")[:2] in (
                        ["engine", "retrieval"], ["engine", "parsev1"],
                    )
                ):
                    violations.append(f"{path.relative_to(root)}:{node.lineno}:{module}")

    assert violations == []
