from __future__ import annotations

import base64
import hashlib
import json


def _scope_digest(scope: dict) -> str:
    canonical = json.dumps(scope, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def encode_cursor(offset: int, *, scope: dict) -> str:
    payload = json.dumps({"offset": offset, "scope": _scope_digest(scope)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None, *, scope: dict) -> int:
    if cursor is None:
        return 0
    try:
        if not cursor or len(cursor) > 512:
            raise ValueError
        payload = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
        offset = payload["offset"]
        if type(offset) is not int or offset < 0 or payload["scope"] != _scope_digest(scope):
            raise ValueError
        return offset
    except (ValueError, KeyError, TypeError, UnicodeError) as exc:
        raise ValueError("cursor does not match this workspace, filter, or sort") from exc
