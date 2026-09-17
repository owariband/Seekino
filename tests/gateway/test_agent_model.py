import json
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from arkruntime import Ark
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain_core.tools import StructuredTool
from openai import APIConnectionError, APIError, OpenAI
from pydantic import BaseModel, ConfigDict

from gateway import agent_model as agent_model_module
from gateway.agent_model import (
    DeepSeekFunctionCallingError,
    DeepSeekFunctionCallingModel,
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
    function_name: str = "submit_video_item",
    call_type: str = "function",
):
    rendered_arguments = (
        arguments if isinstance(arguments, str) else json.dumps(arguments)
    )
    return SimpleNamespace(
        id=call_id,
        type=call_type,
        function=SimpleNamespace(
            name=function_name,
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
    max_model_calls: int = 4,
    handle_text=lambda _text: None,
    handle_tool_call=lambda _item: "accepted",
    request_timeout_seconds: float | None = None,
    check_control=None,
) -> None:
    model.run_agent_loop(
        "根据审核后的证据回答",
        function_name="submit_video_item",
        function_description="Submit one approved video item.",
        result_type=VideoItem,
        max_model_calls=max_model_calls,
        handle_text=handle_text,
        handle_tool_call=handle_tool_call,
        request_timeout_seconds=request_timeout_seconds,
        check_control=check_control,
    )


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
        handle_tool_call=handle_tool,
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
        handle_tool_call=lambda _item: pytest.fail("tool handler must not run"),
    )

    assert texts == ["没有需要展示的视频，直接回答。"]
    assert len(completions.calls) == 1
    assert completions.calls[0]["tool_choice"] == "auto"


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
        handle_tool_call=lambda item: (
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
            handle_tool_call=lambda item: tools.append(item) or "accepted",
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
            handle_tool_call=lambda item: events.append(("tool", item)) or "accepted",
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
            [_tool_call(_video_item(), function_name="unexpected")],
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
            handle_tool_call=lambda item: events.append(item) or "accepted",
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
            handle_tool_call=lambda item: (
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
            handle_tool_call=lambda item: (
                events.append(("tool", item.video_chunk_id)) or "accepted"
            ),
        )

    assert events == [("text", "first"), ("tool", "chunk-1")]
    assert len(completions.calls) == 1


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
            handle_tool_call=lambda item: tools.append(item) or "accepted",
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
        _run_agent(model, handle_tool_call=fail)

    assert len(completions.calls) == 1


def test_deepseek_agent_loop_rejects_non_string_tool_result() -> None:
    model, completions = _function_model(
        _agent_response(
            None,
            calls=[_tool_call(_video_item())],
            finish_reason="tool_calls",
        ),
    )

    with pytest.raises(TypeError, match="must return str"):
        _run_agent(
            model,
            handle_tool_call=lambda _item: {"accepted": True},
        )

    assert len(completions.calls) == 1


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


def _completion(name: str, arguments: dict, *, reason: str = "tool_calls") -> dict:
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
                            "id": "call-1",
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
    client.close()


def test_sdk_query_checks_complete_messages_before_second_model_request() -> None:
    requests = []
    searches = []
    large_query = "下雨场景" + "x" * 3_000

    def retrieve(query: str) -> dict:
        searches.append(query)
        return {"query": query, "hits": []}

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
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

    with pytest.raises(ModelInputTooLargeError, match="message character budget"):
        model.run_query(
            "检索", tools=[_tool(retrieve)], result_type=QueryDraft, max_model_calls=3,
        )

    assert len(requests) == 1
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
        "ark-test", request_timeout_seconds=3, max_output_tokens=400, client=client
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
    client.close()
