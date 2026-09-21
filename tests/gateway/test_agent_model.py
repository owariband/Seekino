import json
import logging
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from arkruntime import Ark
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain_core.tools import StructuredTool, ToolException
from openai import APIConnectionError, APIError, OpenAI
from pydantic import BaseModel, ConfigDict

from gateway import agent_model as agent_model_module
from gateway.agent_model import (
    ArkFunctionCallingModel,
    DeepSeekFunctionCallingError,
    DeepSeekFunctionCallingModel,
    MissingStructuredQueryToolCallError,
    ModelInputTooLargeError,
    SDKQueryAgentModel,
)
from gateway.sse_proxy import ContentFilterError
from gateway.usage import capture_model_usage


class QueryDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_context: str
    message: str
    selected_video_chunk_ids: list[str]


class VideoItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_chunk_id: str
    claim: str


class _FunctionCompletions:
    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        if not self.responses:
            raise AssertionError("unexpected extra completion request")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _tool_call(
    arguments: dict | str,
    *,
    call_id: str | None = "call-1",
    tool_name: str = "submit_video_item",
    call_type: str = "function",
):
    rendered_arguments = (
        arguments if isinstance(arguments, str) else json.dumps(arguments)
    )
    return SimpleNamespace(
        id=call_id,
        type=call_type,
        function=SimpleNamespace(
            name=tool_name,
            arguments=rendered_arguments,
        ),
    )


def _agent_response(
    content: object = None,
    *,
    calls: object = None,
    finish_reason: str | None = "stop",
    role: str = "assistant",
    response_id: str = "deepseek-response",
    total_tokens: int = 15,
):
    return SimpleNamespace(
        id=response_id,
        model="deepseek-flash",
        usage=SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=total_tokens - 10,
            total_tokens=total_tokens,
        ),
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(
                    role=role,
                    content=content,
                    tool_calls=calls,
                ),
            )
        ],
    )


def _video_item(chunk_id: str = "chunk-1", claim: str = "杯子落地") -> dict:
    return {"video_chunk_id": chunk_id, "claim": claim}


def _function_model(*responses):
    completions = _FunctionCompletions(*responses)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    model = DeepSeekFunctionCallingModel(
        "deepseek-flash",
        base_url="https://api.deepseek.com",
        api_key="test-key",
        max_output_tokens=2048,
        request_timeout_seconds=30,
        client=client,
    )
    return model, completions


def _run_agent(
    model,
    *,
    max_model_calls: int | None = 4,
    handle_text=lambda _text: None,
    invoke_tool=lambda _item: "accepted",
    require_tool_call: bool = False,
    request_timeout_seconds: float | None = None,
    check_control=None,
    max_input_chars: int | None = None,
    handle_tool_error: bool = False,
) -> None:
    def submit_video_item(video_chunk_id: str, claim: str) -> str:
        return invoke_tool(VideoItem(
            video_chunk_id=video_chunk_id,
            claim=claim,
        ))

    tool = StructuredTool.from_function(
        func=submit_video_item,
        name="submit_video_item",
        description="Submit one approved video item.",
        args_schema=VideoItem,
        handle_tool_error=handle_tool_error,
    )
    model.run_agent_loop(
        "根据审核后的证据回答",
        tools=[tool],
        max_model_calls=max_model_calls,
        handle_text=handle_text,
        require_tool_call=require_tool_call,
        request_timeout_seconds=request_timeout_seconds,
        check_control=check_control,
        max_input_chars=max_input_chars,
    )


def test_function_loop_releases_oversized_tool_result_before_next_request() -> None:
    model, completions = _function_model(
        _agent_response(
            calls=[_tool_call(_video_item())],
            finish_reason="tool_calls",
        ),
        _agent_response("回答"),
    )

    _run_agent(
        model,
        invoke_tool=lambda _item: "x" * 20_000,
        max_input_chars=10_000,
    )

    assert len(completions.calls) == 2
    second_messages = completions.calls[1]["messages"]
    assert "anivag_context_checkpoint" in second_messages[1]["content"]
    released = json.loads(second_messages[-1]["content"])
    assert released["context_window"] == "tool_result_released"
    assert released["original_bytes"] == 20_000


def test_deepseek_agent_loop_preserves_text_and_tool_order_across_turns() -> None:
    first_arguments = _video_item()
    second_arguments = _video_item("chunk-2", "人物离开房间")
    third_arguments = _video_item("chunk-3", "门已经关闭")
    model, completions = _function_model(
        _agent_response(
            "先看两个关键片段。",
            calls=[
                _tool_call(first_arguments, call_id="call-1"),
                _tool_call(second_arguments, call_id="call-2"),
            ],
            finish_reason="tool_calls",
            response_id="response-1",
        ),
        _agent_response(
            "再补充一个片段。",
            calls=[_tool_call(third_arguments, call_id="call-3")],
            finish_reason="tool_calls",
            response_id="response-2",
            total_tokens=16,
        ),
        _agent_response(
            "## 结论\n\n三个片段共同支持这个结论。",
            response_id="response-3",
            total_tokens=17,
        ),
    )
    events = []

    def handle_text(text: str) -> None:
        events.append(("text", text))

    def handle_tool(item: VideoItem) -> str:
        events.append(("tool", item.video_chunk_id))
        return json.dumps({"accepted": item.video_chunk_id})

    _run_agent(
        model,
        handle_text=handle_text,
        invoke_tool=handle_tool,
    )

    assert events == [
        ("text", "先看两个关键片段。"),
        ("tool", "chunk-1"),
        ("tool", "chunk-2"),
        ("text", "再补充一个片段。"),
        ("tool", "chunk-3"),
        ("text", "## 结论\n\n三个片段共同支持这个结论。"),
    ]
    assert len(completions.calls) == 3
    for request in completions.calls:
        assert request["tool_choice"] == "auto"
        assert request["tools"][0]["function"]["name"] == "submit_video_item"
        assert request["extra_body"] == {"thinking": {"type": "disabled"}}
        assert request["stream"] is False
        assert request["temperature"] == 0
        assert request["max_tokens"] == 2048

    assert completions.calls[0]["messages"] == [
        {"role": "user", "content": "根据审核后的证据回答"}
    ]
    assert [message["role"] for message in completions.calls[1]["messages"]] == [
        "user", "assistant", "tool", "tool",
    ]
    assert completions.calls[1]["messages"][1] == {
        "role": "assistant",
        "content": "先看两个关键片段。",
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "submit_video_item",
                    "arguments": json.dumps(first_arguments),
                },
            },
            {
                "id": "call-2",
                "type": "function",
                "function": {
                    "name": "submit_video_item",
                    "arguments": json.dumps(second_arguments),
                },
            },
        ],
    }
    assert [message["role"] for message in completions.calls[2]["messages"]] == [
        "user", "assistant", "tool", "tool", "assistant", "tool",
    ]


