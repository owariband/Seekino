"""Opt-in local Parse request/response evidence; never sends diagnostics remotely."""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import re
from time import time_ns
from types import SimpleNamespace

_logger = logging.getLogger(__name__)
_capture: ContextVar[tuple[Path, object, dict] | None] = ContextVar("model_diagnostic_capture", default=None)
_SECRET_KEYS = {"authorization", "api_key", "apikey", "access_token", "secret_key"}
_MAX_EVENT_BYTES = 32 * 1024 * 1024


def _redact_text(text):
    text = re.sub(r'''(?i)\bbearer[ \t]+[^"'\s,;}\\]+''', "Bearer [redacted]", text)
    prefix = r'''(?i)((?:\\?["'])?(?:api[_-]?key|authorization|access_token|secret_key)(?:\\?["'])?\s*[:=]\s*)'''
    text = re.sub(prefix + r'''(\\?["'])(.*?)(\2)''', r"\1\2[redacted]\4", text)
    return re.sub(prefix + r'''([^"'\s,;}\\]+)''', r"\1[redacted]", text)


def _error_message(error):
    text = _redact_text(str(error))
    return re.sub(r"(?i)\bdata:[^,\s]*,[a-z0-9+/=]+|\b[a-z][a-z0-9+.-]*://[^\s<>\"']+", "[redacted_url]", text)[:4096]


def _plain(value, directory: Path):
    if callable(getattr(value, "model_dump", None)):
        value = value.model_dump(mode="json", exclude_unset=True)
    elif isinstance(value, SimpleNamespace):
        value = vars(value)
    if isinstance(value, Mapping):
        return {str(key): "[redacted]" if str(key).lower() in _SECRET_KEYS else _plain(item, directory)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item, directory) for item in value]
    if isinstance(value, str) and value.startswith("data:"):
        payload = value.encode("utf-8")
        digest = sha256(payload).hexdigest()
        media = directory.parent / "media"
        media.mkdir(mode=0o700, exist_ok=True)
        target = media / f"{digest}.data-url"
        if not target.exists():
            temporary = media / f".{digest}.{directory.name}.tmp"
            with temporary.open("xb") as handle:
                os.chmod(temporary, 0o600)
                handle.write(payload)
            try:
                os.link(temporary, target)
            except FileExistsError:
                pass
            finally:
                temporary.unlink(missing_ok=True)
        local_media = directory / "media"
        local_media.mkdir(mode=0o700, exist_ok=True)
        try:
            os.link(target, local_media / target.name)
        except FileExistsError:
            pass
        return {"$diagnostic_media": f"media/{target.name}", "sha256": digest}
    if value is None or isinstance(value, (str, int, float, bool)):
        return _redact_text(value) if isinstance(value, str) else value
    raise TypeError(f"Unsupported diagnostic value: {type(value).__name__}")


def _field(value, key, default=None):
    return value.get(key, default) if isinstance(value, Mapping) else getattr(value, key, default)


