from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event
from types import SimpleNamespace

import pytest
from langchain_core.tools import StructuredTool

from gateway.agent_model.errors import ModelInputTooLargeError
from gateway.agent_model.contracts import SupervisorSessionContext
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


def test_current_and_completed_user_history_images_are_forwarded_once():
    loop = LoopModel()
    model = CodexSupervisorModel(agent_model=loop)
    earlier, current = "data:image/png;base64,AAAA", "data:image/png;base64,BBBB"
    history = [
        {"messageId": "u1", "role": "USER", "content": "Earlier image", "imageUrls": [earlier]},
        {"messageId": "u1", "role": "USER", "content": "Duplicate", "imageUrls": [current]},
        {"role": "ASSISTANT", "content": "Earlier answer", "imageUrls": [current]},
        {"role": "USER", "content": "Failed", "status": "FAILED", "imageUrls": [current]},
    ]

    run(model, history=history, image_urls=[current])

    prompt, options = loop.calls[0]
    assert prompt.endswith('Current user message:\n"当前问题"')
    assert options["image_urls"] == [current]
    assert options["initial_messages"] == [
        {"role": "user", "content": [
            {"type": "text", "text": "Earlier image"},
            {"type": "image_url", "image_url": {"url": earlier}},
        ]},
        {"role": "assistant", "content": "Earlier answer"},
        {"role": "user", "content": "当前问题"},
    ]
    model.confirm_turn("conversation", "answer", "Stored answer")
    assert run(model, message="image-only", user_message="", image_urls=[current]) == ["native draft"]


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


class PersistentLoop(LoopModel):
    session_key = "codex:local-storage"

    def __init__(self):
        super().__init__()
        self.threads = {}
        self.events = []
        self.session_closed = False

    def run_agent_loop(self, prompt, **options):
        self.calls.append((prompt, options))
        native_id = options["native_thread_id"] or f"native-{len(self.threads)}"
        self.events.append(("resume" if options["native_thread_id"] else "start", native_id))
        thread = self.threads.setdefault(native_id, {"turns": [], "messages": []})
        loop = self

        class Session:
            native_thread_id = native_id
            stale_last_turn_id = None

            def open(self):
                if loop.session_closed:
                    raise RuntimeError("Codex session is closed")

            @property
            def native_last_turn_id(self):
                if self.stale_last_turn_id is not None:
                    return self.stale_last_turn_id
                return thread["turns"][-1] if thread["turns"] else None

            def inject_messages(self, messages):
                loop.events.append(("inject", deepcopy(messages)))
                if getattr(loop, "inject_error", None) is not None:
                    raise loop.inject_error
                thread["messages"].extend(deepcopy(messages))

            def rollback_last_turn(self):
                if getattr(loop, "revert_error", None) is not None:
                    raise loop.revert_error
                if getattr(loop, "revert_after_apply_error", None) is not None:
                    self.stale_last_turn_id = thread["turns"].pop()
                    loop.events.append(("revert", self.stale_last_turn_id))
                    raise loop.revert_after_apply_error
                loop.events.append(("revert", thread["turns"].pop()))

        options["on_session_ready"](Session())
        if getattr(self, "cancel_before_turn", False):
            raise RuntimeError("cancelled before current turn")
        self.events.append(("turn", native_id))
        thread["turns"].append(f"turn-{len(thread['turns'])}")
        if self.error is not None:
            raise self.error
        options["handle_text"]("native draft")


def session_context(states, history, sequence, *, save=None):
    def save_state(key, state):
        states[key] = deepcopy(state)

    return SupervisorSessionContext(
        states=deepcopy(states), message_sequence_no=sequence,
        load_history=lambda after: [item for item in history if item["sequenceNo"] > after],
        save_state=save or save_state,
    )