def test_deepseek_agent_loop_allows_first_turn_text_stop() -> None:
    model, completions = _function_model(
        _agent_response("没有需要展示的视频，直接回答。"),
    )
    texts = []

    _run_agent(
        model,
        max_model_calls=1,
        handle_text=texts.append,
        invoke_tool=lambda _item: pytest.fail("tool handler must not run"),
    )

    assert texts == ["没有需要展示的视频，直接回答。"]
    assert len(completions.calls) == 1
    assert completions.calls[0]["tool_choice"] == "auto"


def test_ark_agent_loop_requires_video_tool_and_falls_back_to_lite() -> None:
    completions = _FunctionCompletions(
        _agent_response("Pro skipped the required tool."),
        _agent_response(
            None,
            calls=[_tool_call(_video_item())],
            finish_reason="tool_calls",
        ),
        _agent_response("Lite completed the answer."),
    )
    model = ArkFunctionCallingModel(
        "doubao-pro",
        fallback_model="doubao-lite",
        max_output_tokens=2048,
        request_timeout_seconds=30,
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
    )
    events = []

    _run_agent(
        model,
        require_tool_call=True,
        handle_text=lambda text: events.append(("text", text)),
        invoke_tool=lambda item: (
            events.append(("tool", item.video_chunk_id)) or "accepted"
        ),
    )

    assert events == [
        ("tool", "chunk-1"),
        ("text", "Lite completed the answer."),
    ]
    assert [call["model"] for call in completions.calls] == [
        "doubao-pro", "doubao-lite", "doubao-lite",
    ]
    assert [call["tool_choice"] for call in completions.calls] == [
        "required", "required", "auto",
    ]


def test_ark_agent_loop_does_not_fallback_after_tool_handler_error() -> None:
    completions = _FunctionCompletions(
        _agent_response(
            None,
            calls=[_tool_call(_video_item())],
            finish_reason="tool_calls",
        ),
    )
    model = ArkFunctionCallingModel(
        "doubao-pro",
        fallback_model="doubao-lite",
        max_output_tokens=2048,
        request_timeout_seconds=30,
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
    )

    def reject_item(_item: VideoItem) -> str:
        raise RuntimeError("server rejected item")

    with pytest.raises(RuntimeError, match="server rejected item"):
        _run_agent(
            model,
            require_tool_call=True,
            invoke_tool=reject_item,
        )

    assert [call["model"] for call in completions.calls] == ["doubao-pro"]


def test_ark_agent_loop_falls_back_when_pro_stops_without_final_text() -> None:
    completions = _FunctionCompletions(
        _agent_response(
            None,
            calls=[_tool_call(_video_item())],
            finish_reason="tool_calls",
        ),
        _agent_response(None),
        _agent_response("Lite completed the answer."),
    )
    model = ArkFunctionCallingModel(
        "doubao-pro",
        fallback_model="doubao-lite",
        max_output_tokens=2048,
        request_timeout_seconds=30,
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
    )
    events = []

    _run_agent(
        model,
        require_tool_call=True,
        handle_text=lambda text: events.append(("text", text)),
        invoke_tool=lambda item: (
            events.append(("tool", item.video_chunk_id)) or "accepted"
        ),
    )

    assert events == [
        ("tool", "chunk-1"),
        ("text", "Lite completed the answer."),
    ]
    assert [call["model"] for call in completions.calls] == [
        "doubao-pro", "doubao-pro", "doubao-lite",
    ]
    assert [call["tool_choice"] for call in completions.calls] == [
        "required", "auto", "auto",
    ]


def test_deepseek_agent_loop_falls_back_to_ark_in_place_after_tool_result() -> None:
    deepseek = _FunctionCompletions(
        _agent_response(
            None,
            calls=[_tool_call(_video_item())],
            finish_reason="tool_calls",
        ),
        _agent_response(None),
    )
    ark = _FunctionCompletions(_agent_response("Lite completed the answer."))
    model = DeepSeekFunctionCallingModel(
        "deepseek-flash",
        base_url="https://api.deepseek.com",
        api_key="test-key",
        max_output_tokens=2048,
        request_timeout_seconds=30,
        fallback_model="doubao-lite",
        client=SimpleNamespace(chat=SimpleNamespace(completions=deepseek)),
        fallback_client=SimpleNamespace(chat=SimpleNamespace(completions=ark)),
    )
    events = []

    _run_agent(
        model,
        require_tool_call=True,
        handle_text=lambda text: events.append(("text", text)),
        invoke_tool=lambda item: (
            events.append(("tool", item.video_chunk_id)) or "accepted"
        ),
    )

    assert events == [
        ("tool", "chunk-1"),
        ("text", "Lite completed the answer."),
    ]
    assert [call["tool_choice"] for call in deepseek.calls] == ["required", "auto"]
    assert [call["model"] for call in ark.calls] == ["doubao-lite"]
    assert ark.calls[0]["tool_choice"] == "auto"
    assert [message["role"] for message in ark.calls[0]["messages"]] == [
        "user", "assistant", "tool",
    ]
    assert ark.calls[0]["messages"][2] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": "accepted",
    }


def test_deepseek_agent_loop_accepts_empty_stop_after_earlier_visible_text() -> None:
    model, _ = _function_model(
        _agent_response(
            "先展示证据。",
            calls=[_tool_call(_video_item())],
            finish_reason="tool_calls",
        ),
        _agent_response(None),
    )
    events = []

    _run_agent(
        model,
        handle_text=lambda text: events.append(("text", text)),
        invoke_tool=lambda item: (
            events.append(("tool", item.video_chunk_id)) or "accepted"
        ),
    )

    assert events == [("text", "先展示证据。"), ("tool", "chunk-1")]


def test_deepseek_agent_loop_requires_visible_text_before_completion() -> None:
    model, _ = _function_model(
        _agent_response(
            None,
            calls=[_tool_call(_video_item())],
            finish_reason="tool_calls",
        ),
        _agent_response("   "),
    )
    tools = []

    with pytest.raises(DeepSeekFunctionCallingError, match="without visible text"):
        _run_agent(
            model,
            handle_text=lambda _text: pytest.fail("empty text must not be emitted"),
            invoke_tool=lambda item: tools.append(item) or "accepted",
        )

    assert [item.video_chunk_id for item in tools] == ["chunk-1"]


