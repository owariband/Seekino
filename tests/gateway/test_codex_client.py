from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from queue import Queue
from threading import Barrier, Event, Thread
from types import SimpleNamespace

import pytest
from langchain_core.tools import StructuredTool
from openai_codex import ImageInput, TextInput
from openai_codex.generated.v2_all import ReasoningEffort
from openai_codex.errors import TransportClosedError

from gateway.codex.client import (
    CodexModelCallBudget,
    CodexModelCallLimitError,
    CodexSession,
    _ObservedCodexClient,
    codex_reasoning_effort,
)
from gateway.codex.agent import CodexAgentModel, CodexAgentModelError
from gateway.usage import (
    capture_model_usage, control_model_calls, model_call_context, track_model_call,
)


class _FakeClient:
    def __init__(self) -> None:
        self.started = False
        self.initialized = False
        self.closed = False
        self.thread_params = None

    def start(self) -> None:
        self.started = True

    def initialize(self) -> object:
        self.initialized = True
        return {}

    def thread_start(self, params):
        self.thread_params = params
        return SimpleNamespace(thread=SimpleNamespace(id="thread-1"))

    def close(self) -> None:
        self.closed = True


def test_codex_session_starts_ephemeral_read_only_dynamic_tool_thread() -> None:
    fake = _FakeClient()
    captured = {}

    def factory(config, handler):
        captured["config"] = config
        captured["handler"] = handler
        return fake

    tool = StructuredTool.from_function(
        lambda value: value,
        name="lookup",
        description="Look up a value.",
    )
    session = CodexSession(
        model="gpt-5.3-codex",
        tools=[tool],
        reasoning_effort="high",
        codex_bin="/tmp/codex",
        cwd="/tmp",
        max_tool_calls=3,
        check_control=None,
        client_factory=factory,
    )

    session.open()
    session.close()
    session.close()

    assert fake.started is True
    assert fake.initialized is True
    assert fake.closed is True
    assert fake.thread_params["approvalPolicy"] == "never"
    assert fake.thread_params["sandbox"] == "read-only"
    assert fake.thread_params["ephemeral"] is True
    assert fake.thread_params["dynamicTools"][0]["name"] == "lookup"
    assert captured["config"].experimental_api is True
    assert "mcp_servers={}" in captured["config"].config_overrides
    assert "features.shell_tool=false" in captured["config"].config_overrides
    assert "features.unbounded_connection_retries=false" in captured["config"].config_overrides
    assert captured["handler"](
        "item/commandExecution/requestApproval", {}
    ) == {"decision": "cancel"}


def test_model_call_budget_has_a_hard_upper_bound() -> None:
    budget = CodexModelCallBudget(2)

    budget.reserve()
    budget.reserve()

    assert budget.used == 2
    with pytest.raises(CodexModelCallLimitError, match="max_model_calls"):
        budget.reserve()
    assert budget.used == 2


