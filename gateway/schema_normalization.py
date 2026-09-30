from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def inline_schema_refs(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Inline local Pydantic schema references for providers without `$ref` support."""
    definitions = schema.get("$defs", {})

    def pointer(path: tuple[str | int, ...]) -> str:
        encoded = [str(part).replace("~", "~0").replace("/", "~1") for part in path]
        return "#/" + "/".join(encoded)

    def expand(value: Any, path: tuple[str | int, ...] = ()) -> Any:
        if isinstance(value, list):
            return [expand(item, (*path, index)) for index, item in enumerate(value)]
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
            return expand(merged, path)

        expanded: dict[str, Any] = {}
        for key, item in value.items():
            if key == "$defs":
                continue
            if key == "discriminator" and isinstance(item, dict):
                discriminator = dict(item)
                mapping = discriminator.get("mapping")
                branches = value.get("oneOf")
                if isinstance(mapping, dict) and isinstance(branches, list):
                    rewritten: dict[str, str] = {}
                    for tag, target in mapping.items():
                        if not (
                            isinstance(target, str)
                            and target.startswith("#/$defs/")
                        ):
                            rewritten[tag] = target
                            continue
                        matches = [
                            index
                            for index, branch in enumerate(branches)
                            if isinstance(branch, dict) and branch.get("$ref") == target
                        ]
                        if len(matches) != 1:
                            raise ValueError(
                                "discriminator mapping does not identify one local branch: "
                                f"{target}"
                            )
                        rewritten[tag] = pointer((*path, "oneOf", matches[0]))
                    discriminator["mapping"] = rewritten
                expanded[key] = expand(discriminator, (*path, key))
                continue
            expanded[key] = expand(item, (*path, key))
        return expanded

    expanded = expand(dict(schema))
    if not isinstance(expanded, dict):
        raise ValueError("JSON schema root must be an object")
    return expanded