def test_deepseek_validates_full_tool_batch_before_any_callback() -> None:
    model, completions = _function_model(
        _agent_response(
            "must not be emitted",
            calls=[
                _tool_call(_video_item(), call_id="call-1"),
                _tool_call("not-json", call_id="call-2"),
            ],
            finish_reason="tool_calls",
        ),
    )
    events = []

    with pytest.raises(DeepSeekFunctionCallingError, match="do not match schema"):
        _run_agent(
            model,
            handle_text=lambda text: events.append(("text", text)),
            invoke_tool=lambda item: events.append(("tool", item)) or "accepted",
        )

    assert events == []
    assert len(completions.calls) == 1


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (_agent_response("partial", finish_reason="length"), "'length'"),
        (
            _agent_response(
                "answer",
                calls=[_tool_call(_video_item())],
                finish_reason="stop",
            ),
            "must not contain tool calls",
        ),
        (
            _agent_response(None, calls=[], finish_reason="tool_calls"),
            "contained no tool calls",
        ),
        (_agent_response("answer", role="user"), "invalid assistant message"),
        (_agent_response(["not", "text"]), "must be text or null"),
        (
            _agent_response("answer", calls="invalid", finish_reason="tool_calls"),
            "invalid tool calls",
        ),
    ],
)
def test_deepseek_agent_loop_rejects_invalid_turn(response, message) -> None:
    model, _ = _function_model(response)

    with pytest.raises(DeepSeekFunctionCallingError, match=message):
        _run_agent(model)


@pytest.mark.parametrize(
    ("calls", "message"),
    [
        (
            [_tool_call(_video_item(), tool_name="unexpected")],
            "unexpected function",
        ),
        (
            [_tool_call(_video_item(), call_id="")],
            "unique nonempty IDs",
        ),
        (
            [
                _tool_call(_video_item(), call_id="same"),
                _tool_call(_video_item("chunk-2"), call_id="same"),
            ],
            "unique nonempty IDs",
        ),
        (
            [_tool_call(_video_item(), call_type="custom")],
            "unexpected function",
        ),
        ([_tool_call(" ")], "empty function arguments"),
        (
            [_tool_call({**_video_item(), "invented": True})],
            "do not match schema",
        ),
    ],
)
def test_deepseek_agent_loop_rejects_invalid_tool_calls(calls, message) -> None:
    model, _ = _function_model(
        _agent_response(
            "must not emit",
            calls=calls,
            finish_reason="tool_calls",
        ),
    )
    events = []

    with pytest.raises(DeepSeekFunctionCallingError, match=message):
        _run_agent(
            model,
            handle_text=events.append,
            invoke_tool=lambda item: events.append(item) or "accepted",
        )

    assert events == []


def test_deepseek_agent_loop_rejects_reused_tool_id_across_turns() -> None:
    model, completions = _function_model(
        _agent_response(
            None,
            calls=[_tool_call(_video_item(), call_id="same")],
            finish_reason="tool_calls",
        ),
        _agent_response(
            "must not emit",
            calls=[_tool_call(_video_item("chunk-2"), call_id="same")],
            finish_reason="tool_calls",
        ),
    )
    events = []

    with pytest.raises(DeepSeekFunctionCallingError, match="unique nonempty IDs"):
        _run_agent(
            model,
            handle_text=lambda text: events.append(("text", text)),
            invoke_tool=lambda item: (
                events.append(("tool", item.video_chunk_id)) or "accepted"
            ),
        )

    assert events == [("tool", "chunk-1")]
    assert len(completions.calls) == 2


def test_deepseek_agent_loop_stops_at_model_call_budget() -> None:
    model, completions = _function_model(
        _agent_response(
            "first",
            calls=[_tool_call(_video_item())],
            finish_reason="tool_calls",
        ),
        _agent_response("must not be requested"),
    )
    events = []

    with pytest.raises(DeepSeekFunctionCallingError, match="exhausted max_model_calls"):
        _run_agent(
            model,
            max_model_calls=1,
            handle_text=lambda text: events.append(("text", text)),
            invoke_tool=lambda item: (
                events.append(("tool", item.video_chunk_id)) or "accepted"
            ),
        )

    assert events == [("text", "first"), ("tool", "chunk-1")]
    assert len(completions.calls) == 1


def test_deepseek_agent_loop_without_budget_continues_past_old_limit() -> None:
    responses = [
        _agent_response(
            calls=[_tool_call(_video_item(), call_id=f"call-{index}")],
            finish_reason="tool_calls",
        )
        for index in range(5)
    ]
    model, completions = _function_model(*responses, _agent_response("回答"))

    _run_agent(model, max_model_calls=None)

    assert len(completions.calls) == 6


def test_deepseek_agent_loop_uses_one_total_deadline(monkeypatch) -> None:
    ticks = iter([100.0, 100.25, 101.0])
    monkeypatch.setattr(agent_model_module, "monotonic", lambda: next(ticks))
    model, completions = _function_model(
        _agent_response(
            None,
            calls=[_tool_call(_video_item())],
            finish_reason="tool_calls",
        ),
        _agent_response("回答"),
    )

    _run_agent(model, request_timeout_seconds=2)

    assert completions.calls[0]["timeout"] == 1.75
    assert completions.calls[1]["timeout"] == 1.0


def test_deepseek_agent_loop_does_not_request_after_deadline(monkeypatch) -> None:
    ticks = iter([100.0, 100.5, 102.1])
    monkeypatch.setattr(agent_model_module, "monotonic", lambda: next(ticks))
    model, completions = _function_model(
        _agent_response(
            None,
            calls=[_tool_call(_video_item())],
            finish_reason="tool_calls",
        ),
        _agent_response("must not be requested"),
    )

    with pytest.raises(DeepSeekFunctionCallingError, match="deadline expired"):
        _run_agent(model, request_timeout_seconds=2)

    assert len(completions.calls) == 1


def test_deepseek_agent_loop_checks_control_before_callbacks() -> None:
    model, completions = _function_model(_agent_response("must not emit"))
    checks = 0
    texts = []

    def check_control() -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise RuntimeError("execution cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        _run_agent(
            model,
            handle_text=texts.append,
            check_control=check_control,
        )

    assert checks == 2
    assert texts == []
    assert len(completions.calls) == 1


def test_deepseek_agent_loop_propagates_text_handler_error_before_tools() -> None:
    model, completions = _function_model(
        _agent_response(
            "answer",
            calls=[_tool_call(_video_item())],
            finish_reason="tool_calls",
        ),
        _agent_response("must not be requested"),
    )
    tools = []

    def fail(_text: str) -> None:
        raise RuntimeError("text rejected")

    with pytest.raises(RuntimeError, match="text rejected"):
        _run_agent(
            model,
            handle_text=fail,
            invoke_tool=lambda item: tools.append(item) or "accepted",
        )

    assert tools == []
    assert len(completions.calls) == 1


