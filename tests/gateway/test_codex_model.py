from __future__ import annotations

import json
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.tools import BaseTool, StructuredTool
from openai_codex import ImageInput as CodexImageInput, TextInput as CodexTextInput
from openai_codex.errors import JsonRpcError, ServerBusyError
from pydantic import BaseModel, ConfigDict

from gateway.agent_model.errors import (
    AgentModelTimeoutError,
    MissingRequiredFunctionCallError,
    MissingStructuredQueryDraftError,
    ModelFailureReason,
    ModelFailureError,
    ModelInputTooLargeError,
)
from gateway.codex import CodexAgentModel
from gateway.codex.client import CodexModelCallLimitError
from gateway.codex.agent import CodexAgentModelError
from gateway.agent_model.context import ContextWindowPolicy
from gateway.multimodal import ImageInput, TextInput, VideoInput
from gateway.usage import (
    ModelCallCancelledError, capture_model_usage, control_model_calls,
    model_call_context, track_model_call,
)


class Result(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str


class NestedResultValue(BaseModel):
    detail: str


class NestedResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: NestedResultValue


def _usage(
    *,
    total_input_tokens: int = 10,
    total_output_tokens: int = 4,
    last_input_tokens: int | None = None,
    last_output_tokens: int | None = None,
    cache_write_input_tokens: int | None = None,
) -> object:
    def breakdown(input_tokens: int, output_tokens: int) -> object:
        return SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            cached_input_tokens=min(2, input_tokens),
            cache_write_input_tokens=cache_write_input_tokens,
            reasoning_output_tokens=min(1, output_tokens),
        )

    last_input_tokens = (
        total_input_tokens
        if last_input_tokens is None
        else last_input_tokens
    )
    last_output_tokens = (
        total_output_tokens
        if last_output_tokens is None
        else last_output_tokens
    )
    return SimpleNamespace(
        total=breakdown(total_input_tokens, total_output_tokens),
        last=breakdown(last_input_tokens, last_output_tokens),
    )


def _turn(text: str, *, usage: object | None = None) -> object:
    return SimpleNamespace(final_response=text, usage=usage or _usage())


class _FakeSession:
    def __init__(self, script: Sequence[tuple[object, int] | BaseException]) -> None:
        self._script = list(script)
        self.prompts: list[tuple[str, dict[str, Any] | None]] = []
        self.tool_call_count = 0
        self.successful_tool_call_count = 0
        self.interrupted = False
        self.closed = False

    def run_turn(self, prompt: str, *, output_schema=None) -> object:
        self.prompts.append((prompt, output_schema))
        if not self._script:
            raise AssertionError("unexpected Codex turn")
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        turn, tool_call_count = item
        self.tool_call_count = tool_call_count
        return turn

    def interrupt(self) -> None:
        self.interrupted = True

    def close(self) -> None:
        self.closed = True


class _BlockingSession(_FakeSession):
    def __init__(self) -> None:
        super().__init__([])

    def run_turn(self, prompt: str, *, output_schema=None) -> object:
        self.prompts.append((prompt, output_schema))
        while not self.closed:
            time.sleep(0.005)
        raise RuntimeError("closed")

    def interrupt(self) -> None:
        raise AssertionError(
            "interrupt would deadlock while the Codex reader runs a tool"
        )


class _EventBlockingSession(_FakeSession):
    def __init__(self, started: Event, release: Event) -> None:
        super().__init__([])
        self._started = started
        self._release = release

    def run_turn(self, prompt: str, *, output_schema=None) -> object:
        self.prompts.append((prompt, output_schema))
        self._started.set()
        if not self._release.wait(timeout=2):
            raise TimeoutError("test did not release the blocked Codex turn")
        return _turn("first answer")


def _model_for(session: _FakeSession, **overrides) -> CodexAgentModel:
    values = {
        "model": "gpt-5.3-codex",
        "reasoning_effort": "high",
        "request_timeout_seconds": 2,
        "max_output_tokens": 1024,
        "max_input_chars": 10_000,
        "max_concurrency": 1,
        "session_factory": lambda **_kwargs: session,
    }
    values.update(overrides)
    return CodexAgentModel(**values)


@pytest.mark.parametrize("mcp", [False, True])
def test_mcp_control_is_passed_to_each_new_session(mcp):
    options = []

    def factory(**kwargs):
        options.append(kwargs)
        return _FakeSession([])

    model = _model_for(_FakeSession([]), mcp=mcp, session_factory=factory)
    try:
        for _ in range(2):
            model._new_session(tools=[], max_model_calls=1, check_control=None)
        assert [item["mcp"] for item in options] == [mcp, mcp]
    finally:
        model.close()


@pytest.mark.parametrize("mcp", [None, 0, 1, "false", [], {}])
def test_model_rejects_non_boolean_mcp_before_allocating_executor(mcp, monkeypatch):
    def unexpected_executor(**_kwargs):
        pytest.fail("invalid mcp created an executor")

    monkeypatch.setattr("gateway.codex.agent.ThreadPoolExecutor", unexpected_executor)
    with pytest.raises(ValueError, match="mcp must be a boolean"):
        _model_for(_FakeSession([]), mcp=mcp)


