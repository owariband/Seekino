from copy import deepcopy
from threading import Event
from time import monotonic

import pytest

from agent.chat.common.errors import AgentExecutionTimeoutError, AgentModelInvocationError
from agent.chat.common.state import ExecutionControl
from agent.chat.query_hook import QueryHook, QueryHookDecision, RetrievalSlot
from agent.chat.supervisor import SupervisorRuntime
from gateway.config import QueryHookConfig
from gateway.query_hook import QueryHookModelError
from gateway.usage import ModelCallControlError, capture_model_usage, model_call_context, track_model_call
from tests.agent.chat.test_supervisor import FakeSupervisorModel, FakeVideoRuntime, FakeWebSearcher, _inputs


class Models:
    def __init__(self, action="retrieve", query="真是高高在上呢"):
        self.action, self.query = action, query
        self.calls = []
        self.route_error = self.rewrite_error = None

    def route(self, state, question, *, timeout_seconds):
        self.calls.append(("route", deepcopy(state), timeout_seconds))
        if self.route_error:
            raise self.route_error
        with track_model_call(provider="fake", model="jev", operation="query_hook_route"):
            return self.action

    def rewrite(self, prompt, result_type, *, timeout_seconds):
        self.calls.append(("rewrite", prompt, timeout_seconds))
        if self.rewrite_error:
            raise self.rewrite_error
        with track_model_call(provider="fake", model="8b", operation="retrieval_slot"):
            return result_type(retrieval_query=self.query)


class DecisionModel:
    def __init__(self, action="mixed", query="雨中等人"):
        self.result = {"action": action, "retrieval_query": query}
        self.calls = []
        self.error = None

    def generate(self, inputs, **options):
        self.calls.append((inputs, options))
        if self.error:
            raise self.error
        with track_model_call(provider="fake", model="chat", operation="query_hook_decision"):
            return options["result_type"].model_validate(self.result)


@pytest.mark.parametrize("action,query", [("respond", None), ("retrieve", "原句不是你说的那样"), ("mixed", "雨中等人")])
def test_combined_decision_preserves_original_without_calling_legacy_models(action, query):
    legacy, model = Models(), DecisionModel(action, query)
    inputs = _inputs()
    original = deepcopy(inputs)
    usage = []
    before = QueryHook(QueryHookConfig(enabled=True), legacy, decision_model=model)
    with capture_model_usage(usage.append):
        output = before(inputs, ExecutionControl(None))
    assert inputs == original and not legacy.calls
    assert output.startswith(inputs["user_message"] + "\n\n")
    assert output.count("[本轮处理说明｜应用生成]") == 1
    assert len(model.calls) == 1
    options = model.calls[0][1]
    assert options["max_model_calls"] == 1 and 0 < options["request_timeout_seconds"] <= 8
    assert callable(options["check_control"])
    assert [c.operation for c in usage] == ["query_hook_decision"]
    assert usage[0].context["stage"] == "query_hook"
    assert ("候选检索用语（JSON 字符串）：" in output) == (query is not None)


@pytest.mark.parametrize("payload", [
    {"action": "respond", "retrieval_query": "旧目标"},
    {"action": "unknown", "retrieval_query": None},
    {"action": "retrieve", "retrieval_query": " "},
    {"action": "mixed", "retrieval_query": "x" * 161},
    {"action": "respond", "retrieval_query": None, "answer": "不应回答"},
])
def test_combined_decision_rejects_inconsistent_or_unusable_output(payload):
    with pytest.raises(ValueError):
        QueryHookDecision.model_validate(payload)


def test_combined_failure_falls_back_but_quota_and_cancellation_propagate():
    model, inputs = DecisionModel(), _inputs()
    before = QueryHook(QueryHookConfig(enabled=True), Models(), decision_model=model)
    model.error = RuntimeError("provider failure")
    assert before(inputs, ExecutionControl(None)) == inputs["user_message"]
    model.error = ModelCallControlError("quota exhausted")
    with pytest.raises(ModelCallControlError):
        before(inputs, ExecutionControl(None))
    cancel = Event()
    original_generate = model.generate

    def cancelling(*args, **kwargs):
        cancel.set()
        return original_generate(*args, **kwargs)

    model.error = RuntimeError("connection closed")
    model.generate = cancelling
    with pytest.raises(AgentModelInvocationError, match="cancelled"):
        before(inputs, ExecutionControl(None, cancel))


