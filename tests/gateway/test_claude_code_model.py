from __future__ import annotations

import asyncio
import contextvars
import json
import threading
from collections import deque
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
)
from langchain_core.tools import StructuredTool
from langchain_core.tools.base import ToolException
from pydantic import BaseModel

from gateway.agent_model.errors import (
    AgentModelTimeoutError,
    MissingRequiredFunctionCallError,
    MissingStructuredQueryDraftError,
    ModelFailureReason,
    ModelInputTooLargeError,
)
from gateway.claude_code import ClaudeAgentModel, ClaudeAgentModelError
from gateway.claude_code.tools import ToolInvocationState, _build_sdk_tool
from gateway.agent_model.context import ContextWindowPolicy
from gateway.usage import capture_model_usage


class _Answer(BaseModel):
    answer: str


def _result(
    *,
    text: str | None = None,
    structured: Any = None,
    turns: int = 1,
    is_error: bool = False,
    stop_reason: str | None = "end_turn",
    subtype: str | None = None,
    terminal_reason: str | None = None,
) -> ResultMessage:
    return ResultMessage(
        subtype=subtype or ("success" if not is_error else "error_during_execution"),
        duration_ms=10,
        duration_api_ms=8,
        is_error=is_error,
        num_turns=turns,
        session_id="session-test",
        stop_reason=stop_reason,
        result=text,
        structured_output=structured,
        usage={"input_tokens": 12, "output_tokens": 4},
        terminal_reason=terminal_reason,
    )


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text)], model="claude-test")


def _tool() -> StructuredTool:
    def lookup(query: str) -> str:
        return f"found: {query}"

    return StructuredTool.from_function(
        lookup,
        name="lookup",
        description="Look up a value.",
    )


class _FakeClient:
    def __init__(self, options: Any, batches: list[list[Any]]) -> None:
        self.options = options
        self.batches = deque(batches)
        self.prompts: list[Any] = []
        self.interrupted = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args: Any) -> bool:
        return False

    async def query(self, prompt) -> None:
        self.prompts.append(prompt if isinstance(prompt, str) else [message async for message in prompt])

    async def receive_response(self):
        for message in self.batches.popleft():
            yield message

    async def interrupt(self) -> None:
        self.interrupted = True


class _HangingClient(_FakeClient):
    def __init__(self, options: Any, batches: list[list[Any]]) -> None:
        super().__init__(options, batches)
        self.receiving = threading.Event()

    async def receive_response(self):
        self.receiving.set()
        await asyncio.Event().wait()
        if False:  # pragma: no cover - keeps this an async generator
            yield None


class _HangingConnectClient(_FakeClient):
    async def __aenter__(self):
        await asyncio.Event().wait()
        return self


def _model(client_factory, **overrides: Any) -> ClaudeAgentModel:
    values = {
        "model": "claude-test",
        "reasoning_effort": "high",
        "request_timeout_seconds": 2,
        "max_output_tokens": 2048,
        "max_input_chars": 10_000,
        "client_factory": client_factory,
    }
    values.update(overrides)
    return ClaudeAgentModel(**values)