def _context_policy(maximum_input_tokens: int) -> ContextWindowPolicy:
    return ContextWindowPolicy(
        context_window_tokens=maximum_input_tokens + 200,
        output_reserve_tokens=100,
        safety_margin_tokens=100,
        auto_compact_tokens=maximum_input_tokens,
        compact_target_tokens=max(1, maximum_input_tokens // 2),
    )


def test_run_structured_uses_json_schema_and_reports_usage() -> None:
    session = _FakeSession([(_turn('{"answer":"ok"}'), 1)])
    model = _model_for(session)
    usages = []
    try:
        with capture_model_usage(usages.append):
            result = model.run_structured(
                "answer with evidence",
                tools=[],
                result_type=Result,
                max_model_calls=3,
            )
    finally:
        model.close()

    assert result == Result(answer="ok")
    assert session.closed is True
    assert session.prompts[0][1]["properties"]["answer"]["type"] == "string"
    assert len(usages) == 1
    assert usages[0].provider == "codex"
    assert usages[0].granularity == "sdk_turn_run"
    assert usages[0].usage_source == "codex_thread_total"
    assert usages[0].usage.input_tokens == 10
    assert usages[0].usage.reasoning_tokens == 1


@pytest.mark.parametrize("generate", [False, True])
def test_structured_stop_feedback_repairs_in_one_session_without_truncation(generate):
    session = _FakeSession([
        (_turn('{"answer":"draft"}'), 0),
        (_turn('{"answer":"resolved"}', usage=_usage(total_input_tokens=25)), 0),
    ])
    created = []

    def factory(**options):
        created.append(options)
        return session

    feedback_text = "Resolve these Canvas inconsistencies: " + "event-ref " * 100
    feedback = iter([feedback_text, None])
    checks = []

    def review():
        assert not session.closed
        checks.append(len(session.prompts))
        return next(feedback)

    model = _model_for(session, session_factory=factory)
    records = []
    try:
        with capture_model_usage(records.append):
            options = dict(result_type=Result, max_model_calls=2, stop_feedback=review)
            if generate:
                result = model.generate([TextInput("answer")], schema_name="answer", **options)
            else:
                result = model.run_structured("answer", tools=[], **options)
    finally:
        model.close()

    assert result.answer == "resolved" and checks == [1, 2]
    assert len(created) == 1 and created[0]["max_model_calls"] == 2
    assert session.prompts[1][0] == feedback_text
    assert session.prompts[0][1] == session.prompts[1][1]
    assert session.prompts[1][1]["additionalProperties"] is False
    assert session.closed and len(records) == 1
    assert records[0].usage.input_tokens == 25


@pytest.mark.parametrize("limit", [2, 3])
def test_generate_feedback_includes_required_tool_repair_in_shared_budget(limit):
    class Session(_FakeSession):
        def run_turn(self, prompt, *, output_schema=None):
            turn = super().run_turn(prompt, output_schema=output_schema)
            if len(self.prompts) >= 2:
                self.successful_tool_call_count = 1
            return turn

    session = Session([
        (_turn('{"answer":"premature"}'), 0),
        (_turn('{"answer":"draft"}'), 1),
        (_turn('{"answer":"resolved"}'), 1),
    ])
    feedback = iter(["Finish the Canvas review", None])
    tool = StructuredTool.from_function(lambda value: value, name="inspect", description="Inspect evidence.")
    model = _model_for(session)
    try:
        options = dict(
            schema_name="answer", result_type=Result, tools=[tool],
            max_model_calls=limit, stop_feedback=lambda: next(feedback),
        )
        if limit == 2:
            with pytest.raises(CodexAgentModelError) as caught:
                model.generate([TextInput("answer")], **options)
            assert caught.value.reason == ModelFailureReason.LOOP_LIMIT
        else:
            assert model.generate([TextInput("answer")], **options).answer == "resolved"
    finally:
        model.close()
    assert len(session.prompts) == limit and session.closed
    assert all(schema == session.prompts[0][1] for _, schema in session.prompts)


@pytest.mark.parametrize("limit", [2, 3])
def test_generate_feedback_allows_repeated_tool_error_repair_within_budget(limit):
    class Session(_FakeSession):
        def run_turn(self, prompt, *, output_schema=None):
            turn = super().run_turn(prompt, output_schema=output_schema)
            if len(self.prompts) == 3:
                self.successful_tool_call_count = 1
            return turn

    session = Session([
        (_turn('{"answer":"first failed receipt"}'), 1),
        (_turn('{"answer":"second failed receipt"}'), 2),
        (_turn('{"answer":"resolved"}'), 3),
    ])
    reviews = []

    def review():
        reviews.append(session.successful_tool_call_count)
        return None

    tool = StructuredTool.from_function(lambda value: value, name="inspect", description="Inspect evidence.")
    model = _model_for(session)
    try:
        options = dict(
            schema_name="answer", result_type=Result, tools=[tool],
            max_model_calls=limit, stop_feedback=review,
        )
        if limit == 2:
            with pytest.raises(MissingRequiredFunctionCallError) as caught:
                model.generate([TextInput("answer")], **options)
            assert caught.value.reason == ModelFailureReason.LOOP_LIMIT
            assert reviews == []
        else:
            assert model.generate([TextInput("answer")], **options).answer == "resolved"
            assert reviews == [1]
    finally:
        model.close()
    assert len(session.prompts) == limit and session.closed
    assert all(schema == session.prompts[0][1] for _, schema in session.prompts)


def test_structured_feedback_preserves_native_continuation_call_budget():
    from gateway.codex.client import CodexModelCallBudget

    class Session(_FakeSession):
        def __init__(self, limit):
            super().__init__([(_turn('{"answer":"draft"}'), 1)])
            self.budget = CodexModelCallBudget(limit)

        def run_turn(self, prompt, *, output_schema=None):
            self.budget.reserve()
            if not self.prompts:
                self.budget.reserve()
            return super().run_turn(prompt, output_schema=output_schema)

    created = []

    def factory(**options):
        session = Session(options["max_model_calls"])
        created.append(session)
        return session

    model = _model_for(_FakeSession([]), session_factory=factory)
    try:
        with pytest.raises(CodexAgentModelError) as caught:
            model.run_structured(
                "answer", tools=[], result_type=Result, max_model_calls=2,
                stop_feedback=lambda: "repair",
            )
    finally:
        model.close()
    assert caught.value.reason == ModelFailureReason.LOOP_LIMIT
    assert len(created) == 1 and created[0].budget.used == 2
    assert len(created[0].prompts) == 1 and created[0].closed


@pytest.mark.parametrize("response", ["", "not-json", '{"unexpected":true}'])
def test_structured_feedback_does_not_repair_malformed_output(response):
    session = _FakeSession([(_turn(response), 0)])
    model = _model_for(session)
    try:
        with pytest.raises(MissingStructuredQueryDraftError) as caught:
            model.run_structured(
                "answer", tools=[], result_type=Result, max_model_calls=2,
                stop_feedback=lambda: pytest.fail("invalid output reached the stop gate"),
            )
    finally:
        model.close()
    assert caught.value.reason == ModelFailureReason.INVALID_MODEL_RESPONSE
    assert len(session.prompts) == 1 and session.closed


def test_structured_feedback_checks_cancellation_before_next_turn():
    cancelled = Event()
    session = _FakeSession([(_turn('{"answer":"draft"}'), 0)])
    control_checks = []

    def check_control():
        control_checks.append(cancelled.is_set())
        if cancelled.is_set():
            raise ModelCallCancelledError("cancelled during stop feedback")

    def feedback():
        assert control_checks[-1] is False
        cancelled.set()
        return "repair"

    model = _model_for(session)
    try:
        with pytest.raises(ModelCallCancelledError, match="during stop feedback"):
            model.run_structured(
                "answer", tools=[], result_type=Result, max_model_calls=2,
                stop_feedback=feedback, check_control=check_control,
            )
    finally:
        model.close()
    assert control_checks[-1] is True
    assert len(session.prompts) == 1 and session.closed


def test_structured_feedback_does_not_run_after_cancellation():
    cancelled = Event()

    class Session(_FakeSession):
        def run_turn(self, prompt, *, output_schema=None):
            turn = super().run_turn(prompt, output_schema=output_schema)
            cancelled.set()
            return turn

    def check_control():
        if cancelled.is_set():
            raise ModelCallCancelledError("cancelled before stop feedback")

    session = Session([(_turn('{"answer":"draft"}'), 0)])
    model = _model_for(session)
    try:
        with pytest.raises(ModelCallCancelledError, match="before stop feedback"):
            model.run_structured(
                "answer", tools=[], result_type=Result, max_model_calls=2,
                stop_feedback=lambda: pytest.fail("cancelled call reached the stop gate"),
                check_control=check_control,
            )
    finally:
        model.close()
    assert len(session.prompts) == 1 and session.closed


@pytest.mark.parametrize("enabled", [False, True])
def test_structured_feedback_does_not_reset_deadline(monkeypatch, enabled):
    now = [100.0]
    monkeypatch.setattr("gateway.codex.agent.monotonic", lambda: now[0])
    session = _FakeSession([
        (_turn('{"answer":"draft"}'), 0),
        (_turn('{"answer":"resolved"}'), 0),
    ])

    def feedback():
        now[0] += 3.0
        return "repair" if len(session.prompts) == 1 else None

    model = _model_for(session, total_timeout_enabled=enabled)
    try:
        options = dict(tools=[], result_type=Result, max_model_calls=2, stop_feedback=feedback)
        if enabled:
            with pytest.raises(AgentModelTimeoutError, match="deadline expired"):
                model.run_structured("answer", **options)
        else:
            assert model.run_structured("answer", **options).answer == "resolved"
    finally:
        model.close()
    assert len(session.prompts) == (1 if enabled else 2) and session.closed


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("structured", [False, True])
def test_native_diagnostics_preserve_sdk_error_without_prompt(
    tmp_path, monkeypatch, enabled, structured,
) -> None:
    trace_root = tmp_path / "trace"
    if enabled:
        monkeypatch.setenv("ANIVAG_MODEL_TRACE_DIR", str(trace_root))
    else:
        monkeypatch.delenv("ANIVAG_MODEL_TRACE_DIR", raising=False)
    sdk_error = RuntimeError("upstream turn failed")
    sdk_error.code = "upstream_error"
    sdk_error.status_code = 503
    sdk_error.__cause__ = RuntimeError("provider disconnected before completion")
    session = _FakeSession([sdk_error])
    model = _model_for(session)
    context = {
        "video_parse_task_id": "video-task",
        "interval_parse_task_id": "interval-task",
        "stage": "refine", "lease_version": 3,
    }
    usages = []
    prompt = "private full prompt must not be captured"
    try:
        with model_call_context(context), capture_model_usage(usages.append):
            with pytest.raises(CodexAgentModelError) as failure:
                if structured:
                    model.run_structured(
                        prompt, tools=[], result_type=Result, max_model_calls=3,
                    )
                else:
                    model.run_agent_loop(
                        prompt, tools=[], max_model_calls=3, handle_text=lambda _text: None,
                    )
    finally:
        model.close()
    assert failure.value.__cause__ is sdk_error
    assert failure.value.reason == ModelFailureReason.INVALID_MODEL_RESPONSE
    assert session.closed
    assert len(usages) == 1 and usages[0].status == "failed"
    if not enabled:
        assert not trace_root.exists()
        return
    directory = trace_root / usages[0].call_id
    saved = (directory / "events.jsonl").read_text()
    assert prompt not in saved
    events = [json.loads(line) for line in saved.splitlines()]
    assert [event["event"] for event in events] == ["request", "error"]
    assert all(event["call_id"] == usages[0].call_id for event in events)
    assert events[0]["payload"]["context"] == context
    assert events[0]["payload"]["request"] == {
        "model": model.model_name, "operation": usages[0].operation,
        "request_timeout_seconds": 2, "total_timeout_enabled": True,
    }
    chain = events[1]["payload"]["chain"]
    assert [entry["message"] for entry in chain[1:]] == [
        "upstream turn failed", "provider disconnected before completion",
    ]
    assert chain[1]["code"] == "upstream_error"
    assert chain[1]["status_code"] == 503


def test_codex_preserves_sdk_cache_write_counts_in_thread_total() -> None:
    session = _FakeSession([(_turn('{"answer":"ok"}', usage=_usage(cache_write_input_tokens=3)), 1)])
    model = _model_for(session)
    records = []
    try:
        with capture_model_usage(records.append):
            assert model.run_structured(
                "question", tools=[], result_type=Result, max_model_calls=1,
            ) == Result(answer="ok")
    finally:
        model.close()
    assert len(records) == 1
    usage = records[0].usage
    assert usage.input_tokens == 10 and usage.total_tokens == 14
    assert usage.cached_input_tokens == 2 and usage.cache_write_input_tokens == 3


def test_native_loop_does_not_reject_codex_base_input_estimate() -> None:
    created: list[object] = []

    def factory(**_kwargs: object) -> _FakeSession:
        created.append(object())
        return _FakeSession([(_turn("ok"), 0)])

    model = CodexAgentModel(
        model="gpt-5.3-codex",
        request_timeout_seconds=2,
        max_output_tokens=1_024,
        max_input_chars=10_000,
        context_window_policy=_context_policy(13_000),
        max_concurrency=1,
        session_factory=factory,
    )
    try:
        emitted = []
        model.run_agent_loop(
            "short prompt",
            tools=[],
            max_model_calls=1,
            handle_text=emitted.append,
        )
    finally:
        model.close()

    assert len(created) == 1 and emitted == ["ok"]


@pytest.mark.parametrize("structured", [False, True])
def test_native_entries_leave_large_tool_contract_to_codex(structured) -> None:
    created: list[object] = []

    def lookup(value: str) -> str:
        return value

    tool = StructuredTool.from_function(
        lookup,
        name="lookup",
        description="x" * 30_000,
    )

    def factory(**_kwargs: object) -> _FakeSession:
        created.append(object())
        return _FakeSession([(_turn('{"answer":"ok"}' if structured else "ok"), 0)])

    model = CodexAgentModel(
        model="gpt-5.3-codex",
        request_timeout_seconds=2,
        max_output_tokens=1_024,
        max_input_chars=10_000,
        context_window_policy=_context_policy(20_000),
        max_concurrency=1,
        session_factory=factory,
    )
    try:
        if structured:
            assert model.run_structured(
                "short prompt", tools=[tool], result_type=Result, max_model_calls=1,
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
    finally:
        model.close()

    assert len(created) == 1


def test_native_loop_accepts_small_request() -> None:
    session = _FakeSession([(_turn("ok"), 0)])
    model = _model_for(
        session,
        context_window_policy=_context_policy(20_000),
    )
    emitted: list[str] = []
    try:
        model.run_agent_loop(
            "short prompt",
            tools=[],
            max_model_calls=1,
            handle_text=emitted.append,
        )
    finally:
        model.close()

    assert emitted == ["ok"]


@pytest.mark.parametrize("text", ["not-json", '{"unexpected":true}'])
def test_run_structured_rejects_invalid_final_json_and_keeps_usage(text) -> None:
    session = _FakeSession([(_turn(text), 0)])
    model = _model_for(session)
    records = []
    try:
        with capture_model_usage(records.append), pytest.raises(
            MissingStructuredQueryDraftError,
        ) as captured:
            model.run_structured(
                "answer",
                tools=[],
                result_type=Result,
                max_model_calls=2,
            )
    finally:
        model.close()

    assert captured.value.reason == ModelFailureReason.INVALID_MODEL_RESPONSE
    assert len(records) == 1
    assert records[0].status == "failed"
    assert records[0].granularity == "sdk_turn_run"
    assert records[0].usage_source == "codex_thread_total"
    assert records[0].usage.total_tokens == 14


def test_run_structured_classifies_missing_output_as_invalid_response() -> None:
    session = _FakeSession([(_turn(""), 0)])
    model = _model_for(session)
    try:
        with pytest.raises(MissingStructuredQueryDraftError) as captured:
            model.run_structured(
                "answer",
                tools=[],
                result_type=Result,
                max_model_calls=1,
            )
    finally:
        model.close()

    assert captured.value.reason == ModelFailureReason.INVALID_MODEL_RESPONSE


def test_run_structured_closes_nested_output_schema_objects() -> None:
    session = _FakeSession([(_turn('{"value":{"detail":"ok"}}'), 0)])
    model = _model_for(session)
    try:
        result = model.run_structured(
            "answer",
            tools=[],
            result_type=NestedResult,
            max_model_calls=1,
        )
    finally:
        model.close()

    schema = session.prompts[0][1]
    assert result.value.detail == "ok"
    assert schema["additionalProperties"] is False
    assert schema["properties"]["value"]["additionalProperties"] is False


def test_agent_loop_repairs_a_missing_required_tool_call_once() -> None:
    session = _FakeSession([
        (_turn("premature"), 0),
        (_turn("grounded answer"), 1),
    ])
    model = _model_for(session)
    texts = []
    try:
        model.run_agent_loop(
            "find evidence",
            tools=[],
            max_model_calls=2,
            require_tool_call=True,
            handle_text=texts.append,
        )
    finally:
        model.close()

    assert texts == ["grounded answer"]
    assert session.prompts[1][0] == "请先调用一个可用工具，再根据工具结果完成回答。"


@pytest.mark.parametrize("require_tool_call", [False, True])
def test_required_tool_repair_does_not_publish_rejected_native_callbacks(require_tool_call) -> None:
    captured, notified = {}, []

    class ObservedSession(_FakeSession):
        def run_turn(self, prompt, *, output_schema=None):
            turn = super().run_turn(prompt, output_schema=output_schema)
            callback = captured.get("handle_text")
            if callback is not None:
                notified.append(turn.final_response)
                callback(turn.final_response)
            return turn

    session = ObservedSession([(_turn("premature"), 0), (_turn("grounded answer"), 1)])

    def factory(**kwargs):
        captured.update(kwargs)
        return session

    model = _model_for(session, session_factory=factory)
    texts = []
    try:
        model.run_agent_loop(
            "find evidence", tools=[], max_model_calls=2,
            require_tool_call=require_tool_call, handle_text=texts.append,
        )
    finally:
        model.close()

    assert texts == (["grounded answer"] if require_tool_call else ["premature"])
    assert notified == ([] if require_tool_call else ["premature"])
    assert ("handle_text" in captured) is not require_tool_call
    assert len(session.prompts) == (2 if require_tool_call else 1)


def test_agent_loop_fails_when_required_tool_budget_is_exhausted() -> None:
    session = _FakeSession([(_turn("premature"), 0)])
    model = _model_for(session)
    try:
        with pytest.raises(MissingRequiredFunctionCallError) as captured:
            model.run_agent_loop(
                "find evidence",
                tools=[],
                max_model_calls=1,
                require_tool_call=True,
                handle_text=lambda _text: None,
            )
    finally:
        model.close()

    assert captured.value.reason == ModelFailureReason.LOOP_LIMIT


def test_agent_loop_applies_stop_feedback_in_same_session() -> None:
    session = _FakeSession([
        (_turn("draft"), 0),
        (_turn("revised"), 0),
    ])
    model = _model_for(session)
    texts = []
    feedback = iter(["add the missing caveat", None])
    try:
        model.run_agent_loop(
            "answer",
            tools=[],
            max_model_calls=2,
            handle_text=texts.append,
            stop_feedback=lambda: next(feedback),
        )
    finally:
        model.close()

    assert texts == ["draft", "revised"]
    assert session.prompts[1][0] == "add the missing caveat"


def test_agent_loop_reports_final_cumulative_thread_usage_once() -> None:
    session = _FakeSession([
        (_turn("draft", usage=_usage(
            total_input_tokens=10,
            total_output_tokens=4,
        )), 0),
        (_turn("revised", usage=_usage(
            total_input_tokens=25,
            total_output_tokens=9,
            last_input_tokens=15,
            last_output_tokens=5,
        )), 0),
    ])
    model = _model_for(session)
    feedback = iter(["revise", None])
    usages = []
    try:
        with capture_model_usage(usages.append):
            model.run_agent_loop(
                "answer",
                tools=[],
                max_model_calls=2,
                handle_text=lambda _text: None,
                stop_feedback=lambda: next(feedback),
            )
    finally:
        model.close()

    # Codex ThreadTokenUsage.total is cumulative for this ephemeral thread.
    # Capturing the final turn once includes both outer turns without double count.
    assert len(usages) == 1
    assert usages[0].granularity == "sdk_turn_run"
    assert usages[0].usage_source == "codex_thread_total"
    assert usages[0].usage.input_tokens == 25
    assert usages[0].usage.output_tokens == 9
    assert usages[0].usage.total_tokens == 34


def test_agent_loop_keeps_returned_usage_when_text_callback_fails() -> None:
    session = _FakeSession([(_turn("draft"), 0)])
    model = _model_for(session)
    records = []

    def fail(_text):
        raise RuntimeError("callback failed")

    try:
        with capture_model_usage(records.append), pytest.raises(CodexAgentModelError):
            model.run_agent_loop(
                "answer", tools=[], max_model_calls=1, handle_text=fail,
            )
    finally:
        model.close()

    assert len(records) == 1
    assert records[0].status == "failed"
    assert records[0].usage.total_tokens == 14


@pytest.mark.parametrize("operation", ["loop", "structured", "generate"])
def test_only_agent_loop_observes_messages_without_repeating_final_response(operation):
    captured, texts, records = {}, [], []

    class Session(_FakeSession):
        def run_turn(self, prompt, *, output_schema=None):
            if "handle_text" in captured:
                captured["handle_text"]("A")
                captured["handle_text"]("B")
            return super().run_turn(prompt, output_schema=output_schema)

    session = Session([(_turn("B" if operation == "loop" else '{"answer":"ok"}'), 0)])

    def factory(**kwargs):
        captured.update(kwargs)
        return session

    model = _model_for(session, session_factory=factory)
    try:
        with capture_model_usage(records.append):
            if operation == "loop":
                model.run_agent_loop("answer", tools=[], max_model_calls=2, handle_text=texts.append)
            elif operation == "structured":
                assert model.run_structured("answer", tools=[], max_model_calls=2, result_type=Result).answer == "ok"
            else:
                assert model.generate([TextInput("answer")], schema_name="answer", result_type=Result).answer == "ok"
    finally:
        model.close()
    assert ("handle_text" in captured) == (operation == "loop")
    assert texts == (["A", "B"] if operation == "loop" else [])
    assert len(records) == 1 and records[0].usage.total_tokens == 14
    assert session.closed


def test_agent_loop_keeps_usage_when_native_callback_flush_fails():
    error = RuntimeError("visible callback failed")

    class Session(_FakeSession):
        last_turn = _turn("B")

        def run_turn(self, prompt, *, output_schema=None):
            raise error

    session = Session([])
    model = _model_for(session)
    records = []
    try:
        with capture_model_usage(records.append), pytest.raises(CodexAgentModelError) as caught:
            model.run_agent_loop("answer", tools=[], max_model_calls=2, handle_text=lambda text: None)
    finally:
        model.close()
    assert caught.value.__cause__ is error
    assert len(records) == 1 and records[0].usage.total_tokens == 14


def test_executor_restores_host_context_for_nested_model_calls() -> None:
    identity = ContextVar("codex_test_identity", default="outside")

    class Session(_FakeSession):
        def run_turn(self, prompt: str, *, output_schema=None) -> object:
            assert identity.get() == "invocation"
            with track_model_call(provider="test", model="nested", operation="tool"):
                pass
            return _turn('{"answer":"ok"}')

    session = Session([])
    model = _model_for(session)
    records = []
    acquired = []
    released = []
    control = SimpleNamespace(
        acquire=lambda call: acquired.append(call.call_id),
        release=lambda call: released.append(call.call_id),
    )
    token = identity.set("invocation")
    try:
        with (
            model_call_context({"task_id": "task-context"}),
            capture_model_usage(records.append),
            control_model_calls(control),
        ):
            model.run_structured(
                "answer", tools=[], result_type=Result, max_model_calls=1,
            )
    finally:
        identity.reset(token)
        model.close()

    assert {record.model for record in records} == {"nested", "gpt-5.3-codex"}
    assert all(record.context["task_id"] == "task-context" for record in records)
    assert set(acquired) == {record.call_id for record in records}
    assert set(released) == set(acquired)
    assert identity.get() == "outside"


def test_invocation_timeout_force_closes_without_waiting_for_interrupt() -> None:
    session = _BlockingSession()
    model = _model_for(
        session,
        request_timeout_seconds=0.03,
    )
    try:
        with pytest.raises(AgentModelTimeoutError):
            model.run_agent_loop(
                "answer",
                tools=[],
                max_model_calls=2,
                handle_text=lambda _text: None,
            )
    finally:
        model.close()

    assert session.closed is True


@pytest.mark.parametrize("total_timeout_enabled", [True, False])
def test_generate_usage_cancellation_closes_native_transport_without_tools(total_timeout_enabled) -> None:
    started = Event()
    calls = []
    released = []
    errors = []

    class Session(_BlockingSession):
        def run_turn(self, prompt, *, output_schema=None):
            started.set()
            return super().run_turn(prompt, output_schema=output_schema)

    session = Session()
    model = _model_for(session, request_timeout_seconds=5, total_timeout_enabled=total_timeout_enabled)
    control = SimpleNamespace(acquire=calls.append, release=released.append)

    def generate():
        try:
            with control_model_calls(control):
                model.generate([TextInput("inspect")], schema_name="answer", result_type=Result)
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=generate)
    try:
        thread.start()
        assert started.wait(timeout=1)
        assert len(calls) == 1
        calls[0].cancel()
        thread.join(timeout=1)
        assert not thread.is_alive()
        assert session.closed
        assert len(errors) == 1 and isinstance(errors[0], ModelCallCancelledError)
        assert released == calls
        assert not calls[0]._cancel_callbacks
    finally:
        model.close()
        thread.join(timeout=1)


@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.parametrize("max_input_chars", [None, 4])
def test_native_entries_do_not_reject_or_truncate_long_prompt(structured, max_input_chars) -> None:
    session = _FakeSession([(_turn('{"answer":"ok"}' if structured else "ok"), 0)])
    model = _model_for(session, max_input_chars=max_input_chars)
    prompt = "long evidence " * 20_000
    try:
        options = dict(tools=[], max_model_calls=1, max_input_chars=4)
        if structured:
            assert model.run_structured(prompt, result_type=Result, **options).answer == "ok"
        else:
            emitted = []
            model.run_agent_loop(
                prompt, handle_text=emitted.append, **options,
            )
            assert emitted == ["ok"]
    finally:
        model.close()

    assert session.prompts[0][0] == prompt
    assert session.closed


@pytest.mark.parametrize("invalid", [0, -1, True, 1.5])
def test_native_entries_keep_input_limit_parameter_validation(invalid) -> None:
    session = _FakeSession([])
    with pytest.raises(ValueError, match="max_input_chars"):
        _model_for(session, max_input_chars=invalid)
    model = _model_for(session, max_input_chars=None)
    try:
        options = dict(tools=[], max_model_calls=1, max_input_chars=invalid)
        with pytest.raises(ValueError, match="max_input_chars"):
            model.run_structured("Question", result_type=Result, **options)
        with pytest.raises(ValueError, match="max_input_chars"):
            model.run_agent_loop("Question", handle_text=lambda _text: None, **options)
        assert session.prompts == []
    finally:
        model.close()


@pytest.mark.parametrize("structured", [False, True])
def test_native_context_error_is_preserved_after_submitting_request(structured) -> None:
    error = RuntimeError("Provider rejected the request context")
    error.code = "context_length_exceeded"
    session = _FakeSession([error])
    model = _model_for(session, max_input_chars=None)
    try:
        with pytest.raises(CodexAgentModelError) as captured:
            options = dict(tools=[], max_model_calls=1)
            if structured:
                model.run_structured("Question", result_type=Result, **options)
            else:
                model.run_agent_loop("Question", handle_text=lambda _text: None, **options)
        assert captured.value.__cause__ is error
        assert session.prompts and session.closed
    finally:
        model.close()


def test_control_cancellation_is_checked_before_starting_codex() -> None:
    session = _FakeSession([])
    model = _model_for(session)

    class Cancelled(RuntimeError):
        pass

    try:
        with pytest.raises(Cancelled):
            model.run_agent_loop(
                "answer",
                tools=[],
                max_model_calls=1,
                handle_text=lambda _text: None,
                check_control=lambda: (_ for _ in ()).throw(Cancelled()),
            )
    finally:
        model.close()

    assert session.prompts == []


def test_close_is_idempotent_and_rejects_new_invocations() -> None:
    session = _FakeSession([])
    model = _model_for(session)

    model.close()
    model.close()

    with pytest.raises(RuntimeError, match="closed"):
        model.run_agent_loop(
            "answer",
            tools=[],
            max_model_calls=1,
            handle_text=lambda _text: None,
        )


def test_injected_executor_serializes_two_model_instances() -> None:
    executor = ThreadPoolExecutor(max_workers=1)
    started = Event()
    release = Event()
    first_session = _EventBlockingSession(started, release)
    second_session = _FakeSession([(_turn("second answer"), 0)])
    first_model = _model_for(first_session, executor=executor)
    second_model = _model_for(second_session, executor=executor)
    errors: list[BaseException] = []
    texts: list[str] = []

    def run(model: CodexAgentModel, prompt: str) -> None:
        try:
            model.run_agent_loop(
                prompt,
                tools=[],
                max_model_calls=1,
                handle_text=texts.append,
            )
        except BaseException as exc:
            errors.append(exc)

    first_thread = Thread(target=run, args=(first_model, "first"))
    second_thread = Thread(target=run, args=(second_model, "second"))
    try:
        first_thread.start()
        assert started.wait(timeout=1)
        second_thread.start()
        time.sleep(0.03)
        assert second_session.prompts == []

        release.set()
        first_thread.join(timeout=1)
        second_thread.join(timeout=1)

        assert not first_thread.is_alive()
        assert not second_thread.is_alive()
        assert errors == []
        assert texts == ["first answer", "second answer"]
    finally:
        release.set()
        first_model.close()
        second_model.close()
        executor.shutdown(wait=True, cancel_futures=True)


def test_shared_executor_queue_wait_counts_toward_request_timeout() -> None:
    executor = ThreadPoolExecutor(max_workers=1)
    started = Event()
    release = Event()
    first_session = _EventBlockingSession(started, release)
    second_session = _FakeSession([(_turn("should not run"), 0)])
    first_model = _model_for(first_session, executor=executor)
    second_model = _model_for(
        second_session,
        executor=executor,
        request_timeout_seconds=0.03,
    )
    first_errors: list[BaseException] = []

    def run_first() -> None:
        try:
            first_model.run_agent_loop(
                "first",
                tools=[],
                max_model_calls=1,
                handle_text=lambda _text: None,
            )
        except BaseException as exc:
            first_errors.append(exc)

    first_thread = Thread(target=run_first)
    try:
        first_thread.start()
        assert started.wait(timeout=1)

        with pytest.raises(AgentModelTimeoutError):
            second_model.run_agent_loop(
                "second",
                tools=[],
                max_model_calls=1,
                handle_text=lambda _text: None,
            )

        assert second_session.prompts == []
        assert second_session.closed is True
    finally:
        release.set()
        first_thread.join(timeout=1)
        first_model.close()
        second_model.close()
        executor.shutdown(wait=True, cancel_futures=True)

    assert not first_thread.is_alive()
    assert first_errors == []


def test_model_call_budget_is_separate_from_dynamic_tool_limit() -> None:
    session = _FakeSession([(_turn("answer"), 2)])
    captured = {}

    def factory(**kwargs):
        captured.update(kwargs)
        return session

    model = CodexAgentModel(
        model="gpt-5.3-codex",
        request_timeout_seconds=2,
        max_output_tokens=1024,
        max_input_chars=10_000,
        max_concurrency=1,
        session_factory=factory,
    )
    try:
        model.run_agent_loop(
            "use as many tools as the server permits",
            tools=[],
            max_model_calls=1,
            handle_text=lambda _text: None,
        )
    finally:
        model.close()

    assert captured["max_tool_calls"] is None
    assert captured["max_model_calls"] == 1


def test_internal_model_call_budget_exhaustion_has_loop_limit_reason() -> None:
    session = _FakeSession([
        CodexModelCallLimitError("max_model_calls exhausted")
    ])
    model = _model_for(session)
    try:
        with pytest.raises(CodexAgentModelError) as captured:
            model.run_agent_loop(
                "answer",
                tools=[],
                max_model_calls=1,
                handle_text=lambda _text: None,
            )
    finally:
        model.close()

    assert captured.value.reason == ModelFailureReason.LOOP_LIMIT


def test_generate_preserves_image_order_and_uses_media_budget(tmp_path) -> None:
    first, second = tmp_path / "first.png", tmp_path / "second.png"
    first.write_bytes(b"a" * 50_000)
    second.write_bytes(b"b" * 50_000)
    session = _FakeSession([(_turn('{"answer":"observed"}'), 0)])
    model = _model_for(session, max_input_chars=1_500)
    records = []
    try:
        with capture_model_usage(records.append):
            result = model.generate(
                [TextInput("frame one"), ImageInput(first), TextInput("frame two"), ImageInput(second)],
                schema_name="visual_evidence", result_type=Result,
                context_window_policy=_context_policy(20_000), max_model_calls=1,
            )
    finally:
        model.close()

    assert result.answer == "observed"
    inputs, schema = session.prompts[0]
    assert [type(item) for item in inputs] == [CodexTextInput, CodexImageInput, CodexTextInput, CodexImageInput]
    assert inputs[0].text == "frame one" and inputs[2].text == "frame two"
    assert inputs[1].url.startswith("data:image/png;base64,")
    assert inputs[1].url != inputs[3].url
    assert schema["additionalProperties"] is False
    assert session.closed
    assert len(records) == 1 and records[0].operation == "visual_evidence"
    assert records[0].usage.total_tokens == 14


def test_agent_loop_submits_ordered_history_images_outside_text_budget() -> None:
    earlier = "data:image/png;base64," + "A" * 200_000
    current = "data:image/png;base64," + "B" * 200_000
    history = [
        {"role": "user", "content": "Earlier image", "imageUrls": [earlier]},
        {"role": "assistant", "content": "Earlier answer"},
        {"role": "user", "content": "Current image", "imageUrls": [current]},
    ]
    session = _FakeSession([(_turn("Observed"), 0)])
    model = _model_for(session, max_input_chars=1_500, context_window_policy=_context_policy(20_000))
    texts = []
    try:
        model.run_agent_loop("Answer", tools=[], max_model_calls=1, handle_text=texts.append, initial_messages=history)
    finally:
        model.close()

    inputs, schema = session.prompts[0]
    assert schema is None and texts == ["Observed"]
    assert [type(item) for item in inputs] == [
        CodexTextInput, CodexTextInput, CodexImageInput, CodexTextInput, CodexImageInput,
    ]
    assert [inputs[2].url, inputs[4].url] == [earlier, current]
    assert inputs[1].text.endswith("message 1:") and inputs[3].text.endswith("message 3:")
    assert '"imageCount": 1' in inputs[0].text
    assert "Earlier answer" in inputs[0].text and "Current image" in inputs[0].text
    assert all("data:image" not in item.text for item in inputs if isinstance(item, CodexTextInput))
    assert history[0]["imageUrls"] == [earlier] and "imageCount" not in history[0]
    assert history[2]["imageUrls"] == [current] and "imageCount" not in history[2]


@pytest.mark.parametrize("structured", [False, True])
def test_native_entries_submit_current_images_and_rich_history_in_order(structured) -> None:
    earlier = "data:image/png;base64," + "A" * 200_000
    current = [f"https://example.test/{index}.png" for index in range(6)]
    history = [{"role": "user", "content": [
        {"type": "text", "text": "Earlier image"},
        {"type": "image_url", "image_url": {"url": earlier}},
    ]}, {"role": "assistant", "content": "Earlier answer"}]
    session = _FakeSession([(_turn('{"answer":"Observed"}' if structured else "Observed"), 0)])
    model = _model_for(session, max_input_chars=1_500, context_window_policy=_context_policy(50_000))
    try:
        options = dict(tools=[], max_model_calls=1, initial_messages=history, image_urls=current)
        if structured:
            assert model.run_structured("Current question", result_type=Result, **options).answer == "Observed"
        else:
            texts = []
            model.run_agent_loop("Current question", handle_text=texts.append, **options)
            assert texts == ["Observed"]
    finally:
        model.close()
    inputs, _schema = session.prompts[0]
    assert [item.url for item in inputs if isinstance(item, CodexImageInput)] == [earlier, *current]
    assert all("data:image" not in item.text for item in inputs if isinstance(item, CodexTextInput))
    assert "Earlier answer" in inputs[0].text and "Current question" in inputs[0].text
    assert history[0]["content"][1]["image_url"]["url"] == earlier
    assert "imageCount" not in history[0]


@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.parametrize("historical", [False, True])
def test_native_entries_reject_images_when_vision_is_explicitly_disabled(structured, historical) -> None:
    image = "data:image/png;base64,AAAA"
    session = _FakeSession([])
    model = _model_for(session)
    model.supports_vision_input = False
    options = dict(tools=[], max_model_calls=1)
    options.update({"initial_messages": [{"role": "user", "content": "Earlier", "imageUrls": [image]}]}
                   if historical else {"image_urls": [image]})
    try:
        with pytest.raises(ValueError, match="supports_vision_input=False"):
            if structured:
                model.run_structured("Question", result_type=Result, **options)
            else:
                model.run_agent_loop("Question", handle_text=lambda _text: None, **options)
        assert session.prompts == []
    finally:
        model.close()


@pytest.mark.parametrize("fail_preparation", [False, True])
def test_agent_loop_prepares_persistent_session_before_turn_without_reserializing_history(fail_preparation) -> None:
    events = []
    options = {}

    class PersistentSession(_FakeSession):
        native_thread_id = "native-thread"

        def open(self):
            events.append("open")

        def run_turn(self, prompt, *, output_schema=None):
            events.append("turn")
            return super().run_turn(prompt, output_schema=output_schema)

    session = PersistentSession([(_turn("Observed"), 0)])

    def factory(**kwargs):
        options.update(kwargs)
        return session

    def prepare(opened):
        assert opened is session
        assert opened.native_thread_id == "native-thread"
        events.append("prepare")
        if fail_preparation:
            raise RuntimeError("history injection failed")

    current = {"role": "user", "content": "already in current prompt", "imageUrls": ["data:image/png;base64,YQ=="]}
    model = _model_for(session, session_factory=factory)
    texts = []
    try:
        if fail_preparation:
            with pytest.raises(CodexAgentModelError):
                model.run_agent_loop(
                    "Current question", tools=[], max_model_calls=1, handle_text=texts.append,
                    initial_messages=[current], native_thread_id="native-thread", on_session_ready=prepare,
                )
        else:
            model.run_agent_loop(
                "Current question", tools=[], max_model_calls=1, handle_text=texts.append,
                initial_messages=[current], native_thread_id="native-thread", on_session_ready=prepare,
            )
    finally:
        model.close()

    assert options["persistent"] is True and options["native_thread_id"] == "native-thread"
    assert events == (["open", "prepare"] if fail_preparation else ["open", "prepare", "turn"])
    assert session.closed
    if fail_preparation:
        assert session.prompts == [] and texts == []
    else:
        inputs, _schema = session.prompts[0]
        assert inputs[0].text == "Current question"
        assert inputs[2].url == current["imageUrls"][0]
        assert texts == ["Observed"]
    assert "imageCount" not in current


def test_session_key_identifies_codex_storage_without_model_or_plaintext_path(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CODEX_HOME", "sessions")
    first = _model_for(_FakeSession([]), cwd=str(tmp_path), model="first-model")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "sessions"))
    second = _model_for(_FakeSession([]), cwd=str(tmp_path / "elsewhere"), model="second-model")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "other-sessions"))
    other = _model_for(_FakeSession([]), cwd=str(tmp_path))
    try:
        assert first.session_key == second.session_key
        assert first.session_key != other.session_key
        assert str(tmp_path) not in first.session_key
        assert first.session_key.startswith("codex:")
    finally:
        for model in (first, second, other):
            model.close()


