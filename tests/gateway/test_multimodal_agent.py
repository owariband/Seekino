from __future__ import annotations

from copy import deepcopy
import json
import logging
from types import SimpleNamespace
from time import monotonic

import pytest
import httpx
from openai import APIStatusError, RateLimitError
from pydantic import BaseModel

from agent.utils.tool import build_agent_tool
from agent.utils.tool_result import JsonToolResult
from gateway.agent_model.context import ContextWindowPolicy
from gateway.agent_model.errors import ModelFailureReason, ModelInputTooLargeError, QueryModelIncompleteError
from gateway.agent_model.tool_loop import _failure_reason_for_exception
from gateway.ark.multimodal import ArkMultimodalModel
from gateway.errors import ContentFilterError
from gateway.media import ToolImageArtifact
from gateway.multimodal import ImageInput, TextInput, VideoInput
from gateway.openai.multimodal import DeepSeekMultimodalModel
from gateway.openai.vision import OpenAIVisionModel
from gateway.usage import ModelCallCancelledError, capture_model_usage, control_model_calls, model_call_context


class Completion(BaseModel):
    done: bool


class Arguments(BaseModel):
    value: str = ""


class Stream:
    def __init__(self, response):
        self.response_value = response
        self.closed = False

    def __iter__(self):
        event_type = "response.incomplete" if self.response_value.status == "incomplete" else "response.completed"
        yield SimpleNamespace(type=event_type, sequence_number=0, response=self.response_value)

    def close(self):
        self.closed = True


class Responses:
    def __init__(self, values):
        self.values = iter(values)
        self.requests = []
        self.streams = []

    def create(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        if kwargs.get("stream"):
            stream = Stream(value)
            self.streams.append(stream)
            return stream
        return value


def _response(call_id=None, name="get_context_canvas", *, status="completed", input_tokens=100):
    output = ([SimpleNamespace(
        type="function_call", id=f"item-{call_id}", call_id=call_id,
        name=name, arguments=json.dumps({"value": "observed"}), status="completed",
    )] if call_id else [SimpleNamespace(
        type="message", role="assistant", content=[SimpleNamespace(type="output_text", text='{"done":true}')],
    )])
    return SimpleNamespace(
        id=f"response-{call_id or 'final'}", status=status, output=output,
        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=20, total_tokens=input_tokens + 20),
    )


def _model(provider, responses, *, budget=65536, reasoning="high", timeout=30):
    client = SimpleNamespace(responses=responses)
    if provider == "ark":
        return ArkMultimodalModel(
            "ark-vision", max_output_tokens=budget, reasoning_effort=reasoning,
            request_timeout_seconds=timeout, client=client,
        )
    return DeepSeekMultimodalModel(
        "deepseek-vision", base_url="https://example.invalid", api_key="offline",
        max_output_tokens=budget, reasoning_effort=reasoning,
        request_timeout_seconds=timeout, client=client,
    )


def _inputs(tmp_path, provider):
    frame1 = tmp_path / "first.png"
    frame2 = tmp_path / "second.png"
    video = tmp_path / "source.mp4"
    frame1.write_bytes(b"\x89PNG\r\n\x1a\nfirst")
    frame2.write_bytes(b"\x89PNG\r\n\x1a\nsecond")
    video.write_bytes(b"raw-video" * 10000)
    frames = [TextInput("frame at 1000 ms"), ImageInput(frame1), TextInput("frame at 2000 ms"), ImageInput(frame2)]
    return ([TextInput("Analyze the native video."), VideoInput(video, fps=0.5)]
            if provider == "ark" else frames), frames


def _tools(canvas, calls, *, large_content=None):
    def read(value):
        calls.append(("get", value))
        return JsonToolResult(success=True, payload={"note": large_content or canvas.get("note", "")})

    def write(value):
        calls.append(("write", value))
        canvas["note"] = value
        return JsonToolResult(success=True, payload={"written": True})

    return [build_agent_tool(
        func=func, name=name, description="Read or write the shared analysis state.",
        args_schema=Arguments, maximum_result_bytes=40000,
    ).as_langchain_tool() for name, func in (("get_context_canvas", read), ("write_context_canvas", write))]