def test_deepseek_agent_loop_propagates_tool_handler_error() -> None:
    model, completions = _function_model(
        _agent_response(
            None,
            calls=[_tool_call(_video_item())],
            finish_reason="tool_calls",
        ),
        _agent_response("must not be requested"),
    )

    def fail(_item: VideoItem) -> str:
        raise RuntimeError("server rejected item")

    with pytest.raises(RuntimeError, match="server rejected item"):
        _run_agent(model, invoke_tool=fail)

    assert len(completions.calls) == 1


def test_deepseek_agent_loop_returns_recoverable_tool_error_for_retry() -> None:
    model, completions = _function_model(
        _agent_response(
            None,
            calls=[_tool_call(_video_item())],
            finish_reason="tool_calls",
        ),
        _agent_response(
            None,
            calls=[_tool_call(_video_item(), call_id="call-2")],
            finish_reason="tool_calls",
        ),
        _agent_response("answer"),
    )
    attempts = 0

    def retry_once(_item: VideoItem) -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ToolException("时间区间不合法，请修正后重试。")
        return "accepted"

    _run_agent(
        model,
        invoke_tool=retry_once,
        handle_tool_error=True,
    )

    assert attempts == 2
    assert completions.calls[1]["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": "时间区间不合法，请修正后重试。",
    }
    assert completions.calls[2]["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "call-2",
        "content": "accepted",
    }


def test_deepseek_agent_loop_serializes_structured_tool_result() -> None:
    model, completions = _function_model(
        _agent_response(
            None,
            calls=[_tool_call(_video_item())],
            finish_reason="tool_calls",
        ),
        _agent_response("answer"),
    )

    _run_agent(
        model,
        invoke_tool=lambda _item: {"accepted": True},
    )

    assert len(completions.calls) == 2
    assert completions.calls[1]["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": '{"accepted":true}',
    }


def test_deepseek_agent_loop_records_usage_for_each_request() -> None:
    model, _ = _function_model(
        _agent_response(
            "intro",
            calls=[_tool_call(_video_item())],
            finish_reason="tool_calls",
            total_tokens=15,
        ),
        _agent_response("answer", total_tokens=20),
    )
    usages = []

    with capture_model_usage(usages.append):
        _run_agent(model)

    assert len(usages) == 2
    assert [usage.provider for usage in usages] == ["deepseek", "deepseek"]
    assert [usage.model for usage in usages] == ["deepseek-flash"] * 2
    assert [usage.operation for usage in usages] == ["submit_video_item"] * 2
    assert [usage.status for usage in usages] == ["completed", "completed"]
    assert [usage.usage.total_tokens for usage in usages] == [15, 20]


def test_deepseek_agent_loop_marks_invalid_request_usage_failed() -> None:
    model, _ = _function_model(
        _agent_response("partial", finish_reason="length"),
    )
    usages = []

    with capture_model_usage(usages.append):
        with pytest.raises(DeepSeekFunctionCallingError):
            _run_agent(model)

    assert len(usages) == 1
    assert usages[0].status == "failed"
    assert usages[0].usage.total_tokens == 15


def test_deepseek_agent_loop_reports_content_filter() -> None:
    model, _ = _function_model(
        _agent_response(
            None,
            finish_reason="content_filter",
            response_id="filtered-response",
        ),
    )

    with pytest.raises(ContentFilterError) as error:
        _run_agent(model)

    assert error.value.response_id == "filtered-response"


def test_deepseek_agent_loop_wraps_api_error_without_provider_text() -> None:
    provider_error = APIConnectionError(
        request=httpx.Request("POST", "https://api.deepseek.com/chat/completions"),
    )
    model, _ = _function_model(provider_error)

    with pytest.raises(DeepSeekFunctionCallingError, match="request failed") as error:
        _run_agent(model)

    assert error.value.__cause__ is provider_error
    assert str(provider_error) not in str(error.value)


def test_deepseek_agent_loop_maps_api_content_filter_safely() -> None:
    provider_error = APIError(
        "provider-sensitive detail",
        httpx.Request("POST", "https://api.deepseek.com/chat/completions"),
        body={"code": "content_filter"},
    )
    model, _ = _function_model(provider_error)

    with pytest.raises(ContentFilterError) as error:
        _run_agent(model)

    assert error.value.__cause__ is provider_error
    assert "provider-sensitive detail" not in str(error.value)


def test_deepseek_agent_loop_owns_one_client_per_invocation(monkeypatch) -> None:
    clients = []

    class Client:
        def __init__(self, **options):
            self.options = options
            self.closed = False
            self.chat = SimpleNamespace(
                completions=_FunctionCompletions(_agent_response("回答"))
            )
            clients.append(self)

        def close(self):
            self.closed = True

    monkeypatch.setattr(agent_model_module, "OpenAI", Client)
    model = DeepSeekFunctionCallingModel(
        "deepseek-flash",
        base_url="https://api.deepseek.com/",
        api_key="test-key",
        max_output_tokens=2048,
        request_timeout_seconds=30,
    )

    for _prompt in ("回答一", "回答二"):
        _run_agent(model, max_model_calls=1)

    assert len(clients) == 2
    assert all(client.closed for client in clients)
    assert all(client.options == {
        "api_key": "test-key",
        "base_url": "https://api.deepseek.com",
        "timeout": 30,
        "max_retries": 0,
    } for client in clients)


def test_deepseek_agent_loop_closes_primary_when_fallback_construction_fails(
    monkeypatch,
) -> None:
    clients = []

    class Client:
        def __init__(self, **_options):
            self.closed = False
            clients.append(self)

        def close(self):
            self.closed = True

    def fail_fallback_construction(**_options):
        raise RuntimeError("fallback client construction failed")

    monkeypatch.setattr(agent_model_module, "OpenAI", Client)
    monkeypatch.setattr(
        agent_model_module.Ark,
        "volc",
        fail_fallback_construction,
    )
    model = DeepSeekFunctionCallingModel(
        "deepseek-flash",
        base_url="https://api.deepseek.com/",
        api_key="test-key",
        max_output_tokens=2048,
        request_timeout_seconds=30,
        fallback_model="doubao-lite",
    )

    with pytest.raises(RuntimeError, match="fallback client construction failed"):
        _run_agent(model, max_model_calls=1)

    assert len(clients) == 1
    assert clients[0].closed is True


