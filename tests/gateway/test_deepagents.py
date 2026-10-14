from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from pydantic import PrivateAttr

from gateway.agent_model.context import ContextWindowPolicy
from gateway.agent_model.deepagents import DeepAgentSessionStore, DeepAgentsSupervisorModel
from gateway.agent_model.errors import AgentModelTimeoutError, ModelFailureError, ModelFailureReason, ModelInputTooLargeError
from gateway.ark.agent import ArkFunctionCallingFallback, ArkFunctionCallingModel
from gateway.media import ToolImage, ToolImageArtifact
from gateway.openai.agent import OpenAIAgentModel
from gateway.usage import capture_model_usage, model_call_context


class ScriptedModel(BaseChatModel):
    responses: list[AIMessage]
    _calls: list[dict[str, Any]] = PrivateAttr(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "test"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self._calls.append({"messages": deepcopy(messages), "tools": kwargs.get("tools", [])})
        if "Context Extraction Assistant" in "\n".join(message.text for message in messages):
            response = AIMessage(content="Summary: the user asked about the earlier course.")
        else:
            response = self.responses.pop(0)
        return ChatResult(generations=[ChatGeneration(message=response)])


def _model(script, *, store=None, policy=None, vision=False, total_timeout_enabled=True):
    return DeepAgentsSupervisorModel(
        session_store=store or DeepAgentSessionStore(), model="fake", provider_name="fake",
        request_timeout_seconds=30, max_input_chars=120_000,
        context_window_policy=policy, supports_vision_input=vision, chat_model=script,
        total_timeout_enabled=total_timeout_enabled,
    )


def _run(model, *, thread="conversation", message="answer", history=(), tools=(), check=None, limit=10, prompt="Answer from approved course evidence."):
    chunks = []
    model.run_agent(
        prompt, thread_id=thread, message_id=message,
        user_message=f"Question for {message}", history=history, tools=tools,
        max_model_calls=limit, handle_text=chunks.append, check_control=check,
    )
    return chunks


def test_sdk_owns_tool_loop_and_exposes_only_virtual_read_file():
    calls = []
    tool = StructuredTool.from_function(
        lambda query: calls.append(query) or "Approved fact",
        name="search_video_graph", description="Retrieve approved video evidence.",
    )
    script = ScriptedModel(responses=[
        AIMessage(content="", tool_calls=[{"name": tool.name, "args": {"query": "course"}, "id": "tool-1"}]),
        AIMessage(content="The approved answer."),
    ])
    model = _model(script)

    assert _run(model, tools=[tool]) == ["The approved answer."]
    assert calls == ["course"]
    assert {item.name for item in script._calls[0]["tools"]} == {"search_video_graph", "read_file"}
    assert any(isinstance(item, ToolMessage) and item.content == "Approved fact" for item in script._calls[1]["messages"])
    model.confirm_turn("conversation", "answer", "Approved stored answer.")


def test_warm_thread_keeps_tool_state_and_only_adds_current_user():
    script = ScriptedModel(responses=[AIMessage(content="Draft one"), AIMessage(content="Draft two")])
    model = _model(script)
    history = [
        {"messageId": "old-user", "role": "USER", "content": "Earlier question"},
        {"messageId": "old-user", "role": "USER", "content": "Earlier question"},
        {"messageId": "old-answer", "role": "ASSISTANT", "content": "Earlier answer"},
        {"messageId": "failed", "role": "ASSISTANT", "content": "Failed answer", "status": "FAILED"},
    ]
    _run(model, message="a1", history=history, prompt="Scope: first course; playback at 10 seconds.")
    model.confirm_turn("conversation", "a1", "Approved one")
    _run(model, message="a2", history=[*history, {"role": "assistant", "content": "Approved one"}], prompt="Scope: second course; playback at 50 seconds.")
    texts = [item.text for item in script._calls[-1]["messages"]]

    assert texts.count("Earlier question") == 1
    assert texts.count("Earlier answer") == 1
    assert texts.count("Approved one") == 1
    assert "Draft one" not in texts
    assert "Failed answer" not in texts
    assert texts[-1] == "Question for a2"
    assert "Scope: second course; playback at 50 seconds." in texts[0]
    assert "Scope: first course" not in texts[0]


def test_sdk_state_is_isolated_by_conversation_and_shared_after_model_reload():
    store = DeepAgentSessionStore()
    first = _model(ScriptedModel(responses=[AIMessage(content="First")]), store=store)
    _run(first, thread="one", message="a1")
    first.confirm_turn("one", "a1", "First stored")
    first.close()
    script = ScriptedModel(responses=[AIMessage(content="Second"), AIMessage(content="Third")])
    reloaded = _model(script, store=store)
    _run(reloaded, thread="two", message="a2")
    reloaded.confirm_turn("two", "a2", "Second stored")
    _run(reloaded, thread="one", message="a3")

    assert "First stored" not in [item.text for item in script._calls[0]["messages"]]
    assert "First stored" in [item.text for item in script._calls[1]["messages"]]
    assert "Second stored" not in [item.text for item in script._calls[1]["messages"]]
    reloaded.delete_thread("one")
    assert store.checkpointer.get_tuple({"configurable": {"thread_id": "one"}}) is None
    store.close()
    assert store.checkpointer.get_tuple({"configurable": {"thread_id": "two"}}) is None


def test_unconfirmed_turn_cannot_resume_and_discard_bootstraps_completed_history():
    script = ScriptedModel(responses=[AIMessage(content="Unsaved"), AIMessage(content="Fresh")])
    model = _model(script)
    _run(model, message="a1")
    with pytest.raises(RuntimeError, match="not been confirmed"):
        _run(model, message="a2")
    model.discard_turn("conversation", "wrong-message")
    assert "conversation" in model.session_store.pending
    model.discard_turn("conversation", "a1")
    _run(model, message="a2", history=[{"role": "assistant", "content": "Database history"}])

    texts = [item.text for item in script._calls[-1]["messages"]]
    assert "Unsaved" not in texts
    assert "Database history" in texts


@pytest.mark.parametrize("total_timeout_enabled", [True, False])
def test_cancelled_tool_clears_checkpoint_and_does_not_execute_next_tool(total_timeout_enabled):
    cancelled = False
    calls = []

    def search(query: str) -> str:
        nonlocal cancelled
        calls.append(query)
        cancelled = True
        return "result"

    def check():
        if cancelled:
            raise RuntimeError("Cancelled by user")

    tool = StructuredTool.from_function(search, description="Search course evidence.")
    script = ScriptedModel(responses=[AIMessage(content="", tool_calls=[
        {"name": tool.name, "args": {"query": "first"}, "id": "one"},
        {"name": tool.name, "args": {"query": "second"}, "id": "two"},
    ])])
    model = _model(script, total_timeout_enabled=total_timeout_enabled)
    with pytest.raises(RuntimeError, match="Cancelled by user"):
        _run(model, tools=[tool], check=check)

    assert calls == ["first"]
    assert not model.session_store.pending
    assert model.session_store.checkpointer.get_tuple({"configurable": {"thread_id": "conversation"}}) is None


def test_call_limit_is_sdk_enforced_and_failed_turn_is_deleted():
    tool = StructuredTool.from_function(lambda query: "fact", name="search", description="Search evidence.")
    script = ScriptedModel(responses=[AIMessage(content="", tool_calls=[
        {"name": "search", "args": {"query": "query"}, "id": "tool-1"},
    ])])
    model = _model(script)
    with pytest.raises(ModelFailureError) as failure:
        _run(model, tools=[tool], limit=1)
    assert failure.value.reason == ModelFailureReason.LOOP_LIMIT
    assert len(script._calls) == 1
    assert not model.session_store.pending


def test_sdk_summary_offloads_history_without_own_context_controller():
    policy = ContextWindowPolicy(
        context_window_tokens=8_000, output_reserve_tokens=500,
        safety_margin_tokens=500, auto_compact_tokens=1_500, compact_target_tokens=200,
    )
    script = ScriptedModel(responses=[AIMessage(content="After summary")])
    model = _model(script, policy=policy)
    history = [
        {"messageId": f"history-{index}", "role": "user" if index % 2 == 0 else "assistant", "content": "Earlier detailed facts " * 100}
        for index in range(10)
    ]
    assert _run(model, history=history) == ["After summary"]
    assert len(script._calls) == 2
    assert any("Summary: the user asked" in item.text for item in script._calls[-1]["messages"])
    graph = model.session_store.pending["conversation"].graph
    state = graph.get_state({"configurable": {"thread_id": "conversation"}}).values
    assert any(path.startswith("/conversation_history/") for path in state["files"])
    assert len(state["messages"]) == 12


def test_summary_model_request_consumes_the_same_logical_call_budget():
    policy = ContextWindowPolicy(
        context_window_tokens=8_000, output_reserve_tokens=500,
        safety_margin_tokens=500, auto_compact_tokens=1_500, compact_target_tokens=200,
    )
    script = ScriptedModel(responses=[AIMessage(content="Should not run")])
    model = _model(script, policy=policy)
    history = [{"role": "user", "content": "Earlier facts " * 500} for _ in range(6)]
    with pytest.raises(ModelFailureError) as failure:
        _run(model, history=history, limit=1)
    assert failure.value.reason == ModelFailureReason.LOOP_LIMIT
    assert len(script._calls) == 1
    assert "Context Extraction Assistant" in script._calls[0]["messages"][-1].text
    assert not model.session_store.pending


def test_tool_image_artifact_becomes_model_visible_user_image():
    artifact = ToolImageArtifact((ToolImage("data:image/png;base64," + "A" * 200_000, 150_000, "Current playback frame"),))
    tool = StructuredTool.from_function(
        lambda: ("Frame evidence", artifact), name="understand_current_video",
        description="Observe the current frame.", response_format="content_and_artifact",
    )
    search = StructuredTool.from_function(lambda query: "Approved fact", name="search", description="Search video facts.")
    script = ScriptedModel(responses=[
        AIMessage(content="", tool_calls=[
            {"name": tool.name, "args": {}, "id": "frame"},
            {"name": search.name, "args": {"query": "caption"}, "id": "caption"},
        ]),
        AIMessage(content="A visual answer"),
    ])
    model = _model(script, vision=True)
    assert _run(model, tools=[tool, search]) == ["A visual answer"]
    received = script._calls[-1]["messages"]
    image_indexes = [index for index, message in enumerate(received) if isinstance(message, HumanMessage) and isinstance(message.content, list)]
    tool_indexes = [index for index, message in enumerate(received) if isinstance(message, ToolMessage)]
    assert image_indexes and len(tool_indexes) == 2
    assert min(image_indexes) > max(tool_indexes)
    assert any(block.get("type") == "image_url" for block in received[image_indexes[0]].content)
    assert all(not isinstance(message, ToolMessage) or message.artifact is None for message in received)


def test_cold_history_larger_than_character_budget_is_trimmed_by_sdk_summary():
    policy = ContextWindowPolicy(
        context_window_tokens=64_000, output_reserve_tokens=500,
        safety_margin_tokens=500, auto_compact_tokens=8_000, compact_target_tokens=1_000,
    )
    script = ScriptedModel(responses=[AIMessage(content="Resumed answer")])
    model = _model(script, policy=policy)
    history = [{"role": "user" if index % 2 == 0 else "assistant", "content": "Earlier facts " * 1_300} for index in range(10)]
    assert _run(model, history=history) == ["Resumed answer"]
    assert len(script._calls) == 2
    assert len(script._calls[0]["messages"][-1].text) < 120_000


def test_oversized_tool_schema_is_rejected_before_model_request():
    script = ScriptedModel(responses=[AIMessage(content="Must not be requested")])
    model = _model(script)
    tool = StructuredTool.from_function(lambda: "fact", name="large", description="x" * 130_000)
    with pytest.raises(ModelInputTooLargeError):
        _run(model, tools=[tool])
    assert not script._calls
    assert not model.session_store.pending


def test_non_compactable_tool_contract_respects_token_budget():
    policy = ContextWindowPolicy(
        context_window_tokens=6_000, output_reserve_tokens=500,
        safety_margin_tokens=500, auto_compact_tokens=4_500, compact_target_tokens=1_000,
    )
    script = ScriptedModel(responses=[AIMessage(content="Must not be requested")])
    model = _model(script, policy=policy)
    tool = StructuredTool.from_function(lambda: "fact", name="large", description="中文" * 5_000)
    with pytest.raises(ModelInputTooLargeError):
        _run(model, tools=[tool])
    assert not script._calls


def _completion(content="Provider answer", *, calls=(), finish="stop"):
    return SimpleNamespace(
        id="response", model="actual-provider-model", usage=SimpleNamespace(prompt_tokens=13, completion_tokens=7, total_tokens=20),
        choices=[SimpleNamespace(finish_reason=finish, message=SimpleNamespace(role="assistant", content=content, tool_calls=[
            SimpleNamespace(id=identity, type="function", function=SimpleNamespace(name=name, arguments=json.dumps(args)))
            for identity, name, args in calls
        ]))],
    )


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


@pytest.mark.parametrize("total_timeout_enabled", [True, False])
def test_supervisor_total_timeout_switch_preserves_finite_http_budget(monkeypatch, total_timeout_enabled):
    clock = [100.0]
    monkeypatch.setattr("gateway.agent_model.deepagents.monotonic", lambda: clock[0])

    def search(query: str) -> str:
        clock[0] += 40
        return "fact"

    tool = StructuredTool.from_function(search, description="Search evidence.")
    client = FakeClient([
        _completion(content="", calls=[("search-1", "search", {"query": "q"})], finish="tool_calls"),
        _completion(content="Answer after a slow tool"),
    ])
    adapter = OpenAIAgentModel(
        "model", base_url="https://unused.invalid", api_key="test", max_output_tokens=100,
        request_timeout_seconds=2, client=client,
    )
    model = DeepAgentsSupervisorModel(
        session_store=DeepAgentSessionStore(), model="model", provider_name="openai",
        request_timeout_seconds=3, max_input_chars=120_000, context_window_policy=None,
        agent_model=adapter, total_timeout_enabled=total_timeout_enabled,
    )
    try:
        if total_timeout_enabled:
            with pytest.raises(AgentModelTimeoutError, match="execution deadline expired"):
                _run(model, tools=[tool])
            assert len(client.calls) == 1
            assert not model.session_store.pending
        else:
            assert _run(model, tools=[tool]) == ["Answer after a slow tool"]
            assert len(client.calls) == 2
        assert clock[0] == 140
        assert all(0 < request["timeout"] <= 2 for request in client.calls)
    finally:
        model.close()
        model.session_store.close()


def test_api_adapter_retains_profile_tokens_usage_and_never_calls_old_loop(monkeypatch):
    client = FakeClient([_completion()])
    adapter = OpenAIAgentModel(
        "deepseek-model", base_url="https://unused.invalid", api_key="test", max_output_tokens=1234,
        request_timeout_seconds=17, provider_name="deepseek", profile="deepseek", reasoning_effort="high", client=client,
    )
    monkeypatch.setattr(adapter, "run_agent_loop", lambda *_args, **_kwargs: pytest.fail("old loop called"))
    model = DeepAgentsSupervisorModel(
        session_store=DeepAgentSessionStore(), model=adapter.model_name, provider_name=adapter.provider_name,
        request_timeout_seconds=30, max_input_chars=120_000, context_window_policy=None, agent_model=adapter,
    )
    usage = []
    with capture_model_usage(usage.append), model_call_context({"user_id": "owner", "conversation_id": "conversation"}):
        assert _run(model) == ["Provider answer"]
    request = client.calls[0]
    assert request["max_tokens"] == 1234
    assert request["reasoning_effort"] == "high"
    assert request["extra_body"] == {"thinking": {"type": "enabled"}}
    assert 0 < request["timeout"] <= 17
    assert len(usage) == 1 and usage[0].usage.total_tokens == 20
    assert usage[0].model == "actual-provider-model"
    assert usage[0].context["user_id"] == "owner"
    graph = model.session_store.pending["conversation"].graph
    message = graph.get_state({"configurable": {"thread_id": "conversation"}}).values["messages"][-1]
    assert message.usage_metadata == {"input_tokens": 13, "output_tokens": 7, "total_tokens": 20}


@pytest.mark.parametrize("bad_json", ["", '{"query":'])
def test_invalid_provider_tool_arguments_return_receipt_and_allow_sdk_correction(bad_json):
    bad_response = _completion(content="", calls=[("bad", "search", {})], finish="tool_calls")
    bad_response.choices[0].message.tool_calls[0].function.arguments = bad_json
    client = FakeClient([
        bad_response,
        _completion(content="", calls=[("fixed", "search", {"query": "corrected"})], finish="tool_calls"),
        _completion(content="Corrected answer"),
    ])
    adapter = OpenAIAgentModel(
        "model", base_url="https://unused.invalid", api_key="test",
        max_output_tokens=100, request_timeout_seconds=20, client=client,
    )
    calls = []
    tool = StructuredTool.from_function(lambda query: calls.append(query) or "fact", name="search", description="Search evidence.")
    model = DeepAgentsSupervisorModel(
        session_store=DeepAgentSessionStore(), model="model", provider_name="openai",
        request_timeout_seconds=30, max_input_chars=120_000, context_window_policy=None, agent_model=adapter,
    )
    assert _run(model, tools=[tool]) == ["Corrected answer"]
    assert calls == ["corrected"]
    receipt = next(message for message in client.calls[1]["messages"] if message.get("tool_call_id") == "bad")
    assert "Invalid arguments" in receipt["content"]


def test_api_fallback_preserves_ark_controls_and_is_sticky_for_turn(monkeypatch):
    primary = FakeClient([_completion(finish="length")])
    fallback = FakeClient([
        _completion(content="", calls=[("search-1", "search", {"query": "q"})], finish="tool_calls"),
        _completion(content="Fallback answer"),
    ])
    ark = ArkFunctionCallingFallback("fallback-model", max_output_tokens=77, request_timeout_seconds=9, client=fallback)
    adapter = OpenAIAgentModel(
        "primary-model", base_url="https://unused.invalid", api_key="test", max_output_tokens=88,
        request_timeout_seconds=20, client=primary, fallback_adapter=ark,
    )
    monkeypatch.setattr(adapter, "run_agent_loop", lambda *_args, **_kwargs: pytest.fail("old loop called"))
    tool = StructuredTool.from_function(lambda query: "fact", name="search", description="Search evidence.")
    model = DeepAgentsSupervisorModel(
        session_store=DeepAgentSessionStore(), model=adapter.model_name, provider_name=adapter.provider_name,
        request_timeout_seconds=30, max_input_chars=120_000, context_window_policy=None, agent_model=adapter,
    )
    usage = []
    with capture_model_usage(usage.append):
        assert _run(model, tools=[tool]) == ["Fallback answer"]
    assert len(primary.calls) == 1
    assert len(fallback.calls) == 2
    assert all(request["max_completion_tokens"] == 77 and request["timeout"] <= 9 for request in fallback.calls)
    assert [item.provider for item in usage] == ["openai", "ark", "ark"]
    assert [item.status for item in usage] == ["failed", "completed", "completed"]


def test_ark_source_uses_existing_single_request_and_same_source_fallback():
    client = FakeClient([_completion(finish="length"), _completion(content="Ark answer")])
    adapter = ArkFunctionCallingModel(
        "primary", fallback_model="fallback", max_output_tokens=90, request_timeout_seconds=20,
        fallback_max_output_tokens=55, fallback_request_timeout_seconds=8, client=client,
    )
    model = DeepAgentsSupervisorModel(
        session_store=DeepAgentSessionStore(), model="primary", provider_name="ark", request_timeout_seconds=30,
        max_input_chars=120_000, context_window_policy=None, agent_model=adapter,
    )
    assert _run(model) == ["Ark answer"]
    assert [request["model"] for request in client.calls] == ["primary", "fallback"]
    assert client.calls[1]["max_completion_tokens"] == 55
    assert client.calls[1]["timeout"] <= 8
