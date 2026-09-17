from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def inline_schema_refs(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Inline local Pydantic schema references for providers without `$ref` support."""
    definitions = schema.get("$defs", {})

    def expand(value: Any) -> Any:
        if isinstance(value, list):
            return [expand(item) for item in value]
        if not isinstance(value, dict):
            return value
        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            name = reference.rsplit("/", 1)[-1]
            target = definitions.get(name)
            if not isinstance(target, dict):
                raise ValueError(f"unresolved local JSON schema reference: {reference}")
            merged = {
                **target,
                **{key: item for key, item in value.items() if key != "$ref"},
            }
            return expand(merged)
        return {
            key: expand(item)
            for key, item in value.items()
            if key != "$defs"
        }

    expanded = expand(dict(schema))
    if not isinstance(expanded, dict):
        raise ValueError("JSON schema root must be an object")
    return expanded