def test_deepseek_agent_loop_closes_both_clients_when_one_close_fails(
    monkeypatch,
) -> None:
    close_events = []

    class PrimaryClient:
        def __init__(self, **_options):
            self.chat = SimpleNamespace(
                completions=_FunctionCompletions(_agent_response("回答")),
            )

        def close(self):
            close_events.append("primary")

    class FallbackClient:
        def close(self):
            close_events.append("fallback")
            raise RuntimeError("fallback close failed")

    monkeypatch.setattr(agent_model_module, "OpenAI", PrimaryClient)
    monkeypatch.setattr(
        agent_model_module.Ark,
        "volc",
        lambda **_options: FallbackClient(),
    )
    model = DeepSeekFunctionCallingModel(
        "deepseek-flash",
        base_url="https://api.deepseek.com/",
        api_key="test-key",
        max_output_tokens=2048,
        request_timeout_seconds=30,
        fallback_model="doubao-lite",
    )

    with pytest.raises(RuntimeError, match="fallback close failed"):
        _run_agent(model, max_model_calls=1)

    assert close_events == ["fallback", "primary"]


@pytest.mark.parametrize("max_model_calls", [0, -1, True, 1.5])
def test_deepseek_agent_loop_rejects_invalid_model_call_budget(
    max_model_calls,
) -> None:
    model, completions = _function_model(_agent_response("unused"))

    with pytest.raises(ValueError, match="positive integer"):
        _run_agent(model, max_model_calls=max_model_calls)

    assert completions.calls == []


def test_deepseek_agent_loop_rejects_invalid_completion_envelope() -> None:
    response = _agent_response("answer")
    response.choices = []
    model, _ = _function_model(response)

    with pytest.raises(DeepSeekFunctionCallingError, match="invalid chat completion"):
        _run_agent(model)


def _completion(
    name: str,
    arguments: dict,
    *,
    reason: str = "tool_calls",
    call_id: str = "call-1",
) -> dict:
    return {
        "id": "completion-test",
        "model": "ark-test",
        "created": 1,
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "finish_reason": reason,
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ],
                },
            }
        ],
    }


def _tool(function):
    return StructuredTool.from_function(
        function,
        name="search_video_chunk_evidence",
        description="Retrieve actual video evidence for a query.",
    )


def _query_chat_model(
    provider: str,
    model_name: str,
    *,
    fallback_model_name: str | None = None,
):
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace()),
    )
    return agent_model_module._build_query_chat_model(
        provider=provider,
        model_name=model_name,
        client=client,
        base_url=(
            "https://api.deepseek.test/v1"
            if provider == "deepseek"
            else None
        ),
        max_output_tokens=400,
        request_timeout_seconds=3,
        fallback_model_name=fallback_model_name,
    )


def test_query_fallback_uses_remaining_single_request_timeout(monkeypatch) -> None:
    ticks = iter([100.0, 100.75])
    monkeypatch.setattr(agent_model_module, "monotonic", lambda: next(ticks))
    calls = []
    fallback_result = object()
    provider_error = APIConnectionError(
        request=httpx.Request(
            "POST",
            "https://api.deepseek.test/v1/chat/completions",
        ),
    )

    def generate(chat_model, *_args, **kwargs):
        calls.append((chat_model.model_name, kwargs["timeout"]))
        if chat_model.model_name == "deepseek-flash":
            raise provider_error
        return fallback_result

    monkeypatch.setattr(agent_model_module.ChatOpenAI, "_generate", generate)
    primary = _query_chat_model(
        "deepseek",
        "deepseek-flash",
        fallback_model_name="doubao-lite",
    )
    fallback = _query_chat_model("ark", "doubao-lite")
    primary.set_cross_provider_fallback(fallback)

    result = primary._generate([])

    assert result is fallback_result
    assert calls == [
        ("deepseek-flash", 3.0),
        ("doubao-lite", 2.25),
    ]


def test_query_does_not_fallback_after_request_timeout_is_exhausted(
    monkeypatch,
) -> None:
    ticks = iter([100.0, 103.1])
    monkeypatch.setattr(agent_model_module, "monotonic", lambda: next(ticks))
    calls = []

    def generate(chat_model, *_args, **kwargs):
        calls.append((chat_model.model_name, kwargs["timeout"]))
        raise MissingStructuredQueryToolCallError("missing required tool")

    monkeypatch.setattr(agent_model_module.ChatOpenAI, "_generate", generate)
    primary = _query_chat_model(
        "deepseek",
        "deepseek-flash",
        fallback_model_name="doubao-lite",
    )
    primary.set_cross_provider_fallback(
        _query_chat_model("ark", "doubao-lite")
    )

    with pytest.raises(
        MissingStructuredQueryToolCallError,
        match="missing required tool",
    ):
        primary._generate([])

    assert calls == [("deepseek-flash", 3.0)]
    assert primary._fallback_active is False


@pytest.mark.parametrize("error_type", [TypeError, AttributeError])
def test_query_does_not_fallback_for_programming_errors(
    monkeypatch,
    error_type,
) -> None:
    calls = []

    def generate(chat_model, *_args, **kwargs):
        calls.append((chat_model.model_name, kwargs["timeout"]))
        raise error_type("programming error")

    monkeypatch.setattr(agent_model_module.ChatOpenAI, "_generate", generate)
    primary = _query_chat_model(
        "deepseek",
        "deepseek-flash",
        fallback_model_name="doubao-lite",
    )
    primary.set_cross_provider_fallback(
        _query_chat_model("ark", "doubao-lite")
    )

    with pytest.raises(error_type, match="programming error"):
        primary._generate([])

    assert calls == [("deepseek-flash", 3.0)]
    assert primary._fallback_active is False


def test_sdk_query_model_requests_use_remaining_stage_timeout() -> None:
    remaining = iter([1.75, 1.0])
    seen_timeouts = []
    checks = 0

    def check() -> None:
        nonlocal checks
        checks += 1

    def override(**changes):
        return SimpleNamespace(model_settings=changes["model_settings"])

    request = SimpleNamespace(
        model_settings={"timeout": 3},
        override=override,
    )
    middleware = agent_model_module._ExecutionControlMiddleware(
        check,
        lambda: next(remaining),
    )

    for _ in range(2):
        middleware.wrap_model_call(
            request,
            lambda updated: seen_timeouts.append(
                updated.model_settings["timeout"]
            ),
        )

    assert seen_timeouts == [1.75, 1.0]
    assert checks == 4