@pytest.mark.parametrize("provider", ["ark", "deepseek"])
@pytest.mark.parametrize("reasoning", ["none", "high"])
def test_multimodal_tools_preserve_native_inputs_controls_and_actual_usage(tmp_path, provider, reasoning):
    inputs, _frames = _inputs(tmp_path, provider)
    reasoning_item = SimpleNamespace(type="reasoning", id="reasoning-1", summary=[], encrypted_content="opaque")
    first_response = _response("read")
    first_response.output.insert(0, reasoning_item)
    responses = Responses([first_response, _response("write", "write_context_canvas"), _response()])
    model = _model(provider, responses, reasoning=reasoning)
    state, calls, usage = {}, [], []
    with capture_model_usage(usage.append):
        result = model.generate(
            inputs, schema_name="canvas_completion", result_type=Completion,
            tools=_tools(state, calls), max_model_calls=4, max_input_chars=3000,
            context_window_policy=ContextWindowPolicy(128000, 65536, 2048, 50000, 40000),
        )
    assert result.done and state == {"note": "observed"}
    assert calls == [("get", "observed"), ("write", "observed")]
    assert len(usage) == 3
    assert all(item.provider == provider and item.status == "completed" for item in usage)
    assert [item.usage.input_tokens for item in usage] == [100, 100, 100]
    first = responses.requests[0]
    assert first["max_output_tokens"] == 65536
    assert 0 < first["timeout"] <= 30
    assert first["tool_choice"] == "required"
    if provider == "ark":
        assert first["input"][0]["content"][1]["type"] == "input_video"
        assert first["input"][0]["content"][1]["fps"] == 0.5
        assert all(stream.closed for stream in responses.streams)
    else:
        assert [item["type"] for item in first["input"][0]["content"]] == ["input_text", "input_image", "input_text", "input_image"]
    if provider == "ark" and reasoning == "none":
        assert first["thinking"] == {"type": "disabled"}
    else:
        assert first["reasoning"] == {"effort": reasoning}
    second = responses.requests[1]["input"]
    assert any(item.get("type") == "reasoning" and item["encrypted_content"] == "opaque" for item in second)
    assert any(item.get("type") == "function_call" and item["call_id"] == "read" for item in second)
    assert any(item.get("type") == "function_call_output" and item["call_id"] == "read" for item in second)


@pytest.mark.parametrize("provider", ["ark", "deepseek"])
def test_fallback_keeps_completed_tools_and_uses_its_own_media_and_controls(tmp_path, provider):
    inputs, frames = _inputs(tmp_path, provider)
    primary_responses = Responses([_response("read"), _response(status="incomplete", input_tokens=120)])
    fallback_provider = "deepseek" if provider == "ark" else "ark"
    fallback_responses = Responses([_response("write", "write_context_canvas"), _response()])
    primary = _model(provider, primary_responses)
    fallback = _model(fallback_provider, fallback_responses, budget=8192, reasoning="low", timeout=7)
    state, calls, usage = {}, [], []
    with capture_model_usage(usage.append):
        assert primary.generate(
            inputs, schema_name="canvas_completion", result_type=Completion,
            tools=_tools(state, calls), max_model_calls=4,
            fallback_model=fallback, fallback_inputs=frames,
        ).done
    assert calls == [("get", "observed"), ("write", "observed")]
    assert state == {"note": "observed"}
    assert len(usage) == 4
    assert [item.provider for item in usage] == [provider, provider, fallback_provider, fallback_provider]
    assert [item.usage.input_tokens for item in usage] == [100, 120, 100, 100]
    assert usage[1].status == "failed"
    for request in fallback_responses.requests:
        assert request["max_output_tokens"] == 8192
        assert request["reasoning"] == {"effort": "low"}
        assert 0 < request["timeout"] <= 7
        assert [item["type"] for item in request["input"][0]["content"]] == ["input_text", "input_image", "input_text", "input_image"]
        assert any(item.get("type") == "function_call_output" and item["call_id"] == "read" for item in request["input"])


@pytest.mark.parametrize("provider", ["ark", "deepseek"])
@pytest.mark.parametrize("failure", [RuntimeError, TimeoutError])
def test_control_stop_after_request_prevents_tool_execution_and_fallback(tmp_path, provider, failure):
    inputs, _frames = _inputs(tmp_path, provider)
    responses = Responses([_response("read")])
    fallback_responses = Responses([_response()])
    calls = []

    def check():
        if responses.requests:
            raise failure("execution cancelled")

    with pytest.raises(failure, match="execution cancelled"):
        _model(provider, responses).generate(
            inputs, schema_name="completion", result_type=Completion,
            tools=_tools({}, calls), max_model_calls=3, check_control=check,
            fallback_model=_model("ark", fallback_responses),
        )
    assert calls == []
    assert fallback_responses.requests == []
    assert all(stream.closed for stream in responses.streams)


