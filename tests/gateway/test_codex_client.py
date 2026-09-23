from __future__ import annotations

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


def test_none_reasoning_effort_is_sent_explicitly() -> None:
    assert codex_reasoning_effort("none") is ReasoningEffort.none
    assert codex_reasoning_effort("high") is ReasoningEffort.high