def test_sdk_query_stops_when_stage_deadline_expires(monkeypatch) -> None:
    ticks = iter([100.0, 100.0, 102.1])
    monkeypatch.setattr(agent_model_module, "monotonic", lambda: next(ticks))
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    client = Ark(
        api_key="test-ark-key",
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    model = SDKQueryAgentModel(
        "ark-test", request_timeout_seconds=3, max_output_tokens=400, client=client
    )

    with pytest.raises(TimeoutError, match="execution deadline expired"):
        model.run_query(
            "检索", tools=[], result_type=QueryDraft,
            max_model_calls=None,
            request_timeout_seconds=2,
        )

    assert requests == []
    client.close()


def test_sdk_query_runs_actual_tool_round_trip_over_ark_transport() -> None:
    requests = []
    searches = []

    def retrieve(query: str) -> dict:
        searches.append(query)
        return {
            "hits": [{"video_chunk_id": "chunk-real", "video_id": "video-real"}]
        }

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        assert request.url.path == "/api/v3/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-ark-key"
        assert payload["max_completion_tokens"] == 400
        if len(requests) == 1:
            completion = _completion(
                "search_video_chunk_evidence", {"query": "摔杯子"}
            )
        else:
            tool_messages = [m for m in payload["messages"] if m["role"] == "tool"]
            assert "chunk-real" in tool_messages[0]["content"]
            assert "video-real" in tool_messages[0]["content"]
            completion = _completion(
                "QueryDraft",
                {
                    "plan_context": "确认摔杯子的动作",
                    "message": "找到了片段",
                    "selected_video_chunk_ids": ["chunk-real"],
                },
            )
        return httpx.Response(200, json=completion)

    client = Ark(
        api_key="test-ark-key",
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    model = SDKQueryAgentModel(
        "ark-test", request_timeout_seconds=3, max_output_tokens=400, client=client
    )
    result = model.run_query(
        "找到摔杯子的片段",
        tools=[_tool(retrieve)],
        result_type=QueryDraft,
        max_model_calls=2,
    )
    assert result.selected_video_chunk_ids == ["chunk-real"]
    assert searches == ["摔杯子"]
    assert len(requests) == 2
    assert all(request["tool_choice"] == "required" for request in requests)
    assert all(
        {
            tool["function"]["name"]
            for tool in request["tools"]
        } == {"search_video_chunk_evidence", "QueryDraft"}
        for request in requests
    )
    client.close()


def test_sdk_query_falls_back_to_lite_only_after_provider_failure() -> None:
    requested_models = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requested_models.append(payload["model"])
        if payload["model"] == "doubao-pro":
            return httpx.Response(503, json={"error": {"message": "unavailable"}})
        return httpx.Response(
            200,
            json=_completion(
                "QueryDraft",
                {
                    "plan_context": "检索完成",
                    "message": "没有匹配片段",
                    "selected_video_chunk_ids": [],
                },
            ),
        )

    client = Ark(
        api_key="test-ark-key",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    model = SDKQueryAgentModel(
        "doubao-pro",
        fallback_model="doubao-lite",
        request_timeout_seconds=3,
        max_output_tokens=400,
        client=client,
    )

    result = model.run_query(
        "检索", tools=[], result_type=QueryDraft, max_model_calls=1,
    )

    assert result.selected_video_chunk_ids == []
    assert requested_models == ["doubao-pro", "doubao-lite"]
    client.close()


def test_sdk_query_falls_back_from_deepseek_to_ark_with_same_request_state() -> None:
    deepseek_requests = []
    ark_requests = []

    def deepseek_respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        deepseek_requests.append(payload)
        return httpx.Response(200, json={
            "id": "deepseek-missing-tool",
            "model": "deepseek-flash",
            "created": 1,
            "object": "chat.completion",
            "choices": [{
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": "未按要求调用工具",
                    "tool_calls": None,
                },
            }],
        })

    def ark_respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        ark_requests.append(payload)
        return httpx.Response(
            200,
            json=_completion(
                "QueryDraft",
                {
                    "plan_context": "Lite 完成草稿",
                    "message": "没有匹配片段",
                    "selected_video_chunk_ids": [],
                },
            ),
        )

    deepseek_http = httpx.Client(transport=httpx.MockTransport(deepseek_respond))
    deepseek_client = OpenAI(
        api_key="test-deepseek-key",
        base_url="https://api.deepseek.test/v1",
        max_retries=0,
        http_client=deepseek_http,
    )
    ark_http = httpx.Client(transport=httpx.MockTransport(ark_respond))
    ark_client = Ark(
        api_key="test-ark-key",
        max_retries=0,
        http_client=ark_http,
    )
    model = SDKQueryAgentModel(
        "deepseek-flash",
        provider="deepseek",
        base_url="https://api.deepseek.test/v1",
        api_key="test-deepseek-key",
        fallback_model="doubao-lite",
        fallback_provider="ark",
        request_timeout_seconds=3,
        max_output_tokens=400,
        client=deepseek_client,
        fallback_client=ark_client,
    )

    result = model.run_query(
        "检索", tools=[], result_type=QueryDraft, max_model_calls=1,
    )

    assert result.selected_video_chunk_ids == []
    assert len(deepseek_requests) == len(ark_requests) == 1
    assert deepseek_requests[0]["model"] == "deepseek-flash"
    assert deepseek_requests[0]["thinking"] == {"type": "disabled"}
    assert deepseek_requests[0]["tool_choice"] == "required"
    assert ark_requests[0]["model"] == "doubao-lite"
    assert ark_requests[0]["tool_choice"] == "required"
    assert ark_requests[0]["messages"] == deepseek_requests[0]["messages"]
    assert [
        item["function"]["name"] for item in ark_requests[0]["tools"]
    ] == [
        item["function"]["name"] for item in deepseek_requests[0]["tools"]
    ]
    deepseek_client.close()
    ark_client.close()


def test_sdk_query_cross_provider_fallback_keeps_completed_tool_result() -> None:
    deepseek_requests = []
    ark_requests = []
    searches = []

    def retrieve(query: str) -> dict:
        searches.append(query)
        return {"hits": [{"video_chunk_id": "chunk-real"}]}

    def deepseek_respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        deepseek_requests.append(payload)
        if len(deepseek_requests) == 1:
            return httpx.Response(
                200,
                json=_completion("search_video_chunk_evidence", {"query": "KTV"}),
            )
        return httpx.Response(200, json={
            "id": "deepseek-missing-draft",
            "model": "deepseek-flash",
            "created": 1,
            "object": "chat.completion",
            "choices": [{
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": "没有提交 QueryDraft",
                    "tool_calls": None,
                },
            }],
        })

    def ark_respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        ark_requests.append(payload)
        return httpx.Response(
            200,
            json=_completion(
                "QueryDraft",
                {
                    "plan_context": "Lite 接续检索结果",
                    "message": "找到了 KTV 片段",
                    "selected_video_chunk_ids": ["chunk-real"],
                },
            ),
        )

    deepseek_client = OpenAI(
        api_key="test-deepseek-key",
        base_url="https://api.deepseek.test/v1",
        max_retries=0,
        http_client=httpx.Client(
            transport=httpx.MockTransport(deepseek_respond),
        ),
    )
    ark_client = Ark(
        api_key="test-ark-key",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(ark_respond)),
    )
    model = SDKQueryAgentModel(
        "deepseek-flash",
        provider="deepseek",
        base_url="https://api.deepseek.test/v1",
        api_key="test-deepseek-key",
        fallback_model="doubao-lite",
        fallback_provider="ark",
        request_timeout_seconds=3,
        max_output_tokens=400,
        client=deepseek_client,
        fallback_client=ark_client,
    )

    result = model.run_query(
        "检索 KTV",
        tools=[_tool(retrieve)],
        result_type=QueryDraft,
        max_model_calls=2,
    )

    assert result.selected_video_chunk_ids == ["chunk-real"]
    assert searches == ["KTV"]
    assert len(deepseek_requests) == 2
    assert len(ark_requests) == 1
    assert ark_requests[0]["messages"] == deepseek_requests[1]["messages"]
    assert [message["role"] for message in ark_requests[0]["messages"]] == [
        "user", "assistant", "tool",
    ]
    assert "chunk-real" in ark_requests[0]["messages"][2]["content"]
    deepseek_client.close()
    ark_client.close()