@pytest.mark.parametrize("structured", [False, True])
def test_native_entries_leave_six_images_to_codex_context_management(structured) -> None:
    session = _FakeSession([(_turn('{"answer":"Observed"}' if structured else "Observed"), 0)])
    model = _model_for(session, context_window_policy=_context_policy(13_100))
    images = [f"https://example.test/{index}.png" for index in range(6)]
    try:
        options = dict(tools=[], max_model_calls=1, image_urls=images)
        if structured:
            assert model.run_structured("Answer", result_type=Result, **options).answer == "Observed"
        else:
            emitted = []
            model.run_agent_loop("Answer", handle_text=emitted.append, **options)
            assert emitted == ["Observed"]
        assert [item.url for item in session.prompts[0][0] if isinstance(item, CodexImageInput)] == images
    finally:
        model.close()


def test_generate_counts_image_tokens_before_starting_session(tmp_path) -> None:
    image = tmp_path / "frame.png"
    image.write_bytes(b"image")
    session = _FakeSession([])
    model = _model_for(session)
    try:
        with pytest.raises(ModelInputTooLargeError, match="initial context"):
            model.generate(
                [ImageInput(image)], schema_name="visual", result_type=Result,
                context_window_policy=_context_policy(15_000),
            )
        assert session.prompts == []
    finally:
        model.close()


