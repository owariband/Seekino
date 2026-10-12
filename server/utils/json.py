from __future__ import annotations

import json
from copy import deepcopy
from typing import Any


def convert_json_string_payload(
    value: Any,
    *,
    encode: bool,
    string_path: tuple[str, ...] = (),
) -> Any:
    """Encode/decode a payload as a JSON string, preserving Python float values.

    A path limits conversion to one subtree without mutating the input. Decode
    also accepts historical arrays/objects; encode normalizes existing strings
    first to avoid adding a second encoding layer.
    """
    if value is None:
        return None
    result = deepcopy(value) if string_path else value
    parent = result
    for key in string_path[:-1]:
        if not isinstance(parent, dict):
            return result
        parent = parent.get(key)
    if string_path:
        key = string_path[-1]
        if not isinstance(parent, dict) or key not in parent:
            return result
        payload = parent[key]
    else:
        payload = value
    if isinstance(payload, str):
        payload = json.loads(payload)
    converted = (
        json.dumps(payload, allow_nan=False, separators=(",", ":"))
        if encode else payload
    )
    if string_path:
        parent[key] = converted
        return result
    return converted