def test_same_codex_thread_catches_up_external_messages_after_model_and_runtime_switch():
    loop = PersistentLoop()
    model = CodexSupervisorModel(agent_model=loop)
    states = {}
    run(model, message="a0", session_context=session_context(states, [], 1))
    assert states[loop.session_key]["threadId"] == "native-0"
    assert states[loop.session_key]["syncedThroughSequence"] == -1
    model.confirm_turn("conversation", "a0", "native draft")

    history = [
        {"role": "USER" if sequence % 2 == 0 else "ASSISTANT", "content": f"message {sequence}",
         "messageId": f"m{sequence}", "sequenceNo": sequence, "status": "COMPLETED"}
        for sequence in range(16)
    ]
    loop.model_name = "another-codex-model"
    model = CodexSupervisorModel(agent_model=loop)
    run(model, message="a1", history=history[-12:], session_context=session_context(states, history, 17))
    assert loop.events[2] == ("resume", "native-0")
    injected = loop.events[3][1]
    assert len(injected) == 14
    assert [item["messageId"] for item in injected] == [f"m{i}" for i in range(2, 16)]
    assert [item["role"] for item in injected] == ["user", "assistant"] * 7
    assert loop.events[4] == ("turn", "native-0")
    assert loop.calls[-1][1]["initial_messages"] is None
    assert "message 2" not in loop.calls[-1][0]
    model.confirm_turn("conversation", "a1", "native draft")
    assert states[loop.session_key] == {"threadId": "native-0", "syncedThroughSequence": 17}


@pytest.mark.parametrize("visible_history", [False, True])
def test_disabled_vision_rejects_already_synced_images_before_running_warm_thread(visible_history):
    loop = PersistentLoop()
    states = {}
    model = CodexSupervisorModel(agent_model=loop)
    image = "data:image/png;base64,AAAA"
    run(model, message="first", image_urls=[image], session_context=session_context(states, [], 1))
    model.confirm_turn("conversation", "first", "native draft")
    history = [
        {"role": "USER", "content": "Earlier image", "imageUrls": [image], "messageId": "user", "sequenceNo": 0},
        {"role": "ASSISTANT", "content": "native draft", "messageId": "first", "sequenceNo": 1},
    ]
    assert session_context(states, history, 3).load_history(1) == []
    loop.supports_vision_input = False
    model = CodexSupervisorModel(agent_model=loop)
    previous_states = deepcopy(states)
    with pytest.raises(ValueError, match="supports_vision_input=False"):
        run(model, message="next", history=history if visible_history else [],
            session_context=session_context(states, history, 3))
    assert loop.threads["native-0"]["turns"] == ["turn-0"]
    assert states == previous_states
    assert model._pending == {}


def test_disabled_vision_new_text_thread_loads_committed_history_once():
    loop = PersistentLoop()
    loop.supports_vision_input = False
    model = CodexSupervisorModel(agent_model=loop)
    loads = []

    def load_history(after):
        loads.append(after)
        return []

    context = SupervisorSessionContext(states={}, message_sequence_no=1,
                                       load_history=load_history, save_state=lambda *_args: None)
    assert run(model, session_context=context) == ["native draft"]
    assert loads == [-1]


@pytest.mark.parametrize("warm", [False, True])
def test_disabled_vision_rejects_unknown_history_only_when_resuming_native_thread(warm):
    loop = PersistentLoop()
    states = {}
    if warm:
        model = CodexSupervisorModel(agent_model=loop)
        run(model, message="first", session_context=session_context(states, [], 1))
        model.confirm_turn("conversation", "first", "native draft")
    loop.supports_vision_input = False
    model = CodexSupervisorModel(agent_model=loop)
    writes = []

    def unreadable_history(_after):
        raise ConnectionError("committed history is unavailable")

    context = SupervisorSessionContext(states=deepcopy(states), message_sequence_no=3 if warm else 1,
                                       load_history=unreadable_history, save_state=lambda *args: writes.append(args))
    if warm:
        with pytest.raises(ValueError, match="Cannot verify committed history.*supports_vision_input=False"):
            run(model, message="next", session_context=context)
        assert loop.threads["native-0"]["turns"] == ["turn-0"]
        assert model._pending == {}
        assert writes == []
    else:
        assert run(model, session_context=context) == ["native draft"]