def test_reader_tools_keep_host_identity_and_isolate_concurrent_contexts() -> None:
    identity = ContextVar("codex_reader_test_identity", default="outside")
    barrier = Barrier(4)
    sessions = []
    handlers = []
    records_by_host = {}
    controls_by_host = {}

    class Control:
        def __init__(self):
            self.acquired = []
            self.released = []

        def acquire(self, call):
            self.acquired.append(call.call_id)

        def release(self, call):
            self.released.append(call.call_id)

    def lookup(wait: bool) -> str:
        original = identity.get()
        if wait:
            barrier.wait(timeout=2)
        with track_model_call(provider="test", model=original, operation="tool"):
            pass
        identity.set("changed inside tool")
        return original

    tool = StructuredTool.from_function(
        lookup, name="lookup", description="Read the host identity.",
    )
    for host in ("alpha", "beta"):
        records = records_by_host[host] = []
        control = controls_by_host[host] = Control()

        def factory(_config, handler):
            handlers.append(handler)
            return _FakeClient()

        token = identity.set(host)
        try:
            with (
                model_call_context({"task_id": host}),
                capture_model_usage(records.append),
                control_model_calls(control),
            ):
                session = CodexSession(
                    model="gpt-5.3-codex", tools=[tool], reasoning_effort="high",
                    codex_bin=None, cwd=None, max_tool_calls=None,
                    check_control=None, client_factory=factory,
                )
        finally:
            identity.reset(token)
        session.open()
        sessions.append(session)

    def invoke(handler, call_id, *, wait):
        return handler("item/tool/call", {
            "callId": call_id, "tool": "lookup", "arguments": {"wait": wait},
        })

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [
                pool.submit(invoke, handler, f"concurrent-{index}-{call}", wait=True)
                for index, handler in enumerate(handlers)
                for call in range(2)
            ]
            assert all(future.result(timeout=3)["success"] for future in futures)
        for index, handler in enumerate(handlers):
            assert invoke(handler, f"later-{index}", wait=False)["success"]
    finally:
        for session in sessions:
            session.close()

    for host, records in records_by_host.items():
        assert len(records) == 3
        assert all(record.model == host for record in records)
        assert all(record.context["task_id"] == host for record in records)
        control = controls_by_host[host]
        assert set(control.acquired) == {record.call_id for record in records}
        assert set(control.released) == set(control.acquired)
    assert identity.get() == "outside"


def test_none_reasoning_effort_is_sent_explicitly() -> None:
    assert codex_reasoning_effort("none") is ReasoningEffort.none
    assert codex_reasoning_effort("high") is ReasoningEffort.high


def test_codex_session_passes_real_ordered_images_to_sdk(monkeypatch) -> None:
    captured = {}
    result = SimpleNamespace(final_response='{"answer":"ok"}')

    class FakeThread:
        def __init__(self, client, thread_id):
            assert thread_id == "thread-1"

        def turn(self, inputs, **kwargs):
            captured["inputs"] = inputs
            captured["kwargs"] = kwargs
            return SimpleNamespace(run=lambda: result)

    monkeypatch.setattr("gateway.codex.client.Thread", FakeThread)
    fake = _FakeClient()
    session = CodexSession(
        model="vision-model", tools=[], reasoning_effort="high", codex_bin=None,
        cwd=None, max_tool_calls=None, check_control=None,
        client_factory=lambda *_args: fake,
    )
    inputs = [TextInput("first"), ImageInput("data:image/png;base64,YQ=="), TextInput("last")]
    schema = {"type": "object"}
    try:
        assert session.run_turn(inputs, output_schema=schema) is result
        assert session.successful_tool_call_count == 0
    finally:
        session.close()

    assert captured["inputs"] is inputs
    assert captured["kwargs"]["output_schema"] == schema
    assert fake.closed


class _TransportClient(_ObservedCodexClient):
    """Exercise the real SDK reader/router without starting a Codex process."""

    def __init__(self, config, handler, on_turn):
        super().__init__(config=config, approval_handler=handler)
        self.incoming = Queue()
        self.sent = []
        self.on_turn = on_turn

    def start(self):
        self._reader_thread = Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    def initialize(self):
        return {}

    def thread_start(self, params):
        return SimpleNamespace(thread=SimpleNamespace(id="thread-1"))

    def _read_message(self):
        message = self.incoming.get(timeout=3)
        if isinstance(message, BaseException):
            raise message
        return message

    def _write_message(self, message):
        self.sent.append(message)
        if message.get("method") == "turn/start":
            self.on_turn(self, message)

    def close(self):
        error = TransportClosedError("fake transport closed")
        self.incoming.put(error)
        self._router.fail_all(error)


def _message(text, *, item_id=None):
    return {"method": "item/completed", "params": {
        "threadId": "thread-1", "turnId": "turn-1", "completedAtMs": 1,
        "item": {"id": item_id or text, "type": "agentMessage", "text": text,
                 "phase": "final_answer" if text == "B" else "commentary"},
    }}


