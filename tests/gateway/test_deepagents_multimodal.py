from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent.utils.context_canvas_tools import build_context_canvas_tools
from agent.video_parse_v2.timelineEvent_canvas import CanvasCompletion, ParseCanvasWrite, ParseContextCanvas
from gateway.agent_model.deepagents_multimodal import generate_with_deepagents
from gateway.agent_model.errors import ModelFailureReason, QueryModelIncompleteError
from gateway.errors import ContentFilterError
from gateway.multimodal import TextInput
from gateway.openai.vision import OpenAIVisionModel
from gateway.usage import capture_model_usage
from tests.gateway.test_multimodal_agent import ChatCompletions, Responses, _chat_response, _inputs, _model, _response


def _canvas(stage="understanding"):
    canvas = ParseContextCanvas(
        scope=f"{stage}:offline", stage=stage,
        members={"member": {
            "sequence_no": 0, "version": 1, "bounds": (0, 20000),
            "content": {"persons": [], "timeline": []},
        }},
        evidence={"member": {"asr": {"transcript": [{
            "start_seconds": 1000, "end_seconds": 2000, "text": "Hello",
        }]}}},
    )
    calls = []
    tools = build_context_canvas_tools(
        canvas, role=stage, read_collections=("members", "events", "evidence", "background"),
        write_args_schema=ParseCanvasWrite, maximum_result_bytes=8192,
        maximum_read_chars=4096, maximum_catalog_items=20, on_call=calls.append,
    )
    return canvas, [tool.as_langchain_tool() for tool in tools], calls


def _call(identity, name, arguments):
    response = _response(identity, name)
    response.output[0].arguments = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return response


def _finish(identity="complete", arguments=None):
    return _call(identity, "CanvasCompletion", {"complete": True} if arguments is None else arguments)


def _write(canvas, identity="write"):
    return _call(identity, "write_context_canvas", {
        "batch_id": identity, "expected_revision": canvas.revision,
        "operations": [{
            "op": "add_event", "member_ref": "member", "key": "scene-1", "event": {
                "event_type": "SCENE", "start_seconds": 1000, "end_seconds": 2000,
                "description": "A quiet room.", "participants": [], "visible_persons": [],
            },
        }],
    })


def _read(identity="read"):
    return _call(identity, "get_context_canvas", {
        "collection": "evidence", "reference": "member", "section": "asr",
    })


def _run(model, tools, *, inputs=None, **kwargs):
    return generate_with_deepagents(
        model, inputs or [TextInput("Interpret the supplied video evidence.")],
        schema_name="canvas_completion", result_type=CanvasCompletion, tools=tools,
        max_model_calls=kwargs.pop("max_model_calls", 16), request_timeout_seconds=None,
        check_control=kwargs.pop("check_control", None), context_checkpoint=None,
        context_window_policy=None, max_input_chars=None,
        fallback_model=kwargs.pop("fallback_model", None),
        fallback_inputs=kwargs.pop("fallback_inputs", None),
        fallback_on_error=kwargs.pop("fallback_on_error", None), **kwargs,
    )


@pytest.mark.parametrize("stage", ["understanding", "refine"])
def test_canvas_business_error_is_corrected_in_the_same_draft(stage):
    canvas, tools, calls = _canvas(stage)
    revision = canvas.revision
    invalid = _call("invalid", "write_context_canvas", {
        "batch_id": "invalid", "expected_revision": revision,
        "operations": [{"op": "set_compact", "series_compact": "forbidden", "video_compact": "forbidden"}],
    })
    responses = Responses([invalid, _write(canvas), _finish()])

    assert _run(_model("deepseek", responses), tools).complete

    assert calls == ["write_context_canvas", "write_context_canvas"]
    assert canvas.revision == revision + 1
    assert len(canvas.result("member").timeline) == 1
    error = next(item for item in responses.requests[1]["input"] if item.get("call_id") == "invalid" and item.get("type") == "function_call_output")
    assert "INVALID_OPERATIONS" in error["output"]
    assert "no operation was applied" in error["output"]