def test_context_compaction_preserves_native_video_and_canonical_tool_state(tmp_path):
    inputs, _frames = _inputs(tmp_path, "ark")
    responses = Responses([_response("read-1"), _response("read-2"), _response("read-3"), _response()])
    state, calls, checkpoints = {"note": "canonical"}, [], []

    def checkpoint():
        checkpoints.append(dict(state))
        return {"canonical_note": state["note"]}

    assert _model("ark", responses, budget=1024).generate(
        inputs, schema_name="completion", result_type=Completion,
        tools=_tools(state, calls, large_content="evidence" * 4000), max_model_calls=5,
        context_checkpoint=checkpoint,
        context_window_policy=ContextWindowPolicy(10000, 1024, 512, 6000, 5000),
    ).done
    assert len(calls) == 3 and state == {"note": "canonical"}
    assert checkpoints
    assert any("tool_result_released" in json.dumps(request["input"]) for request in responses.requests)
    assert all(request["input"][0]["content"][1]["type"] == "input_video" for request in responses.requests)


def test_preflight_rejects_text_budget_before_any_provider_request():
    responses = Responses([_response("read")])
    with pytest.raises(ModelInputTooLargeError):
        _model("deepseek", responses).generate(
            [TextInput("text" * 1000)], schema_name="completion", result_type=Completion,
            tools=_tools({}, []), max_input_chars=100,
        )
    assert responses.requests == []


def test_parse_content_filter_fallback_preserves_prior_tool_history(tmp_path):
    inputs, frames = _inputs(tmp_path, "ark")
    primary = Responses([_response("read"), ContentFilterError("filtered", code="content_filter")])
    fallback = Responses([_response("write", "write_context_canvas"), _response()])
    state, calls = {}, []
    assert _model("ark", primary).generate(
        inputs, schema_name="completion", result_type=Completion, tools=_tools(state, calls),
        fallback_model=_model("deepseek", fallback), fallback_inputs=frames,
        fallback_on_error=lambda error: isinstance(error, ContentFilterError),
    ).done
    assert calls == [("get", "observed"), ("write", "observed")]


@pytest.mark.parametrize("provider", ["ark", "deepseek"])
def test_parse_filter_only_fallback_does_not_retry_output_limit(tmp_path, provider):
    inputs, frames = _inputs(tmp_path, provider)
    primary = Responses([_response("read"), _response(status="incomplete")])
    fallback = Responses([_response()])
    with pytest.raises(QueryModelIncompleteError) as captured:
        _model(provider, primary).generate(
            inputs, schema_name="completion", result_type=Completion, tools=_tools({}, []),
            fallback_model=_model("ark", fallback), fallback_inputs=frames,
            fallback_on_error=lambda error: isinstance(error, ContentFilterError),
        )
    assert captured.value.reason is ModelFailureReason.OUTPUT_TOKEN_LIMIT
    assert fallback.requests == []


@pytest.mark.parametrize("provider", ["ark", "deepseek"])
def test_model_control_cancels_actual_transport_without_executing_tools(tmp_path, provider):
    inputs, _frames = _inputs(tmp_path, provider)
    admitted, released, closed, calls, usage = [], [], [], [], []

    class Control:
        def acquire(self, call):
            admitted.append(call)

        def release(self, call):
            released.append(call.call_id)

    class CancellingResponses(Responses):
        def create(self, **kwargs):
            value = super().create(**kwargs)
            admitted[-1].cancel()
            return value

    responses = CancellingResponses([_response("read")])
    model = _model(provider, responses)
    model._client.close = lambda: closed.append(True)
    with control_model_calls(Control()), capture_model_usage(usage.append), pytest.raises(ModelCallCancelledError):
        model.generate(inputs, schema_name="completion", result_type=Completion, tools=_tools({}, calls))
    assert calls == [] and closed
    assert released == [admitted[0].call_id]
    assert len(usage) == 1 and usage[0].status == "failed"
    assert all(stream.closed for stream in responses.streams)


def test_fallback_media_is_checked_against_the_active_context_budget(tmp_path):
    inputs, frames = _inputs(tmp_path, "ark")
    primary = Responses([ContentFilterError("filtered", code="content_filter")])
    fallback = Responses([_response()])
    with pytest.raises(ModelInputTooLargeError, match="fallback"):
        _model("ark", primary, budget=1024).generate(
            inputs, schema_name="completion", result_type=Completion, tools=_tools({}, []),
            fallback_model=_model("deepseek", fallback, budget=1024), fallback_inputs=frames * 5,
            context_window_policy=ContextWindowPolicy(10000, 1024, 512, 6000, 5000),
        )
    assert len(primary.requests) == 1 and fallback.requests == []


def test_output_reserve_covers_the_larger_fallback_budget_before_request():
    primary, fallback = Responses([_response("read")]), Responses([_response()])
    with pytest.raises(ValueError, match="output reserve"):
        _model("deepseek", primary, budget=1024).generate(
            [TextInput("Complete the analysis.")], schema_name="completion", result_type=Completion,
            tools=_tools({}, []), fallback_model=_model("ark", fallback, budget=8192),
            context_window_policy=ContextWindowPolicy(20000, 1024, 512, 15000, 10000),
        )
    assert primary.requests == fallback.requests == []