def test_cancelled_native_turn_is_reverted_before_retry_without_losing_original_thread():
    loop = PersistentLoop()
    states = {}
    model = CodexSupervisorModel(agent_model=loop)
    run(model, message="ok", session_context=session_context(states, [], 1))
    model.confirm_turn("conversation", "ok", "native draft")
    loop.error = RuntimeError("cancelled")
    with pytest.raises(RuntimeError, match="cancelled"):
        run(model, message="cancelled", session_context=session_context(states, [], 3))
    loop.error = None
    model = CodexSupervisorModel(agent_model=loop)
    run(model, message="retry", session_context=session_context(states, [], 5))
    assert ("revert", "turn-1") in loop.events
    assert loop.threads["native-0"]["turns"] == ["turn-0", "turn-1"]
    assert len(loop.threads) == 1


def test_commit_before_confirmation_failure_is_recognized_on_resume():
    loop = PersistentLoop()
    states = {}
    model = CodexSupervisorModel(agent_model=loop)
    run(model, message="answer", session_context=session_context(states, [], 1))
    # SQL committed; process stopped before confirm_turn could update the binding.
    history = [
        {"role": "USER", "content": "question", "messageId": "user", "sequenceNo": 0},
        {"role": "ASSISTANT", "content": "answer", "messageId": "answer", "sequenceNo": 1},
    ]
    model = CodexSupervisorModel(agent_model=loop)
    run(model, message="next", session_context=session_context(states, history, 3))
    assert not any(event[0] == "revert" for event in loop.events)
    assert [event[1] for event in loop.events if event[0] == "inject"] == [[
        {"role": "user", "content": "question", "messageId": "user"},
        {"role": "assistant", "content": "answer", "messageId": "answer"},
    ]]
    assert states[loop.session_key]["syncedThroughSequence"] == 1


def test_old_uncertain_batch_retries_from_confirmed_cursor_without_blocking_answer():
    loop = PersistentLoop()
    loop.threads["original"] = {"turns": [], "messages": []}
    states = {loop.session_key: {"threadId": "original", "syncedThroughSequence": -1, "pendingSync": 0}}
    history = [{"role": "USER", "content": "history", "messageId": "u0", "sequenceNo": 0}]
    model = CodexSupervisorModel(agent_model=loop)
    assert run(model, session_context=session_context(states, history, 2)) == ["native draft"]
    model.confirm_turn("conversation", "answer", "native draft")
    assert "pendingSync" not in states[loop.session_key]
    assert len([event for event in loop.events if event[0] == "inject"]) == 1
    assert ("turn", "original") in loop.events
    assert states[loop.session_key]["syncedThroughSequence"] == 2


@pytest.mark.parametrize("blocked_operation", ["history", "binding", "pending"])
@pytest.mark.parametrize("stop", ["cancelled", "closed"])
def test_preparation_stops_after_blocking_history_or_storage_returns(blocked_operation, stop):
    loop = PersistentLoop()
    model = CodexSupervisorModel(agent_model=loop)
    states, writes = {}, []
    blocked, release, cancelled = Event(), Event(), Event()
    history = [{"role": "USER", "content": "previous question", "messageId": "u0", "sequenceNo": 0}]

    def wait_for_stop():
        blocked.set()
        assert release.wait(5)

    def load_history(after):
        if blocked_operation == "history":
            wait_for_stop()
        return history

    def save_state(key, state):
        states[key] = deepcopy(state)
        writes.append(deepcopy(state))
        if (blocked_operation == "binding" and len(writes) == 1
                or blocked_operation == "pending" and "pendingTurn" in state):
            wait_for_stop()

    def check_control():
        if cancelled.is_set():
            raise RuntimeError("cancelled")

    context = SupervisorSessionContext({}, 3, load_history, save_state)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run, model, session_context=context, check_control=check_control)
        try:
            assert blocked.wait(5)
            if stop == "cancelled":
                cancelled.set()
            else:
                loop.session_closed = True
        finally:
            release.set()
        with pytest.raises(RuntimeError, match="cancelled" if stop == "cancelled" else "session is closed"):
            future.result(timeout=5)

    expected = {"threadId": "native-0"}
    if blocked_operation == "pending":
        expected.update(syncedThroughSequence=0, pendingTurn={"messageId": "answer", "previousTurnId": None})
    assert states[loop.session_key] == expected
    assert len(writes) == (2 if blocked_operation == "pending" else 1)
    assert not any(event[0] in {"revert", "turn"} for event in loop.events)
    assert len([event for event in loop.events if event[0] == "inject"]) == (1 if blocked_operation == "pending" else 0)