def _output_preview(kind, value, directory, call, state):
    if kind == "sse":
        response = _field(value, "response")
        state["response_id"] = _field(response, "id") or state.get("response_id")
        event_type = _field(value, "type")
        if event_type in {"response.output_text.delta", "response.output_text.done"}:
            key = (_field(value, "output_index", 0), _field(value, "item_id", ""), _field(value, "content_index", 0))
            text = _field(value, "delta" if event_type.endswith("delta") else "text")
            if isinstance(text, str) and text:
                parts = state.setdefault("parts", {})
                if event_type.endswith("done"):
                    parts[key] = [text]
                else:
                    parts.setdefault(key, []).append(text)
        item = _field(value, "item")
        if _field(item, "type") == "function_call":
            function = state.setdefault("functions", {}).setdefault(_field(item, "id"), {"parts": []})
            function.update({key: _field(item, key) for key in ("name", "call_id")})
            if isinstance(_field(item, "arguments"), str) and _field(item, "arguments"):
                function["parts"] = [_field(item, "arguments")]
        if event_type in {"response.function_call_arguments.delta", "response.function_call_arguments.done"}:
            function = state.setdefault("functions", {}).setdefault(_field(value, "item_id"), {"parts": []})
            text = _field(value, "delta" if event_type.endswith("delta") else "arguments")
            if isinstance(text, str):
                if event_type.endswith("done"):
                    function["parts"] = [text]
                else:
                    function["parts"].append(text)
        if _field(value, "type") not in {"response.completed", "response.failed", "response.incomplete"}:
            return
    elif kind == "response":
        response = value
    elif kind == "flush":
        response = state.get("response")
        if response is None:
            if not state.get("parts") and not state.get("functions"):
                return
            response = {"id": state.get("response_id"), "status": "interrupted"}
    else:
        return
    if kind != "flush":
        state["response"] = response
        return
    target = directory / "output-preview.json"
    if target.exists():
        return
    outputs, terminal_keys = [], set()

    def add(kind, text, **identity):
        text = _redact_text(text) if isinstance(text, str) else ""
        if kind == "output_text" and not text:
            return
        omitted = max(0, len(text) - 4000)
        preview = text if not omitted else text[:2000] + f"\n… [省略 {omitted} 字符] …\n" + text[-2000:]
        outputs.append({"type": kind, **identity, "chars": len(text),
                        "omitted_chars": omitted, "preview": preview})

    for output_index, item in enumerate(_field(response, "output", []) or []):
        if _field(item, "type") == "function_call":
            arguments = _field(item, "arguments")
            streamed = state.get("functions", {}).get(_field(item, "id"), {})
            add("function_call", arguments or "".join(streamed.get("parts", [])), name=_field(item, "name"),
                call_id=_field(item, "call_id"), item_id=_field(item, "id"), source="terminal" if arguments else "stream")
        for content_index, part in enumerate(_field(item, "content", []) or []):
            if _field(part, "type") == "output_text":
                add("output_text", _field(part, "text"), item_id=_field(item, "id"))
                if _field(part, "text"):
                    terminal_keys.add((output_index, _field(item, "id", ""), content_index))
    for item_id, function in state.get("functions", {}).items():
        if not any(item.get("item_id") == item_id and item["type"] == "function_call" for item in outputs):
            add("function_call", "".join(function["parts"]), item_id=item_id,
                name=function.get("name"), call_id=function.get("call_id"), source="stream", terminal_missing=True)
    aggregate_text = _field(response, "output_text")
    aggregate_used = not terminal_keys and isinstance(aggregate_text, str) and bool(aggregate_text)
    if aggregate_used:
        add("output_text", _field(response, "output_text"))
    if not aggregate_used:
        for key, parts in sorted(state.get("parts", {}).items()):
            if key not in terminal_keys:
                add("output_text", "".join(parts), item_id=key[1], source="stream", terminal_missing=True)
    for choice in _field(response, "choices", []) or []:
        message = _field(choice, "message")
        if isinstance(_field(message, "content"), str):
            add("output_text", _field(message, "content"))
        elif isinstance(_field(message, "content"), list):
            for part in _field(message, "content"):
                if _field(part, "type") in {"text", "output_text"}:
                    add("output_text", _field(part, "text"))
        for tool in _field(message, "tool_calls", []) or []:
            function = _field(tool, "function")
            add("function_call", _field(function, "arguments"), name=_field(function, "name"),
                call_id=_field(tool, "id"))
    summary = {"call_id": call.call_id, "provider_request_id": call.request_id,
               "response_id": _field(response, "id"), "status": _field(response, "status", "completed"),
               "client_status": state.get("client_status", "completed"),
               "context": {key: call.context[key] for key in (
                   "video_parse_task_id", "chunk_parse_task_id", "interval_parse_task_id", "video_chunk_id",
                   "stage", "sequence_no", "lease_version", "retry_count", "agent_run_id", "agent_round",
               ) if isinstance(call.context.get(key), (str, int))}, "outputs": outputs}
    encoded = json.dumps(summary, ensure_ascii=True, separators=(",", ":"))
    with target.open("x", encoding="utf-8") as handle:
        os.chmod(target, 0o600)
        handle.write(encoded + "\n")
    _logger.info("event=model_output_trace summary=%s", encoded)