def _tool_request():
    return {"id": "request-1", "method": "item/tool/call", "params": {
        "callId": "call-1", "tool": "submit_video_item", "arguments": {},
    }}


def _turn_messages(client, request, *, early_tool=False, error=None):
    started = {"id": request["id"], "result": {
        "turn": {"id": "turn-1", "items": [], "status": "inProgress"},
    }}
    messages = [_message("A"), _tool_request()]
    messages.insert(2 if early_tool else 0, started)
    messages.extend([_message("A"), _message("B"), {
        "method": "turn/completed", "params": {"threadId": "thread-1", "turn": {
            "id": "turn-1", "items": [], "status": "failed" if error else "completed",
            "error": {"message": error} if error else None,
        }},
    }])
    for message in messages:
        client.incoming.put(message)


def _ordered_session(handle_text, submit, on_turn=_turn_messages):
    clients = []

    def factory(config, handler):
        client = _TransportClient(config, handler, on_turn)
        clients.append(client)
        return client

    session = CodexSession(
        model="fake", tools=[StructuredTool.from_function(
            submit, name="submit_video_item", description="Submit a reviewed item.",
        )], reasoning_effort="none", codex_bin=None, cwd=None,
        max_tool_calls=None, check_control=None, client_factory=factory,
        handle_text=handle_text,
    )
    return session, clients


def _error_notification(*, will_retry, status=None, thread_id="thread-1", turn_id="turn-1"):
    error = {"message": "model connection failed"}
    if status is not None:
        error["codexErrorInfo"] = {"httpConnectionFailed": {"httpStatusCode": status}}
    return {"method": "error", "params": {
        "threadId": thread_id, "turnId": turn_id,
        "willRetry": will_retry, "error": error,
    }}


