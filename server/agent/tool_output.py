from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .state import AgentToolInvocationError, AgentToolOutputError


@dataclass(frozen=True, slots=True)
class CursorPosition:
    resource_id: str
    offset: int
    fingerprint: str


def bounded_json(payload: object, maximum_bytes: int) -> str:
    _validate_maximum_bytes(maximum_bytes)
    encoded = _json(payload)
    if len(encoded.encode("utf-8")) > maximum_bytes:
        raise AgentToolOutputError(
            f"Tool result exceeds {maximum_bytes} UTF-8 bytes"
        )
    return encoded


def cursor_position(cursor: str, *, namespace: str) -> CursorPosition:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError, binascii.Error) as exc:
        raise AgentToolInvocationError("Invalid Tool cursor") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("namespace") != namespace
        or not isinstance(payload.get("resource_id"), str)
        or not payload["resource_id"]
        or isinstance(payload.get("offset"), bool)
        or not isinstance(payload.get("offset"), int)
        or payload["offset"] < 0
        or not isinstance(payload.get("fingerprint"), str)
        or not payload["fingerprint"]
    ):
        raise AgentToolInvocationError("Invalid or mismatched Tool cursor")
    return CursorPosition(
        resource_id=payload["resource_id"],
        offset=payload["offset"],
        fingerprint=payload["fingerprint"],
    )


def page_items(
    items: list[dict[str, Any]],
    *,
    namespace: str,
    resource_id: str,
    cursor: str | None,
    maximum_items: int,
    maximum_bytes: int,
    metadata: dict[str, Any] | None = None,
) -> str:
    if maximum_items <= 0:
        raise ValueError("maximum_items must be greater than zero")
    _validate_maximum_bytes(maximum_bytes)
    fingerprint = _fingerprint(items)
    offset = 0
    if cursor is not None:
        position = cursor_position(cursor, namespace=namespace)
        if position.resource_id != resource_id or position.fingerprint != fingerprint:
            raise AgentToolInvocationError("Stale or mismatched Tool cursor")
        offset = position.offset
    if offset > len(items):
        raise AgentToolInvocationError("Tool cursor offset is outside the resource")

    page: list[dict[str, Any]] = []
    for item in items[offset : offset + maximum_items]:
        candidate = [*page, item]
        next_offset = offset + len(candidate)
        payload = _item_page_payload(
            candidate,
            total=len(items),
            next_cursor=(
                _cursor(namespace, resource_id, next_offset, fingerprint)
                if next_offset < len(items)
                else None
            ),
            metadata=metadata,
        )
        if len(_json(payload).encode("utf-8")) > maximum_bytes:
            break
        page = candidate

    next_offset = offset + len(page)
    if next_offset < len(items) and not page:
        raise AgentToolOutputError(
            "One catalog item cannot fit inside the Tool result budget"
        )
    return bounded_json(
        _item_page_payload(
            page,
            total=len(items),
            next_cursor=(
                _cursor(namespace, resource_id, next_offset, fingerprint)
                if next_offset < len(items)
                else None
            ),
            metadata=metadata,
        ),
        maximum_bytes,
    )


def page_text(
    content: str,
    *,
    namespace: str,
    resource_id: str,
    cursor: str | None,
    maximum_bytes: int,
) -> str:
    _validate_maximum_bytes(maximum_bytes)
    fingerprint = _fingerprint(content)
    offset = 0
    if cursor is not None:
        position = cursor_position(cursor, namespace=namespace)
        if position.resource_id != resource_id or position.fingerprint != fingerprint:
            raise AgentToolInvocationError("Stale or mismatched Tool cursor")
        offset = position.offset
    if offset > len(content):
        raise AgentToolInvocationError("Tool cursor offset is outside the resource")

    low, high, best = offset, len(content), offset
    while low <= high:
        end = (low + high) // 2
        next_cursor = (
            _cursor(namespace, resource_id, end, fingerprint)
            if end < len(content)
            else None
        )
        payload = {
            "id": resource_id,
            "content": content[offset:end],
            "next_cursor": next_cursor,
        }
        if len(_json(payload).encode("utf-8")) <= maximum_bytes:
            best = end
            low = end + 1
        else:
            high = end - 1
    if best == offset and offset < len(content):
        raise AgentToolOutputError(
            "Tool result budget is too small for a readable content page"
        )
    return bounded_json({
        "id": resource_id,
        "content": content[offset:best],
        "next_cursor": (
            _cursor(namespace, resource_id, best, fingerprint)
            if best < len(content)
            else None
        ),
    }, maximum_bytes)


def _item_page_payload(
    items: list[dict[str, Any]],
    *,
    total: int,
    next_cursor: str | None,
    metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        **(metadata or {}),
        "items": items,
        "total": total,
        "next_cursor": next_cursor,
    }


def _cursor(namespace: str, resource_id: str, offset: int, fingerprint: str) -> str:
    encoded = _json({
        "namespace": namespace,
        "resource_id": resource_id,
        "offset": offset,
        "fingerprint": fingerprint,
    }).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()[:24]


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _validate_maximum_bytes(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("maximum_bytes must be a positive integer")