@pytest.mark.parametrize("provider", ["ark", "deepseek"])
def test_high_output_budget_is_preserved_without_a_token_policy(tmp_path, provider):
    inputs, _frames = _inputs(tmp_path, provider)
    responses = Responses([_response("write", "write_context_canvas"), _response()])
    state, calls = {}, []
    assert _model(provider, responses, budget=131072).generate(
        inputs, schema_name="completion", result_type=Completion, tools=_tools(state, calls),
        max_model_calls=2, context_window_policy=None, max_input_chars=400000,
    ).done
    assert state == {"note": "observed"} and calls == [("write", "observed")]
    assert all(request["max_output_tokens"] == 131072 for request in responses.requests)


@pytest.mark.parametrize("provider", ["ark", "deepseek"])
def test_tool_image_artifact_reaches_responses_after_its_tool_output(tmp_path, provider):
    _inputs(tmp_path, provider)
    artifact = ToolImageArtifact.from_paths([(tmp_path / "first.png", "Frame at 1000 ms")])
    tool = build_agent_tool(
        func=lambda value: JsonToolResult(success=True, payload={"note": "visible evidence"}, artifact=artifact),
        name="get_context_canvas", description="Read actual frame evidence.",
        args_schema=Arguments, maximum_result_bytes=1024,
    ).as_langchain_tool()
    responses = Responses([_response("read"), _response()])
    assert _model(provider, responses).generate(
        [TextInput("Explain the evidence.")], schema_name="completion", result_type=Completion,
        tools=[tool], max_input_chars=3000,
    ).done
    inputs = responses.requests[1]["input"]
    assert inputs[-2]["type"] == "function_call_output"
    assert "visible evidence" in inputs[-2]["output"]
    assert inputs[-1]["role"] == "user"
    blocks = inputs[-1]["content"]
    assert blocks[-2] == {"type": "input_text", "text": "Frame at 1000 ms"}
    assert blocks[-1]["type"] == "input_image"
    assert blocks[-1]["image_url"].startswith("data:image/png;base64,")


@pytest.mark.parametrize("provider", ["ark", "deepseek"])
def test_incomplete_function_batch_executes_no_tools(tmp_path, provider):
    inputs, _frames = _inputs(tmp_path, provider)
    response = _response("read")
    partial = _response("write", "write_context_canvas").output[0]
    partial.status = "in_progress"
    response.output.append(partial)
    calls = []
    with pytest.raises(QueryModelIncompleteError, match="function call did not complete"):
        _model(provider, Responses([response])).generate(
            inputs, schema_name="completion", result_type=Completion, tools=_tools({}, calls),
        )
    assert calls == []