@pytest.mark.parametrize("with_text", [False, True])
@pytest.mark.parametrize("early_error", [False, True])
@pytest.mark.parametrize("will_retry,status", [(False, None), (True, 401), (True, 403)])
def test_native_error_without_turn_completion_is_raised_and_closes_transport(
    with_text, early_error, will_retry, status,
):
    def on_turn(client, request):
        started = {"id": request["id"], "result": {
            "turn": {"id": "turn-1", "items": [], "status": "inProgress"},
        }}
        error = _error_notification(will_retry=will_retry, status=status)
        for message in ([error, started] if early_error else [started, error]):
            client.incoming.put(message)

    session, clients = _ordered_session(
        (lambda _text: None) if with_text else None, lambda: "accepted", on_turn,
    )
    model = CodexAgentModel(
        model="fake", request_timeout_seconds=600, max_output_tokens=256,
        max_input_chars=200_000, max_concurrency=1,
        session_factory=lambda **_kwargs: session,
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            pending = pool.submit(
                model.run_agent_loop, "answer", tools=[], max_model_calls=None,
                handle_text=lambda _text: None,
            )
            with pytest.raises(CodexAgentModelError) as caught:
                pending.result(timeout=2)
            assert str(caught.value.__cause__) == "model connection failed"
        finally:
            model.close()
    clients[0]._reader_thread.join(timeout=1)
    assert not clients[0]._reader_thread.is_alive()
    assert session.tool_call_count == 0


@pytest.mark.parametrize("with_text", [False, True])
@pytest.mark.parametrize("error", [
    _error_notification(will_retry=True, status=503),
    _error_notification(will_retry=False, thread_id="another-thread"),
    _error_notification(will_retry=False, turn_id="previous-turn"),
])
def test_retryable_and_other_turn_errors_do_not_fail_a_successful_turn(with_text, error):
    def on_turn(client, request):
        client.incoming.put(error)
        _turn_messages(client, request)

    displayed = []
    session, clients = _ordered_session(
        displayed.append if with_text else None, lambda: "accepted", on_turn,
    )
    try:
        assert session.run_turn("answer").final_response == "B"
    finally:
        session.close()
    assert displayed == (["A", "B"] if with_text else [])
    assert session.successful_tool_call_count == 1


@pytest.mark.parametrize("early_tool", [False, True])
def test_native_messages_and_tools_keep_reader_order_and_context(early_tool):
    identity = ContextVar("ordered_codex_identity", default="outside")
    text_started, release_text, tool_started = Event(), Event(), Event()
    displayed, usage = [], []

    def handle_text(text):
        assert identity.get() == "host"
        if text == "A":
            text_started.set()
            assert release_text.wait(timeout=2)
        displayed.append(text)

    def submit():
        tool_started.set()
        assert identity.get() == "host"
        with track_model_call(provider="fake", model="test", operation="tool"):
            pass
        displayed.append("card")
        return "accepted"

    token = identity.set("host")
    try:
        with model_call_context({"task_id": "ordered"}), capture_model_usage(usage.append):
            session, clients = _ordered_session(
                handle_text, submit,
                lambda client, request: _turn_messages(client, request, early_tool=early_tool),
            )
    finally:
        identity.reset(token)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(session.run_turn, "answer")
            assert text_started.wait(timeout=1)
            assert not tool_started.wait(timeout=0.05)
            release_text.set()
            assert result.result(timeout=2).final_response == "B"
    finally:
        release_text.set()
        session.close()
    assert displayed == ["A", "card", "B"]
    assert len(usage) == 1 and usage[0].context["task_id"] == "ordered"
    assert next(message for message in clients[0].sent if message.get("id") == "request-1")["result"] == {
        "contentItems": [{"type": "inputText", "text": "accepted"}], "success": True,
    }


def test_native_close_wakes_reader_waiting_for_queued_tool():
    text_started, release_text, tool_started = Event(), Event(), Event()

    def handle_text(text):
        text_started.set()
        assert release_text.wait(timeout=2)

    def submit():
        tool_started.set()
        return "accepted"

    session, clients = _ordered_session(handle_text, submit)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(session.run_turn, "answer")
            assert text_started.wait(timeout=1)
            session.close()
            with pytest.raises((RuntimeError, TransportClosedError)):
                result.result(timeout=1)
            clients[0]._reader_thread.join(timeout=1)
            assert not clients[0]._reader_thread.is_alive()
    finally:
        release_text.set()
        session.close()
        session._dispatcher.join(timeout=1)
    assert not tool_started.is_set()
    assert not session._dispatcher.is_alive()


def test_native_dispatch_does_not_need_a_free_model_executor_worker():
    barrier = Barrier(2)
    displayed = [[], []]

    def submit():
        barrier.wait(timeout=2)
        return "accepted"

    sessions = [_ordered_session(parts.append, submit)[0] for parts in displayed]
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(session.run_turn, "answer") for session in sessions]
            assert [future.result(timeout=3).final_response for future in futures] == ["B", "B"]
    finally:
        for session in sessions:
            session.close()
    assert displayed == [["A", "B"], ["A", "B"]]
    assert all(session.successful_tool_call_count == 1 for session in sessions)


@pytest.mark.parametrize("callback_fails", [False, True])
@pytest.mark.parametrize("early_tool", [False, True])
def test_native_observation_preserves_original_terminal_error(callback_fails, early_tool):
    error = RuntimeError("visible callback failed")

    def handle_text(text):
        if callback_fails:
            raise error

    session, _ = _ordered_session(
        handle_text, lambda: "accepted",
        lambda client, request: _turn_messages(
            client, request, early_tool=early_tool, error="upstream HTTP 429",
        ),
    )
    try:
        with pytest.raises(RuntimeError, match="visible callback failed" if callback_fails else "upstream HTTP 429") as caught:
            session.run_turn("answer")
    finally:
        session.close()
    if callback_fails:
        assert caught.value is error