def test_sdk_query_falls_back_in_place_when_pro_omits_structured_draft(
    caplog,
) -> None:
    requested_models = []
    searches = []

    def retrieve(query: str) -> dict:
        searches.append(query)
        return {"hits": [{"video_chunk_id": "chunk-real"}]}

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requested_models.append(payload["model"])
        if len(requested_models) == 1:
            return httpx.Response(
                200,
                json=_completion(
                    "search_video_chunk_evidence", {"query": "KTV"}
                ),
            )
        if payload["model"] == "doubao-pro":
            return httpx.Response(200, json={
                "id": "completion-pro-invalid-tool",
                "model": "doubao-pro",
                "created": 1,
                "object": "chat.completion",
                "choices": [{
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "id": "call-invalid",
                            "type": "function",
                            "function": {
                                "name": "QueryDraft",
                                "arguments": "{",
                            },
                        }],
                    },
                }],
            })
        return httpx.Response(
            200,
            json=_completion(
                "QueryDraft",
                {
                    "plan_context": "Lite 补齐结构化草稿",
                    "message": "找到了 KTV 片段",
                    "selected_video_chunk_ids": ["chunk-real"],
                },
            ),
        )

    client = Ark(
        api_key="test-ark-key",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    model = SDKQueryAgentModel(
        "doubao-pro",
        fallback_model="doubao-lite",
        request_timeout_seconds=3,
        max_output_tokens=400,
        client=client,
    )

    with caplog.at_level(logging.WARNING, logger="gateway.agent_model"):
        result = model.run_query(
            "检索 KTV",
            tools=[_tool(retrieve)],
            result_type=QueryDraft,
            max_model_calls=2,
        )

    assert result.selected_video_chunk_ids == ["chunk-real"]
    assert requested_models == ["doubao-pro", "doubao-pro", "doubao-lite"]
    assert searches == ["KTV"]
    assert "error_type=MissingStructuredQueryToolCallError" in caplog.text
    client.close()


def test_sdk_query_stops_after_lite_also_omits_required_tool() -> None:
    requested_models = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requested_models.append(payload["model"])
        return httpx.Response(200, json={
            "id": f"completion-{payload['model']}",
            "model": payload["model"],
            "created": 1,
            "object": "chat.completion",
            "choices": [{
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": "没有调用必需工具。",
                    "tool_calls": None,
                },
            }],
        })

    client = Ark(
        api_key="test-ark-key",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    model = SDKQueryAgentModel(
        "doubao-pro",
        fallback_model="doubao-lite",
        request_timeout_seconds=3,
        max_output_tokens=400,
        client=client,
    )

    with pytest.raises(MissingStructuredQueryToolCallError):
        model.run_query(
            "检索", tools=[], result_type=QueryDraft, max_model_calls=1,
        )

    assert requested_models == ["doubao-pro", "doubao-lite"]
    client.close()


def test_sdk_query_compacts_complete_messages_before_second_model_request() -> None:
    requests = []
    searches = []
    large_query = "下雨场景" + "x" * 3_000

    def retrieve(query: str) -> dict:
        searches.append(query)
        return {"query": query, "hits": []}

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 2:
            tool_messages = [
                message
                for message in payload["messages"]
                if message["role"] == "tool"
            ]
            assert json.loads(tool_messages[-1]["content"])[
                "context_window"
            ] == "tool_result_released"
            return httpx.Response(200, json=_completion(
                "QueryDraft",
                {
                    "plan_context": "已完成有界检索",
                    "message": "未找到候选",
                    "selected_video_chunk_ids": [],
                },
            ))
        return httpx.Response(
            200,
            json=_completion("search_video_chunk_evidence", {"query": large_query}),
        )

    client = Ark(
        api_key="test-ark-key",
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    model = SDKQueryAgentModel(
        "ark-test", request_timeout_seconds=3, max_output_tokens=400,
        max_input_chars=5_000, client=client,
    )

    result = model.run_query(
        "检索", tools=[_tool(retrieve)], result_type=QueryDraft, max_model_calls=3,
    )

    assert result.selected_video_chunk_ids == []
    assert len(requests) == 2
    assert searches == [large_query]
    client.close()


def test_sdk_query_budget_stops_before_extra_model_request() -> None:
    requests = []

    def retrieve(query: str) -> dict:
        return {"query": query, "hits": []}

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=_completion("search_video_chunk_evidence", {"query": "再找"}),
        )

    client = Ark(
        api_key="test-ark-key",
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    model = SDKQueryAgentModel(
        "ark-test", request_timeout_seconds=3, max_output_tokens=400, client=client
    )
    with pytest.raises(ModelCallLimitExceededError):
        model.run_query(
            "检索", tools=[_tool(retrieve)], result_type=QueryDraft, max_model_calls=1
        )
    assert len(requests) == 1
    client.close()


def test_sdk_query_continues_past_previous_model_call_limit() -> None:
    requests = []
    searches = []

    def retrieve(query: str) -> dict:
        searches.append(query)
        return {"query": query, "hits": []}

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        turn = len(requests)
        if turn > 13:
            return httpx.Response(200, json=_completion(
                "QueryDraft",
                {
                    "plan_context": "已完成多轮检索",
                    "message": "无匹配片段",
                    "selected_video_chunk_ids": [],
                },
                call_id=f"call-{turn}",
            ))
        return httpx.Response(
            200,
            json=_completion(
                "search_video_chunk_evidence",
                {"query": f"第 {turn} 轮"},
                call_id=f"call-{turn}",
            ),
        )

    client = Ark(
        api_key="test-ark-key",
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    model = SDKQueryAgentModel(
        "ark-test", request_timeout_seconds=3, max_output_tokens=400, client=client
    )
    result = model.run_query(
        "检索", tools=[_tool(retrieve)], result_type=QueryDraft,
        max_model_calls=None,
        request_timeout_seconds=30,
    )

    assert result.selected_video_chunk_ids == []
    assert len(requests) == 14
    assert len(searches) == 13
    client.close()


def test_sdk_query_propagates_tool_stop_without_retry() -> None:
    requests = []

    class ExecutionStopped(RuntimeError):
        pass

    def retrieve(query: str) -> dict:
        raise ExecutionStopped("execution cancelled")

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=_completion("search_video_chunk_evidence", {"query": "找"}),
        )

    client = Ark(
        api_key="test-ark-key",
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    model = SDKQueryAgentModel(
        "ark-test", fallback_model="ark-lite",
        request_timeout_seconds=3, max_output_tokens=400, client=client,
    )
    with pytest.raises(ExecutionStopped, match="cancelled"):
        model.run_query(
            "检索", tools=[_tool(retrieve)], result_type=QueryDraft, max_model_calls=3
        )
    assert len(requests) == 1
    client.close()


def test_sdk_query_does_not_accept_truncated_structured_tool_call() -> None:
    client = Ark(
        api_key="test-ark-key",
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json=_completion(
                        "QueryDraft",
                        {
                            "plan_context": "partial",
                            "message": "partial",
                            "selected_video_chunk_ids": [],
                        },
                        reason="length",
                    ),
                )
            )
        ),
    )
    model = SDKQueryAgentModel(
        "ark-test", request_timeout_seconds=3, max_output_tokens=400, client=client
    )
    with pytest.raises(RuntimeError, match="length"):
        model.run_query("检索", tools=[], result_type=QueryDraft, max_model_calls=2)
    client.close()