def test_generate_keeps_per_call_context_policy_local() -> None:
    instance_policy = _context_policy(30_000)
    session = _FakeSession([(_turn('{"answer":"ok"}'), 0)])
    model = _model_for(session, context_window_policy=instance_policy)
    try:
        with pytest.raises(ModelInputTooLargeError):
            model.generate(
                [TextInput("evidence")], schema_name="visual", result_type=Result,
                context_window_policy=_context_policy(13_000),
            )
        assert model.context_window_policy is instance_policy
        assert model.generate(
            [TextInput("evidence")], schema_name="visual", result_type=Result,
        ).answer == "ok"
        assert model.context_window_policy is instance_policy
    finally:
        model.close()


def test_generate_rejects_direct_video_before_starting_session(tmp_path) -> None:
    session = _FakeSession([])
    model = _model_for(session)
    try:
        with pytest.raises(ValueError, match="ordered ImageInput frames"):
            model.generate(
                [VideoInput(tmp_path / "clip.mp4", 1)], schema_name="visual", result_type=Result,
            )
    finally:
        model.close()
    assert session.prompts == []


def test_generate_rejects_failed_business_tools_even_with_valid_json() -> None:
    session = _FakeSession([(_turn('{"answer":"premature"}'), 1)])
    model = _model_for(session)
    tool = StructuredTool.from_function(lambda value: value, name="inspect", description="Inspect evidence.")
    try:
        with pytest.raises(MissingRequiredFunctionCallError) as failure:
            model.generate(
                [TextInput("evidence")], schema_name="visual", result_type=Result,
                tools=[tool], max_model_calls=1,
            )
    finally:
        model.close()
    assert failure.value.reason == ModelFailureReason.LOOP_LIMIT
    assert session.closed


