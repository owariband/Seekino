from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.tools import StructuredTool

from gateway.agent_model.errors import ModelInputTooLargeError
from gateway.codex.agent import CodexAgentModel
from gateway.codex.protocol import CodexToolBridge
from gateway.codex.supervisor import CodexSupervisorModel
from gateway.media import ToolImage, ToolImageArtifact
from gateway.usage import capture_model_usage, model_call_context


class LoopModel:
    provider_name = "codex"
    model_name = "configured-supervisor-model"
    supports_vision_input = True

    def __init__(self) -> None:
        self.calls = []
        self.error = None
        self.closed = 0

    def run_agent_loop(self, prompt, **options):
        self.calls.append((prompt, options))
        if options.get("check_control") is not None:
            options["check_control"]()
        if self.error is not None:
            raise self.error
        options["handle_text"]("native draft")

    def close(self):
        self.closed += 1


def run(model, *, thread="conversation", message="answer", history=(), tools=(), **overrides):
    texts = []
    options = dict(
        thread_id=thread, message_id=message, user_message="当前问题",
        history=history, tools=tools, max_model_calls=4,
        handle_text=texts.append,
    )
    options.update(overrides)
    model.run_agent("Current series background", **options)
    return texts


def test_completed_history_is_filtered_and_next_turn_uses_sql_final_answer():
    loop = LoopModel()
    model = CodexSupervisorModel(agent_model=loop)
    history = [
        {"messageId": "u1", "role": "USER", "content": "earlier question", "status": "COMPLETED"},
        {"messageId": "u1", "role": "USER", "content": "duplicate"},
        {"messageId": "a1", "role": "ASSISTANT", "content": "earlier answer"},
        {"role": "ASSISTANT", "content": "failed draft", "status": "FAILED"},
        {"role": "SYSTEM", "content": "not stored conversation content"},
        {"role": "USER", "content": None},
    ]

    assert run(model, message="a2", history=history) == ["native draft"]
    assert loop.calls[0][1]["initial_messages"] == [
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier answer"},
    ]
    assert loop.calls[0][0].endswith('Current user message:\n"当前问题"')
    model.confirm_turn("conversation", "a2", "SQL final answer")
    run(model, message="a3", history=[{
        "messageId": "a2", "role": "ASSISTANT", "content": "SQL final answer",
        "status": "COMPLETED",
    }])
    assert loop.calls[1][1]["initial_messages"] == [{"role": "assistant", "content": "SQL final answer"}]


def test_pending_turn_requires_matching_confirmation_and_other_threads_can_run():
    loop = LoopModel()
    model = CodexSupervisorModel(agent_model=loop)
    run(model, message="first")
    model.confirm_turn("conversation", "stale", "unrelated final")
    model.discard_turn("conversation", "stale")
    with pytest.raises(RuntimeError, match="not been confirmed"):
        run(model, message="second")
    assert run(model, thread="other", message="other-answer") == ["native draft"]
    assert len(loop.calls) == 2
    model.confirm_turn("conversation", "first", "stored answer")
    assert run(model, message="second") == ["native draft"]


def test_failure_discard_and_deletion_allow_rebuilding_from_completed_history():
    loop = LoopModel()
    model = CodexSupervisorModel(agent_model=loop)
    loop.error = RuntimeError("native failure")
    with pytest.raises(RuntimeError, match="native failure"):
        run(model, message="failed")
    loop.error = None
    run(model, message="discarded")
    model.discard_turn("conversation", "discarded")
    run(model, message="deleted")
    model.delete_thread("conversation")
    assert run(model, message="rebuilt") == ["native draft"]
    assert loop.calls[-1][1]["initial_messages"] is None


def test_run_forwards_tools_budget_timeout_and_cancellation_without_overrides():
    loop = LoopModel()
    model = CodexSupervisorModel(agent_model=loop)
    tool = StructuredTool.from_function(lambda: "evidence", name="understand_current_video", description="Get video evidence.")
    checks = []
    check = lambda: checks.append("checked")
    run(model, tools=[tool], max_model_calls=2, request_timeout_seconds=3,
        max_input_chars=4_000, check_control=check)
    options = loop.calls[0][1]
    assert options["tools"] == [tool]
    assert options["max_model_calls"] == 2
    assert options["request_timeout_seconds"] == 3
    assert options["max_input_chars"] == 4_000
    assert options["check_control"] is check
    assert checks == ["checked"]


