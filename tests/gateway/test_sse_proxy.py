import json
from types import SimpleNamespace

import pytest
from arkruntime._models import construct_type
from arkruntime.types.responses.response_stream_event import ResponseStreamEvent

from gateway.streaming.responses import request_response_json, request_sse_response, request_sse_text
from gateway.errors import SSEProxyError
from gateway.model_diagnostics import capture_model_diagnostics
from gateway.usage import capture_model_usage, model_call_context, track_model_call


class FakeStream:
    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.closed = False

    def __iter__(self):
        return iter(self.events)

    def close(self) -> None:
        self.closed = True


class FakeResponses:
    def __init__(self, stream: FakeStream) -> None:
        self.stream = stream
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> FakeStream:
        self.calls.append(kwargs)
        return self.stream


def _sdk_completed_event(output: list[dict[str, object]] | None, **fields: object) -> object:
    return construct_type(
        type_=ResponseStreamEvent,
        value={
            "type": "response.completed", "sequence_number": 2,
            "response": {
                "id": "sdk-response", "object": "response", "created_at": 1,
                "model": "test", "status": "completed", "output": output, **fields,
            },
        },
    )


def _empty_output_summary(caplog: pytest.LogCaptureFixture) -> dict[str, object]:
    records = [record for record in caplog.records if "event=ark_sse_empty_output" in record.getMessage()]
    assert len(records) == 1
    assert records[0].exc_info is None
    assert "\n" not in records[0].getMessage()
    return json.loads(records[0].getMessage().split("summary=", 1)[1])


def test_parse_diagnostic_records_sdk_empty_terminal_and_worker_request_id(tmp_path, monkeypatch):
    monkeypatch.setenv("SEEKINO_MODEL_TRACE_DIR", str(tmp_path))
    event = _sdk_completed_event([
        {"id": "m", "type": "message", "role": "assistant", "status": "completed",
         "content": [{"type": "output_text", "text": "", "annotations": []}]},
    ])
    stream = FakeStream([event])
    stream.response = SimpleNamespace(headers={"x-request-id": "upstream-empty-request"})
    request = {"model": "test", "input": [], "tool_choice": "required"}
    with model_call_context({"video_parse_task_id": "parse-empty"}):
        with pytest.raises(SSEProxyError, match="no output text") as caught:
            with track_model_call(provider="ark", model="test", operation="understand") as call:
                with capture_model_diagnostics(call, request):
                    request_sse_response(FakeResponses(stream), usage_call=call, **request)
    events = [json.loads(line) for line in (tmp_path / call.call_id / "events.jsonl").read_text().splitlines()]
    assert [item["event"] for item in events] == ["request", "sse", "error"]
    terminal = events[1]["payload"]["response"]
    assert terminal["status"] == "completed"
    assert terminal["output"][0]["content"][0]["text"] == ""
    assert "tools" not in terminal  # SDK defaults must not appear as wire-supplied nulls.
    assert events[2]["payload"]["chain"][0]["response_id"] == "sdk-response"
    assert events[2]["provider_request_id"] == caught.value.request_id == "upstream-empty-request"
    assert stream.closed


def test_parse_asr_sse_diagnostic_captures_request_and_response_once(tmp_path, monkeypatch):
    monkeypatch.setenv("SEEKINO_MODEL_TRACE_DIR", str(tmp_path))
    event = _sdk_completed_event([
        {"id": "m", "type": "message", "role": "assistant", "status": "completed",
         "content": [{"type": "output_text", "text": '{"transcript":"hello"}', "annotations": []}]},
    ])
    stream = FakeStream([event])
    usage = []
    with model_call_context({"video_parse_task_id": "parse-asr", "stage": "asr"}), capture_model_usage(usage.append):
        result = request_response_json(FakeResponses(stream), model="test", input=[{"role": "user", "content": "ASR prompt"}])
    assert result == {"transcript": "hello"} and stream.closed
    events = [json.loads(line) for line in (tmp_path / usage[0].call_id / "events.jsonl").read_text().splitlines()]
    assert [item["event"] for item in events] == ["request", "sse", "completed"]
    assert events[0]["payload"]["request"]["stream"] is True
    assert events[0]["payload"]["request"]["input"][0]["content"] == "ASR prompt"
    assert events[0]["payload"]["context"]["stage"] == "asr"