def _context_policy(maximum_input_tokens: int) -> ContextWindowPolicy:
    return ContextWindowPolicy(
        context_window_tokens=maximum_input_tokens + 200,
        output_reserve_tokens=100,
        safety_margin_tokens=100,
        auto_compact_tokens=maximum_input_tokens,
        compact_target_tokens=max(1, maximum_input_tokens // 2),
    )


def test_structured_result_uses_sdk_json_schema_without_network() -> None:
    created: list[_FakeClient] = []

    def factory(options):
        client = _FakeClient(options, [[_result(structured={"answer": "ok"})]])
        created.append(client)
        return client

    model = _model(factory)

    result = model.run_structured(
        "answer with evidence",
        tools=[_tool()],
        result_type=_Answer,
        max_model_calls=3,
    )

    assert result == _Answer(answer="ok")
    options = created[0].options
    assert options.output_format["type"] == "json_schema"
    assert options.output_format["schema"]["properties"]["answer"]["type"] == "string"
    assert options.tools == []
    assert options.allowed_tools == ["mcp__seekino__lookup"]
    assert options.permission_mode == "dontAsk"
    assert options.setting_sources == []
    assert options.skills == []
    assert options.effort == "high"
    assert options.thinking is None


@pytest.mark.parametrize("structured", [False, True])
def test_native_entries_send_ordered_real_images_outside_text_budget(structured) -> None:
    created = []

    def factory(options):
        client = _FakeClient(options, [[_assistant("ok"), _result(text="ok", structured={"answer": "ok"})]])
        created.append(client)
        return client

    current = ["data:image/png;base64," + ("A" * 200_000 if index == 0 else f"image-{index}") for index in range(6)]
    history = [{"role": "user", "content": [
        {"type": "text", "text": "Earlier image"},
        {"type": "image_url", "image_url": {"url": "https://example.test/prior.png"}},
    ]}, {"role": "assistant", "content": "Earlier answer"}]
    model = _model(factory, max_input_chars=2_000, context_window_policy=_context_policy(30_000))
    options = dict(tools=[_tool()], max_model_calls=1, initial_messages=history, image_urls=current)
    if structured:
        assert model.run_structured("Current question", result_type=_Answer, **options).answer == "ok"
    else:
        emitted = []
        model.run_agent_loop("Current question", handle_text=emitted.append, **options)
        assert emitted == ["ok"]
    envelope, = created[0].prompts[0]
    assert envelope["type"] == "user" and envelope["message"]["role"] == "user"
    content = envelope["message"]["content"]
    sources = [block["source"] for block in content if block["type"] == "image"]
    assert sources[0] == {"type": "url", "url": "https://example.test/prior.png"}
    assert sources[1:] == [{"type": "base64", "media_type": "image/png", "data": url.split(",", 1)[1]} for url in current]
    assert all("data:image" not in block["text"] for block in content if block["type"] == "text")
    assert "Earlier answer" in content[0]["text"] and "Current question" in content[0]["text"]
    assert "imageCount" not in history[0] and len(history[0]["content"]) == 2


@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.parametrize("historical", [False, True])
def test_native_entries_reject_disabled_vision_before_creating_client(structured, historical) -> None:
    model = _model(lambda _options: pytest.fail("client must not be created"))
    model.supports_vision_input = False
    image = "data:image/png;base64,AAAA"
    options = dict(tools=[_tool()], max_model_calls=1)
    options.update({"initial_messages": [{"role": "user", "content": "Earlier", "imageUrls": [image]}]}
                   if historical else {"image_urls": [image]})
    with pytest.raises(ValueError, match="supports_vision_input=False"):
        if structured:
            model.run_structured("Question", result_type=_Answer, **options)
        else:
            model.run_agent_loop("Question", handle_text=lambda _text: None, **options)


@pytest.mark.parametrize("max_input_chars", [None, 32])
def test_feedback_round_preserves_images_and_leaves_long_prompt_to_sdk(max_input_chars) -> None:
    created = []

    def factory(options):
        answer = "answer " * 100
        client = _FakeClient(options, [[_assistant(answer), _result(text=answer)]])
        created.append(client)
        return client

    feedback_text = "Check the image again"
    feedback = iter([feedback_text, None])
    image = "data:image/png;base64,YQ=="
    _model(factory, max_input_chars=max_input_chars).run_agent_loop(
        "Describe the image", tools=[_tool()], max_model_calls=3, image_urls=[image],
        handle_text=lambda _text: None, stop_feedback=lambda: next(feedback),
    )
    assert len(created) == 2
    for client in created:
        envelope, = client.prompts[0]
        assert [block["source"] for block in envelope["message"]["content"] if block["type"] == "image"] == [
            {"type": "base64", "media_type": "image/png", "data": "YQ=="},
        ]
    assert feedback_text in created[1].prompts[0][0]["message"]["content"][0]["text"]


@pytest.mark.parametrize("structured", [False, True])
def test_native_entries_leave_six_images_to_sdk_context_management(structured) -> None:
    created = []

    def factory(options):
        client = _FakeClient(options, [[_assistant("ok"), _result(text="ok", structured={"answer": "ok"})]])
        created.append(client)
        return client

    model = _model(factory, context_window_policy=_context_policy(2_000))
    images = [f"https://example.test/{index}.png" for index in range(6)]
    options = dict(tools=[_tool()], max_model_calls=1, image_urls=images)
    if structured:
        assert model.run_structured("Question", result_type=_Answer, **options).answer == "ok"
    else:
        emitted = []
        model.run_agent_loop("Question", handle_text=emitted.append, **options)
        assert emitted == ["ok"]
    envelope, = created[0].prompts[0]
    assert [block["source"]["url"] for block in envelope["message"]["content"] if block["type"] == "image"] == images


@pytest.mark.parametrize("structured", [False, True])
def test_native_entries_leave_large_mcp_contract_to_sdk(structured) -> None:
    created: list[object] = []

    def lookup(value: str) -> str:
        return value

    tool = StructuredTool.from_function(
        lookup,
        name="lookup",
        description="x" * 9_000,
    )

    def factory(_options: Any) -> _FakeClient:
        created.append(object())
        return _FakeClient(_options, [[_assistant("ok"), _result(text="ok", structured={"answer": "ok"})]])

    model = _model(
        factory,
        context_window_policy=_context_policy(2_000),
    )
    if structured:
        assert model.run_structured(
            "short prompt", tools=[tool], result_type=_Answer, max_model_calls=1,
        ).answer == "ok"
    else:
        emitted = []
        model.run_agent_loop(
            "short prompt",
            tools=[tool],
            max_model_calls=1,
            handle_text=emitted.append,
        )
        assert emitted == ["ok"]

    assert len(created) == 1


def test_native_loop_accepts_small_request() -> None:
    created: list[_FakeClient] = []

    def factory(options: Any) -> _FakeClient:
        client = _FakeClient(
            options,
            [[_assistant("ok"), _result(text="ok")]],
        )
        created.append(client)
        return client

    model = _model(
        factory,
        context_window_policy=_context_policy(4_000),
    )
    emitted: list[str] = []
    model.run_agent_loop(
        "short prompt",
        tools=[_tool()],
        max_model_calls=1,
        handle_text=emitted.append,
    )

    assert emitted == ["ok"]
    assert len(created) == 1


def test_agent_loop_emits_text_and_applies_stop_feedback() -> None:
    created: list[_FakeClient] = []
    batches = deque(
        [
            [_assistant("draft"), _result(text="draft")],
            [_assistant("final"), _result(text="final")],
        ]
    )

    def factory(options):
        client = _FakeClient(options, [batches.popleft()])
        created.append(client)
        return client

    feedback = iter(["add the missing detail", None])
    emitted: list[str] = []
    records = []
    with capture_model_usage(records.append):
        _model(factory).run_agent_loop(
            "question",
            tools=[_tool()],
            max_model_calls=4,
            handle_text=emitted.append,
            stop_feedback=lambda: next(feedback),
        )

    assert emitted == ["draft", "final"]
    assert created[0].prompts == ["question"]
    follow_up = created[1].prompts[0]
    payload = json.loads(follow_up.split("\n", 1)[1])
    assert payload == {
        "original_instruction": "question",
        "previous_response": "draft",
        "feedback": "add the missing detail",
    }
    assert created[0].options.max_turns == 4
    assert created[0].options.resume is None
    assert created[1].options.max_turns == 3
    assert created[1].options.resume is None
    assert all(
        client.options.extra_args == {"no-session-persistence": None}
        for client in created
    )
    assert len(records) == 2
    assert len({record.call_id for record in records}) == 2
    assert all(record.granularity == "sdk_turn_run" for record in records)
    assert all(record.usage_source == "claude_result_usage" for record in records)
    assert [record.usage.total_tokens for record in records] == [16, 16]


def test_agent_loop_reduces_follow_up_turn_budget_and_rejects_overrun() -> None:
    created: list[_FakeClient] = []
    batches = deque(
        [
            [_assistant("draft"), _result(text="draft", turns=2)],
            [_assistant("late"), _result(text="late", turns=2)],
        ]
    )

    def factory(options):
        client = _FakeClient(options, [batches.popleft()])
        created.append(client)
        return client

    feedback = iter(["revise", None])
    with pytest.raises(ClaudeAgentModelError) as captured:
        _model(factory).run_agent_loop(
            "question",
            tools=[_tool()],
            max_model_calls=3,
            handle_text=lambda _text: None,
            stop_feedback=lambda: next(feedback),
        )

    assert captured.value.reason is ModelFailureReason.LOOP_LIMIT
    assert created[1].options.max_turns == 1
    assert created[1].options.resume is None


def test_invocation_disables_session_persistence_without_redirecting_auth_root() -> None:
    created: list[_FakeClient] = []

    def factory(options):
        client = _FakeClient(options, [[_assistant("ok"), _result(text="ok")]])
        created.append(client)
        return client

    _model(
        factory,
        env={"CLAUDE_CONFIG_DIR": "/original/claude-config"},
    ).run_agent_loop(
        "question",
        tools=[_tool()],
        max_model_calls=1,
        handle_text=lambda _text: None,
    )

    environment = created[0].options.env
    assert environment["CLAUDE_CONFIG_DIR"] == "/original/claude-config"
    assert "CLAUDE_SECURESTORAGE_CONFIG_DIR" not in environment
    assert created[0].options.extra_args == {"no-session-persistence": None}


def test_max_turns_result_has_loop_limit_reason() -> None:
    def factory(options):
        return _FakeClient(
            options,
            [[
                _result(
                    is_error=True,
                    stop_reason=None,
                    subtype="error_max_turns",
                    terminal_reason="max_turns",
                )
            ]],
        )

    with pytest.raises(ClaudeAgentModelError) as captured:
        _model(factory).run_agent_loop(
            "question",
            tools=[_tool()],
            max_model_calls=1,
            handle_text=lambda _text: None,
        )

    assert captured.value.reason is ModelFailureReason.LOOP_LIMIT


def test_invalid_structured_result_is_not_classified_as_a_missing_tool() -> None:
    def factory(options):
        return _FakeClient(options, [[_result(structured={"answer": 3})]])

    records = []
    with capture_model_usage(records.append), pytest.raises(
        MissingStructuredQueryDraftError,
    ) as captured:
        _model(factory).run_structured(
            "answer with evidence",
            tools=[_tool()],
            result_type=_Answer,
            max_model_calls=1,
        )

    assert captured.value.reason is ModelFailureReason.INVALID_MODEL_RESPONSE
    assert len(records) == 1
    # ResultMessage succeeded; business schema validation happens afterwards.
    assert records[0].status == "completed"
    assert records[0].usage.total_tokens == 16


def test_required_tool_gets_one_correction_then_fails() -> None:
    def factory(options):
        return _FakeClient(
            options,
            [
                [_assistant("first"), _result(text="first")],
                [_assistant("second"), _result(text="second")],
            ],
        )

    with pytest.raises(MissingRequiredFunctionCallError, match="stopped twice"):
        _model(factory).run_agent_loop(
            "question",
            tools=[_tool()],
            max_model_calls=3,
            handle_text=lambda _text: None,
            require_tool_call=True,
        )


def test_required_tool_budget_exhaustion_has_loop_limit_reason() -> None:
    def factory(options):
        return _FakeClient(
            options,
            [[_assistant("no tool"), _result(text="no tool")]],
        )

    with pytest.raises(MissingRequiredFunctionCallError) as captured:
        _model(factory).run_agent_loop(
            "question",
            tools=[_tool()],
            max_model_calls=1,
            handle_text=lambda _text: None,
            require_tool_call=True,
        )

    assert captured.value.reason is ModelFailureReason.LOOP_LIMIT


def test_sdk_tool_invokes_langchain_with_full_tool_call_and_maps_error() -> None:
    calls: list[dict[str, Any]] = []

    class FakeTool:
        name = "lookup"
        description = "Look up a value."

        @staticmethod
        def get_input_jsonschema():
            return {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            }

        @staticmethod
        def invoke(call):
            calls.append(call)
            from langchain_core.messages import ToolMessage

            return ToolMessage(
                "duplicate item",
                tool_call_id=call["id"],
                name=call["name"],
                status="error",
            )

    state = ToolInvocationState()
    sdk_definition = _build_sdk_tool(FakeTool(), state)  # type: ignore[arg-type]

    result = asyncio.run(sdk_definition.handler({"query": "tomori"}))

    assert state.count == 1
    assert calls[0]["type"] == "tool_call"
    assert calls[0]["name"] == "lookup"
    assert calls[0]["args"] == {"query": "tomori"}
    assert calls[0]["id"].startswith("claude-")
    assert result == {
        "content": [{"type": "text", "text": "duplicate item"}],
        "is_error": True,
    }


def test_tool_exception_becomes_error_result() -> None:
    def fail(value: str) -> str:
        raise ToolException(f"bad {value}")

    item = StructuredTool.from_function(
        fail,
        name="fail",
        description="Fail safely.",
        handle_tool_error=True,
    )
    definition = _build_sdk_tool(item, ToolInvocationState())

    result = asyncio.run(definition.handler({"value": "input"}))

    assert result["is_error"] is True
    assert result["content"][0]["text"] == "bad input"


def test_options_expose_only_seekino_mcp_tools() -> None:
    created: list[_FakeClient] = []

    def factory(options):
        client = _FakeClient(options, [[_assistant("ok"), _result(text="ok")]])
        created.append(client)
        return client

    _model(factory).run_agent_loop(
        "question",
        tools=[_tool()],
        max_model_calls=1,
        handle_text=lambda _text: None,
    )
    options = created[0].options

    assert options.tools == []
    assert options.allowed_tools == ["mcp__seekino__lookup"]
    assert options.can_use_tool is None


def test_auth_modes_clear_competing_inherited_credentials() -> None:
    factory = lambda options: _FakeClient(options, [])
    api_model = _model(factory, auth_mode="api_key", api_key="secret")
    local_model = _model(factory, auth_mode="local_subscription")
    bridge = __import__(
        "gateway.claude_code.tools", fromlist=["build_tool_bridge"]
    ).build_tool_bridge([_tool()])

    assert api_model._options(bridge, max_model_calls=1).env["ANTHROPIC_API_KEY"] == "secret"
    api_env = api_model._options(bridge, max_model_calls=1).env
    local_env = local_model._options(bridge, max_model_calls=1).env
    assert api_env["ANTHROPIC_AUTH_TOKEN"] == ""
    assert api_env["ANTHROPIC_BASE_URL"] == ""
    assert api_env["CLAUDE_CODE_OAUTH_TOKEN"] == ""
    assert all(local_env[name] == "" for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
    ))