def test_close_is_idempotent_and_prevents_another_native_run():
    loop = LoopModel()
    model = CodexSupervisorModel(agent_model=loop)
    run(model)
    model.close()
    model.close()
    assert loop.closed == 1
    with pytest.raises(RuntimeError, match="Supervisor is closed"):
        run(model, message="after-close")
    assert len(loop.calls) == 1


class NativeSession:
    def __init__(self, options):
        self.options = options
        self.prompts = []
        self.closed = False
        self.tool_call_count = 0
        self.successful_tool_call_count = 0
        self.receipt = None

    def run_turn(self, prompt, *, output_schema=None):
        self.prompts.append((prompt, output_schema))
        if self.options["tools"]:
            tool = self.options["tools"][0]
            self.receipt = CodexToolBridge(
                self.options["tools"], check_control=self.options["check_control"],
            ).handle("item/tool/call", {"callId": "video", "tool": tool.name, "arguments": {}})
            self.tool_call_count = self.successful_tool_call_count = 1
        return SimpleNamespace(final_response="native answer", usage=SimpleNamespace(total=SimpleNamespace(
            input_tokens=10, output_tokens=4, total_tokens=14,
        )))

    def close(self):
        self.closed = True


def native_model():
    sessions = []

    def create(**options):
        session = NativeSession(options)
        sessions.append(session)
        return session

    loop = CodexAgentModel(
        model="configured-native-model", reasoning_effort="low",
        request_timeout_seconds=2, max_output_tokens=1024, max_input_chars=10_000,
        max_concurrency=1, session_factory=create,
    )
    loop.supports_vision_input = True
    return CodexSupervisorModel(agent_model=loop), sessions


def test_native_turns_create_fresh_sessions_and_rebind_evidence_tools_with_images():
    model, sessions = native_model()
    usage = []

    def tool_for(caption):
        artifact = ToolImageArtifact((ToolImage("data:image/png;base64,AAAA", 3, caption),))
        return StructuredTool.from_function(
            lambda: (caption, artifact), name="understand_current_video",
            description="Get current video evidence.", response_format="content_and_artifact",
        )

    try:
        with model_call_context({"assistant_message_id": "a1"}), capture_model_usage(usage.append):
            assert run(model, message="a1", tools=[tool_for("first frame caption")]) == ["native answer"]
        model.confirm_turn("conversation", "a1", "stored final")
        with model_call_context({"assistant_message_id": "a2"}), capture_model_usage(usage.append):
            assert run(model, message="a2", tools=[tool_for("updated frame caption")],
                       history=[{"role": "ASSISTANT", "content": "stored final"}]) == ["native answer"]
        assert len(sessions) == 2 and all(session.closed for session in sessions)
        assert sessions[0].receipt["contentItems"][0]["text"] == "first frame caption"
        assert sessions[1].receipt["contentItems"][0]["text"] == "updated frame caption"
        assert sessions[1].receipt["contentItems"][-1] == {"type": "inputImage", "imageUrl": "data:image/png;base64,AAAA"}
        assert "stored final" in sessions[1].prompts[0][0]
        assert "native answer" not in sessions[1].prompts[0][0]
        assert all(session.prompts[0][1] is None for session in sessions)
        assert [item.context["assistant_message_id"] for item in usage] == ["a1", "a2"]
        assert all(item.model == "configured-native-model" and item.granularity == "sdk_turn_run" for item in usage)
        assert [item.usage.total_tokens for item in usage] == [14, 14]
    finally:
        model.close()


def test_native_history_budget_is_checked_before_start_and_failed_turn_can_retry():
    model, sessions = native_model()
    try:
        with pytest.raises(ModelInputTooLargeError):
            run(model, message="too-large", history=[{"role": "USER", "content": "x" * 10_000}])
        assert sessions == []
        assert run(model, message="retry") == ["native answer"]
    finally:
        model.close()


def test_native_cancellation_before_start_clears_pending_turn():
    model, sessions = native_model()

    def cancelled():
        raise RuntimeError("cancelled")

    try:
        with pytest.raises(RuntimeError, match="cancelled"):
            run(model, message="cancelled", check_control=cancelled)
        assert sessions == []
        assert run(model, message="retry") == ["native answer"]
    finally:
        model.close()
