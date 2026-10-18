"""Single-pass Result, deterministic Loop Control and the high-risk Evaluator gate."""

from __future__ import annotations

import json

import pytest

from agent.chat.supervisor import SupervisorRuntime
from agent.chat.video_search.loop_control import decide, evaluator_risks, improved
from agent.chat.video_search.runtime import AgentRuntime
from agent.chat.video_search.state import ResultAttempt, ResultOutput
from tests.agent.chat.test_runtime import Chat, Evaluator, Query, Retrieval, _hit, _inputs


def _output(*, passed=True, missing=(), ask=False, items=None, answer="The cup breaks here."):
    return {
        "answer_markdown": answer,
        "video_items": items if items is not None else [
            {"video_chunk_id": "chunk-1", "start_ms": 12, "end_ms": 18, "claim": "The cup breaks."},
        ],
        "self_check": {
            "pass": passed, "reason": "" if passed else "The line was not found.",
            "missing_evidence": list(missing), "needs_user_confirmation": ask,
        },
    }


class ResultModel:
    model_name = "result-test"
    provider_name = "fake"

    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.contexts = []
        self.calls = []

    def run_structured(self, prompt, *, tools, result_type, max_model_calls, request_timeout_seconds, **_):
        assert tools == [] and max_model_calls == 1 and result_type is ResultOutput
        assert request_timeout_seconds > 0
        self.contexts.append(json.loads(prompt.rsplit("\n\n", 1)[1]))
        return result_type.model_validate(self.outputs[min(len(self.contexts), len(self.outputs)) - 1])


def _runtime(result, *, evaluator=None, retrieval=None, query=None, **kwargs):
    query, evaluator, chat = query or Query(), evaluator or Evaluator(), Chat()
    runtime = AgentRuntime(
        query, evaluator, chat, (retrieval or Retrieval()).search,
        retrieval_channel="lexical", result_model=result, **kwargs,
    )
    return runtime, query, evaluator, chat


def _kick(runtime, inputs=None):
    handoffs = []
    output = runtime.kick(
        inputs or _inputs(), render_with_chat=False, single_pass_result=True,
        on_handoff=lambda canvas, turn: handoffs.append(canvas.get("review", "latest")),
    )
    return output, handoffs[0]["result"]


def test_pass_skips_the_evaluator_and_chat_and_delivers_the_result():
    result = ResultModel(_output())
    runtime, query, evaluator, chat = _runtime(result)

    output, review = _kick(runtime)

    assert evaluator.contexts == [] and chat.contexts == []
    assert len(query.contexts) == len(result.contexts) == 1
    assert review["outcome"] == "pass" and review["loops"] == 1
    assert [block.type for block in output.render_blocks] == ["markdown", "video"]
    assert output.render_blocks[0].markdown == "The cup breaks here."
    assert (output.render_blocks[1].start_seconds, output.render_blocks[1].end_seconds) == (12, 18)
    assert output.hits[0].selection_reason.summary == "The cup breaks."
    item = result.contexts[0]["draft_items"][0]
    assert item["video_chunk_id"] == "chunk-1" and item["chunk_range_ms"] == [10, 20]
    assert item["draft"]["start_ms"] == 11


def test_result_may_extend_into_an_adjacent_retrieved_chunk():
    left, right = _hit("chunk-1"), _hit("chunk-2")
    right.evidence.video_id = left.evidence.video_id
    right.evidence.sequence_no = 1
    right.evidence.start_seconds, right.evidence.end_seconds = 20, 30
    result = ResultModel(_output(items=[
        {"video_chunk_id": "chunk-1", "start_ms": 12, "end_ms": 28, "claim": "Across the cut."},
    ]))
    runtime, *_ = _runtime(result, retrieval=Retrieval([left, right]))

    output, _ = _kick(runtime)

    video = output.render_blocks[1]
    assert (video.start_seconds, video.end_seconds) == (12, 28)
    assert video.source_video_chunk_ids == ["chunk-1", "chunk-2"]
    assert result.contexts[0]["draft_items"][0]["adjacent_chunks"] == [
        {"video_chunk_id": "chunk-2", "range_ms": [20, 30]},
    ]


def test_host_validation_keeps_the_drafted_range_and_drops_unknown_ids():
    result = ResultModel(_output(items=[
        {"video_chunk_id": "chunk-1", "start_ms": 5, "end_ms": 50, "claim": "Too wide."},
        {"video_chunk_id": "chunk-2", "start_ms": 12, "end_ms": 18, "claim": "Not drafted."},
    ]))
    runtime, *_ = _runtime(result)

    output, review = _kick(runtime)

    videos = [block for block in output.render_blocks if block.type == "video"]
    assert [(block.video_chunk_id, block.start_seconds, block.end_seconds) for block in videos] == [
        ("chunk-1", 11, 19),
    ]
    assert len(review["warnings"]) == 2