@pytest.mark.parametrize("failure", ["load", "inject", "save"])
def test_history_failures_do_not_block_answer_and_retry_old_gap(failure, caplog):
    loop = PersistentLoop()
    loop.threads["original"] = {"turns": [], "messages": []}
    states = {loop.session_key: {"threadId": "original", "syncedThroughSequence": -1}}
    history = [
        {"role": "USER", "content": "missing question", "messageId": "u0", "sequenceNo": 0},
        {"role": "ASSISTANT", "content": "missing answer", "messageId": "a1", "sequenceNo": 1},
    ]
    failing = True

    def load(after):
        if failing and failure == "load":
            raise ConnectionError("history unavailable")
        return [item for item in history if item["sequenceNo"] > after]

    def save(key, state):
        if failing and failure == "save":
            raise ConnectionError("session metadata unavailable")
        states[key] = deepcopy(state)

    loop.inject_error = ConnectionError("history injection unavailable") if failure == "inject" else None
    model = CodexSupervisorModel(agent_model=loop)
    context = SupervisorSessionContext(deepcopy(states), 3, load, save)
    assert run(model, message="a3", session_context=context) == ["native draft"]
    model.confirm_turn("conversation", "a3", "native draft")
    assert model._pending == {}
    assert states[loop.session_key]["syncedThroughSequence"] == -1
    assert f"codex_{'session_save' if failure == 'save' else 'history_' + failure}_failed" in caplog.text

    history.extend([
        {"role": "USER", "content": "current question", "messageId": "u2", "sequenceNo": 2},
        {"role": "ASSISTANT", "content": "native draft", "messageId": "a3", "sequenceNo": 3},
    ])
    failing = False
    loop.inject_error = None
    model = CodexSupervisorModel(agent_model=loop)
    assert run(model, message="a5", session_context=SupervisorSessionContext(deepcopy(states), 5, load, save)) == ["native draft"]
    injected = [event[1] for event in loop.events if event[0] == "inject"][-1]
    assert [item["messageId"] for item in injected] == ["u0", "a1", "u2", "a3"]
    model.confirm_turn("conversation", "a5", "native draft")
    assert states[loop.session_key]["syncedThroughSequence"] == 5
    assert len(loop.threads) == 1


def test_confirmation_interruption_after_sync_failure_does_not_skip_missing_history():
    loop = PersistentLoop()
    states = {}
    history = [{"role": "USER", "content": "missing", "messageId": "u0", "sequenceNo": 0}]
    loop.inject_error = ConnectionError("injection failed")
    model = CodexSupervisorModel(agent_model=loop)
    assert run(model, message="a2", session_context=session_context(states, history, 2)) == ["native draft"]
    # SQL committed the answer, but the process stopped before confirm_turn.
    history.extend([
        {"role": "USER", "content": "current", "messageId": "u1", "sequenceNo": 1},
        {"role": "ASSISTANT", "content": "native draft", "messageId": "a2", "sequenceNo": 2},
    ])
    loop.inject_error = None
    model = CodexSupervisorModel(agent_model=loop)
    run(model, message="a4", session_context=session_context(states, history, 4))
    assert [item["messageId"] for item in loop.threads["native-0"]["messages"]] == ["u0", "u1", "a2"]