def test_generate_repairs_failed_tool_completion_in_same_session() -> None:
    class Session(_FakeSession):
        def run_turn(self, prompt, *, output_schema=None):
            turn = super().run_turn(prompt, output_schema=output_schema)
            if len(self.prompts) == 2:
                self.successful_tool_call_count = 1
            return turn

    session = Session([
        (_turn('{"answer":"premature"}'), 1),
        (_turn('{"answer":"grounded"}'), 2),
    ])
    model = _model_for(session)
    tool = StructuredTool.from_function(lambda value: value, name="inspect", description="Inspect evidence.")
    records = []
    try:
        with capture_model_usage(records.append):
            result = model.generate(
                [TextInput("evidence")], schema_name="visual", result_type=Result,
                tools=[tool], max_model_calls=4,
            )
    finally:
        model.close()
    assert result.answer == "grounded"
    assert "do not repeat successful writes" in session.prompts[1][0]
    assert session.prompts[0][1] == session.prompts[1][1]
    assert session.closed
    assert len(records) == 1


def test_generate_fails_after_second_premature_tool_completion() -> None:
    session = _FakeSession([
        (_turn('{"answer":"premature"}'), 1),
        (_turn('{"answer":"still premature"}'), 2),
    ])
    model = _model_for(session)
    tool = StructuredTool.from_function(lambda value: value, name="inspect", description="Inspect evidence.")
    try:
        with pytest.raises(MissingRequiredFunctionCallError, match="twice"):
            model.generate(
                [TextInput("evidence")], schema_name="visual", result_type=Result,
                tools=[tool], max_model_calls=4,
            )
    finally:
        model.close()
    assert len(session.prompts) == 2
    assert session.closed