def test_none_reasoning_explicitly_disables_thinking() -> None:
    model = _model(
        lambda options: _FakeClient(options, []),
        reasoning_effort="none",
    )
    bridge = __import__(
        "gateway.claude_code.tools", fromlist=["build_tool_bridge"]
    ).build_tool_bridge([_tool()])

    options = model._options(bridge, max_model_calls=1)

    assert options.effort is None
    assert options.thinking == {"type": "disabled"}


def test_cancelling_sync_tool_does_not_wait_for_worker_thread() -> None:
    release = threading.Event()
    request_context = contextvars.ContextVar("request_context", default="missing")
    observed: list[str] = []

    def block(value: str) -> str:
        observed.append(request_context.get())
        release.wait()
        return value

    item = StructuredTool.from_function(
        block,
        name="block",
        description="Block until released.",
    )
    definition = _build_sdk_tool(item, ToolInvocationState())

    async def invoke_and_cancel() -> None:
        request_context.set("agent-query")
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(definition.handler({"value": "input"}), timeout=0.03)

    try:
        asyncio.run(invoke_and_cancel())
    finally:
        release.set()

    assert observed == ["agent-query"]


def test_timeout_interrupts_the_sdk_client() -> None:
    created: list[_HangingClient] = []

    def factory(options):
        client = _HangingClient(options, [])
        created.append(client)
        return client

    with pytest.raises(AgentModelTimeoutError, match="deadline expired"):
        _model(factory, request_timeout_seconds=0.03).run_agent_loop(
            "question",
            tools=[_tool()],
            max_model_calls=1,
            handle_text=lambda _text: None,
        )

    assert created[0].interrupted is True


