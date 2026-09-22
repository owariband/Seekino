from __future__ import annotations

import json
from types import SimpleNamespace

from gateway.web_search import WebSearchError, WebSearchHit
from server.agent.supervisor import SupervisorRuntime


def _inputs(*, series_ids=None):
    return {
        "conversation_id": "conversation", "assistant_message_id": "answer",
        "workspace_id": "workspace", "series_ids": series_ids or [],
        "series_context": [{"seriesId": "series", "name": "Course"}]
        if series_ids else [],
        "user_message": "What happened?", "history": [], "requested_top_k": 3,
    }


class FakeVideoRuntime:
    def __init__(self):
        self.calls = []

    def kick(self, inputs, **kwargs):
        self.calls.append((inputs, kwargs))
        return SimpleNamespace(agent_reply=SimpleNamespace(
            message="Approved video explanation.", result_count=1,
        ))

    def close(self):
        pass


class FakeWebSearcher:
    def __init__(self, *, fail=False):
        self.calls = []
        self.fail = fail

    def search(self, query, *, max_results, timeout_seconds):
        self.calls.append((query, max_results, timeout_seconds))
        if self.fail:
            raise WebSearchError("down")
        return [WebSearchHit("Official page", "https://example.org/source", "A summary.")]


class FakeSupervisorModel:
    model_name = "supervisor-test"
    provider_name = "fake"

    def __init__(self, *, calls=(), answer="Direct answer."):
        self.calls = calls
        self.answer = answer
        self.tool_results = []
        self.prompts = []

    def run_agent_loop(
        self, prompt, *, tools, max_model_calls, handle_text,
        request_timeout_seconds, check_control, max_input_chars,
    ):
        self.prompts.append(prompt)
        assert request_timeout_seconds > 0
        assert max_input_chars > 0
        check_control()
        available = {tool.name: tool for tool in tools}
        assert set(available) == {"search_web", "search_video_graph"}
        for name in self.calls:
            self.tool_results.append(json.loads(available[name].invoke({"query": "question"})))
        handle_text(self.answer)


def _runtime(model, *, video=None, web=None):
    return SupervisorRuntime(
        video or FakeVideoRuntime(), model, web or FakeWebSearcher(),
        max_web_results=5, web_timeout_seconds=12,
        execution_timeout_seconds=60, max_tool_result_bytes=4096,
        max_prompt_chars=10000,
    )


def test_direct_chat_never_loads_video_context_or_uses_search():
    model = FakeSupervisorModel()
    runtime = _runtime(model)
    result = runtime.kick(
        _inputs(), load_video_context=lambda: (_ for _ in ()).throw(AssertionError()),
    )
    assert result.content == "Direct answer."
    assert result.video_result is None
    assert result.web_sources == ()
    assert "Course" not in model.prompts[0]


def test_video_tool_loads_context_once_and_preserves_approved_answer():
    model = FakeSupervisorModel(calls=("search_video_graph", "search_video_graph"), answer="Rewritten")
    video = FakeVideoRuntime()
    runtime = _runtime(model, video=video)
    loads = []
    result = runtime.kick(
        _inputs(series_ids=["series"]),
        load_video_context=lambda: loads.append(True) or [{"seriesId": "series", "videos": []}],
    )
    assert result.content == "Approved video explanation."
    assert result.video_result is not None
    assert len(loads) == len(video.calls) == 1
    assert video.calls[0][0]["user_message"] == "question"
    assert video.calls[0][1]["deadline"] > 0
    assert model.tool_results[0]["ok"] is True
    assert model.tool_results[1]["reused"] is True


def test_supervisor_can_use_web_and_video_in_same_turn():
    model = FakeSupervisorModel(
        calls=("search_web", "search_video_graph"),
        answer="Current context from [the page](https://example.org/source).",
    )
    result = _runtime(model).kick(
        _inputs(series_ids=["series"]), load_video_context=lambda: [],
    )
    assert result.content.startswith("Approved video explanation.")
    assert "### 网页补充" in result.content
    assert "https://example.org/source" in result.content
    assert len(result.web_sources) == 1


def test_web_failure_is_tool_feedback_and_does_not_fail_direct_chat():
    model = FakeSupervisorModel(calls=("search_web",), answer="Search is unavailable.")
    result = _runtime(model, web=FakeWebSearcher(fail=True)).kick(
        _inputs(), load_video_context=lambda: [],
    )
    assert model.tool_results[0]["error"]["code"] == "WEB_SEARCH_FAILED"
    assert result.content == "Search is unavailable."


def test_video_failure_is_tool_feedback_and_web_cannot_verify_video_claims():
    class FailingVideo(FakeVideoRuntime):
        def kick(self, inputs, **kwargs):
            raise RuntimeError("model unavailable")

    model = FakeSupervisorModel(calls=("search_video_graph",), answer="Video unavailable.")
    result = _runtime(model, video=FailingVideo()).kick(
        _inputs(series_ids=["series"]), load_video_context=lambda: [],
    )
    assert model.tool_results[0]["error"]["code"] == "VIDEO_SEARCH_FAILED"
    assert result.video_result is None
    assert result.content == "Video unavailable."


def test_unreferenced_web_hits_are_labelled_as_search_results():
    model = FakeSupervisorModel(calls=("search_web",), answer="A cautious answer.")
    result = _runtime(model).kick(_inputs(), load_video_context=lambda: [])
    assert "网页检索结果（未作为回答依据）" in result.content
    assert "https://example.org/source" in result.content


def test_reference_prompt_contains_location_but_not_evidence_body():
    model = FakeSupervisorModel()
    inputs = _inputs(series_ids=["series"])
    inputs["reference_context"] = {
        "sourceStatus": "COMPLETED",
        "selectedRange": {"startMs": 1000, "endMs": 2000},
        "evidence": {"videoChunkId": "chunk-1", "transcript": "private evidence"},
    }
    _runtime(model).kick(inputs, load_video_context=lambda: [])
    assert "chunk-1" in model.prompts[0]
    assert "private evidence" not in model.prompts[0]


def test_video_tool_without_scope_returns_feedback():
    model = FakeSupervisorModel(calls=("search_video_graph",), answer="No video is selected.")
    result = _runtime(model).kick(_inputs(), load_video_context=lambda: [])
    assert model.tool_results[0]["error"]["code"] == "NO_VIDEO_SCOPE"
    assert result.video_result is None