def test_generate_timeout_closes_native_session() -> None:
    session = _BlockingSession()
    model = _model_for(session)
    try:
        with pytest.raises(AgentModelTimeoutError):
            model.generate(
                [TextInput("evidence")], schema_name="visual", result_type=Result,
                request_timeout_seconds=0.03,
            )
    finally:
        model.close()
    assert session.closed


@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.parametrize("data,expected", [
    ({"codexErrorInfo": "server_overloaded"}, ModelFailureReason.PROVIDER_CAPACITY),
    ({"codexErrorInfo": "rateLimitExceeded"}, ModelFailureReason.PROVIDER_RATE_LIMIT),
    ({"codexErrorInfo": {"responseTooManyFailedAttempts": {"httpStatusCode": 429}}}, ModelFailureReason.PROVIDER_RATE_LIMIT),
    ({"codexErrorInfo": "usageLimitExceeded"}, ModelFailureReason.INVALID_MODEL_RESPONSE),
    ({"codexErrorInfo": "badRequest"}, ModelFailureReason.INVALID_MODEL_RESPONSE),
    ({"codexErrorInfo": "other"}, ModelFailureReason.INVALID_MODEL_RESPONSE),
    ({"codexErrorInfo": {}}, ModelFailureReason.INVALID_MODEL_RESPONSE),
    (None, ModelFailureReason.INVALID_MODEL_RESPONSE),
])
def test_native_rpc_codes_override_capacity_message(structured, data, expected):
    error = JsonRpcError(
        -32000, "Selected model is at capacity. Please try a different model.", data,
    )
    session = _FakeSession([error])
    model = _model_for(session)
    try:
        with pytest.raises(CodexAgentModelError) as caught:
            if structured:
                model.run_structured("answer", tools=[], result_type=Result, max_model_calls=3)
            else:
                model.run_agent_loop(
                    "answer", tools=[], max_model_calls=3, handle_text=lambda _text: None,
                )
    finally:
        model.close()
    assert caught.value.reason == expected
    assert caught.value.__cause__ is error
    assert len(session.prompts) == 1 and session.closed