def test_fail_a_asks_the_user_without_another_loop():
    result = ResultModel(_output(passed=False, ask=True, answer="Which episode do you mean?"))
    runtime, query, *_ = _runtime(result)

    output, review = _kick(runtime)

    assert review["outcome"] == "ask_user"
    assert len(query.contexts) == len(result.contexts) == 1
    assert output.render_blocks[0].markdown == "Which episode do you mean?"


def test_fail_b_with_a_concrete_gap_replans_with_the_self_check_as_advice():
    result = ResultModel(_output(passed=False, missing=["the line before 00:20 in episode 1"]), _output())
    runtime, query, *_ = _runtime(result)

    output, review = _kick(runtime)

    assert review["outcome"] == "pass" and review["loops"] == 2
    assert len(query.contexts) == len(result.contexts) == 2
    assert "the line before 00:20 in episode 1" in query.contexts[1]["latest_evaluator_advice"]
    assert result.contexts[1]["previous_self_check"]["pass"] is False
    assert not output.render_blocks[0].markdown.startswith(">")


def test_fail_b_without_a_concrete_gap_degrades_immediately():
    result = ResultModel(_output(passed=False))
    runtime, query, *_ = _runtime(result)

    output, review = _kick(runtime)

    assert review["outcome"] == "degrade" and review["loops"] == 1
    assert len(query.contexts) == 1
    markdown = output.render_blocks[0].markdown
    assert markdown.startswith("> **Not fully verified**") and markdown.endswith("The cup breaks here.")
    assert [block.type for block in output.render_blocks] == ["markdown", "video"]


def test_max_loops_degrades_to_the_best_draft():
    result = ResultModel(_output(passed=False, missing=["the broken cup"]))
    runtime, query, *_ = _runtime(result, result_max_loops=2)

    output, review = _kick(runtime)

    assert review["outcome"] == "degrade" and review["loops"] == 2
    assert len(query.contexts) == len(result.contexts) == 2
    assert "Still missing: the broken cup" in output.render_blocks[0].markdown


def test_a_loop_without_improvement_stops_before_max_loops():
    # The second Plan turn drafts the same chunk and the self-check is no better.
    result = ResultModel(_output(passed=False, missing=["the broken cup"]))
    runtime, query, *_ = _runtime(result, result_max_loops=3)

    _, review = _kick(runtime)

    assert review["outcome"] == "degrade"
    assert len(query.contexts) == len(result.contexts) == 2


def test_chinese_questions_get_a_chinese_uncertainty_note():
    result = ResultModel(_output(passed=False))
    runtime, *_ = _runtime(result)
    inputs = _inputs()
    inputs["user_message"] = "杯子在哪里摔碎的？"

    output, _ = _kick(runtime, inputs)

    assert output.render_blocks[0].markdown.startswith("> **未能完全核实**")


def test_speaker_question_runs_the_evaluator_and_a_rejection_replans_without_a_result_call():
    result = ResultModel(_output())
    runtime, query, evaluator, _ = _runtime(result, evaluator=Evaluator(approve_at=2))
    inputs = _inputs()
    inputs["user_message"] = "Who said the line about the broken cup?"

    _, review = _kick(runtime, inputs)

    assert len(evaluator.contexts) == 2 and len(query.contexts) == 2
    assert len(result.contexts) == 1
    assert review["outcome"] == "pass"
    assert result.contexts[0]["review_advice"] == "approved"


def test_evaluator_rejection_on_the_last_loop_degrades_with_its_advice():
    result = ResultModel(_output())
    runtime, _, evaluator, _ = _runtime(result, evaluator=Evaluator(approve_at=99), result_max_loops=1)
    inputs = _inputs()
    inputs["user_message"] = "Who broke the cup?"

    output, review = _kick(runtime, inputs)

    assert review["outcome"] == "degrade" and len(result.contexts) == 1
    assert "find broken cup, not picked-up cup" in output.render_blocks[0].markdown


def test_evaluator_policy_always_restores_the_review_for_every_request():
    runtime, _, evaluator, _ = _runtime(ResultModel(_output()), evaluator_policy="always")

    _kick(runtime)

    assert len(evaluator.contexts) == 1