class ChatCompletions(Responses):
    def create(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        return value


def _chat_response(call_id=None, name="get_context_canvas"):
    return SimpleNamespace(
        id="chat-result", usage=SimpleNamespace(prompt_tokens=130, completion_tokens=20, total_tokens=150),
        choices=[SimpleNamespace(
            finish_reason="tool_calls" if call_id else "stop",
            message=SimpleNamespace(
                role="assistant", content=None if call_id else '{"done":true}',
                tool_calls=[SimpleNamespace(id=call_id, type="function", function=SimpleNamespace(
                    name=name, arguments='{"value":"observed"}',
                ))] if call_id else [],
            ),
        )],
    )


@pytest.mark.parametrize("provider", ["ark", "deepseek"])
@pytest.mark.parametrize("chat_primary", [True, False])
def test_responses_and_chat_fallback_share_history_and_endpoint_settings(
    monkeypatch, tmp_path, provider, chat_primary,
):
    native_inputs, frames = _inputs(tmp_path, provider)
    primary_values = ["read", ContentFilterError("filtered", code="content_filter")]
    fallback_values = ["write", None]
    chat_values = primary_values if chat_primary else fallback_values
    response_values = fallback_values if chat_primary else primary_values
    chat = ChatCompletions([
        value if isinstance(value, Exception) else _chat_response(value, "write_context_canvas" if value == "write" else "get_context_canvas")
        for value in chat_values
    ])
    responses = Responses([
        value if isinstance(value, Exception) else _response(value, "write_context_canvas" if value == "write" else "get_context_canvas")
        for value in response_values
    ])
    closed = []
    monkeypatch.setattr("gateway.openai.vision.OpenAI", lambda **kwargs: SimpleNamespace(
        chat=SimpleNamespace(completions=chat), close=lambda: closed.append(True),
    ))
    vision = OpenAIVisionModel(
        "portable-vision", provider="portable", base_url="https://offline.invalid", api_key="offline",
        max_output_tokens=8192, token_parameter="max_completion_tokens", response_format="json_object",
        request_timeout_seconds=7,
    )
    response_model = _model(provider, responses, budget=65536, reasoning="low", timeout=17)
    primary, fallback = (vision, response_model) if chat_primary else (response_model, vision)
    state, calls, usage = {}, [], []
    with capture_model_usage(usage.append):
        assert primary.generate(
            frames if chat_primary else native_inputs, schema_name="completion", result_type=Completion,
            tools=_tools(state, calls), max_model_calls=4, fallback_model=fallback,
            fallback_inputs=frames, fallback_on_error=lambda error: isinstance(error, ContentFilterError),
            context_window_policy=ContextWindowPolicy(128000, 65536, 2048, 50000, 40000),
        ).done
    assert calls == [("get", "observed"), ("write", "observed")]
    assert state == {"note": "observed"} and closed
    assert len(usage) == 4 and sum(record.status == "failed" for record in usage) == 1
    for request in chat.requests:
        assert request["max_completion_tokens"] == 8192
        assert request["response_format"] == {"type": "json_object"}
        assert 0 < request["timeout"] <= 7
        assert request["messages"][0]["content"][-1]["text"].startswith("Return only JSON")
    for request in responses.requests:
        assert request["max_output_tokens"] == 65536
        assert request["reasoning"] == {"effort": "low"}
        assert 0 < request["timeout"] <= 17
    if chat_primary:
        transferred = responses.requests[0]["input"]
        assert any(item.get("type") == "function_call_output" and item["call_id"] == "read" for item in transferred)
        assert [item["type"] for item in transferred[0]["content"]] == ["input_text", "input_image", "input_text", "input_image"]
    else:
        transferred = chat.requests[0]["messages"]
        assert any(item.get("role") == "tool" and item["tool_call_id"] == "read" for item in transferred)
        assert [item["type"] for item in transferred[0]["content"][:-1]] == ["text", "image_url", "text", "image_url"]
        assert all("responses_output" not in message for message in transferred)


def test_parse_canvas_partial_updates_keep_omitted_fields_through_model_validation():
    from agent.utils.context_canvas_tools import build_context_canvas_tools
    from agent.video_parse_v2.timelineEvent_canvas import CanvasCompletion, ParseCanvasWrite, ParseContextCanvas

    from engine.parsev2.schemas import VideoParseResult
    original = VideoParseResult.model_validate({"persons": ["Alice"], "timeline": [{
        "event_type": "ACTION", "start_seconds": 1000, "end_seconds": 3000,
        "description": "original", "participants": ["Alice"], "visible_persons": ["Alice"],
    }]})
    canvas = ParseContextCanvas(scope="refine:offline", members={"member": {
        "sequence_no": 0, "version": 2, "bounds": (0, 20000), "content": original,
    }})
    event_ref = canvas.items("events")[0][0]
    read, operation = {"collection": "events"}, {
        "op": "patch_event", "event_ref": event_ref, "fields": {"description": "corrected"},
    }
    tools = build_context_canvas_tools(
        canvas, role="refine", read_collections=("events",), write_args_schema=ParseCanvasWrite,
        maximum_result_bytes=4096, maximum_read_chars=1024, maximum_catalog_items=10,
    )
    revision = canvas.revision
    read_response = _response("read")
    read_response.output[0].arguments = json.dumps(read)
    write_response = _response("write", "write_context_canvas")
    write_response.output[0].arguments = json.dumps({
        "batch_id": "partial-update", "expected_revision": revision, "operations": [operation],
    })
    final = _response()
    final.output[0].content[0].text = '{"complete":true}'
    responses = Responses([read_response, write_response, final])
    result = _model("deepseek", responses).generate(
        [TextInput("Correct only the supplied field.")], schema_name="canvas_completion", result_type=CanvasCompletion,
        tools=[tool.as_langchain_tool() for tool in tools], max_model_calls=3,
    )
    assert result.complete and canvas.revision == revision + 1
    receipt = next(item for item in responses.requests[-1]["input"]
                   if item.get("type") == "function_call_output" and item["call_id"] == "write")
    assert "error" not in json.loads(receipt["output"])
    expected = original.model_copy(deep=True)
    expected.timeline[0].description = "corrected"
    assert canvas.result("member") == expected


@pytest.mark.parametrize("provider", ["ark", "deepseek", "chat"])
@pytest.mark.parametrize("reasoning", ["none", "high"])
def test_multimodal_request_log_correlates_controls_without_logging_payloads(
    caplog, monkeypatch, tmp_path, provider, reasoning,
):
    inputs, frames = _inputs(tmp_path, provider)
    secret = "PROMPT_SECRET_DO_NOT_LOG"
    inputs = [*inputs, TextInput(secret)]
    if provider == "chat":
        inputs = [*frames, TextInput(secret)]
        responses = ChatCompletions([_chat_response("read"), _chat_response()])
        monkeypatch.setattr("gateway.openai.vision.OpenAI", lambda **kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=responses), close=lambda: None,
        ))
        model = OpenAIVisionModel(
            "portable-vision", provider="portable", base_url="https://offline.invalid",
            api_key="API_KEY_SECRET", max_output_tokens=4096,
            token_parameter="max_completion_tokens", request_timeout_seconds=30,
        )
    else:
        responses = Responses([_response("read"), _response()])
        model = _model(provider, responses, budget=4096, reasoning=reasoning)
    context = {
        "video_parse_task_id": "task\nforged=1" + "t" * 300,
        "chunk_parse_task_id": "chunk-123", "interval_parse_task_id": "interval-456",
        "video_chunk_id": "video-chunk-789", "run_id": "run-123",
        "sequence_no": 7, "lease_version": 3,
        "authorization": "AUTH_SECRET_DO_NOT_LOG",
    }
    usage = []
    with caplog.at_level(logging.INFO, logger="gateway.agent_model.multimodal"):
        with model_call_context(context), capture_model_usage(usage.append):
            assert model.generate(
                inputs, schema_name="schema\nforged=1" + "s" * 200, result_type=Completion,
                tools=_tools({}, []), max_model_calls=2,
            ).done
    messages = [record.getMessage() for record in caplog.records
                if record.name == "gateway.agent_model.multimodal"
                and record.getMessage().startswith("event=multimodal_model_request ")]
    summaries = [json.loads(message.split(" summary=", 1)[1]) for message in messages]
    assert len(summaries) == len(responses.requests) == 2
    assert [summary["call_id"] for summary in summaries] == [call.call_id for call in usage]
    first = summaries[0]
    assert first["provider"] == ("portable" if provider == "chat" else provider)
    assert first["model"] == model.model_name and first["result_type"] == "Completion"
    assert first["response_format_type"] == "json_schema"
    assert first["response_format_strict"] is True
    assert first["schema_name"] == ("schema\nforged=1" + "s" * 200)[:128]
    for key in context.keys() - {"authorization"}:
        assert first[key] == (context[key][:256] if isinstance(context[key], str) else context[key])
    assert first["protocol"] == ("chat" if provider == "chat" else "responses")
    assert [summary["tool_choice"] for summary in summaries] == ["required", "auto"]
    assert first["tool_count"] == 2
    assert first["tool_names"] == ["get_context_canvas", "write_context_canvas"]
    assert first["max_output_tokens"] == 4096 and 0 < first["timeout"] <= 30
    assert first["token_parameter"] == ("max_completion_tokens" if provider == "chat" else "max_output_tokens")
    assert first["thinking"] == ("disabled" if provider == "ark" and reasoning == "none" else None)
    assert first["reasoning_effort"] == (None if provider == "chat" or provider == "ark" and reasoning == "none" else reasoning)
    assert first["input_item_count"] == 1 and first["content_block_count"] == len(inputs)
    assert first["text_chars"] == sum(len(item.text) for item in inputs if isinstance(item, TextInput))
    assert first["media_item_count"] == sum(not isinstance(item, TextInput) for item in inputs)
    assert all("\n" not in message for message in messages)
    assert all(value not in "".join(messages) for value in (
        secret, context["authorization"], "API_KEY_SECRET", "data:image/", "data:video/", "observed",
    ))