@pytest.mark.parametrize("bad_name,bad_arguments", [
    ("write_context_canvas", "not-json"),
    ("write_context_canvas", {"batch_id": "missing-operations"}),
    ("unexpected_tool", {}),
])
def test_invalid_tool_calls_return_feedback_without_aborting_the_harness(bad_name, bad_arguments):
    canvas, tools, calls = _canvas()
    revision = canvas.revision
    responses = Responses([_call("invalid", bad_name, bad_arguments), _write(canvas), _finish()])

    assert _run(_model("deepseek", responses), tools).complete

    assert calls == ["write_context_canvas"]
    assert canvas.revision == revision + 1
    feedback = next(item for item in responses.requests[1]["input"] if item.get("type") == "function_call_output" and item.get("call_id") == "invalid")
    assert "Unknown tool" in feedback["output"] or "Invalid arguments" in feedback["output"]


@pytest.mark.parametrize("final_text", ['Completed the requested updates. {"complete":true}', ""])
def test_invalid_final_text_continues_without_repeating_successful_write(final_text):
    canvas, tools, calls = _canvas()
    revision = canvas.revision
    bad_final = _response()
    bad_final.output[0].content[0].text = final_text
    responses = Responses([_write(canvas), bad_final, _finish()])

    assert _run(_model("deepseek", responses, stream=False), tools).complete

    assert calls == ["write_context_canvas"]
    assert canvas.revision == revision + 1
    history = responses.requests[2]["input"]
    assert any(item.get("type") == "function_call_output" and item.get("call_id") == "write" for item in history)
    assert "Do not repeat successful writes" in json.dumps(history)
    assert "ordinary final text is not a completion" in json.dumps(history)


@pytest.mark.parametrize("arguments", [{"complete": False}, "not-json", "[]"])
def test_completion_argument_errors_are_returned_for_correction(arguments):
    canvas, tools, calls = _canvas()
    revision = canvas.revision
    responses = Responses([_read(), _finish("invalid-complete", arguments), _finish()])

    assert _run(_model("deepseek", responses), tools).complete

    assert calls == ["get_context_canvas"]
    assert canvas.revision == revision
    feedback = next(item for item in responses.requests[2]["input"] if item.get("type") == "function_call_output" and item.get("call_id") == "invalid-complete")
    assert "Error" in feedback["output"] or "Invalid arguments" in feedback["output"]


@pytest.mark.parametrize("arguments", [{"complete": False}, "not-json", "[]"])
def test_mixed_evidence_read_and_invalid_completion_execute_read_then_recover(arguments):
    canvas, tools, calls = _canvas()
    revision = canvas.revision
    combined = _read()
    combined.output.extend(_finish("invalid-complete", arguments).output)
    responses = Responses([combined, _finish()])

    assert _run(_model("deepseek", responses), tools).complete

    assert calls == ["get_context_canvas"]
    assert canvas.revision == revision
    assert len(responses.requests) == 2
    history = responses.requests[1]["input"]
    assert any(item.get("type") == "function_call_output" and item.get("call_id") == "read" for item in history)
    feedback = next(item for item in history if item.get("type") == "function_call_output" and item.get("call_id") == "invalid-complete")
    assert "Completion deferred" in feedback["output"]


@pytest.mark.parametrize("read_in_same_turn", [False, True])
def test_multiple_completion_calls_require_a_single_corrected_completion(read_in_same_turn):
    canvas, tools, calls = _canvas()
    revision = canvas.revision
    combined = _read() if read_in_same_turn else _finish("first-complete")
    if read_in_same_turn:
        combined.output.extend(_finish("first-complete").output)
    combined.output.extend(_finish("second-complete").output)
    responses = Responses([
        *([] if read_in_same_turn else [_read()]), combined, _finish(),
    ])

    assert _run(_model("deepseek", responses), tools).complete

    assert calls == ["get_context_canvas"]
    assert canvas.revision == revision
    assert len(responses.requests) == 2 + int(not read_in_same_turn)
    history = responses.requests[-1]["input"]
    feedback = {item["call_id"]: item["output"] for item in history if item.get("type") == "function_call_output"}
    for identity in ("first-complete", "second-complete"):
        assert "multiple structured responses" in feedback[identity].lower()
        if read_in_same_turn:
            assert "Completion deferred" in feedback[identity]