def test_empty_output_log_preserves_real_sdk_structure_and_call_context(caplog) -> None:
    event = _sdk_completed_event(
        [{"id": "m", "type": "message", "role": "assistant", "status": "completed",
          "content": [{"type": "output_text", "text": "", "annotations": []}]}],
        tools=[{"type": "function", "name": "private-tool", "parameters": {"private": "hidden"}}],
        tool_choice="required", text={"format": {"type": "json_schema", "name": "private-schema", "strict": True}},
    )
    stream = FakeStream([event])
    context = {"video_parse_task_id": "task\n1", "run_id": "run-1", "sequence_no": 4,
               "lease_version": 3, "prompt": "private-context-must-not-log"}
    with model_call_context(context), track_model_call(provider="ark", model="test", operation="understand") as call:
        call.request_id = "provider-request"
        with pytest.raises(SSEProxyError, match="no output text") as caught:
            request_sse_response(FakeResponses(stream), usage_call=call, model="test", input=["private-input"])
    summary = _empty_output_summary(caplog)
    assert summary["call_id"] == call.call_id
    assert summary["video_parse_task_id"] == "task\n1"
    assert summary["run_id"] == "run-1" and summary["sequence_no"] == 4 and summary["lease_version"] == 3
    assert summary["provider_request_id"] == "provider-request"
    assert summary["provider"] == "ark" and summary["model"] == "test" and summary["operation"] == "understand"
    assert summary["response_id"] == caught.value.response_id == "sdk-response"
    assert summary["terminal_status"] == "completed" and summary["output_count"] == 1
    assert summary["item_classes"] == {"ItemOutputMessage": 1}
    assert summary["content_classes"] == {"OutputContentItemText": 1}
    assert summary["item_types"] == {"message": 1} and summary["content_types"] == {"output_text": 1}
    assert summary["text_field_count"] == summary["empty_text_field_count"] == 1
    assert summary["terminal_text_chars"] == summary["streamed_text_chars"] == summary["text_chars"] == 0
    assert summary["function_count"] == 0 and summary["unknown_dict_structures"] == 0
    assert summary["terminal_tool_count"] == 1 and summary["terminal_tool_choice"] == "required"
    assert summary["terminal_format_type"] == "json_schema" and summary["terminal_format_strict"] is True
    assert summary["output_rejected"] is True and summary["refusal_present"] is False
    assert not summary["summary_truncated"] and stream.closed
    assert "private-" not in caplog.text and "hidden" not in caplog.text


def test_empty_output_log_distinguishes_intermediate_tool_from_empty_terminal(caplog) -> None:
    item = {"type": "function_call", "id": "f", "call_id": "c", "name": "private-tool",
            "arguments": "private-arguments", "status": "completed"}
    events = [construct_type(type_=ResponseStreamEvent, value=wire) for wire in [
        {"type": "response.output_item.done", "sequence_number": 0, "output_index": 0, "item": item},
        {"type": "response.function_call_arguments.done", "sequence_number": 1,
         "item_id": "f", "output_index": 0, "arguments": "private-arguments"},
    ]]
    stream = FakeStream([*events, _sdk_completed_event(None)])
    with track_model_call(provider="ark", model="test", operation="understand") as call:
        with pytest.raises(SSEProxyError, match="no output text"):
            request_sse_response(FakeResponses(stream), usage_call=call, model="test", input=[])
    summary = _empty_output_summary(caplog)
    assert summary["event_types"] == {"response.output_item.done": 1,
                                      "response.function_call_arguments.done": 1, "response.completed": 1}
    assert summary["stream_item_types"] == {"function_call": 1}
    assert summary["output_item_done_count"] == summary["function_args_done_count"] == 1
    assert summary["function_args_delta_count"] == summary["output_count"] == summary["function_count"] == 0
    assert summary["output_container_type"] == "NoneType" and stream.closed
    assert summary["terminal_tool_count"] is summary["terminal_tool_choice"] is None
    assert summary["terminal_format_type"] is summary["terminal_format_strict"] is None
    assert "private-" not in caplog.text