def test_combined_late_result_is_discarded_and_parent_deadline_is_not_swallowed(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("agent.chat.query_hook.monotonic", lambda: clock[0])
    model, inputs = DecisionModel(), _inputs()
    original_generate = model.generate

    def slow(*args, **kwargs):
        clock[0] += 9
        return original_generate(*args, **kwargs)

    model.generate = slow
    before = QueryHook(QueryHookConfig(enabled=True), Models(), decision_model=model)
    assert before(inputs, ExecutionControl(None)) == inputs["user_message"]
    with pytest.raises(AgentExecutionTimeoutError):
        before(inputs, ExecutionControl(monotonic() - 1))
    assert len(model.calls) == 1


def hook(models, **config):
    return QueryHook(QueryHookConfig(enabled=True, **config), models)


@pytest.mark.parametrize("action", ["respond", "retrieve", "mixed"])
def test_template_branch_preserves_original_and_never_asks_8b_to_route(action):
    models = Models(action)
    inputs = _inputs()
    inputs["user_message"] = "真是高高在上呢 哪里出现过  "
    original = deepcopy(inputs)
    output = hook(models)(inputs, ExecutionControl(None))
    assert inputs == original
    assert output.startswith(inputs["user_message"] + "\n\n")
    assert "不评价上一轮检索是否正确" in output
    assert "不约束后续轮次" in output
    assert "以用户原文为准" in output
    assert [call[0] for call in models.calls] == (
        ["route"] if action == "respond" else ["route", "rewrite"]
    )
    if action != "respond":
        assert f"已确定 action：{action}" in models.calls[1][1]
        assert "真是高高在上呢" in output
    else:
        assert "不新增视频检索" in output
        assert "候选检索用语（JSON 字符串）：" not in output


def test_mixed_does_not_force_response_order_or_rewrite_response_task():
    models = Models("mixed", "她后来参加活动的画面")
    inputs = _inputs()
    inputs["user_message"] = "先找她后来参加活动的画面，再把刚才那段解释改成一句话。"
    output = hook(models)(inputs, ExecutionControl(None))
    assert inputs["user_message"] in output
    assert "先后顺序遵循当前原文" in output
    assert "候选检索用语（JSON 字符串）：\"她后来参加活动的画面\"" in output


def test_route_failure_uses_original_and_rewrite_failure_retains_template():
    models = Models()
    inputs = _inputs()
    models.route_error = QueryHookModelError("unavailable")
    assert hook(models)(inputs, ExecutionControl(None)) == inputs["user_message"]
    assert len(models.calls) == 1
    models.route_error = None
    models.rewrite_error = QueryHookModelError("unavailable")
    output = hook(models)(inputs, ExecutionControl(None))
    assert "简单检索" in output
    assert "候选检索用语（JSON 字符串）：" not in output


@pytest.mark.parametrize("stage", ["route", "rewrite"])
def test_admission_failures_are_not_silently_bypassed(stage):
    models = Models()
    setattr(models, f"{stage}_error", ModelCallControlError("quota exhausted"))
    with pytest.raises(ModelCallControlError):
        hook(models)(_inputs(), ExecutionControl(None))


def test_cancel_during_route_stops_before_rewrite_or_supervisor():
    cancel = Event()

    class CancellingModels(Models):
        def route(self, *args, **kwargs):
            cancel.set()
            raise QueryHookModelError("connection closed")

    models = CancellingModels()
    with pytest.raises(AgentModelInvocationError, match="cancelled"):
        hook(models)(_inputs(), ExecutionControl(None, cancel))
    assert not models.calls


def test_expired_parent_deadline_is_not_an_optional_hook_failure():
    models = Models()
    with pytest.raises(AgentExecutionTimeoutError):
        hook(models)(_inputs(), ExecutionControl(monotonic() - 1))
    assert not models.calls


def test_hook_deadline_discards_late_route_result(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("agent.chat.query_hook.monotonic", lambda: clock[0])

    class SlowModels(Models):
        def route(self, *args, **kwargs):
            clock[0] += 9
            return super().route(*args, **kwargs)

    models = SlowModels()
    inputs = _inputs()
    assert hook(models)(inputs, ExecutionControl(None)) == inputs["user_message"]
    assert [call[0] for call in models.calls] == ["route"]


def test_recent_history_is_bounded_plain_text_without_attachments():
    inputs = _inputs()
    inputs["history"] = [
        {"role": "user", "content": "早" * 100},
        {"role": "assistant", "content": "已经找到了", "status": "COMPLETED", "secret": "private-url"},
        {"role": "user", "content": "失败消息", "status": "FAILED"},
        {"role": "tool", "content": "工具详情"},
        {"role": "USER", "content": "原话", "attachments": [{"mediaUri": "private-image"}]},
    ]
    models = Models("respond")
    hook(models, history_message_limit=2, history_max_chars=6)(inputs, ExecutionControl(None))
    history = models.calls[0][1]["recent_messages"]
    assert history == [
        {"role": "assistant", "content": "经找到了"},
        {"role": "user", "content": "原话"},
    ]
    assert "private" not in str(models.calls)


@pytest.mark.parametrize("extra", [
    {"attachments": [{"mediaUri": "image"}]},
    {"image_urls": ["data:image/png;base64,test"]},
    {"extra_context": {"playback": {"positionSeconds": 2}}},
    {"user_message": "长" * 2001},
])
def test_text_hook_does_not_override_visual_or_oversized_requests(extra):
    inputs = {**_inputs(), **extra}
    models = Models()
    assert hook(models)(inputs, ExecutionControl(None)) == inputs["user_message"]
    assert not models.calls


def test_disabled_hook_has_no_provider_calls():
    models = Models()
    inputs = _inputs()
    assert QueryHook(QueryHookConfig(), models)(inputs, ExecutionControl(None)) == inputs["user_message"]
    assert not models.calls


def test_each_turn_is_independent_and_usage_keeps_parent_identity():
    models = Models("retrieve")
    before = hook(models)
    inputs = _inputs()
    usage = []
    with capture_model_usage(usage.append), model_call_context({"tenant_id": "tenant", "run_id": "run"}):
        first = before(inputs, ExecutionControl(None))
        models.action = "respond"
        second = before(inputs, ExecutionControl(None))
    assert "简单检索" in first and "简单回应" in second
    assert "候选检索用语（JSON 字符串）：" not in second
    assert [call.model for call in usage] == ["jev", "8b", "jev"]
    assert all(call.context["stage"] == "query_hook" for call in usage)
    assert all(call.context["tenant_id"] == "tenant" and call.context["run_id"] == "run" for call in usage)


@pytest.mark.parametrize("query", ["", " ", "x" * 161])
def test_empty_or_excessive_slot_cannot_be_appended(query):
    with pytest.raises(ValueError):
        RetrievalSlot(retrieval_query=query)


@pytest.mark.parametrize("combined", [False, True])
def test_supervisor_receives_append_once_while_tools_history_and_inputs_keep_original(combined):
    models = Models("retrieve")
    supervisor = FakeSupervisorModel(calls=("query_video",))
    video = FakeVideoRuntime()
    runtime = SupervisorRuntime(
        video, supervisor, FakeWebSearcher(),
        max_web_results=5, web_timeout_seconds=12, execution_timeout_seconds=60,
        max_tool_result_bytes=4096, max_prompt_chars=10000,
        query_hook=QueryHook(QueryHookConfig(enabled=True), models,
            decision_model=DecisionModel("retrieve", "真是高高在上呢") if combined else None),
    )
    inputs = _inputs(series_ids=["series"])
    inputs["history"] = [{"role": "user", "content": "原始历史", "messageId": "old"}]
    original = deepcopy(inputs)
    runtime.kick(inputs, load_video_context=lambda: [])
    assert inputs == original
    assert [call[0] for call in models.calls] == ([] if combined else ["route", "rewrite"])
    sent = supervisor.runs[0][2]
    assert sent.startswith(inputs["user_message"] + "\n\n")
    assert sent.count("[本轮处理说明｜应用生成]") == 1
    assert supervisor.runs[0][3] == original["history"]
    assert "[本轮处理说明｜应用生成]" not in supervisor.prompts[0]
    assert video.calls[0][0]["original_user_message"] == original["user_message"]
    runtime.close()