@pytest.mark.parametrize("message,image,ranks,expected", [
    ("这个出自于哪里？", False, [1], ()),
    ("谁在这集说了这句话？", False, [1], ("speaker_attribution",)),
    ("Which shot looks like this?", False, [1], ("visual_similarity",)),
    ("where is this from?", True, [1], ("visual_similarity",)),
    ("where is this from?", False, [7], ("low_retrieval_confidence",)),
])
def test_evaluator_risk_rules(message, image, ranks, expected):
    from agent.chat.video_search.state import LexicalRetrievalChannelWeights, RetrievalCall

    hit = _hit()
    hit.candidate.fused_rank = ranks[0]
    weights = LexicalRetrievalChannelWeights(lexical=1, semantic=0, visual=0)
    call = RetrievalCall(
        query="q", top_k=3, requested_weights=weights, effective_weights=weights,
        result=Retrieval([hit]).search("q", tenant_id="workspace", series_ids=["series"], top_k=3,
                                       lexical_weight=1, text_weight=0, vision_weight=0),
    )
    inputs = _inputs()
    inputs["user_message"] = message
    if image:
        inputs["image_urls"] = ["data:image/png;base64,x"]

    assert evaluator_risks(inputs, ["chunk-1"], [call], low_confidence_rank=3) == expected
    # An empty draft is left to the Result self-check.
    assert "low_retrieval_confidence" not in evaluator_risks(inputs, [], [call], low_confidence_rank=3)


def test_risk_rules_read_the_original_question_not_the_supervisor_sub_query():
    inputs = _inputs()
    inputs["user_message"] = "Find the source and speaker of this line"
    inputs["original_user_message"] = "这个出自于哪里？"

    assert evaluator_risks(inputs, [], [], low_confidence_rank=3) == ()


def _attempt(loop, *, passed=False, missing=(), draft=("chunk-1",), rejected=False):
    output = None if rejected else ResultOutput.model_validate(_output(passed=passed, missing=missing))
    return ResultAttempt(
        loop=loop, output=output, items=(), draft_ids=frozenset(draft), retrieved_ids=frozenset(draft),
        evaluator_rejected=rejected, evaluator_advice="missing cup" if rejected else "",
    )


def test_score_monotonicity_counts_new_drafted_evidence_or_a_better_self_check():
    first = _attempt(1, missing=["a", "b"])
    assert not improved(_attempt(2, missing=["a", "b"]), [first])
    assert improved(_attempt(2, missing=["a"]), [first])
    assert improved(_attempt(2, missing=["a", "b"], draft=("chunk-2",)), [first])
    assert decide(_attempt(2, missing=["a", "b"]), [first], max_loops=3) == "degrade"
    assert decide(_attempt(2, missing=["a"]), [first], max_loops=3) == "replan"
    assert decide(_attempt(1, rejected=True), [], max_loops=2) == "replan"


class _SinglePassSupervisorModel:
    model_name = "supervisor-test"
    provider_name = "fake"

    def __init__(self):
        self.payloads = []
        self.prompts = []

    def run_agent(self, prompt, *, tools, handle_text, check_control, **_):
        self.prompts.append(prompt)
        available = {tool.name: tool for tool in tools}
        assert set(available) == {"search_web", "search_video_graph"}
        handle_text("Let me check the video.")
        self.payloads.append(json.loads(available["search_video_graph"].invoke({"query": "broken cup"})))


def test_supervisor_streams_the_result_and_only_keeps_routing_tools():
    from tests.agent.chat.test_supervisor import FakeWebSearcher, _inputs as supervisor_inputs

    video, *_ = _runtime(ResultModel(_output()))
    model = _SinglePassSupervisorModel()
    events = []
    supervisor = SupervisorRuntime(
        video, model, FakeWebSearcher(), max_web_results=5, web_timeout_seconds=12,
        execution_timeout_seconds=60, max_tool_result_bytes=4096, max_prompt_chars=10000,
        single_pass_result=True,
    )

    result = supervisor.kick(
        supervisor_inputs(series_ids=["series"]), load_video_context=lambda: [], emit=events.append,
    )

    payload = model.payloads[0]
    assert payload["delivered"] is True and payload["outcome"] == "pass"
    assert payload["video_item_count"] == 1
    assert "submit_video_item" not in model.prompts[0]
    assert result.content == "Let me check the video.\n\nThe cup breaks here."
    videos = [block for block in result.video_result.render_blocks if block.type == "video"]
    assert [(block.video_chunk_id, block.start_seconds, block.end_seconds) for block in videos] == [
        ("chunk-1", 12, 18),
    ]
    # The answer reached the client as soon as the search returned, before the Supervisor finished.
    snapshots = [event for event in events if event["type"] == "answer.snapshot"]
    assert any("The cup breaks here." in event["content"] for event in snapshots)