def test_empty_output_log_bounds_unknown_dictionary_shapes_and_excludes_payloads(caplog) -> None:
    item = {"type": "private-type", "content": [
        {"type": "private-content", "text": "private-text", "body": "private-body"},
        {"type": "refusal", "refusal": "private-refusal"},
    ] * 20, "arguments": "private-arguments", "headers": {"Authorization": "private-token"}}
    response = SimpleNamespace(id="bounded", status="completed", output=[item] * 40,
                               tool_choice={"name": "private-choice"},
                               text={"format": {"type": "private-format", "strict": "private-strict"}})
    stream = FakeStream([SimpleNamespace(type="private-event", sequence_number=0),
                         SimpleNamespace(type="response.completed", sequence_number=1, response=response)])
    with pytest.raises(SSEProxyError, match="no output text"):
        request_response_json(FakeResponses(stream), model="test", input=[])
    summary = _empty_output_summary(caplog)
    assert summary["event_types"] == {"other": 1, "response.completed": 1}
    assert summary["output_count"] == 40 and summary["item_types"] == {"other": 32}
    assert summary["content_types"] == {"other": 512, "refusal": 512}
    assert summary["unknown_dict_structures"] == 32 * 33
    assert summary["summary_truncated"] is True and summary["refusal_present"] is True
    assert summary["terminal_tool_choice"] == summary["terminal_format_type"] == "other"
    assert summary["terminal_format_strict"] is None and stream.closed
    assert len(caplog.text) < 3000 and "private-" not in caplog.text


@pytest.mark.parametrize("status", ["completed", "private-status"])
def test_empty_output_log_handles_dictionary_terminal_and_non_string_request_id(status, caplog) -> None:
    response = {"status": status, "output": [{"type": "message", "content": [
        {"type": "output_text", "text": "private-unrecognized-text"}]}]}
    stream = FakeStream([SimpleNamespace(type="response.completed", sequence_number=0, response=response)])
    with track_model_call(provider="ark", model="test", operation="understand") as call:
        call.request_id = 123
        with pytest.raises(SSEProxyError, match="no output text"):
            request_sse_response(FakeResponses(stream), usage_call=call, model="test", input=[])
    summary = _empty_output_summary(caplog)
    assert summary["provider_request_id"] is None and summary["response_id"] is None
    assert summary["terminal_status"] == ("completed" if status == "completed" else "other")
    assert summary["output_count"] == 1
    assert summary["unknown_dict_structures"] == 3 and summary["text_field_count"] == 1
    assert summary["terminal_text_chars"] == 0 and summary["empty_text_field_count"] == 0
    assert stream.closed and "private-" not in caplog.text


@pytest.mark.parametrize("output", [
    [{"id": "m", "type": "message", "role": "assistant", "status": "completed",
      "content": [{"type": "output_text", "text": "ok", "annotations": []}]}],
    [{"type": "function_call", "id": "f", "call_id": "c", "name": "read", "arguments": "{}"}],
])
def test_real_sdk_nonempty_or_tool_terminal_response_has_no_empty_output_log(output, caplog) -> None:
    event = _sdk_completed_event(output)
    stream = FakeStream([event])
    with track_model_call(provider="ark", model="test", operation="read") as call:
        terminal, text = request_sse_response(FakeResponses(stream), usage_call=call, model="test", input=[])
    assert terminal is event.response and text == ("ok" if output[0]["type"] == "message" else "")
    assert stream.closed and "ark_sse_empty_output" not in caplog.text