def test_close_interrupts_an_active_sdk_client() -> None:
    created: list[_HangingClient] = []
    client_created = threading.Event()

    def factory(options):
        client = _HangingClient(options, [])
        created.append(client)
        client_created.set()
        return client

    model = _model(factory, request_timeout_seconds=5)
    failures: list[BaseException] = []

    def invoke() -> None:
        try:
            model.run_agent_loop(
                "question",
                tools=[_tool()],
                max_model_calls=1,
                handle_text=lambda _text: None,
            )
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=invoke)
    worker.start()
    assert client_created.wait(timeout=1)
    assert created[0].receiving.wait(timeout=1)

    model.close()
    worker.join(timeout=2)

    assert worker.is_alive() is False
    assert created[0].interrupted is True
    assert len(failures) == 1
    assert "closed" in str(failures[0]).lower()


def test_timeout_covers_sdk_client_initialization() -> None:
    def factory(options):
        return _HangingConnectClient(options, [])

    with pytest.raises(AgentModelTimeoutError, match="deadline expired"):
        _model(factory, request_timeout_seconds=0.03).run_agent_loop(
            "question",
            tools=[_tool()],
            max_model_calls=1,
            handle_text=lambda _text: None,
        )


def test_result_usage_is_reported_to_the_shared_tracker() -> None:
    records = []

    def factory(options):
        return _FakeClient(options, [[_assistant("ok"), _result(text="ok")]])

    with capture_model_usage(records.append):
        _model(factory).run_agent_loop(
            "question",
            tools=[_tool()],
            max_model_calls=1,
            handle_text=lambda _text: None,
        )

    assert len(records) == 1
    assert records[0].provider == "claude_code"
    assert records[0].model == "claude-test"
    assert records[0].status == "completed"
    assert records[0].granularity == "sdk_turn_run"
    assert records[0].usage_source == "claude_result_usage"
    assert records[0].usage_scope == "main_loop"
    assert records[0].usage is not None
    assert records[0].usage.input_tokens == 12
    assert records[0].usage.output_tokens == 4
    assert records[0].usage.total_tokens == 16


