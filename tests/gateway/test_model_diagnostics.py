from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import stat
from threading import Barrier
from types import SimpleNamespace

from arkruntime._models import construct_type
from arkruntime.types.responses.response_stream_event import ResponseStreamEvent
import pytest

from gateway import model_diagnostics as diagnostics
from gateway.usage import ModelCallTracker, capture_model_usage, model_call_context, track_model_call


def _events(directory: Path) -> list[dict]:
    return [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]


class Unserializable:
    def __init__(self):
        self.calls = 0

    def model_dump(self, **_kwargs):
        self.calls += 1
        raise AssertionError("diagnostics must not serialize disabled requests")


@pytest.mark.parametrize("enabled,context", [
    (False, {"video_parse_task_id": "parse-disabled"}),
    (True, {"assistant_message_id": "chat"}),
    (True, {"video_parse_task_id": None}),
])
def test_disabled_and_nonparse_capture_do_not_serialize_or_create_artifacts(tmp_path, monkeypatch, caplog, enabled, context):
    root = tmp_path / "trace"
    if enabled:
        monkeypatch.setenv("ANIVAG_MODEL_TRACE_DIR", str(root))
    else:
        monkeypatch.delenv("ANIVAG_MODEL_TRACE_DIR", raising=False)
    request = Unserializable()
    caplog.set_level(logging.INFO, logger="gateway.model_diagnostics")
    with model_call_context(context):
        call = ModelCallTracker(provider="ark", model="offline", operation="understand")
        with diagnostics.capture_model_diagnostics(call, request):
            diagnostics.record_model_event("response", request)
    assert request.calls == 0
    assert not root.exists()
    assert not caplog.records
    assert diagnostics._capture.get() is None


def test_request_and_response_media_share_private_checksum_verified_hardlinks_without_mutation(tmp_path, monkeypatch):
    root = tmp_path / "trace"
    monkeypatch.setenv("ANIVAG_MODEL_TRACE_DIR", str(root))
    media_url = "data:video/mp4;base64," + base64.b64encode(b"offline-video" * 5000).decode()
    request = {"model": "offline", "input": [{"role": "user", "content": [
        {"type": "input_text", "text": "private prompt"},
        {"type": "input_video", "video_url": media_url, "fps": 1},
        {"type": "input_image", "image_url": media_url},
    ]}]}
    original = deepcopy(request)
    calls = []
    for task in ("task-a", "task-b"):
        with model_call_context({"video_parse_task_id": task, "stage": "understand"}):
            with track_model_call(provider="ark", model="offline", operation="canvas") as call:
                with diagnostics.capture_model_diagnostics(call, request):
                    call.request_id = "request-" + task
                    diagnostics.record_model_event("response", {"id": task, "text": "private answer"})
                calls.append(call)
    digest = sha256(media_url.encode()).hexdigest()
    shared = root / "media" / f"{digest}.data-url"
    assert shared.read_text() == media_url
    assert list((root / "media").iterdir()) == [shared]
    for call in calls:
        directory = root / call.call_id
        events = _events(directory)
        assert [event["event"] for event in events] == ["request", "response", "completed"]
        assert all(event["call_id"] == call.call_id for event in events)
        assert events[0]["provider_request_id"] is None
        assert events[1]["provider_request_id"] == events[2]["provider_request_id"] == call.request_id
        saved = events[0]["payload"]["request"]
        assert diagnostics.restore_diagnostic_media(saved, directory) == request
        reference = saved["input"][0]["content"][1]["video_url"]
        assert reference["sha256"] == digest
        local = directory / reference["$diagnostic_media"]
        assert os.path.samefile(shared, local)
        assert media_url not in (directory / "events.jsonl").read_text()
        for path in (directory, directory / "media"):
            assert stat.S_IMODE(path.stat().st_mode) == 0o700
        for path in (local, directory / "events.jsonl"):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(shared.parent.stat().st_mode) == 0o700
    assert request == original
    shared.write_text("corrupt")
    with pytest.raises(ValueError, match="checksum mismatch"):
        diagnostics.restore_diagnostic_media(reference, root / calls[0].call_id)


def test_restoration_rejects_paths_and_symlinks_outside_call_directory(tmp_path):
    directory = tmp_path / "call"
    directory.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    (directory / "link").symlink_to(outside)
    for reference in ("../outside", str(outside), "link"):
        with pytest.raises(ValueError, match="escapes its directory"):
            diagnostics.restore_diagnostic_media({
                "$diagnostic_media": reference, "sha256": sha256(b"outside").hexdigest(),
            }, directory)


