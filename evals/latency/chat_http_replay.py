"""Submit one authorized Chat message over HTTP/SSE and measure its stream.

The caller owns conversation creation/seeding and passes an already-authenticated
``httpx.Client``. Credentials and CSRF headers stay in memory; only private
measurement artifacts are written. No retries or business/DB work happen here.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

import httpx


TERMINAL_TYPES = {"answer.completed", "answer.failed", "answer.interrupted"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_private(path: Path, text: str, *, exclusive: bool = False) -> None:
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(text)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_json(path: Path, value: object, *, exclusive: bool = False) -> None:
    _write_private(path, json.dumps(value, ensure_ascii=False, indent=2, default=str), exclusive=exclusive)


def _touch_private(path: Path) -> None:
    _write_private(path, "")


def _http_timeout(timeout_seconds: float) -> httpx.Timeout:
    bounded = max(float(timeout_seconds), 0.001)
    return httpx.Timeout(
        bounded,
        connect=min(10.0, bounded),
        read=min(60.0, bounded),
        write=min(30.0, bounded),
        pool=min(10.0, bounded),
    )


def _event_row(event: dict, received_seconds: float) -> dict:
    event_type = event.get("type")
    row = {"received_seconds": round(received_seconds, 6), "type": event_type}
    if isinstance(event.get("sequence"), int) and not isinstance(event.get("sequence"), bool):
        row["sequence"] = event["sequence"]
    if isinstance(event.get("messageId"), str):
        row["message_id"] = event["messageId"]
    if isinstance(event.get("phase"), str):
        row["phase"] = event["phase"]
    if event_type == "answer.reasoning.delta":
        delta = event.get("delta")
        row["reasoning_chars"] = len(delta) if isinstance(delta, str) else 0
    elif event_type == "answer.delta":
        delta = event.get("delta")
        row["delta_chars"] = len(delta) if isinstance(delta, str) else 0
    assistant = event.get("assistantMessage")
    if isinstance(assistant, dict):
        if isinstance(assistant.get("messageId"), str):
            row["assistant_message_id"] = assistant["messageId"]
        if isinstance(assistant.get("status"), str):
            row["assistant_status"] = assistant["status"]
        if isinstance(assistant.get("content"), str):
            row["content_chars"] = len(assistant["content"])
    return row


def _number(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _cancel_own_message(
    client: httpx.Client,
    *,
    conversation_id: str,
    message_id: str,
    tenant_id: str,
    headers: dict,
) -> dict:
    cancel_headers = {
        key: value for key, value in headers.items()
        if key.lower() not in {"accept", "content-type", "idempotency-key"}
    }
    cancel_headers["Accept"] = "application/json"
    try:
        response = client.post(
            "/api/agent/messages/cancel",
            params={
                "conversationId": conversation_id,
                "tenantId": tenant_id,
                "messageId": message_id,
            },
            headers=cancel_headers,
            timeout=10.0,
        )
        return {
            "attempted": True,
            "ok": response.is_success,
            "status_code": response.status_code,
        }
    except Exception as error:
        return {"attempted": True, "ok": False, "error": type(error).__name__}


def replay_message(
    client: httpx.Client,
    *,
    conversation_id: str,
    payload: dict,
    headers: dict,
    output_dir: Path,
    timeout_seconds: float = 300,
) -> dict:
    output_dir = Path(output_dir)
    result_path = output_dir / "result.json"
    if result_path.exists():
        raise FileExistsError(f"refusing to overwrite {result_path}")

    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    output_dir.chmod(0o700)
    _write_json(output_dir / "input.json", payload)
    events_path = output_dir / "events.jsonl"
    _touch_private(events_path)

    request_headers = {str(key): str(value) for key, value in headers.items()}
    request_headers["Accept"] = "text/event-stream"
    started_at_utc = _utc_now()
    started = monotonic()
    deadline = started + max(float(timeout_seconds), 0.001)

    ftt_seconds: float | None = None
    client_tt_seconds: float | None = None
    message_id: str | None = None
    terminal: dict | None = None
    terminal_type: str | None = None
    event_counts: dict[str, int] = {}
    timeout = False
    error: dict | None = None
    status = "failed"

    try:
        with client.stream(
            "POST",
            "/api/agent/messages/send",
            params={"conversationId": conversation_id},
            json=payload,
            headers=request_headers,
            timeout=_http_timeout(timeout_seconds),
        ) as response:
            if response.status_code >= 400:
                error = {"type": "HTTPStatusError", "status_code": response.status_code}
            else:
                sse_event: str | None = None
                data_lines: list[str] = []
                with events_path.open("a", encoding="utf-8") as events_file:
                    for line in response.iter_lines():
                        now = monotonic()
                        if now >= deadline:
                            timeout = True
                            error = {"type": "DeadlineExceeded"}
                            break
                        if line == "":
                            if data_lines:
                                raw = "\n".join(data_lines)
                                data_lines = []
                                try:
                                    event = json.loads(raw)
                                except (TypeError, ValueError):
                                    event = None
                                if isinstance(event, dict):
                                    event_type = event.get("type") or sse_event
                                    if not isinstance(event_type, str) or not event_type:
                                        continue
                                    received = monotonic() - started
                                    event_counts[event_type] = event_counts.get(event_type, 0) + 1
                                    events_file.write(json.dumps(
                                        _event_row(event, received), ensure_ascii=False,
                                    ) + "\n")
                                    events_file.flush()
                                    if message_id is None:
                                        started_message = event.get("messageId")
                                        if isinstance(started_message, str) and started_message:
                                            message_id = started_message
                                    if event_type == "answer.delta" and ftt_seconds is None:
                                        delta = event.get("delta")
                                        if isinstance(delta, str) and delta.strip():
                                            ftt_seconds = received
                                    elif event_type == "answer.snapshot" and ftt_seconds is None:
                                        assistant = event.get("assistantMessage")
                                        content = assistant.get("content") if isinstance(assistant, dict) else None
                                        if isinstance(content, str) and content.strip():
                                            ftt_seconds = received
                                    if event_type in TERMINAL_TYPES:
                                        terminal = event
                                        terminal_type = event_type
                                        client_tt_seconds = received
                                        assistant = event.get("assistantMessage")
                                        if isinstance(assistant, dict):
                                            if message_id is None and isinstance(assistant.get("messageId"), str):
                                                message_id = assistant["messageId"]
                                            if event_type == "answer.completed" and ftt_seconds is None:
                                                content = assistant.get("content")
                                                if isinstance(content, str) and content.strip():
                                                    ftt_seconds = received
                                        break
                            sse_event = None
                            continue
                        if line.startswith(":"):
                            continue
                        if line.startswith("event:"):
                            sse_event = line[6:].strip()
                        elif line.startswith("data:"):
                            data_lines.append(line[5:].lstrip())
                if terminal_type is None and not timeout and error is None:
                    error = {"type": "EOFError"}
    except httpx.TimeoutException as exc:
        timeout = True
        error = {"type": type(exc).__name__}
    except httpx.HTTPError as exc:
        error = {"type": type(exc).__name__}
    except Exception as exc:
        error = {"type": type(exc).__name__}

    elapsed_seconds = monotonic() - started
    cancellation = None
    if terminal_type is None and message_id is not None:
        tenant_id = payload.get("tenantId") or payload.get("tenant_id")
        if isinstance(tenant_id, str) and tenant_id:
            cancellation = _cancel_own_message(
                client,
                conversation_id=conversation_id,
                message_id=message_id,
                tenant_id=tenant_id,
                headers=request_headers,
            )

    assistant = terminal.get("assistantMessage") if isinstance(terminal, dict) else None
    if not isinstance(assistant, dict):
        assistant = {}
    if terminal_type is not None:
        status = {
            "answer.completed": "completed",
            "answer.failed": "failed",
            "answer.interrupted": "interrupted",
        }[terminal_type]
        if error is None and terminal_type != "answer.completed":
            terminal_error = terminal.get("error")
            if isinstance(terminal_error, dict) and isinstance(terminal_error.get("code"), str):
                error = {"type": "TerminalFailure", "code": terminal_error["code"]}
    elif timeout:
        status = "timeout"
    elif error is None:
        error = {"type": "EOFError"}

    timing = assistant.get("timing") if isinstance(assistant.get("timing"), dict) else None
    total_duration_ms = timing.get("totalDurationMs") if isinstance(timing, dict) else None
    server_tt = _number(total_duration_ms)
    server_tt_seconds = server_tt / 1000.0 if server_tt is not None else None
    content = assistant.get("content") if isinstance(assistant.get("content"), str) else None
    render_blocks = assistant.get("renderBlocks")
    if not isinstance(render_blocks, list):
        render_blocks = []

    result = {
        "conversation_id": conversation_id,
        "message_id": message_id,
        "status": status,
        "terminal_type": terminal_type,
        "ftt_seconds": ftt_seconds,
        "client_tt_seconds": client_tt_seconds,
        "server_tt_seconds": server_tt_seconds,
        "elapsed_seconds": elapsed_seconds,
        "event_counts": event_counts,
        "content": content,
        "render_blocks": render_blocks,
        "timing": timing,
        "timeout": timeout,
        "error": error,
        "cancellation": cancellation,
        "started_at_utc": started_at_utc,
        "ended_at_utc": _utc_now(),
    }

    if terminal_type is not None:
        _write_json(output_dir / "terminal.json", terminal)
    _write_json(result_path, result, exclusive=True)
    return result