def test_usage_includes_cache_creation_and_cache_read_tokens() -> None:
    records = []
    result = _result(text="ok")
    result.usage = {
        "input_tokens": 12,
        "output_tokens": 4,
        "cache_creation_input_tokens": 3,
        "cache_read_input_tokens": 5,
        "cache_creation": {"ephemeral_5m_input_tokens": 1, "ephemeral_1h_input_tokens": 2},
    }

    def factory(options):
        return _FakeClient(options, [[_assistant("ok"), result]])

    with capture_model_usage(records.append):
        _model(factory).run_agent_loop(
            "question",
            tools=[_tool()],
            max_model_calls=1,
            handle_text=lambda _text: None,
        )

    usage = records[0].usage
    assert usage is not None
    assert usage.input_tokens == 20
    assert usage.output_tokens == 4
    assert usage.total_tokens == 24
    assert usage.cached_input_tokens == 5
    assert usage.cache_write_input_tokens == 3
    assert usage.cache_write_5m_input_tokens == 1 and usage.cache_write_1h_input_tokens == 2


def test_sdk_model_usage_keeps_actual_models_and_scoped_cost_without_reusing_run_total() -> None:
    records = []
    result = _result(text="ok")
    result.total_cost_usd = 0.012
    result.model_usage = {
        "sonnet-alias": {"canonicalModel": "actual-sonnet", "inputTokens": 12, "outputTokens": 4,
                         "cacheReadInputTokens": 5, "cacheCreationInputTokens": 3, "costUSD": 0.01},
        "actual-haiku": {"inputTokens": 3, "outputTokens": 2, "cacheReadInputTokens": 2,
                         "cacheCreationInputTokens": 1, "costUSD": 0.002},
    }
    with capture_model_usage(records.append):
        _model(lambda options: _FakeClient(options, [[_assistant("ok"), result]])).run_agent_loop(
            "question", tools=[_tool()], max_model_calls=1, handle_text=lambda _text: None,
        )
    assert len(records) == 1
    sonnet, haiku = records[0].sdk_model_usage
    assert sonnet.model == "actual-sonnet" and haiku.model == "actual-haiku"
    assert sonnet.usage.input_tokens == 20 and sonnet.usage.total_tokens == 24
    assert sonnet.usage.cached_input_tokens == 5 and sonnet.usage.cache_write_input_tokens == 3
    assert haiku.usage.total_tokens == 8
    assert sonnet.cost_usd == 0.01 and haiku.cost_usd == 0.002
    assert records[0].usage.total_tokens == 16  # Result usage only describes the main loop.
    assert records[0].usage_scope == "main_loop"