@pytest.mark.parametrize("error,expected", [
    (ServerBusyError(-32000, "busy"), ModelFailureReason.PROVIDER_CAPACITY),
    (ServerBusyError(-32000, "busy", {"codexErrorInfo": "other"}), ModelFailureReason.INVALID_MODEL_RESPONSE),
    (RuntimeError("Selected model is at capacity. Please try a different model."), ModelFailureReason.PROVIDER_CAPACITY),
    (RuntimeError("rate limit exceeded"), ModelFailureReason.PROVIDER_RATE_LIMIT),
    (RuntimeError("unrelated validation mentioned capacity"), ModelFailureReason.INVALID_MODEL_RESPONSE),
])
def test_native_transient_error_uses_stable_reason_without_internal_retry(error, expected):
    session = _FakeSession([error])
    model = _model_for(session)
    try:
        with pytest.raises(CodexAgentModelError) as caught:
            model.run_structured("answer", tools=[], result_type=Result, max_model_calls=3)
    finally:
        model.close()
    assert caught.value.reason == expected
    assert len(session.prompts) == 1 and session.closed


def test_native_preserves_existing_nontransient_failure_reason():
    error = ModelFailureError(
        "Selected model is at capacity. Please try a different model.",
        reason=ModelFailureReason.REQUIRED_TOOL_MISSING,
    )
    session = _FakeSession([error])
    model = _model_for(session)
    try:
        with pytest.raises(ModelFailureError) as caught:
            model.run_structured("answer", tools=[], result_type=Result, max_model_calls=3)
    finally:
        model.close()
    assert caught.value is error
    assert caught.value.reason == ModelFailureReason.REQUIRED_TOOL_MISSING