def record_model_event(kind: str, value) -> None:
    capture = _capture.get()
    if capture is None:
        return
    directory, call, state = capture
    try:
        _output_preview(kind, value, directory, call, state)
        if (directory / "truncated").exists():
            return
        event = {"event": kind, "time_ns": time_ns(), "call_id": call.call_id,
                 "provider_request_id": call.request_id, "payload": _plain(value, directory)}
        target = directory / "events.jsonl"
        with target.open("a", encoding="utf-8") as handle:
            os.chmod(target, 0o600)
            encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
            if handle.tell() + len(encoded.encode("utf-8")) > _MAX_EVENT_BYTES:
                (directory / "truncated").touch(mode=0o600)
                _logger.warning("event=model_diagnostic_truncated call_id=%s", call.call_id)
                return
            handle.write(encoded)
    except Exception as error:
        _logger.warning("event=model_diagnostic_write_failed call_id=%s error_type=%s", call.call_id, type(error).__name__)


@contextmanager
def capture_model_diagnostics(call, request):
    if _capture.get() is not None and _capture.get()[1] is call:
        yield
        return
    root = os.environ.get("SEEKINO_MODEL_TRACE_DIR")
    token = _capture.set(None)
    if root and call.context.get("video_parse_task_id"):
        try:
            trace_root = Path(root).resolve()
            trace_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            trace_root.chmod(0o700)
            directory = trace_root / call.call_id
            directory.mkdir(mode=0o700, exist_ok=False)
            _capture.set((directory, call, {}))
            record_model_event("request", {"format_version": 1, "events_format": "sdk_parsed",
                                          "context": dict(call.context), "provider": call.provider,
                                          "operation": call.operation, "request": request})
            _logger.info("event=model_diagnostic_started call_id=%s artifact=%s", call.call_id, directory)
        except Exception as error:
            _logger.warning("event=model_diagnostic_write_failed call_id=%s error_type=%s", call.call_id, type(error).__name__)
    try:
        yield
    except BaseException as error:
        if _capture.get() is not None:
            _capture.get()[2]["client_status"] = "failed"
        try:
            if _capture.get() is not None:
                chain, seen, current = [], set(), error
                while current is not None and id(current) not in seen and len(chain) < 32:
                    seen.add(id(current))
                    chain.append({"type": type(current).__name__, "message": _error_message(current),
                                  **{key: getattr(current, key, None) for key in (
                                      "code", "status_code", "response_id", "request_id", "reason", "partial_output_chars")}})
                    current = current.__cause__ or current.__context__
                record_model_event("error", {"chain": chain})
        except Exception as diagnostic_error:
            _logger.warning("event=model_diagnostic_write_failed call_id=%s error_type=%s", call.call_id, type(diagnostic_error).__name__)
        # Preserve correlation when wrappers reach the worker retry logger.
        try:
            if getattr(error, "request_id", None) is None:
                error.request_id = call.request_id
        except Exception:
            pass
        raise
    else:
        record_model_event("completed", {})
    finally:
        try:
            if _capture.get() is not None:
                directory, captured_call, state = _capture.get()
                _output_preview("flush", None, directory, captured_call, state)
        except Exception as error:
            _logger.warning("event=model_diagnostic_write_failed call_id=%s error_type=%s", call.call_id, type(error).__name__)
        finally:
            _capture.reset(token)


def restore_diagnostic_media(value, directory: Path):
    """Restore saved request media for offline comparison or an explicit replay."""
    if isinstance(value, dict) and "$diagnostic_media" in value:
        target = (directory / value["$diagnostic_media"]).resolve()
        if not target.is_relative_to(directory.resolve()):
            raise ValueError("Diagnostic media escapes its directory")
        payload = target.read_bytes()
        if sha256(payload).hexdigest() != value["sha256"]:
            raise ValueError("Diagnostic media checksum mismatch")
        return payload.decode("utf-8")
    if isinstance(value, dict):
        return {key: restore_diagnostic_media(item, directory) for key, item in value.items()}
    if isinstance(value, list):
        return [restore_diagnostic_media(item, directory) for item in value]
    return value