def test_result_cost_without_model_breakdown_is_not_attached_to_main_loop_tokens() -> None:
    records = []
    result = _result(text="ok")
    result.total_cost_usd = 0.02
    with capture_model_usage(records.append):
        _model(lambda options: _FakeClient(options, [[_assistant("ok"), result]])).run_agent_loop(
            "question", tools=[_tool()], max_model_calls=1, handle_text=lambda _text: None,
        )
    assert records[0].sdk_model_usage == ()
    assert records[0].usage_scope == "main_loop"
    assert records[0].usage.total_tokens == 16


def test_invalid_sdk_model_usage_and_cost_remain_unknown_without_breaking_result() -> None:
    records = []
    result = _result(text="ok")
    result.model_usage = {
        "actual": {"inputTokens": 2, "outputTokens": 1, "cacheReadInputTokens": -1,
                   "cacheCreationInputTokens": 0, "costUSD": float("nan")},
    }
    with capture_model_usage(records.append):
        _model(lambda options: _FakeClient(options, [[_assistant("ok"), result]])).run_agent_loop(
            "question", tools=[_tool()], max_model_calls=1, handle_text=lambda _text: None,
        )
    assert records[0].status == "completed"
    assert records[0].sdk_model_usage[0].usage is None
    assert records[0].sdk_model_usage[0].cost_usd is None