def test_tool_only_terminal_response_keeps_usage_and_closes_stream() -> None:
    response = SimpleNamespace(
        id="tool-response", status="completed",
        output=[SimpleNamespace(type="function_call", call_id="read-1", name="read", arguments="{}")],
        usage=SimpleNamespace(input_tokens=80, output_tokens=12, total_tokens=92),
    )
    stream = FakeStream([SimpleNamespace(type="response.completed", sequence_number=0, response=response)])
    calls = []
    with capture_model_usage(calls.append), track_model_call(provider="ark", model="test", operation="read") as call:
        terminal, text = request_sse_response(FakeResponses(stream), usage_call=call, model="test", input=[])
    assert terminal is response and text == ""
    assert stream.closed
    assert len(calls) == 1 and calls[0].usage.total_tokens == 92
    with pytest.raises(SSEProxyError, match="no output text"):
        request_response_json(FakeResponses(FakeStream(stream.events)), model="test", input=[])


def _output_response(response_id: str, text: str) -> object:
    return SimpleNamespace(
        id=response_id,
        output=[
            SimpleNamespace(
                type="message",
                content=[SimpleNamespace(type="output_text", text=text)],
            )
        ],
    )


def test_request_response_json_collects_only_output_text() -> None:
    text = json.dumps({"summary": "ok", "events": []})
    response = _output_response("response-1", text)
    stream = FakeStream(
        [
            SimpleNamespace(
                type="response.created",
                sequence_number=0,
                response=response,
            ),
            SimpleNamespace(
                type="response.reasoning_summary_text.delta",
                sequence_number=1,
                delta="must not be included",
            ),
            SimpleNamespace(
                type="response.output_text.delta",
                sequence_number=2,
                output_index=0,
                item_id="message-1",
                content_index=0,
                delta=text[:10],
            ),
            SimpleNamespace(
                type="response.output_text.delta",
                sequence_number=3,
                output_index=0,
                item_id="message-1",
                content_index=0,
                delta=text[10:],
            ),
            SimpleNamespace(
                type="response.output_text.done",
                sequence_number=4,
                output_index=0,
                item_id="message-1",
                content_index=0,
                text=text,
            ),
            SimpleNamespace(
                type="response.completed",
                sequence_number=5,
                response=response,
            ),
        ]
    )
    responses = FakeResponses(stream)

    assert request_response_json(responses, model="test", input=[]) == {
        "summary": "ok",
        "events": [],
    }
    assert responses.calls == [{"stream": True, "model": "test", "input": []}]
    assert stream.closed is True


def test_request_response_json_exposes_failed_response_metadata() -> None:
    stream = FakeStream(
        [
            SimpleNamespace(
                type="response.created",
                sequence_number=0,
                response=SimpleNamespace(id="response-2"),
            ),
            SimpleNamespace(
                type="response.output_text.delta",
                sequence_number=1,
                output_index=0,
                item_id="message-1",
                content_index=0,
                delta="partial",
            ),
            SimpleNamespace(
                type="response.failed",
                sequence_number=2,
                response=SimpleNamespace(
                    id="response-2",
                    error=SimpleNamespace(
                        code="OutputTextSensitiveContentDetected",
                        message="output rejected",
                    ),
                ),
            ),
        ]
    )

    with pytest.raises(SSEProxyError, match="output rejected") as caught:
        request_response_json(FakeResponses(stream), model="test", input=[])

    assert caught.value.code == "OutputTextSensitiveContentDetected"
    assert caught.value.response_id == "response-2"
    assert caught.value.partial_output_chars == 7
    assert stream.closed is True


@pytest.mark.parametrize(
    ("terminal_event", "message"),
    [
        (
            SimpleNamespace(
                type="response.incomplete",
                sequence_number=0,
                response=SimpleNamespace(
                    id="response-3",
                    incomplete_details=SimpleNamespace(
                        reason="content_filter"
                    ),
                ),
            ),
            "content_filter",
        ),
        (
            SimpleNamespace(
                type="error",
                sequence_number=0,
                code="InternalError",
                message="stream error",
            ),
            "stream error",
        ),
    ],
)
def test_request_response_json_rejects_non_completed_terminal_events(
    terminal_event: object,
    message: str,
) -> None:
    stream = FakeStream([terminal_event])

    with pytest.raises(SSEProxyError, match=message):
        request_response_json(FakeResponses(stream), model="test", input=[])

    assert stream.closed is True