def test_completion_before_any_business_tool_requires_evidence_read():
    _canvas_state, tools, calls = _canvas()
    responses = Responses([_finish("premature"), _read(), _finish()])

    assert _run(_model("deepseek", responses), tools).complete

    assert calls == ["get_context_canvas"]
    assert "Read the supplied evidence" in json.dumps(responses.requests[1]["input"])
    assert len(responses.requests) == 3


@pytest.mark.parametrize("read_first", [False, True])
@pytest.mark.parametrize("valid_write", [False, True])
def test_completion_in_same_turn_as_write_must_execute_and_review_receipt(read_first, valid_write):
    canvas, tools, calls = _canvas()
    revision = canvas.revision
    combined = _write(canvas) if valid_write else _call("invalid", "write_context_canvas", {
        "batch_id": "invalid", "expected_revision": revision,
        "operations": [{"op": "set_compact", "series_compact": "forbidden", "video_compact": "forbidden"}],
    })
    combined.output.extend(_finish("premature").output)
    responses = Responses([
        *([_read("initial-read")] if read_first else []), combined,
        _read("verify") if valid_write else _write(canvas), _finish(),
    ])

    assert _run(_model("deepseek", responses), tools).complete

    assert calls == [
        *(["get_context_canvas"] if read_first else []), "write_context_canvas",
        "get_context_canvas" if valid_write else "write_context_canvas",
    ]
    assert canvas.revision == revision + 1
    assert len(responses.requests) == 3 + int(read_first)
    if not valid_write:
        assert "INVALID_OPERATIONS" in json.dumps(responses.requests[1 + int(read_first)]["input"])


@pytest.mark.parametrize("provider", ["ark", "deepseek"])
def test_shared_harness_preserves_media_system_reasoning_and_only_bound_tools(tmp_path, provider):
    inputs, _frames = _inputs(tmp_path, provider)
    _canvas_state, tools, calls = _canvas()
    read = _read()
    read.output.insert(0, SimpleNamespace(type="reasoning", id="reasoning-1", summary=[], encrypted_content="opaque"))
    responses = Responses([read, _finish()])
    usage = []
    with capture_model_usage(usage.append):
        assert _run(_model(provider, responses), tools, inputs=inputs).complete

    assert calls == ["get_context_canvas"]
    assert len(usage) == 2
    assert all(item.provider == provider and item.status == "completed" for item in usage)
    for request in responses.requests:
        assert request["input"][0]["role"] == "system"
        anchor = next(item for item in request["input"] if item.get("role") == "user")
        expected = ["input_text", "input_video"] if provider == "ark" else ["input_text", "input_image", "input_text", "input_image"]
        assert [item["type"] for item in anchor["content"]] == expected
        assert request["tool_choice"] == "required"
        assert "text" not in request
        assert {tool["name"] for tool in request["tools"]} == {"get_context_canvas", "write_context_canvas", "CanvasCompletion"}
        completion = next(tool for tool in request["tools"] if tool["name"] == "CanvasCompletion")
        assert completion["parameters"]["properties"]["complete"]["const"] is True
    assert any(item.get("type") == "reasoning" and item.get("encrypted_content") == "opaque" for item in responses.requests[1]["input"])
    assert any(item.get("type") == "function_call_output" and item.get("call_id") == "read" for item in responses.requests[1]["input"])
    assert all(stream.closed for stream in responses.streams)


def test_content_filter_fallback_retains_tools_and_changes_active_media(tmp_path):
    inputs, frames = _inputs(tmp_path, "ark")
    _canvas_state, tools, calls = _canvas()
    primary = Responses([_read(), ContentFilterError("filtered", code="content_filter")])
    fallback = Responses([_finish()])
    assert _run(
        _model("ark", primary), tools, inputs=inputs,
        fallback_model=_model("deepseek", fallback, budget=8192, reasoning="low"),
        fallback_inputs=frames, fallback_on_error=lambda error: isinstance(error, ContentFilterError),
    ).complete

    assert calls == ["get_context_canvas"]
    request = fallback.requests[0]
    anchor = next(item for item in request["input"] if item.get("role") == "user")
    assert [item["type"] for item in anchor["content"]] == ["input_text", "input_image", "input_text", "input_image"]
    assert request["max_output_tokens"] == 8192
    assert request["reasoning"] == {"effort": "low"}
    assert any(item.get("type") == "function_call_output" and item.get("call_id") == "read" for item in request["input"])


