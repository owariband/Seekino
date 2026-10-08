from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from threading import Barrier
from types import SimpleNamespace

import pytest
from langchain_core.tools import StructuredTool
from openai_codex.generated.v2_all import ReasoningEffort

from gateway.codex.client import (
    CodexModelCallBudget,
    CodexModelCallLimitError,
    CodexSession,
    codex_reasoning_effort,
)
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