@pytest.mark.parametrize("provider", ["ark", "deepseek"])
def test_parse_diagnostics_restore_each_round_and_exclude_chat(monkeypatch, tmp_path, provider):
    from gateway.model_diagnostics import restore_diagnostic_media

    root = tmp_path / "model-traces"
    monkeypatch.setenv("SEEKINO_MODEL_TRACE_DIR", str(root))
    inputs, _frames = _inputs(tmp_path, provider)
    prompt = "Preserve the full prompt, schemas, and exact tool feedback."
    inputs.append(TextInput(prompt))
    responses = Responses([_response("read"), _response("write", "write_context_canvas"), _response()])
    context = {"video_parse_task_id": "task-1", "chunk_parse_task_id": "chunk-1",
               "stage": "understand", "sequence_no": 4, "lease_version": 3}
    state, calls, usage, saved = {}, [], [], []
    with model_call_context(context), capture_model_usage(usage.append):
        assert _model(provider, responses).generate(
            inputs, schema_name="canvas_completion", result_type=Completion,
            tools=_tools(state, calls), max_model_calls=3,
        ).done
    assert state == {"note": "observed"} and calls == [("get", "observed"), ("write", "observed")]
    assert len(usage) == 3 and {call.status for call in usage} == {"completed"}
    for round_no, (call, actual_request) in enumerate(zip(usage, responses.requests), 1):
        directory = root / call.call_id
        events = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]
        assert events[0]["event"] == "request" and events[-1]["event"] == "completed"
        assert {event["call_id"] for event in events} == {call.call_id}
        payload = events[0]["payload"]
        assert payload["events_format"] == "sdk_parsed"
        assert payload["context"] == dict(call.context)
        assert all(payload["context"][key] == value for key, value in context.items())
        assert payload["context"]["agent_round"] == round_no
        restored = restore_diagnostic_media(payload["request"], directory)
        assert restored == {**actual_request, "stream": provider == "ark"}
        assert restored["tools"][0]["parameters"]["properties"]["value"]["type"] == "string"
        assert restored["text"]["format"]["schema"] == Completion.model_json_schema()
        assert restored["input"][0]["content"][-1]["text"] == prompt
        assert any(event["event"] == "response" for event in events)
        assert any(event["event"] == "sse" for event in events) == (provider == "ark")
        saved.append(restored)
    assert len({call.context["agent_run_id"] for call in usage}) == 1
    second = saved[1]["input"]
    assert json.loads(next(item["arguments"] for item in second
                           if item.get("type") == "function_call" and item["call_id"] == "read")) == {"value": "observed"}
    assert json.loads(next(item["output"] for item in second
                           if item.get("type") == "function_call_output" and item["call_id"] == "read")) == {"note": ""}
    final = saved[2]["input"]
    assert json.loads(next(item["arguments"] for item in final
                           if item.get("type") == "function_call" and item["call_id"] == "write")) == {"value": "observed"}
    assert json.loads(next(item["output"] for item in final
                           if item.get("type") == "function_call_output" and item["call_id"] == "write")) == {"written": True}
    assert len(list(root.glob("*/events.jsonl"))) == 3
    chat = ChatCompletions([_chat_response("read"), _chat_response()])
    monkeypatch.setattr("gateway.openai.vision.OpenAI", lambda **kwargs: SimpleNamespace(
        chat=SimpleNamespace(completions=chat), close=lambda: None,
    ))
    vision = OpenAIVisionModel(
        "portable-vision", provider="portable", base_url="https://offline.invalid", api_key="offline",
        request_timeout_seconds=30,
    )
    assert vision.generate([TextInput("Ordinary Chat.")], schema_name="completion", result_type=Completion,
                           tools=_tools({}, []), max_model_calls=2).done
    assert len(list(root.glob("*/events.jsonl"))) == 3