def test_chat_vision_adapter_uses_same_completion_tools_without_json_response_format(monkeypatch, tmp_path):
    _native, frames = _inputs(tmp_path, "deepseek")
    _canvas_state, tools, calls = _canvas()
    read = _chat_response("read")
    read.choices[0].message.tool_calls[0].function.arguments = json.dumps({
        "collection": "evidence", "reference": "member", "section": "asr",
    })
    finish = _chat_response("complete", "CanvasCompletion")
    finish.choices[0].message.tool_calls[0].function.arguments = '{"complete":true}'
    responses = ChatCompletions([read, finish])
    closed = []
    monkeypatch.setattr("gateway.openai.vision.OpenAI", lambda **kwargs: SimpleNamespace(
        chat=SimpleNamespace(completions=responses), close=lambda: closed.append(True),
    ))
    model = OpenAIVisionModel(
        "portable-vision", stream=False, provider="portable", base_url="https://offline.invalid", api_key="offline",
        max_output_tokens=8192, token_parameter="max_completion_tokens", response_format="json_object",
        request_timeout_seconds=7,
    )

    assert _run(model, tools, inputs=frames).complete

    assert calls == ["get_context_canvas"] and closed
    for request in responses.requests:
        assert "response_format" not in request
        assert request["tool_choice"] == "required"
        assert request["max_completion_tokens"] == 8192
        assert request["messages"][0]["role"] == "system"
        anchor = next(item for item in request["messages"] if item.get("role") == "user")
        assert [item["type"] for item in anchor["content"]] == ["text", "image_url", "text", "image_url"]


def test_incomplete_output_is_not_executed_or_accepted_as_completion():
    canvas, tools, calls = _canvas()
    before = canvas.snapshot()
    incomplete = _write(canvas)
    incomplete.status = "incomplete"
    responses = Responses([incomplete])

    with pytest.raises(QueryModelIncompleteError) as raised:
        _run(_model("deepseek", responses), tools)

    assert raised.value.reason == ModelFailureReason.OUTPUT_TOKEN_LIMIT
    assert calls == []
    assert canvas.snapshot() == before


def test_cancellation_after_request_stops_before_canvas_execution():
    canvas, tools, calls = _canvas()
    before = canvas.snapshot()
    responses = Responses([_write(canvas)])
    fallback = Responses([_finish()])

    def check():
        if responses.requests:
            raise RuntimeError("execution cancelled")

    with pytest.raises(RuntimeError, match="execution cancelled"):
        _run(
            _model("deepseek", responses), tools, check_control=check,
            fallback_model=_model("deepseek", fallback),
        )

    assert calls == []
    assert canvas.snapshot() == before
    assert fallback.requests == []


def test_explicit_model_call_budget_stops_without_another_provider_request():
    _canvas_state, tools, calls = _canvas()
    responses = Responses([_read(), _finish()])

    with pytest.raises(QueryModelIncompleteError) as raised:
        _run(_model("deepseek", responses), tools, max_model_calls=1)

    assert raised.value.reason == ModelFailureReason.LOOP_LIMIT
    assert calls == ["get_context_canvas"]
    assert len(responses.requests) == 1


def test_separate_runs_do_not_restore_old_canvas_or_tool_history():
    responses = Responses([_read("first-read"), _finish("first-complete"), _read("second-read"), _finish("second-complete")])
    model = _model("deepseek", responses)
    first, first_tools, first_calls = _canvas()
    second, second_tools, second_calls = _canvas("refine")

    assert _run(model, first_tools).complete
    assert _run(model, second_tools).complete

    assert first_calls == second_calls == ["get_context_canvas"]
    assert first.id != second.id
    second_initial = responses.requests[2]["input"]
    assert not any(item.get("type") == "function_call_output" for item in second_initial)
    assert "first-read" not in json.dumps(second_initial)