def test_concurrent_calls_keep_task_ownership_and_publish_complete_shared_media(tmp_path, monkeypatch):
    root = tmp_path / "trace"
    monkeypatch.setenv("ANIVAG_MODEL_TRACE_DIR", str(root))
    media_url = "data:video/mp4;base64," + base64.b64encode(b"shared-video" * 100000).decode()
    barrier = Barrier(4)
    original_link = os.link

    def link_complete_media(source, target, *args, **kwargs):
        if str(target).endswith(".data-url"):
            assert Path(source).read_text() == media_url
            if Path(target).exists():
                assert Path(target).read_text() == media_url
        return original_link(source, target, *args, **kwargs)

    monkeypatch.setattr(diagnostics.os, "link", link_complete_media)

    def capture(index):
        context = {"video_parse_task_id": f"task-{index}", "chunk_parse_task_id": f"chunk-{index}"}
        with model_call_context(context):
            with track_model_call(provider="ark", model="offline", operation="canvas") as call:
                barrier.wait(timeout=5)
                with diagnostics.capture_model_diagnostics(call, {"input": [media_url], "index": index}):
                    call.request_id = f"request-{index}"
                    diagnostics.record_model_event("response", {"index": index})
                assert diagnostics._capture.get() is None
                return call.call_id, context, index

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(capture, range(4)))
    shared_files = list((root / "media").iterdir())
    assert len(shared_files) == 1 and not shared_files[0].name.startswith(".")
    shared = shared_files[0]
    assert shared.read_text() == media_url
    for call_id, context, index in results:
        directory = root / call_id
        events = _events(directory)
        assert [event["event"] for event in events] == ["request", "response", "completed"]
        assert all(event["call_id"] == call_id for event in events)
        assert events[0]["payload"]["context"] == context
        assert events[1]["payload"] == {"index": index}
        assert events[1]["provider_request_id"] == f"request-{index}"
        restored = diagnostics.restore_diagnostic_media(events[0]["payload"]["request"], directory)
        assert restored == {"input": [media_url], "index": index}
        assert os.path.samefile(shared, next((directory / "media").iterdir()))


def test_nested_same_call_is_recorded_once_and_nested_other_call_restores_parent(tmp_path, monkeypatch):
    monkeypatch.setenv("ANIVAG_MODEL_TRACE_DIR", str(tmp_path))
    ignored = Unserializable()
    with model_call_context({"video_parse_task_id": "outer"}):
        with track_model_call(provider="ark", model="offline", operation="outer") as outer:
            with diagnostics.capture_model_diagnostics(outer, {"outer": True}):
                with diagnostics.capture_model_diagnostics(outer, ignored):
                    diagnostics.record_model_event("outer-response", {})
                with model_call_context({"video_parse_task_id": "inner"}):
                    with track_model_call(provider="ark", model="offline", operation="inner") as inner:
                        with diagnostics.capture_model_diagnostics(inner, {"inner": True}):
                            diagnostics.record_model_event("inner-response", {})
                diagnostics.record_model_event("outer-after-inner", {})
    assert ignored.calls == 0
    assert [event["event"] for event in _events(tmp_path / outer.call_id)] == [
        "request", "outer-response", "outer-after-inner", "completed",
    ]
    assert [event["event"] for event in _events(tmp_path / inner.call_id)] == ["request", "inner-response", "completed"]
    assert diagnostics._capture.get() is None


def test_nested_nonparse_capture_does_not_write_to_outer_parse(tmp_path, monkeypatch):
    monkeypatch.setenv("ANIVAG_MODEL_TRACE_DIR", str(tmp_path))
    ignored = Unserializable()
    with model_call_context({"video_parse_task_id": "outer"}):
        outer = ModelCallTracker(provider="ark", model="offline", operation="outer")
        with diagnostics.capture_model_diagnostics(outer, {}):
            with model_call_context({"video_parse_task_id": None, "assistant_message_id": "chat"}):
                inner = ModelCallTracker(provider="ark", model="offline", operation="chat")
                with diagnostics.capture_model_diagnostics(inner, ignored):
                    diagnostics.record_model_event("chat-response", ignored)
            diagnostics.record_model_event("outer-response", {})
    assert ignored.calls == 0
    assert not (tmp_path / inner.call_id).exists()
    assert [event["event"] for event in _events(tmp_path / outer.call_id)] == ["request", "outer-response", "completed"]