def test_parse_diagnostics_preserve_empty_sdk_response_error_chain_and_request_id(monkeypatch, tmp_path):
    from arkruntime._models import construct_type
    from arkruntime.types.responses.response_stream_event import ResponseStreamEvent

    root = tmp_path / "model-traces"
    monkeypatch.setenv("SEEKINO_MODEL_TRACE_DIR", str(root))
    event = construct_type(type_=ResponseStreamEvent, value={
        "type": "response.completed", "sequence_number": 0, "response": {
            "id": "sdk-empty-response", "object": "response", "created_at": 1,
            "model": "ark-vision", "status": "completed", "output": [{
                "id": "empty-message", "type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": "", "annotations": []}],
            }],
        },
    })

    class SDKStream(Stream):
        def __iter__(self):
            yield event

    class SDKResponses(Responses):
        def create(self, **kwargs):
            self.requests.append(deepcopy(kwargs))
            stream = SDKStream(event.response)
            stream.response = SimpleNamespace(headers={"x-request-id": "provider-empty-request"})
            self.streams.append(stream)
            return stream

    responses, usage, calls = SDKResponses([]), [], []
    with model_call_context({"video_parse_task_id": "task-empty"}), capture_model_usage(usage.append):
        with pytest.raises(QueryModelIncompleteError, match="Multimodal Responses request failed") as caught:
            _model("ark", responses).generate(
                [TextInput("Diagnose an empty completion.")], schema_name="canvas_completion",
                result_type=Completion, tools=_tools({}, calls), max_model_calls=2,
            )
    assert len(usage) == 1 and usage[0].status == "failed"
    assert caught.value.request_id == usage[0].request_id == "provider-empty-request"
    assert calls == [] and len(responses.streams) == 1 and responses.streams[0].closed
    events = [json.loads(line) for line in (root / usage[0].call_id / "events.jsonl").read_text().splitlines()]
    terminal = next(item for item in events if item["event"] == "sse")
    assert terminal["provider_request_id"] == "provider-empty-request"
    assert terminal["payload"] == event.model_dump(mode="json", exclude_unset=True)
    assert terminal["payload"]["response"]["output"][0]["content"][0]["text"] == ""
    error = events[-1]
    assert error["event"] == "error" and error["call_id"] == usage[0].call_id
    assert error["provider_request_id"] == "provider-empty-request"
    chain = error["payload"]["chain"]
    assert [item["type"] for item in chain] == ["QueryModelIncompleteError", "SSEProxyError"]
    assert chain[1]["response_id"] == "sdk-empty-response" and chain[1]["partial_output_chars"] == 0


