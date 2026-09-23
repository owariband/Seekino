import json
from types import SimpleNamespace

import pytest

from gateway.ark.sse import request_sse_json, request_sse_text
from gateway.errors import SSEProxyError


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


def test_request_sse_json_collects_only_output_text() -> None:
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

    assert request_sse_json(responses, model="test", input=[]) == {
        "summary": "ok",
        "events": [],
    }
    assert responses.calls == [{"stream": True, "model": "test", "input": []}]
    assert stream.closed is True


def test_request_sse_json_exposes_failed_response_metadata() -> None:
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
        request_sse_json(FakeResponses(stream), model="test", input=[])

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
def test_request_sse_json_rejects_non_completed_terminal_events(
    terminal_event: object,
    message: str,
) -> None:
    stream = FakeStream([terminal_event])

    with pytest.raises(SSEProxyError, match=message):
        request_sse_json(FakeResponses(stream), model="test", input=[])

    assert stream.closed is True


def test_request_sse_json_rejects_stream_without_terminal_event() -> None:
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
        request_sse_json(FakeResponses(stream), model="test", input=[])

    assert stream.closed is True


def test_request_sse_json_rejects_empty_completed_response() -> None:
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
        request_sse_json(FakeResponses(stream), model="test", input=[])

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