def test_authentication_fields_are_redacted_without_logging_request_or_response_bodies(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("ANIVAG_MODEL_TRACE_DIR", str(tmp_path))
    caplog.set_level(logging.INFO, logger="gateway.model_diagnostics")
    request = {
        "headers": {"Authorization": "Bearer hidden-auth"},
        "api_key": "hidden-key", "nested": [{"APIKEY": "hidden-apikey", "access_token": "hidden-token", "secret_key": "hidden-secret"}],
        "input": "private-request-body",
    }
    original = deepcopy(request)
    with model_call_context({"video_parse_task_id": "parse-private"}):
        with track_model_call(provider="ark", model="offline", operation="understand") as call:
            with diagnostics.capture_model_diagnostics(call, request):
                diagnostics.record_model_event("response", {"text": "private-response-body"})
    saved_text = (tmp_path / call.call_id / "events.jsonl").read_text()
    saved = _events(tmp_path / call.call_id)[0]["payload"]["request"]
    assert saved["headers"]["Authorization"] == saved["api_key"] == "[redacted]"
    assert set(saved["nested"][0].values()) == {"[redacted]"}
    assert "private-request-body" in saved_text and "private-response-body" in saved_text
    assert "hidden-" not in saved_text
    assert "hidden-" not in caplog.text and "private-" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    assert request == original


@pytest.mark.parametrize("failure", ["directory", "event-write", "media-link", "serialization"])
@pytest.mark.parametrize("raise_original", [False, True])
def test_diagnostic_io_failure_preserves_result_exception_and_resets_context(tmp_path, monkeypatch, caplog, failure, raise_original):
    root = tmp_path / "trace"
    monkeypatch.setenv("ANIVAG_MODEL_TRACE_DIR", str(root))
    request = {"input": ["data:image/png;base64,cHJpdmF0ZQ=="]}
    if failure == "directory":
        root.write_text("blocked")
    elif failure == "event-write":
        original_open = Path.open

        def fail_open(path, *args, **kwargs):
            if path.name == "events.jsonl":
                raise OSError("private diagnostic disk error")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", fail_open)
    elif failure == "media-link":
        def fail_link(*_args, **_kwargs):
            raise OSError("private diagnostic media error")

        monkeypatch.setattr(diagnostics.os, "link", fail_link)
    else:
        request = Unserializable()
    original_error = RuntimeError("original provider failure")
    result = object()
    usage = []
    with capture_model_usage(usage.append), model_call_context({"video_parse_task_id": "parse-io"}):
        if raise_original:
            with pytest.raises(RuntimeError) as caught:
                with track_model_call(provider="ark", model="offline", operation="understand") as call:
                    call.request_id = "provider-request"
                    with diagnostics.capture_model_diagnostics(call, request):
                        raise original_error
            assert caught.value is original_error
            assert caught.value.request_id == "provider-request"
        else:
            with track_model_call(provider="ark", model="offline", operation="understand") as call:
                with diagnostics.capture_model_diagnostics(call, request):
                    diagnostics.record_model_event("response", result)
                    returned = result
            assert returned is result
    assert diagnostics._capture.get() is None
    assert len(usage) == 1 and usage[0].status == ("failed" if raise_original else "completed")
    assert "event=model_diagnostic_write_failed" in caplog.text
    assert "private diagnostic" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_event_limit_marks_truncation_and_stops_later_serialization(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("ANIVAG_MODEL_TRACE_DIR", str(tmp_path))
    assert diagnostics._MAX_EVENT_BYTES == 32 * 1024 * 1024
    ignored = Unserializable()
    with model_call_context({"video_parse_task_id": "parse-large"}):
        with track_model_call(provider="ark", model="offline", operation="understand") as call:
            with diagnostics.capture_model_diagnostics(call, {}):
                directory = tmp_path / call.call_id
                before = (directory / "events.jsonl").read_bytes()
                diagnostics.record_model_event("response", {"text": "x" * (32 * 1024 * 1024)})
                diagnostics.record_model_event("later", ignored)
    marker = directory / "truncated"
    assert marker.is_file() and stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert (directory / "events.jsonl").read_bytes() == before
    assert len(before) < 32 * 1024 * 1024
    assert ignored.calls == 0
    assert "event=model_diagnostic_truncated" in caplog.text
    assert diagnostics._capture.get() is None


def test_sdk_serialization_preserves_omitted_fields_explicit_nulls_and_empty_text(tmp_path, monkeypatch):
    monkeypatch.setenv("ANIVAG_MODEL_TRACE_DIR", str(tmp_path))
    wire = {"type": "response.completed", "sequence_number": 1, "response": {
        "id": "response-empty", "status": "completed", "model": "offline", "error": None,
        "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": ""}]}],
    }}
    omitted = construct_type(value=wire, type_=ResponseStreamEvent)
    explicit = construct_type(value={**wire, "response": {**wire["response"], "tools": None}}, type_=ResponseStreamEvent)
    with model_call_context({"video_parse_task_id": "parse-sdk"}):
        with track_model_call(provider="ark", model="offline", operation="understand") as call:
            with diagnostics.capture_model_diagnostics(call, {}):
                diagnostics.record_model_event("sse", omitted)
                diagnostics.record_model_event("sse", explicit)
    first, second = [event["payload"] for event in _events(tmp_path / call.call_id) if event["event"] == "sse"]
    assert first == wire
    assert "tools" not in first["response"]
    assert "tools" in second["response"] and second["response"]["tools"] is None
    assert first["response"]["error"] is None
    assert first["response"]["output"][0]["content"][0]["text"] == ""