def test_native_reconciliation_failure_does_not_block_current_answer(caplog):
    loop = PersistentLoop()
    states = {}
    model = CodexSupervisorModel(agent_model=loop)
    loop.error = RuntimeError("native generation failed")
    with pytest.raises(RuntimeError, match="native generation failed"):
        run(model, message="failed", session_context=session_context(states, [], 1))
    loop.error = None
    loop.revert_error = ConnectionError("revert unavailable")
    model = CodexSupervisorModel(agent_model=loop)
    assert run(model, message="retry", session_context=session_context(states, [], 3)) == ["native draft"]
    assert "codex_history_reconcile_failed" in caplog.text


def test_uncertain_revert_followed_by_cancellation_does_not_remove_committed_native_turn():
    loop = PersistentLoop()
    states = {}
    model = CodexSupervisorModel(agent_model=loop)
    run(model, message="committed", session_context=session_context(states, [], 1))
    model.confirm_turn("conversation", "committed", "native draft")
    loop.error = RuntimeError("generation failed")
    with pytest.raises(RuntimeError, match="generation failed"):
        run(model, message="failed", session_context=session_context(states, [], 3))

    loop.error = None
    loop.revert_after_apply_error = TimeoutError("revert applied, refresh response lost")
    loop.cancel_before_turn = True
    model = CodexSupervisorModel(agent_model=loop)
    with pytest.raises(RuntimeError, match="cancelled before current turn"):
        run(model, message="cancelled", session_context=session_context(states, [], 5))
    assert loop.threads["native-0"]["turns"] == ["turn-0"]
    assert "pendingTurn" not in states[loop.session_key]

    loop.revert_after_apply_error = None
    loop.cancel_before_turn = False
    model = CodexSupervisorModel(agent_model=loop)
    run(model, message="retry", session_context=session_context(states, [], 7))
    assert loop.threads["native-0"]["turns"] == ["turn-0", "turn-1"]
    assert [event for event in loop.events if event[0] == "revert"] == [("revert", "turn-1")]
    assert states[loop.session_key]["pendingTurn"]["previousTurnId"] == "turn-0"


def test_committed_turn_missing_from_native_storage_replays_both_user_and_assistant():
    loop = PersistentLoop()
    states = {}
    model = CodexSupervisorModel(agent_model=loop)
    run(model, message="answer", session_context=session_context(states, [], 1))
    loop.threads["native-0"]["turns"].clear()
    history = [
        {"role": "USER", "content": "question", "messageId": "user", "sequenceNo": 0},
        {"role": "ASSISTANT", "content": "answer", "messageId": "answer", "sequenceNo": 1},
    ]
    model = CodexSupervisorModel(agent_model=loop)
    run(model, message="next", session_context=session_context(states, history, 3))
    assert [item["role"] for item in loop.threads["native-0"]["messages"]] == ["user", "assistant"]


def test_confirmed_reply_revisions_are_added_without_discarding_native_tool_history():
    loop = PersistentLoop()
    states = {}
    model = CodexSupervisorModel(agent_model=loop)
    run(model, message="a0", session_context=session_context(states, [], 1))
    model.confirm_turn("conversation", "a0", "verified reply with corrected sources")
    assert states[loop.session_key]["syncedThroughSequence"] == 0
    history = [{"role": "ASSISTANT", "content": "verified reply with corrected sources",
                "messageId": "a0", "sequenceNo": 1}]
    run(model, message="next", session_context=session_context(states, history, 3))
    assert loop.threads["native-0"]["messages"] == [{
        "role": "assistant", "content": "verified reply with corrected sources", "messageId": "a0",
    }]
    assert not any(event[0] == "revert" for event in loop.events)


def test_persistent_image_turn_keeps_current_question_text_separate_from_history():
    loop = PersistentLoop()
    model = CodexSupervisorModel(agent_model=loop)
    image = "data:image/png;base64,AAAA"
    run(model, user_message="Describe this image", image_urls=[image],
        session_context=session_context({}, [], 1))
    assert "Describe this image" in loop.calls[0][0]
    assert loop.calls[0][1]["initial_messages"] is None
    assert loop.calls[0][1]["image_urls"] == [image]
