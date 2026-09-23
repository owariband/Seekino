from __future__ import annotations

import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, ConfigDict

from gateway.harness.errors import (
    AgentModelTimeoutError,
    MissingRequiredFunctionCallError,
    MissingStructuredQueryDraftError,
    ModelFailureReason,
    ModelInputTooLargeError,
)
from gateway.codex import CodexAgentModel
from gateway.codex.client import CodexModelCallLimitError
from gateway.codex.agent import CodexAgentModelError
from gateway.harness.context import ContextWindowPolicy
from gateway.usage import capture_model_usage


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
) -> object:
    def breakdown(input_tokens: int, output_tokens: int) -> object:
        return SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            cached_input_tokens=min(2, input_tokens),
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
    assert usages[0].usage.input_tokens == 10
    assert usages[0].usage.reasoning_tokens == 1


def test_context_preflight_reserves_codex_base_input_before_starting_session() -> None:
    created: list[object] = []

    def factory(**_kwargs: object) -> _FakeSession:
        created.append(object())
        return _FakeSession([(_turn("unused"), 0)])

    model = CodexAgentModel(
        model="gpt-5.3-codex",
        request_timeout_seconds=2,
        max_output_tokens=1_024,
        max_input_chars=10_000,
        context_window_policy=_context_policy(13_000),
        session_factory=factory,
    )
    try:
        with pytest.raises(ModelInputTooLargeError, match="fixed_input_tokens=13000"):
            model.run_agent_loop(
                "short prompt",
                tools=[],
                max_model_calls=1,
                handle_text=lambda _text: None,
            )
    finally:
        model.close()

    assert created == []


def test_context_preflight_counts_dynamic_tool_contract() -> None:
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
        return _FakeSession([(_turn("unused"), 0)])

    model = CodexAgentModel(
        model="gpt-5.3-codex",
        request_timeout_seconds=2,
        max_output_tokens=1_024,
        max_input_chars=10_000,
        context_window_policy=_context_policy(20_000),
        session_factory=factory,
    )
    try:
        with pytest.raises(ModelInputTooLargeError, match="initial context"):
            model.run_agent_loop(
                "short prompt",
                tools=[tool],
                max_model_calls=1,
                handle_text=lambda _text: None,
            )
    finally:
        model.close()

    assert created == []


def test_context_preflight_allows_a_small_request_below_the_hard_limit() -> None:
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


def test_run_structured_rejects_invalid_final_json() -> None:
    session = _FakeSession([(_turn("not-json"), 0)])
    model = _model_for(session)
    try:
        with pytest.raises(MissingStructuredQueryDraftError) as captured:
            model.run_structured(
                "answer",
                tools=[],
                result_type=Result,
                max_model_calls=2,
            )
    finally:
        model.close()

    assert captured.value.reason == ModelFailureReason.INVALID_MODEL_RESPONSE


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
    assert usages[0].usage.input_tokens == 25
    assert usages[0].usage.output_tokens == 9
    assert usages[0].usage.total_tokens == 34


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


def test_input_limit_is_enforced_before_starting_codex() -> None:
    session = _FakeSession([])
    model = _model_for(session, max_input_chars=4)
    try:
        with pytest.raises(ModelInputTooLargeError):
            model.run_agent_loop(
                "too long",
                tools=[],
                max_model_calls=1,
                handle_text=lambda _text: None,
            )
    finally:
        model.close()

    assert session.prompts == []


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