def test_request_response_json_rejects_stream_without_terminal_event() -> None:
    stream = FakeStream(
        [
            SimpleNamespace(
                type="response.in_progress",
                sequence_number=0,
                response=SimpleNamespace(id="response-4"),
            )
        ]
    )

    with pytest.raises(SSEProxyError, match="without a terminal event"):
        request_response_json(FakeResponses(stream), model="test", input=[])

    assert stream.closed is True


def test_request_response_json_rejects_empty_completed_response() -> None:
    response = SimpleNamespace(id="response-5", output=[])
    stream = FakeStream(
        [
            SimpleNamespace(
                type="response.completed",
                sequence_number=0,
                response=response,
            )
        ]
    )

    with pytest.raises(SSEProxyError, match="no output text"):
        request_response_json(FakeResponses(stream), model="test", input=[])

    assert stream.closed is True


def _delta(text: str, sequence: int) -> object:
    return SimpleNamespace(
        type="response.output_text.delta",
        sequence_number=sequence,
        output_index=0,
        item_id="message-1",
        content_index=0,
        delta=text,
    )


def test_text_stream_exposes_first_original_delta_before_provider_finishes() -> None:
    class PacedStream:
        finished = False
        closed = False

        def __iter__(self):
            yield _delta("第一段", 0)
            yield _delta("第二段", 1)
            self.finished = True
            yield SimpleNamespace(
                type="response.completed",
                sequence_number=2,
                response=_output_response("response-live", "第一段第二段"),
            )

        def close(self):
            self.closed = True

    provider = PacedStream()
    deltas = request_sse_text(FakeResponses(provider), model="test", input=[])
    assert next(deltas) == "第一段"
    assert provider.finished is False
    assert list(deltas) == ["第二段"]
    assert provider.finished is True
    assert provider.closed is True


def test_text_stream_consumer_close_closes_provider() -> None:
    stream = FakeStream([_delta("partial", 0)])
    deltas = request_sse_text(FakeResponses(stream), model="test", input=[])
    assert next(deltas) == "partial"
    deltas.close()
    assert stream.closed is True


@pytest.mark.parametrize(
    ("events", "message"),
    [
        ([_delta("partial", 0)], "without a terminal event"),
        ([_delta("one", 1), _delta("two", 1)], "strictly increasing"),
        (
            [
                _delta("partial", 0),
                SimpleNamespace(
                    type="response.output_text.done",
                    sequence_number=1,
                    output_index=0,
                    item_id="message-1",
                    content_index=0,
                    text="different",
                ),
                SimpleNamespace(
                    type="response.completed",
                    sequence_number=2,
                    response=_output_response("response-test", "different"),
                ),
            ],
            "differs from output_text.done",
        ),
        (
            [
                _delta("partial", 0),
                SimpleNamespace(
                    type="response.completed",
                    sequence_number=1,
                    response=_output_response("response-test", "different"),
                ),
            ],
            "differs from the completed response",
        ),
        (
            [
                _delta("partial", 0),
                SimpleNamespace(
                    type="response.incomplete",
                    sequence_number=1,
                    response=SimpleNamespace(
                        id="response-test",
                        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
                    ),
                ),
            ],
            "max_output_tokens",
        ),
    ],
)
def test_text_stream_rejects_failed_completion_after_partial_delta(events, message):
    provider = FakeStream(events)
    with pytest.raises(SSEProxyError, match=message):
        list(request_sse_text(FakeResponses(provider), model="test", input=[]))
    assert provider.closed is True


def test_text_stream_does_not_synthesize_deltas_from_completed_response() -> None:
    provider = FakeStream(
        [
            SimpleNamespace(
                type="response.completed",
                sequence_number=0,
                response=_output_response("response-test", "full answer"),
            )
        ]
    )
    with pytest.raises(SSEProxyError, match="no text deltas"):
        list(request_sse_text(FakeResponses(provider), model="test", input=[]))
    assert provider.closed is True