def test_parse_diagnostics_keep_response_when_final_schema_validation_fails(monkeypatch, tmp_path):
    root = tmp_path / "model-traces"
    monkeypatch.setenv("SEEKINO_MODEL_TRACE_DIR", str(root))
    invalid = _response()
    invalid.output[0].content[0].text = '{"done":"not-a-boolean"}'
    responses, usage = Responses([_response("read"), invalid]), []
    with model_call_context({"video_parse_task_id": "task-invalid"}), capture_model_usage(usage.append):
        with pytest.raises(QueryModelIncompleteError, match="final output does not match its schema"):
            _model("ark", responses).generate(
                [TextInput("Validate the final completion.")], schema_name="canvas_completion",
                result_type=Completion, tools=_tools({}, []), max_model_calls=2,
            )
    assert [call.status for call in usage] == ["completed", "failed"]
    assert all(stream.closed for stream in responses.streams)
    events = [json.loads(line) for line in (root / usage[-1].call_id / "events.jsonl").read_text().splitlines()]
    receipt = next(item["payload"] for item in events if item["event"] == "response")
    assert receipt["output"][0]["content"][0]["text"] == '{"done":"not-a-boolean"}'
    assert events[0]["payload"]["context"]["agent_round"] == 2
    assert events[-1]["event"] == "error"
    assert [item["type"] for item in events[-1]["payload"]["chain"]] == ["QueryModelIncompleteError", "ValidationError"]


@pytest.mark.parametrize("status,code,expected", [
    (429, "rate_limit_exceeded", ModelFailureReason.PROVIDER_RATE_LIMIT),
    (429, None, ModelFailureReason.PROVIDER_RATE_LIMIT),
    (503, None, ModelFailureReason.PROVIDER_CAPACITY),
    (429, "insufficient_quota", ModelFailureReason.INVALID_MODEL_RESPONSE),
    (429, "AccountOverdueError", ModelFailureReason.INVALID_MODEL_RESPONSE),
    (401, None, ModelFailureReason.INVALID_MODEL_RESPONSE),
    (403, "rate_limit_exceeded", ModelFailureReason.INVALID_MODEL_RESPONSE),
    (400, "bad_request", ModelFailureReason.INVALID_MODEL_RESPONSE),
    (400, "unknown_provider_code", ModelFailureReason.INVALID_MODEL_RESPONSE),
    (429, "unknown_provider_code", ModelFailureReason.INVALID_MODEL_RESPONSE),
    (503, "upstream_error", ModelFailureReason.INVALID_MODEL_RESPONSE),
    (503, "bad_request", ModelFailureReason.INVALID_MODEL_RESPONSE),
])
def test_multimodal_structured_provider_failure_does_not_retry_or_use_message(
    status, code, expected,
):
    response = httpx.Response(status, request=httpx.Request("POST", "https://example.invalid"))
    error_type = RateLimitError if status == 429 else APIStatusError
    error = error_type(
        "Selected model is at capacity. Please try a different model.",
        response=response, body={"code": code},
    )
    responses = Responses([error])
    with pytest.raises(QueryModelIncompleteError) as caught:
        _model("deepseek", responses).generate(
            [TextInput("Validate the completion.")], schema_name="canvas_completion",
            result_type=Completion, tools=_tools({}, []), max_model_calls=2,
        )
    assert caught.value.reason == expected
    assert caught.value.__cause__ is error
    assert len(responses.requests) == 1


@pytest.mark.parametrize("code,status,body,expected", [
    (None, None, None, ModelFailureReason.PROVIDER_CAPACITY),
    ("other", None, None, ModelFailureReason.INVALID_MODEL_RESPONSE),
    (None, 400, None, ModelFailureReason.INVALID_MODEL_RESPONSE),
    (None, None, {"error": {"code": "rate_limit_exceeded"}}, ModelFailureReason.PROVIDER_RATE_LIMIT),
    (None, None, {"error": {"code": "insufficient_quota"}}, ModelFailureReason.INVALID_MODEL_RESPONSE),
    (None, None, {"error": {"code": "unknown"}}, ModelFailureReason.INVALID_MODEL_RESPONSE),
])
def test_provider_code_precedence_for_unstructured_and_nested_errors(code, status, body, expected):
    error = RuntimeError("Selected model is at capacity. Please try a different model.")
    error.code, error.status_code, error.body = code, status, body
    assert _failure_reason_for_exception(error) == expected
