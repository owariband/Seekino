from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from typing import Any

import tiktoken

from .errors import (
    AgentToolFeedbackError, AgentToolInvocationError, AgentToolOutputError,
)


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


def range_items(
    items: list[dict[str, Any]],
    *,
    start: int,
    total: int,
    maximum_bytes: int,
    maximum_chars: int,
    encoding: tiktoken.Encoding,
    metadata: dict[str, Any],
) -> str:
    """Keep a full prefix of the requested range and mark the remaining entries."""
    _validate_maximum_bytes(maximum_bytes)
    if maximum_chars <= 0:
        raise ValueError("maximum_chars must be greater than zero")
    warning = (
        "请减少item检索数目，超限已被裁剪，无法看到全量信息。"
        "缩小start/end项区间；单项过大时用reference/start/end字符区间读取。"
    )
    brief = [
        {"reference": item["reference"], "section": item["section"],
         "total_chars": item["total_chars"], "truncated": True}
        for item in items
    ]
    end = start + len(items)
    payload = {
        **metadata, "start": start, "end": end, "total": total,
        "complete_end": start, "truncated": bool(items), "msg": warning if items else "",
        "token_limit": 8_000, "token_encoding": encoding.name, "items": list(brief),
    }

    def fits() -> bool:
        encoded = _json(payload)
        return (
            len(encoded.encode("utf-8")) <= maximum_bytes
            and len(encoding.encode(encoded, disallowed_special=())) <= payload["token_limit"]
        )

    payload.update(items=items, complete_end=end, truncated=False, msg="")
    if sum(len(item["content"]) for item in items) <= maximum_chars and fits():
        return bounded_json(payload, maximum_bytes)
    payload.update(items=list(brief), complete_end=start, truncated=True, msg=warning)
    if not fits():
        raise AgentToolFeedbackError(
            "BATCH_RANGE_TOO_LARGE", "Batch metadata alone exceeds the result budget.",
            "Reduce the references or narrow the start/end item range.",
        )
    content_chars = 0
    for index, item in enumerate(items):
        payload["items"][index] = item
        payload["complete_end"] = start + index + 1
        payload["truncated"] = payload["complete_end"] < end
        payload["msg"] = warning if payload["truncated"] else ""
        item_chars = len(item["content"])
        if content_chars + item_chars > maximum_chars or not fits():
            payload["items"][index] = brief[index]
            payload["complete_end"] = start + index
            payload["truncated"], payload["msg"] = True, warning
            break
        content_chars += item_chars
    return bounded_json(payload, maximum_bytes)


def range_text(
    content: str,
    *,
    resource_id: str,
    section: str,
    start: int,
    end: int | None,
    maximum_bytes: int,
    maximum_chars: int,
) -> str:
    _validate_maximum_bytes(maximum_bytes)
    if maximum_chars <= 0:
        raise ValueError("maximum_chars must be greater than zero")
    total_chars = len(content)
    if end is None:
        end = total_chars
    if (
        start < 0
        or end > total_chars
        or (start >= end and (start != 0 or total_chars != 0))
    ):
        raise AgentToolFeedbackError(
            "INVALID_RANGE",
            f"Requested [{start}, {end}) is outside section {section!r} "
            f"with {total_chars} characters.",
            f"Use 0 <= start < end <= {total_chars}, or [0, 0) if empty.",
        )

    def payload(stop: int) -> dict[str, Any]:
        return {
            "id": resource_id,
            "section": section,
            "start": start,
            "end": stop,
            "total_chars": total_chars,
            "content": content[start:stop],
        }

    requested = _json(payload(end))
    requested_bytes = len(requested.encode("utf-8"))
    requested_chars = end - start
    if requested_chars <= maximum_chars and requested_bytes <= maximum_bytes:
        return requested

    max_end = min(end, start + maximum_chars)
    best = max_end
    if len(_json(payload(max_end)).encode("utf-8")) > maximum_bytes:
        low, high, best = start, max_end, start
        while low <= high:
            middle = (low + high) // 2
            if len(_json(payload(middle)).encode("utf-8")) <= maximum_bytes:
                best = middle
                low = middle + 1
            else:
                high = middle - 1
    if best == start:
        raise AgentToolOutputError(
            "Tool result budget is too small for a readable content range"
        )
    raise AgentToolFeedbackError(
        "RANGE_TOO_LARGE",
        f"Requested [{start}, {end}) from {section!r} spans "
        f"{requested_chars} characters (limit {maximum_chars}) and needs "
        f"{requested_bytes} UTF-8 bytes (limit {maximum_bytes}).",
        f"Retry the same section with start={start} and end<={best}; "
        f"then continue from start={best}.",
        suggested_end=best,
    )


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