def test_exception_chain_and_provider_request_id_are_recorded_without_leaking_auth_or_urls(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("ANIVAG_MODEL_TRACE_DIR", str(tmp_path))
    inner = ValueError("Authorization=inner-key https://provider.invalid/private?token=hidden data:image/png;base64,cHJpdmF0ZQ==")
    inner.request_id = "inner-request"
    inner.status_code = 400
    outer = RuntimeError("Bearer outer-key api_key=outer-api-key")
    outer.__cause__ = inner
    usage = []
    with capture_model_usage(usage.append), model_call_context({"video_parse_task_id": "parse-failed"}):
        with pytest.raises(RuntimeError) as caught:
            with track_model_call(provider="ark", model="offline", operation="understand") as call:
                call.request_id = "provider-request"
                with diagnostics.capture_model_diagnostics(call, {}):
                    raise outer
    assert caught.value is outer and outer.__cause__ is inner
    assert outer.request_id == "provider-request"
    error = _events(tmp_path / call.call_id)[-1]
    assert error["event"] == "error"
    assert error["provider_request_id"] == "provider-request"
    chain = error["payload"]["chain"]
    assert [item["type"] for item in chain] == ["RuntimeError", "ValueError"]
    assert chain[1]["request_id"] == "inner-request" and chain[1]["status_code"] == 400
    assert "[redacted]" in chain[0]["message"] and "[redacted_url]" in chain[1]["message"]
    contents = (tmp_path / call.call_id / "events.jsonl").read_text()
    assert all(secret not in contents for secret in ("outer-key", "outer-api-key", "inner-key", "provider.invalid", "cHJpdmF0ZQ=="))
    assert "outer-key" not in caplog.text and "provider.invalid" not in caplog.text
    assert usage[0].status == "failed" and usage[0].request_id == "provider-request"
    assert diagnostics._capture.get() is None


@pytest.mark.parametrize("enabled", [False, True])
def test_exception_with_broken_string_preserves_original_failure_and_request_correlation(tmp_path, monkeypatch, caplog, enabled):
    class BrokenMessage(RuntimeError):
        def __str__(self):
            raise ValueError("private failure while rendering provider exception")

    if enabled:
        monkeypatch.setenv("ANIVAG_MODEL_TRACE_DIR", str(tmp_path))
    else:
        monkeypatch.delenv("ANIVAG_MODEL_TRACE_DIR", raising=False)
    original = BrokenMessage()
    usage = []
    with model_call_context({"video_parse_task_id": "parse-broken"}), capture_model_usage(usage.append):
        with pytest.raises(BrokenMessage) as caught:
            with track_model_call(provider="ark", model="offline", operation="understand") as call:
                call.request_id = "provider-broken-request"
                with diagnostics.capture_model_diagnostics(call, {}):
                    raise original
    assert caught.value is original
    assert original.request_id == "provider-broken-request"
    assert usage[0].status == "failed" and usage[0].request_id == "provider-broken-request"
    assert diagnostics._capture.get() is None
    assert "private failure" not in caplog.text
    if enabled:
        assert "event=model_diagnostic_write_failed" in caplog.text
    else:
        assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("text,secrets", [
    ('{"api_key":"synthetic-json-api-key","secret_key":"synthetic-json-secret","access_token":"synthetic-json-token"}',
     ("synthetic-json-api-key", "synthetic-json-secret", "synthetic-json-token")),
    ('{"Authorization":"Bearer synthetic-json-bearer"}', ("synthetic-json-bearer",)),
    ('{ "api_key" : "synthetic spaced key", "Authorization" : "Bearer synthetic-spaced-bearer" }',
     ("synthetic", "spaced key", "synthetic-spaced-bearer")),
    (r'{"arguments":"{\"api_key\":\"synthetic-escaped-key\",\"Authorization\":\"Bearer synthetic-escaped-bearer\"}"}',
     ("synthetic-escaped-key", "synthetic-escaped-bearer")),
    ("Bearer synthetic-plain-bearer api_key=synthetic-plain-api-key", ("synthetic-plain-bearer", "synthetic-plain-api-key")),
])
def test_credentials_in_json_arguments_and_error_messages_are_redacted(tmp_path, monkeypatch, caplog, text, secrets):
    monkeypatch.setenv("ANIVAG_MODEL_TRACE_DIR", str(tmp_path))
    original_error = RuntimeError(text)
    with model_call_context({"video_parse_task_id": "parse-json-auth"}):
        with pytest.raises(RuntimeError) as caught:
            with track_model_call(provider="ark", model="offline", operation="understand") as call:
                with diagnostics.capture_model_diagnostics(call, {"arguments": text}):
                    raise original_error
    assert caught.value is original_error
    events = _events(tmp_path / call.call_id)
    saved_text = (tmp_path / call.call_id / "events.jsonl").read_text()
    assert all(secret not in saved_text and secret not in caplog.text for secret in secrets)
    saved_arguments = events[0]["payload"]["request"]["arguments"]
    assert "[redacted]" in saved_arguments and "[redacted]" in events[-1]["payload"]["chain"][0]["message"]
    if text.startswith("{"):
        assert isinstance(json.loads(saved_arguments), dict)


def test_namespace_response_is_saved_but_transport_objects_are_not_traversed(tmp_path, monkeypatch, caplog):
    class Transport:
        def __init__(self):
            self.api_key = "private-transport-key"
            self.body = "private-transport-body"

    monkeypatch.setenv("ANIVAG_MODEL_TRACE_DIR", str(tmp_path))
    response = SimpleNamespace(id="offline-response", output=[SimpleNamespace(text="offline-answer")], error=None)
    original = deepcopy(response)
    transport = Transport()
    with model_call_context({"video_parse_task_id": "parse-transport"}):
        with track_model_call(provider="ark", model="offline", operation="understand") as call:
            with diagnostics.capture_model_diagnostics(call, {}):
                diagnostics.record_model_event("response", response)
                diagnostics.record_model_event("transport", transport)
    events = _events(tmp_path / call.call_id)
    assert [event["event"] for event in events] == ["request", "response", "completed"]
    assert events[1]["payload"] == {"id": "offline-response", "output": [{"text": "offline-answer"}], "error": None}
    assert response == original
    assert transport.api_key == "private-transport-key"
    assert "error_type=TypeError" in caplog.text and "private-transport" not in caplog.text
    assert "private-transport" not in (tmp_path / call.call_id / "events.jsonl").read_text()


def _output_preview(directory, caplog):
    target = directory / "output-preview.json"
    artifact = json.loads(target.read_text())
    records = [record for record in caplog.records if "event=model_output_trace " in record.getMessage()]
    assert len(records) == 1
    assert "\n" not in records[0].getMessage() and records[0].exc_info is None
    summary = json.loads(records[0].getMessage().split("summary=", 1)[1])
    assert summary == artifact
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    return artifact


@pytest.mark.parametrize("length", [1, 2000, 4000])
def test_output_preview_keeps_short_text_once_without_overlapping_head_and_tail(tmp_path, monkeypatch, caplog, length):
    monkeypatch.setenv("ANIVAG_MODEL_TRACE_DIR", str(tmp_path))
    caplog.set_level(logging.INFO, logger="gateway.model_diagnostics")
    text = "x" * length
    response = {"id": "response-short", "status": "completed", "output": [
        {"type": "message", "id": "message-short", "content": [{"type": "output_text", "text": text}]},
    ]}
    original = deepcopy(response)
    with model_call_context({"video_parse_task_id": "parse-preview"}):
        with track_model_call(provider="ark", model="offline", operation="understand") as call:
            call.request_id = "request-preview"
            with diagnostics.capture_model_diagnostics(call, {}):
                diagnostics.record_model_event("response", response)
    preview = _output_preview(tmp_path / call.call_id, caplog)
    assert preview["call_id"] == call.call_id and preview["provider_request_id"] == "request-preview"
    assert preview["response_id"] == "response-short" and preview["status"] == "completed"
    assert preview["context"]["video_parse_task_id"] == "parse-preview"
    assert len(preview["outputs"]) == 1
    output = preview["outputs"][0]
    assert output["type"] == "output_text" and output["item_id"] == "message-short"
    assert output["chars"] == length and output["omitted_chars"] == 0
    assert output["preview"] == text
    assert response == original


@pytest.mark.parametrize("head,middle,tail", [
    ("A" * 2000, "M" * 700, "Z" * 2000),
    ("头🐱" * 1000, "中🙂" * 150, "尾🐶" * 1000),
])
def test_output_preview_counts_unicode_characters_and_saves_exact_2000_character_edges(tmp_path, monkeypatch, caplog, head, middle, tail):
    monkeypatch.setenv("ANIVAG_MODEL_TRACE_DIR", str(tmp_path))
    caplog.set_level(logging.INFO, logger="gateway.model_diagnostics")
    text = head + middle + tail
    response = SimpleNamespace(id="response-long", status="completed", output=[SimpleNamespace(
        type="message", id="message-long", content=[SimpleNamespace(type="output_text", text=text)],
    )])
    original = deepcopy(response)
    with model_call_context({"video_parse_task_id": "parse-unicode"}):
        with track_model_call(provider="ark", model="offline", operation="understand") as call:
            with diagnostics.capture_model_diagnostics(call, {}):
                diagnostics.record_model_event("response", response)
    output = _output_preview(tmp_path / call.call_id, caplog)["outputs"][0]
    assert output["chars"] == len(text) and output["omitted_chars"] == len(middle)
    assert output["preview"] == head + f"\n… [省略 {len(middle)} 字符] …\n" + tail
    assert response == original


def test_output_preview_records_tool_only_sdk_arguments_and_deduplicates_terminal_events(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("ANIVAG_MODEL_TRACE_DIR", str(tmp_path))
    caplog.set_level(logging.INFO, logger="gateway.model_diagnostics")
    arguments = json.dumps({"operations": [{"op": "patch_event", "event_ref": "event-1"}]})
    wire = {"type": "response.completed", "sequence_number": 3, "response": {
        "id": "response-tool", "status": "completed", "output": [{
            "type": "function_call", "id": "item-tool", "call_id": "tool-call", "name": "write_context_canvas",
            "arguments": arguments, "status": "completed",
        }],
    }}
    terminal = construct_type(value=wire, type_=ResponseStreamEvent)
    original = terminal.model_dump(mode="json", exclude_unset=True)
    with model_call_context({"video_parse_task_id": "parse-tool-preview"}):
        with track_model_call(provider="ark", model="offline", operation="understand") as call:
            with diagnostics.capture_model_diagnostics(call, {}):
                diagnostics.record_model_event("sse", terminal)
                diagnostics.record_model_event("sse", terminal)
                diagnostics.record_model_event("response", terminal.response)
    preview = _output_preview(tmp_path / call.call_id, caplog)
    assert preview["response_id"] == "response-tool"
    assert preview["outputs"] == [{
        "type": "function_call", "item_id": "item-tool", "call_id": "tool-call", "name": "write_context_canvas",
        "source": "terminal", "chars": len(arguments), "omitted_chars": 0, "preview": arguments,
    }]
    assert terminal.model_dump(mode="json", exclude_unset=True) == original


@pytest.mark.parametrize("status", ["completed", "failed", "incomplete"])
def test_output_preview_records_empty_sdk_terminal_status_and_ignores_nonterminal_sse(tmp_path, monkeypatch, caplog, status):
    monkeypatch.setenv("ANIVAG_MODEL_TRACE_DIR", str(tmp_path))
    caplog.set_level(logging.INFO, logger="gateway.model_diagnostics")
    terminal = construct_type(type_=ResponseStreamEvent, value={
        "type": "response." + status, "sequence_number": 2,
        "response": {"id": "response-empty", "status": status, "output": [
            {"type": "message", "content": [{"type": "output_text", "text": ""}]},
        ]},
    })
    with model_call_context({"video_parse_task_id": "parse-empty-preview"}):
        with track_model_call(provider="ark", model="offline", operation="understand") as call:
            with diagnostics.capture_model_diagnostics(call, {}):
                directory = tmp_path / call.call_id
                diagnostics.record_model_event("sse", {"type": "response.created", "response": {"id": "response-empty"}})
                assert not (directory / "output-preview.json").exists()
                diagnostics.record_model_event("sse", terminal)
    preview = _output_preview(directory, caplog)
    assert preview["response_id"] == "response-empty" and preview["status"] == status
    assert preview["outputs"] == []


@pytest.mark.parametrize("enabled,context", [
    (False, {"video_parse_task_id": "parse-disabled-preview"}),
    (True, {"assistant_message_id": "chat-preview"}),
])
def test_disabled_and_chat_calls_do_not_emit_output_preview_or_output_log(tmp_path, monkeypatch, caplog, enabled, context):
    caplog.set_level(logging.INFO, logger="gateway.model_diagnostics")
    if enabled:
        monkeypatch.setenv("ANIVAG_MODEL_TRACE_DIR", str(tmp_path))
    else:
        monkeypatch.delenv("ANIVAG_MODEL_TRACE_DIR", raising=False)
    response = {"id": "ignored", "status": "completed", "output": [
        {"type": "message", "content": [{"type": "output_text", "text": "ignored-private-answer"}]},
    ]}
    with model_call_context(context):
        call = ModelCallTracker(provider="ark", model="offline", operation="understand")
        with diagnostics.capture_model_diagnostics(call, {}):
            diagnostics.record_model_event("response", response)
    assert not list(tmp_path.rglob("output-preview.json"))
    assert "event=model_output_trace" not in caplog.text and "ignored-private-answer" not in caplog.text


def test_output_preview_redacts_credentials_before_cutting_and_preserves_response(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("ANIVAG_MODEL_TRACE_DIR", str(tmp_path))
    caplog.set_level(logging.INFO, logger="gateway.model_diagnostics")
    prefix = "Bearer synthetic-preview-bearer\n"
    suffix = '\n{"api_key":"synthetic-preview-api-key"}'
    text = prefix + "中" * 4400 + suffix
    redacted = 'Bearer [redacted]\n' + "中" * 4400 + '\n{"api_key":"[redacted]"}'
    response = {"id": "response-redacted", "status": "completed", "output": [
        {"type": "message", "content": [{"type": "output_text", "text": text}]},
    ]}
    original = deepcopy(response)
    with model_call_context({"video_parse_task_id": "parse-redacted-preview"}):
        with track_model_call(provider="ark", model="offline", operation="understand") as call:
            with diagnostics.capture_model_diagnostics(call, {}):
                diagnostics.record_model_event("response", response)
    output = _output_preview(tmp_path / call.call_id, caplog)["outputs"][0]
    omitted = len(redacted) - 4000
    assert output["preview"] == redacted[:2000] + f"\n… [省略 {omitted} 字符] …\n" + redacted[-2000:]
    assert output["chars"] == len(redacted) and output["omitted_chars"] == omitted
    assert "synthetic-preview" not in caplog.text
    assert "synthetic-preview" not in (tmp_path / call.call_id / "output-preview.json").read_text()
    assert response == original


def test_http_failure_without_terminal_response_does_not_fabricate_output_preview(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("ANIVAG_MODEL_TRACE_DIR", str(tmp_path))
    caplog.set_level(logging.INFO, logger="gateway.model_diagnostics")
    original_error = RuntimeError("offline HTTP error")
    with model_call_context({"video_parse_task_id": "parse-http-error"}):
        with pytest.raises(RuntimeError) as caught:
            with track_model_call(provider="ark", model="offline", operation="understand") as call:
                with diagnostics.capture_model_diagnostics(call, {}):
                    raise original_error
    assert caught.value is original_error
    assert not (tmp_path / call.call_id / "output-preview.json").exists()
    assert "event=model_output_trace" not in caplog.text


@pytest.mark.parametrize("status", ["completed", "failed", "incomplete"])
def test_streamed_text_is_used_when_terminal_is_empty_and_done_does_not_duplicate_deltas(tmp_path, monkeypatch, caplog, status):
    monkeypatch.setenv("ANIVAG_MODEL_TRACE_DIR", str(tmp_path))
    caplog.set_level(logging.INFO, logger="gateway.model_diagnostics")
    parts = ["开头" * 500, "middle" * 1000, "尾巴" * 500]
    text = "".join(parts)
    identity = {"item_id": "message-stream", "output_index": 0, "content_index": 0}
    terminal = construct_type(type_=ResponseStreamEvent, value={
        "type": "response." + status, "sequence_number": 6,
        "response": {"id": "response-stream", "status": status, "output": []},
    })
    with model_call_context({"video_parse_task_id": "parse-stream-preview"}):
        with track_model_call(provider="ark", model="offline", operation="understand") as call:
            call.request_id = "request-stream"
            with diagnostics.capture_model_diagnostics(call, {}):
                directory = tmp_path / call.call_id
                for index, part in enumerate(parts):
                    diagnostics.record_model_event("sse", {
                        "type": "response.output_text.delta", "sequence_number": index, "delta": part, **identity,
                    })
                diagnostics.record_model_event("sse", {
                    "type": "response.output_text.done", "sequence_number": 4, "text": text, **identity,
                })
                assert not (directory / "output-preview.json").exists()
                assert "event=model_output_trace" not in caplog.text
                diagnostics.record_model_event("sse", terminal)
                diagnostics.record_model_event("response", terminal.response)
    preview = _output_preview(directory, caplog)
    assert preview["status"] == status and preview["response_id"] == "response-stream"
    assert len(preview["outputs"]) == 1
    output = preview["outputs"][0]
    omitted = len(text) - 4000
    assert output["type"] == "output_text" and output["item_id"] == "message-stream"
    assert output["chars"] == len(text) and output["omitted_chars"] == omitted
    assert output["preview"] == text[:2000] + f"\n… [省略 {omitted} 字符] …\n" + text[-2000:]


def test_stream_interruption_preserves_partial_output_and_original_exception(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("ANIVAG_MODEL_TRACE_DIR", str(tmp_path))
    caplog.set_level(logging.INFO, logger="gateway.model_diagnostics")
    original_error = ConnectionError("offline stream interrupted")
    text = "已输出🐱的内容"
    with model_call_context({"video_parse_task_id": "parse-interrupted-preview"}):
        with pytest.raises(ConnectionError) as caught:
            with track_model_call(provider="ark", model="offline", operation="understand") as call:
                call.request_id = "request-interrupted"
                with diagnostics.capture_model_diagnostics(call, {}):
                    diagnostics.record_model_event("sse", {
                        "type": "response.output_text.delta", "item_id": "partial-message", "output_index": 0,
                        "content_index": 0, "sequence_number": 1, "delta": text,
                    })
                    raise original_error
    assert caught.value is original_error and original_error.request_id == "request-interrupted"
    preview = _output_preview(tmp_path / call.call_id, caplog)
    assert preview["status"] == "interrupted" and preview["provider_request_id"] == "request-interrupted"
    assert len(preview["outputs"]) == 1
    output = preview["outputs"][0]
    assert output["type"] == "output_text" and output["chars"] == len(text)
    assert output["omitted_chars"] == 0 and output["preview"] == text
    assert diagnostics._capture.get() is None


def test_output_preview_supports_chat_choices_text_and_function_arguments(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("ANIVAG_MODEL_TRACE_DIR", str(tmp_path))
    caplog.set_level(logging.INFO, logger="gateway.model_diagnostics")
    response = SimpleNamespace(id="chat-response", choices=[SimpleNamespace(message=SimpleNamespace(
        content="chat completion text", tool_calls=[SimpleNamespace(
            id="chat-tool", function=SimpleNamespace(name="write_context_canvas", arguments='{"value":1}'),
        )],
    ))])
    original = deepcopy(response)
    with model_call_context({"video_parse_task_id": "parse-chat-protocol"}):
        with track_model_call(provider="openai", model="offline", operation="understand") as call:
            with diagnostics.capture_model_diagnostics(call, {}):
                diagnostics.record_model_event("response", response)
    preview = _output_preview(tmp_path / call.call_id, caplog)
    assert preview["outputs"] == [
        {"type": "output_text", "chars": 20, "omitted_chars": 0, "preview": "chat completion text"},
        {"type": "function_call", "name": "write_context_canvas", "call_id": "chat-tool",
         "chars": 11, "omitted_chars": 0, "preview": '{"value":1}'},
    ]
    assert response == original


def test_truncated_event_recording_still_preserves_terminal_output_preview(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("ANIVAG_MODEL_TRACE_DIR", str(tmp_path))
    caplog.set_level(logging.INFO, logger="gateway.model_diagnostics")
    response = {"id": "response-after-cap", "status": "completed", "output_text": "after event cap"}
    with model_call_context({"video_parse_task_id": "parse-preview-cap"}):
        with track_model_call(provider="ark", model="offline", operation="understand") as call:
            with diagnostics.capture_model_diagnostics(call, {}):
                directory = tmp_path / call.call_id
                (directory / "truncated").touch(mode=0o600)
                diagnostics.record_model_event("response", response)
    preview = _output_preview(directory, caplog)
    assert [event["event"] for event in _events(directory)] == ["request"]
    assert preview["response_id"] == "response-after-cap"
    assert preview["outputs"][0]["preview"] == "after event cap"


def test_local_schema_failure_retains_provider_completed_status_and_failed_client_status(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("ANIVAG_MODEL_TRACE_DIR", str(tmp_path))
    caplog.set_level(logging.INFO, logger="gateway.model_diagnostics")
    response = {"id": "response-invalid-schema", "status": "completed", "output_text": "invalid schema text"}
    original_error = ValueError("local schema validation failed")
    with model_call_context({"video_parse_task_id": "parse-schema-preview"}):
        with pytest.raises(ValueError) as caught:
            with track_model_call(provider="ark", model="offline", operation="understand") as call:
                call.request_id = "request-schema"
                with diagnostics.capture_model_diagnostics(call, {}):
                    diagnostics.record_model_event("response", response)
                    raise original_error
    assert caught.value is original_error and original_error.request_id == "request-schema"
    preview = _output_preview(tmp_path / call.call_id, caplog)
    assert preview["status"] == "completed" and preview["client_status"] == "failed"
    assert preview["outputs"][0]["preview"] == "invalid schema text"


def test_tool_only_stream_interruption_preserves_arguments_and_function_identity(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("ANIVAG_MODEL_TRACE_DIR", str(tmp_path))
    caplog.set_level(logging.INFO, logger="gateway.model_diagnostics")
    original_error = ConnectionError("offline tool stream interrupted")
    fragments = ['{"operations":[', '{"op":"patch_event"}']
    with model_call_context({"video_parse_task_id": "parse-tool-interrupted"}):
        with pytest.raises(ConnectionError) as caught:
            with track_model_call(provider="ark", model="offline", operation="understand") as call:
                call.request_id = "request-tool-interrupted"
                with diagnostics.capture_model_diagnostics(call, {}):
                    diagnostics.record_model_event("sse", {"type": "response.created", "response": {"id": "response-tool-interrupted"}})
                    diagnostics.record_model_event("sse", {"type": "response.output_item.added", "output_index": 0, "item": {
                        "type": "function_call", "id": "item-stream-tool", "name": "write_context_canvas",
                        "call_id": "stream-tool-call", "arguments": "",
                    }})
                    for fragment in fragments:
                        diagnostics.record_model_event("sse", {
                            "type": "response.function_call_arguments.delta", "item_id": "item-stream-tool",
                            "output_index": 0, "delta": fragment,
                        })
                    raise original_error
    assert caught.value is original_error and original_error.request_id == "request-tool-interrupted"
    preview = _output_preview(tmp_path / call.call_id, caplog)
    assert preview["status"] == "interrupted" and preview["client_status"] == "failed"
    assert preview["response_id"] == "response-tool-interrupted"
    assert preview["outputs"] == [{
        "type": "function_call", "item_id": "item-stream-tool", "name": "write_context_canvas",
        "call_id": "stream-tool-call", "source": "stream", "terminal_missing": True,
        "chars": len("".join(fragments)), "omitted_chars": 0, "preview": "".join(fragments),
    }]


@pytest.mark.parametrize("terminal_arguments,expected,source", [
    ("", '{"from":"done"}', "stream"),
    ('{"from":"terminal"}', '{"from":"terminal"}', "terminal"),
])
def test_function_argument_done_replaces_delta_text_and_terminal_arguments_take_precedence(tmp_path, monkeypatch, caplog, terminal_arguments, expected, source):
    monkeypatch.setenv("ANIVAG_MODEL_TRACE_DIR", str(tmp_path))
    caplog.set_level(logging.INFO, logger="gateway.model_diagnostics")
    with model_call_context({"video_parse_task_id": "parse-tool-done"}):
        with track_model_call(provider="ark", model="offline", operation="understand") as call:
            with diagnostics.capture_model_diagnostics(call, {}):
                identity = {"item_id": "item-done", "output_index": 0}
                diagnostics.record_model_event("sse", {"type": "response.output_item.added", "item": {
                    "type": "function_call", "id": "item-done", "name": "write_context_canvas", "call_id": "done-call",
                }})
                diagnostics.record_model_event("sse", {"type": "response.function_call_arguments.delta", "delta": '{"from":', **identity})
                diagnostics.record_model_event("sse", {"type": "response.function_call_arguments.delta", "delta": '"delta"}', **identity})
                diagnostics.record_model_event("sse", {"type": "response.function_call_arguments.done", "arguments": '{"from":"done"}', **identity})
                diagnostics.record_model_event("sse", {"type": "response.completed", "response": {
                    "id": "response-done", "status": "completed", "output": [{
                        "type": "function_call", "id": "item-done", "name": "write_context_canvas", "call_id": "done-call",
                        "arguments": terminal_arguments,
                    }],
                }})
    preview = _output_preview(tmp_path / call.call_id, caplog)
    assert len(preview["outputs"]) == 1
    output = preview["outputs"][0]
    assert output["preview"] == expected and output["chars"] == len(expected)
    assert output["source"] == source and output["call_id"] == "done-call"