def test_sdk_query_checks_control_after_model_before_running_tools() -> None:
    checks = 0
    searches = []

    def check_control():
        nonlocal checks
        checks += 1
        if checks == 3:
            raise RuntimeError("kick deadline exceeded")

    def retrieve(query: str) -> dict:
        searches.append(query)
        return {"hits": []}

    client = Ark(
        api_key="test-ark-key",
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json=_completion("search_video_chunk_evidence", {"query": "找"}),
                )
            )
        ),
    )
    model = SDKQueryAgentModel(
        "ark-test", request_timeout_seconds=3, max_output_tokens=400, client=client
    )
    with pytest.raises(RuntimeError, match="deadline exceeded"):
        model.run_query(
            "检索",
            tools=[_tool(retrieve)],
            result_type=QueryDraft,
            max_model_calls=3,
            check_control=check_control,
        )
    assert checks == 3
    assert searches == []
    client.close()


def test_sdk_query_preserves_ark_ak_sk_authentication(monkeypatch) -> None:
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    authorizations = []

    def respond(request):
        authorizations.append(request.headers["Authorization"])
        return httpx.Response(
            200,
            json=_completion(
                "QueryDraft",
                {
                    "plan_context": "检索完成",
                    "message": "无匹配片段",
                    "selected_video_chunk_ids": [],
                },
            ),
        )

    client = Ark(
        ak="test-ak",
        sk="test-sk",
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    monkeypatch.setattr(client, "_get_endpoint_sts_token", lambda *args: "test-sts")
    model = SDKQueryAgentModel(
        "ark-test", request_timeout_seconds=3, max_output_tokens=400, client=client
    )
    result = model.run_query("检索", tools=[], result_type=QueryDraft, max_model_calls=1)
    assert result.selected_video_chunk_ids == []
    assert authorizations == ["Bearer test-sts"]
    client.close()


def test_sdk_query_default_client_is_owned_per_invocation(monkeypatch) -> None:
    clients = []
    inputs = []

    def respond(request):
        inputs.append(json.loads(request.content)["messages"])
        return httpx.Response(
            200,
            json=_completion(
                "QueryDraft",
                {
                    "plan_context": "检索完成",
                    "message": "无匹配片段",
                    "selected_video_chunk_ids": [],
                },
            ),
        )

    def new_client(**options):
        assert options == {"timeout": 3, "max_retries": 0}
        transport = httpx.Client(transport=httpx.MockTransport(respond))
        clients.append(transport)
        return Ark(api_key="test-key", http_client=transport)

    monkeypatch.setattr(Ark, "volc", new_client)
    model = SDKQueryAgentModel(
        "ark-test", request_timeout_seconds=3, max_output_tokens=400
    )
    for prompt in ("会话一", "会话二"):
        model.run_query(prompt, tools=[], result_type=QueryDraft, max_model_calls=1)
    assert len(clients) == 2
    assert all(client.is_closed for client in clients)
    assert inputs == [
        [{"role": "user", "content": "会话一"}],
        [{"role": "user", "content": "会话二"}],
    ]


@pytest.mark.parametrize("provider", ["deepseek", "ollama", "llama_cpp"])
def test_sdk_query_openai_compatible_providers_use_their_token_parameter(provider):
    requests = []

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["max_tokens"] == 400
        assert "max_completion_tokens" not in payload
        return httpx.Response(
            200,
            json=_completion(
                "QueryDraft",
                {
                    "plan_context": "完成",
                    "message": "无片段",
                    "selected_video_chunk_ids": [],
                },
            ),
        )

    client = OpenAI(
        api_key="test-key",
        base_url="http://localhost:9000/v1",
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    model = SDKQueryAgentModel(
        "test-local",
        provider=provider,
        client=client,
        request_timeout_seconds=3,
        max_output_tokens=400,
    )
    result = model.run_query("检索", tools=[], result_type=QueryDraft, max_model_calls=1)
    assert result.selected_video_chunk_ids == []
    assert len(requests) == 1
    if provider == "deepseek":
        assert requests[0]["thinking"] == {"type": "disabled"}
    else:
        assert "thinking" not in requests[0]
    client.close()