@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.parametrize("max_input_chars", [None, 5])
def test_native_entries_leave_long_prompt_to_sdk_and_enforce_close(structured, max_input_chars) -> None:
    created = []

    def factory(options):
        client = _FakeClient(options, [[_assistant("ok"), _result(text="ok", structured={"answer": "ok"})]])
        created.append(client)
        return client

    model = _model(factory, max_input_chars=max_input_chars)
    prompt = "long evidence " * 20_000
    options = dict(tools=[_tool()], max_model_calls=1, max_input_chars=5)
    if structured:
        assert model.run_structured(prompt, result_type=_Answer, **options).answer == "ok"
    else:
        emitted = []
        model.run_agent_loop(
            prompt, handle_text=emitted.append, **options,
        )
        assert emitted == ["ok"]
    assert created[0].prompts == [prompt]

    model.close()
    model.close()
    with pytest.raises(RuntimeError, match="closed"):
        model.run_agent_loop(
            "short",
            tools=[_tool()],
            max_model_calls=1,
            handle_text=lambda _text: None,
        )


@pytest.mark.parametrize("structured", [False, True])
def test_native_context_window_error_still_reaches_caller(structured) -> None:
    def factory(options):
        return _FakeClient(options, [[_result(
            is_error=True, stop_reason="model_context_window_exceeded",
        )]])

    model = _model(factory, max_input_chars=None, context_window_policy=None)
    with pytest.raises(ModelInputTooLargeError, match="model context window"):
        options = dict(tools=[_tool()], max_model_calls=1)
        if structured:
            model.run_structured("Question", result_type=_Answer, **options)
        else:
            model.run_agent_loop("Question", handle_text=lambda _text: None, **options)


@pytest.mark.parametrize("invalid", [0, -1, True, 1.5])
def test_native_entries_keep_input_limit_parameter_validation(invalid) -> None:
    factory = lambda _options: pytest.fail("client must not be created")
    with pytest.raises(ValueError, match="max_input_chars"):
        _model(factory, max_input_chars=invalid)
    model = _model(factory, max_input_chars=None)
    try:
        options = dict(tools=[_tool()], max_model_calls=1, max_input_chars=invalid)
        with pytest.raises(ValueError, match="max_input_chars"):
            model.run_structured("Question", result_type=_Answer, **options)
        with pytest.raises(ValueError, match="max_input_chars"):
            model.run_agent_loop("Question", handle_text=lambda _text: None, **options)
    finally:
        model.close()
