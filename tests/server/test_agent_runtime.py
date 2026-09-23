from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from time import monotonic

import pytest

from gateway.agent_model import (
    ModelFailureReason, ModelInputTooLargeError, QueryModelIncompleteError,
)
from server.agent import AgentModelInvocationError, AgentRuntime
from server.agent.common.state import RunInputs
from server.agent.video_search.errors import (
    VideoSearchFailure, VideoSearchFailureCode, VideoSearchFailurePhase,
)
from server.agent.video_search.state import (
    QueryDraft, RetrievalChannelWeights,
)
from server.agent.video_search.tools import UpdateVideoItemArguments
from server.schema import (
    AgentReply,
    MatchedEvidence,
    SelectionReason,
    VideoChunkCandidate,
    VideoChunkEvidence,
    VideoChunkSearchHit,
    VideoChunkSearchResult,
)


def _inputs(name="conversation", *, workspace="workspace", series="series") -> RunInputs:
    return {
        "conversation_id": name, "assistant_message_id": f"{name}-answer",
        "workspace_id": workspace, "series_ids": [series], "series_context": [],
        "user_message": f"{name}: find the broken cup", "history": [], "requested_top_k": 8,
    }


def _retrieval_args(
    query: str,
    top_k: int,
    *,
    transcript: float = 0.0,
    semantic: float = 0.5,
    visual: float = 0.5,
) -> dict:
    return {
        "query": query,
        "top_k": top_k,
        "channel_weights": {
            "transcript": transcript,
            "semantic": semantic,
            "visual": visual,
        },
    }


def _tool_map(tools):
    return {tool.name: tool for tool in tools}


def _invoke_json(tool, arguments):
    return json.loads(tool.invoke(arguments))


def _read_all(tool, video_chunk_id):
    overview = _invoke_json(tool, {
        "video_chunk_id": video_chunk_id,
        "section": "overview",
    })
    sections = {}
    for item in overview["sections"]:
        name, total = item["name"], item["total_chars"]
        start = 0
        content = []
        while start < total:
            end = total
            while True:
                page = _invoke_json(tool, {
                    "video_chunk_id": video_chunk_id,
                    "section": name,
                    "start": start,
                    "end": end,
                })
                if "error" not in page:
                    break
                assert page["error"]["code"] == "RANGE_TOO_LARGE"
                end = page["error"]["suggested_end"]
                assert start < end < total
            assert page["start"] == start
            assert page["end"] == end
            content.append(page["content"])
            start = end
        sections[name] = json.loads("".join(content))
    return sections


def _hit(chunk_id="chunk-1", *, workspace="workspace", series="series"):
    evidence = VideoChunkEvidence(
        series_id=series, workspace_id=workspace, series_name="series name",
        series_description=None, series_status="READY", important_extra_data=None,
        character_bible=[], video_id=f"video-{chunk_id}", episode_order=1,
        original_filename="episode.mp4", original_source_uri="episode.mp4",
        inference_proxy_uri=None, content_hash="hash", duration_ms=10_000,
        upload_status="COMPLETED", parse_status="COMPLETED", video_chunk_id=chunk_id,
        sequence_no=0, start_ms=1000, end_ms=2000, analysis_window_start_ms=0,
        analysis_window_end_ms=3000, chunk_media_ref="chunk.mp4",
        analysis_window_media_ref="window.mp4", transcript=[], baseline_visual_caption=None,
        keyframe_uris=[], all_time_events=[], all_time_event_text="cup action",
        retrieval_text="cup action", video_compact="cup action", context_before_compact="",
        context_after_compact="", window_summary="cup action", model_provider="ark",
        model_name="parser", pipeline_version="v1", is_fallback=False,
        parse_provenance=None, parsed_at=1_789_724_800,
    )
    return VideoChunkSearchHit(
        candidate=VideoChunkCandidate(video_chunk_id=chunk_id, fused_rank=1, fused_score=1),
        selection_reason=SelectionReason(summary="matched action", matched_evidence=[], limitations=[]),
        evidence=evidence,
    )


def _selected_range(chunk_id: str, *, start_ms: int = 1100, end_ms: int = 1900) -> dict:
    return {
        "video_chunk_id": chunk_id,
        "start_ms": start_ms,
        "end_ms": end_ms,
    }


class Retrieval:
    def __init__(self, hits=None):
        self.hits = hits if hits is not None else [_hit(), _hit("chunk-2")]
        self.calls = []
        self.weights = []

    def search(
        self,
        query,
        *,
        workspace_id,
        series_ids,
        top_k,
        transcript_weight,
        text_weight,
        vision_weight,
    ):
        self.calls.append((query, workspace_id, series_ids, top_k))
        self.weights.append((transcript_weight, text_weight, vision_weight))
        hits = [hit.model_copy(deep=True) for hit in self.hits[:top_k]]
        return VideoChunkSearchResult(
            query=query, series_ids=series_ids,
            agent_reply=AgentReply(message="retrieval", result_count=len(hits)), hits=hits,
        )


class Query:
    model_name = "query-test"
    provider_name = "fake"

    def __init__(
        self,
        *,
        search_rounds=(1,),
        selected=("chunk-1",),
        selected_ranges=None,
        searches_per_round=1,
        channel_weights=(0.0, 0.5, 0.5),
    ):
        self.search_rounds = search_rounds
        self.selected = selected
        self.selected_ranges = selected_ranges
        self.searches_per_round = searches_per_round
        self.channel_weights = channel_weights
        self.prompts = []
        self.contexts = []
        self.tool_results = []
        self.draft_tool_results = []
        self.budgets = []
        self.request_timeouts = []

    def run_structured(
        self, prompt, *, tools, result_type, max_model_calls, request_timeout_seconds,
        check_control=None, max_input_chars=None,
    ):
        self.prompts.append(prompt)
        context = json.loads(prompt.rsplit("\n\n", 1)[1])
        self.contexts.append(context)
        self.budgets.append(max_model_calls)
        assert request_timeout_seconds > 0
        self.request_timeouts.append(request_timeout_seconds)
        number = context["turn_number"]
        tools_by_name = _tool_map(tools)
        if number in self.search_rounds:
            for index in range(self.searches_per_round):
                self.tool_results.append(
                    _invoke_json(tools_by_name["retrieve"], _retrieval_args(
                        f"query-{number}-{index}",
                        8,
                        transcript=self.channel_weights[0],
                        semantic=self.channel_weights[1],
                        visual=self.channel_weights[2],
                    ))
                )
        ranges = (
            [_selected_range(chunk_id) for chunk_id in self.selected]
            if self.selected_ranges is None
            else self.selected_ranges
        )
        for item in ranges:
            _read_all(tools_by_name["read_trieve_item"], item["video_chunk_id"])
            self.draft_tool_results.append(
                tools_by_name["update_draft_item"].invoke({
                    **item,
                    "claim": f"Grounded evidence for {item['video_chunk_id']}.",
                })
            )
        return result_type.model_validate({
            "plan_context": f"plan-{number}", "message": f"draft-{number}",
        })


class Evaluator:
    model_name = "evaluator-test"
    provider_name = "fake"

    def __init__(self, approve_at=1):
        self.approve_at = approve_at
        self.prompts = []
        self.contexts = []
        self.budgets = []
        self.request_timeouts = []

    def run_structured(
        self, prompt, *, tools, result_type, max_model_calls, request_timeout_seconds,
        check_control=None, max_input_chars=None,
    ):
        self.prompts.append(prompt)
        self.contexts.append(json.loads(prompt.rsplit("\n\n", 1)[1]))
        self.budgets.append(max_model_calls)
        assert request_timeout_seconds > 0
        self.request_timeouts.append(request_timeout_seconds)
        tools_by_name = _tool_map(tools)
        cursor = None
        item_ids = []
        while True:
            page = _invoke_json(tools_by_name["get_draft_catelog"], {"cursor": cursor})
            item_ids.extend(item["video_chunk_id"] for item in page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        for video_chunk_id in item_ids:
            _read_all(tools_by_name["read_draft_item"], video_chunk_id)
        number = len(self.contexts)
        return result_type.model_validate({
            "action": "complete" if number == self.approve_at else "continue",
            "advice": "find broken cup, not picked-up cup" if number != self.approve_at else "approved",
        })


class Chat:
    model_name = "chat-test"
    provider_name = "fake"

    def __init__(self):
        self.contexts = []
        self.request_timeouts = []
        self.model_call_budgets = []
        self.text_blocks = []
        self.video_items = []
        self.tool_results = []
        self.require_tool_calls = []

    def run_agent_loop(
        self, prompt, *, tools, max_model_calls, handle_text, require_tool_call=False,
        request_timeout_seconds=None, check_control=None, max_input_chars=None,
    ):
        assert [tool.name for tool in tools] == [
            "remove_video_item", "update_video_item",
            "get_draft_catelog", "read_draft_item",
        ]
        tools_by_name = _tool_map(tools)
        update_video_item = tools_by_name["update_video_item"]
        assert update_video_item.description
        assert update_video_item.args_schema is UpdateVideoItemArguments
        context = json.loads(prompt.rsplit("\n\n", 1)[1])
        self.contexts.append(context)
        assert request_timeout_seconds is not None
        assert request_timeout_seconds > 0
        self.request_timeouts.append(request_timeout_seconds)
        self.model_call_budgets.append(max_model_calls)
        assert check_control is not None
        chunk_ids = context["approved_turn"]["video_chunk_ids"]
        self.require_tool_calls.append(require_tool_call)
        assert require_tool_call is bool(chunk_ids)
        text_blocks = (
            ["## Final answer", "Grounded answer."]
            if chunk_ids else ["## Final answer\n\nGrounded answer."]
        )
        check_control()
        self.text_blocks.append(text_blocks[0])
        handle_text(text_blocks[0])
        for chunk_id in chunk_ids:
            check_control()
            item = UpdateVideoItemArguments.model_validate({
                "video_chunk_id": chunk_id,
                "claim": f"Grounded evidence for {chunk_id}.",
            })
            self.video_items.append(item)
            self.tool_results.append(json.loads(update_video_item.invoke(
                item.model_dump(mode="python")
            )))
        if len(text_blocks) == 2:
            check_control()
            self.text_blocks.append(text_blocks[1])
            handle_text(text_blocks[1])


def _runtime(query=None, evaluator=None, chat=None, retrieval=None, **kwargs):
    query, evaluator, chat, retrieval = (
        query or Query(), evaluator or Evaluator(), chat or Chat(), retrieval or Retrieval()
    )
    return AgentRuntime(query, evaluator, chat, retrieval.search, **kwargs), query, evaluator, chat, retrieval


def test_runtime_rejects_nonpositive_read_character_limit() -> None:
    with pytest.raises(ValueError, match="max_read_chars_per_call"):
        _runtime(max_read_chars_per_call=0)


@pytest.mark.parametrize("value", [0, float("inf"), float("nan")])
def test_runtime_rejects_invalid_chat_timeout(value) -> None:
    with pytest.raises(ValueError, match="chat_timeout_seconds"):
        _runtime(chat_timeout_seconds=value)


def test_runtime_obeys_outer_deadline_before_starting_video_graph():
    runtime, query, _, _, _ = _runtime()
    with pytest.raises(AgentModelInvocationError, match="deadline exceeded"):
        runtime.kick(_inputs(), deadline=monotonic() - 1)
    assert query.prompts == []


@pytest.mark.parametrize("approve_at", [1, 2, 3])
def test_approval_immediately_stops_loop_and_streams_once(approve_at):
    runtime, query, evaluator, chat, retrieval = _runtime(evaluator=Evaluator(approve_at))
    events = []
    result = runtime.kick(_inputs(), emit=events.append)
    assert len(query.contexts) == len(evaluator.contexts) == approve_at
    assert len(chat.contexts) == len(retrieval.calls) == 1
    assert 0 < chat.request_timeouts[0] <= 480
    assert result.agent_reply.message == "## Final answer\n\nGrounded answer."
    assert result.agent_reply.result_count == 1
    assert result.hits[0].evidence.start_ms == 1000
    assert result.hits[0].evidence.end_ms == 2000
    assert result.render_blocks[1].start_ms == 1100
    assert result.render_blocks[1].end_ms == 1900
    assert "update_draft_item" in query.prompts[0]
    assert "read_trieve_item" in query.prompts[0]
    assert "事实性结论" in query.prompts[0]
    assert "用户意图" in evaluator.prompts[0]
    assert "检索策略" in evaluator.prompts[0]
    assert "每个 Draft Item" in evaluator.prompts[0]
    assert "播放范围" in evaluator.prompts[0]
    assert evaluator.contexts[0]["current_draft"]["video_chunk_ids"] == [
        "chunk-1"
    ]
    assert [event["phase"] for event in events if event["type"] == "answer.progress"] == [
        *(["RETRIEVING", "EVALUATING"] * approve_at), "RESPONDING",
    ]
    delivery = [event for event in events if event["type"] != "answer.progress"]
    assert [event["type"] for event in delivery] == [
        "answer.evidence", "answer.block", "answer.block", "answer.block",
        "answer.delta",
    ]
    assert delivery[0]["searchResult"] == result.model_dump(mode="json", by_alias=True)
    assert [event["block"] for event in delivery[1:4]] == [
        block.model_dump(mode="json", by_alias=True)
        for block in result.render_blocks
    ]
    assert [event["block"]["type"] for event in delivery[1:4]] == [
        "markdown", "video", "markdown",
    ]
    assert result.hits[0].selection_reason.summary == "Grounded evidence for chunk-1."
    assert delivery[-1]["delta"] == "## Final answer\n\nGrounded answer."
    assert [item.video_chunk_id for item in chat.video_items] == ["chunk-1"]
    assert chat.require_tool_calls == [True]
    assert chat.tool_results == [{
        "accepted": True,
        "operation": "inserted",
        "video_chunk_id": "chunk-1",
        "video_item_count": 1,
    }]
    assert all(event["conversationId"] == "conversation" for event in events)
    assert all(event["messageId"] == "conversation-answer" for event in events)
    assert runtime.model_name == "chat-test"


def test_agent_logs_retrieved_selected_and_submitted_hit_counts(caplog):
    caplog.set_level(logging.INFO)
    runtime, *_ = _runtime(evaluator=Evaluator(2))

    runtime.kick(_inputs())

    assert "event=agent_turn_evaluated" in caplog.text
    assert "retrieved_hit_count=2 selected_hit_count=1" in caplog.text
    assert (
        'review_action=continue review_source=evaluator evaluator_action=continue '
        'evaluator_advice="find broken cup, not picked-up cup" '
        'review_advice="find broken cup, not picked-up cup"'
    ) in caplog.text
    assert (
        'review_action=complete review_source=evaluator evaluator_action=complete '
        'evaluator_advice="approved" review_advice="approved"'
    ) in caplog.text
    assert "event=agent_chat_rendered" in caplog.text
    assert "approved_hit_count=1 submitted_hit_count=1" in caplog.text
    for role in ("Query", "Evaluator", "Chat"):
        assert f"event=agent_tool_started role={role}" in caplog.text
        assert f"event=agent_tool_finished role={role}" in caplog.text
    assert "conversation_id=conversation message_id=conversation-answer" in caplog.text


def test_fourth_turn_runs_after_three_rejections_and_can_be_approved():
    runtime, query, evaluator, chat, _retrieval = _runtime(evaluator=Evaluator(4))
    events = []
    result = runtime.kick(_inputs(), emit=events.append)
    assert len(query.contexts) == len(evaluator.contexts) == 4
    assert len(chat.contexts) == 1
    assert result.agent_reply.message == "## Final answer\n\nGrounded answer."
    assert [event["phase"] for event in events if event["type"] == "answer.progress"] == [
        *(["RETRIEVING", "EVALUATING"] * 4), "RESPONDING",
    ]


def test_enabled_turn_budget_rejects_third_continue() -> None:
    runtime, query, evaluator, chat, _retrieval = _runtime(
        evaluator=Evaluator(99), limits_enabled=True,
    )
    with pytest.raises(VideoSearchFailure) as captured:
        runtime.kick(_inputs())
    assert captured.value.code is VideoSearchFailureCode.LOOP_LIMIT_EXCEEDED
    assert captured.value.phase is VideoSearchFailurePhase.EVALUATOR
    assert len(query.contexts) == len(evaluator.contexts) == 3
    assert chat.contexts == []


def test_auto_mode_ignores_legacy_count_limits_without_changing_runtime() -> None:
    runtime, query, evaluator, chat, _retrieval = _runtime(
        evaluator=Evaluator(4), limits_enabled=True,
        max_model_calls_per_turn=1, max_tool_calls_per_turn=1,
    )

    runtime.kick(_inputs(), max_loop_turns=0)

    assert len(query.contexts) == len(evaluator.contexts) == 4
    assert query.budgets == evaluator.budgets == [None] * 4
    assert chat.model_call_budgets == [None]
    assert runtime._limits_enabled is True
    assert runtime._max_loop_turns == 3


def test_custom_mode_limits_only_query_evaluator_turns_up_to_100() -> None:
    runtime, query, evaluator, chat, _retrieval = _runtime(
        evaluator=Evaluator(4), limits_enabled=True,
        max_model_calls_per_turn=1, max_tool_calls_per_turn=1,
    )

    runtime.kick(_inputs(), max_loop_turns=100)

    assert len(query.contexts) == len(evaluator.contexts) == 4
    assert query.budgets == evaluator.budgets == [None] * 4
    assert chat.model_call_budgets == [None]
    assert runtime._max_loop_turns == 3


def test_custom_mode_rejects_at_its_own_turn_limit() -> None:
    runtime, query, evaluator, chat, _retrieval = _runtime(
        evaluator=Evaluator(4), limits_enabled=False,
    )

    with pytest.raises(VideoSearchFailure) as captured:
        runtime.kick(_inputs(), max_loop_turns=2)
    assert captured.value.code is VideoSearchFailureCode.LOOP_LIMIT_EXCEEDED
    assert captured.value.phase is VideoSearchFailurePhase.EVALUATOR

    assert len(query.contexts) == len(evaluator.contexts) == 2
    assert chat.contexts == []


def test_concurrent_kicks_keep_auto_and_custom_limits_isolated() -> None:
    rendezvous = Barrier(2)

    class OverlappingQuery(Query):
        def run_structured(self, prompt, **kwargs):
            context = json.loads(prompt.rsplit("\n\n", 1)[1])
            if context["turn_number"] == 1:
                rendezvous.wait(timeout=5)
            return super().run_structured(prompt, **kwargs)

    class PerRunEvaluator:
        model_name = "per-run-evaluator"

        def run_structured(self, prompt, *, tools, result_type, max_model_calls, **_kwargs):
            assert max_model_calls is None
            context = json.loads(prompt.rsplit("\n\n", 1)[1])
            tools_by_name = _tool_map(tools)
            catalog = _invoke_json(tools_by_name["get_draft_catelog"], {})
            for item in catalog["items"]:
                _read_all(tools_by_name["read_draft_item"], item["video_chunk_id"])
            return result_type.model_validate({
                "action": (
                    "complete"
                    if context["inputs"]["conversation_id"] == "auto"
                    and len(context["turns"]) == 3
                    else "continue"
                ),
                "advice": "retry",
            })

    runtime, query, _evaluator, _chat, _retrieval = _runtime(
        query=OverlappingQuery(), evaluator=PerRunEvaluator(),
        limits_enabled=True,
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        auto = executor.submit(runtime.kick, _inputs("auto"), max_loop_turns=0)
        custom = executor.submit(runtime.kick, _inputs("custom"), max_loop_turns=2)
        assert auto.result(timeout=5).agent_reply.result_count == 1
        with pytest.raises(VideoSearchFailure) as captured:
            custom.result(timeout=5)
        assert captured.value.code is VideoSearchFailureCode.LOOP_LIMIT_EXCEEDED
        assert captured.value.phase is VideoSearchFailurePhase.EVALUATOR

    assert len([c for c in query.contexts if c["inputs"]["conversation_id"] == "auto"]) == 4
    assert len([c for c in query.contexts if c["inputs"]["conversation_id"] == "custom"]) == 2
    assert query.budgets == [None] * 6
    assert runtime._limits_enabled is True
    assert runtime._max_loop_turns == 3


@pytest.mark.parametrize("value", [-1, 101, True, 1.5, "2"])
def test_kick_rejects_invalid_loop_turn_limit(value) -> None:
    runtime, query, evaluator, chat, _retrieval = _runtime()

    with pytest.raises(ValueError, match="max_loop_turns must be between 0 and 100"):
        runtime.kick(_inputs(), max_loop_turns=value)

    assert query.contexts == evaluator.contexts == chat.contexts == []


def test_evaluator_cannot_complete_without_reading_the_full_draft(caplog) -> None:
    caplog.set_level(logging.INFO)

    class BlindEvaluator:
        model_name = "blind-evaluator"

        def __init__(self) -> None:
            self.calls = 0

        def run_structured(self, _prompt, *, result_type, **_kwargs):
            self.calls += 1
            return result_type.model_validate({
                "action": "complete",
                "advice": "approved without reading",
            })

    evaluator = BlindEvaluator()
    runtime, _query, _evaluator, chat, _retrieval = _runtime(
        evaluator=evaluator, limits_enabled=True,
    )

    with pytest.raises(VideoSearchFailure) as captured:
        runtime.kick(_inputs())
    assert captured.value.code is VideoSearchFailureCode.LOOP_LIMIT_EXCEEDED
    assert captured.value.phase is VideoSearchFailurePhase.EVALUATOR

    assert evaluator.calls == 3
    assert chat.contexts == []
    assert (
        'review_action=continue review_source=draft_read_guard '
        'evaluator_action=complete evaluator_advice="approved without reading"'
    ) in caplog.text


def test_next_query_gets_original_plan_all_calls_drafts_and_review_advice():
    runtime, query, evaluator, chat, retrieval = _runtime(
        query=Query(search_rounds=(1, 2), searches_per_round=2), evaluator=Evaluator(3),
    )
    result = runtime.kick(_inputs())
    third = query.contexts[2]
    assert third["original_plan"] == "plan-1"
    assert len(third["turns"]) == 2
    for number, record in enumerate(third["turns"], 1):
        assert record["plan_context"] == f"plan-{number}"
        assert record["draft"]["message"] == f"draft-{number}"
        assert record["review"] == {
            "action": "continue", "advice": "find broken cup, not picked-up cup",
        }
        assert len(record["retrieval_calls"]) == 2
        assert record["retrieval_calls"][0]["video_chunk_ids"] == ["chunk-1", "chunk-2"]
        assert "result" not in record["retrieval_calls"][0]
        assert "retrieval_result" not in record
        assert "draft_output" not in record
    assert third["turns"][1]["evidence_source_turns"] == {"chunk-1": 2}
    assert result.query == "query-2-1"
    assert [
        entry.model_dump(mode="json", by_alias=True)
        for entry in result.retrieval_trace
    ] == [
        {
            "query": f"query-{turn}-{call}",
            "topK": 8,
            "strategyVersion": "query-adaptive-three-channel-rrf-v2",
            "requestedWeights": {
                "transcript": 0.0,
                "semantic": 0.5,
                "visual": 0.5,
            },
            "effectiveWeights": {
                "transcript": 0.0,
                "semantic": 0.5,
                "visual": 0.5,
            },
            "resultCount": 2,
            "videoChunkIds": ["chunk-1", "chunk-2"],
        }
        for turn in (1, 2)
        for call in (0, 1)
    ]
    assert len(retrieval.calls) == 4
    assert len(evaluator.contexts[1]["retrieval_calls"]) == 2
    assert evaluator.contexts[1]["retrieval_calls"][0]["video_chunk_ids"] == [
        "chunk-1", "chunk-2",
    ]
    assert evaluator.contexts[2]["retrieval_calls"] == []
    assert evaluator.contexts[2]["evidence_source_turns"] == {"chunk-1": 2}
    assert evaluator.contexts[2]["current_draft"]["video_chunk_ids"] == ["chunk-1"]
    assert "selected_output" not in evaluator.contexts[2]
    approved = chat.contexts[0]["approved_turn"]
    assert approved["number"] == 3
    assert approved["message"] == "draft-3"
    assert approved["video_chunk_ids"] == ["chunk-1"]
    assert "turns" not in chat.contexts[0]
    assert "retrieval_result" not in approved
    assert "chunk-2" not in json.dumps(chat.contexts[0])
    # Creating/rendering the approved turn does not mutate previously delivered snapshots.
    assert query.contexts[1]["turns"][0] == third["turns"][0]


@pytest.mark.parametrize("mutation, expected", [
    (lambda hit: setattr(hit.evidence, "workspace_id", "other"), "outside the fixed scope"),
    (lambda hit: setattr(hit.evidence, "series_id", "other"), "outside the fixed scope"),
    (lambda hit: setattr(hit.evidence, "end_ms", 10_001), "invalid video time range"),
    (lambda hit: setattr(hit.evidence, "start_ms", -1), "invalid video time range"),
    (lambda hit: setattr(hit.evidence, "end_ms", 1000), "invalid video time range"),
])
def test_invalid_retrieved_scope_or_time_fails_before_evaluation(mutation, expected):
    hit = _hit()
    mutation(hit)
    runtime, _query, evaluator, chat, _retrieval = _runtime(retrieval=Retrieval([hit]))
    with pytest.raises(AgentModelInvocationError, match=expected):
        runtime.kick(_inputs())
    assert evaluator.contexts == chat.contexts == []


def test_unknown_selected_id_can_be_corrected_before_evaluator_or_chat():
    class CorrectingQuery(Query):
        def run_structured(self, prompt, **kwargs):
            feedback = _invoke_json(
                _tool_map(kwargs["tools"])["read_trieve_item"],
                {"video_chunk_id": "invented", "section": "overview"},
            )
            assert feedback["error"]["code"] == "UNKNOWN_ITEM"
            return super().run_structured(prompt, **kwargs)

    runtime, _query, evaluator, chat, _retrieval = _runtime(query=CorrectingQuery())
    result = runtime.kick(_inputs())

    assert len(evaluator.contexts) == len(chat.contexts) == 1
    assert [item.candidate.video_chunk_id for item in result.hits] == ["chunk-1"]


def test_out_of_chunk_range_is_returned_to_query_for_same_turn_revision():
    class CorrectingQuery(Query):
        def run_structured(self, prompt, **kwargs):
            self.selected_ranges = [
                _selected_range("chunk-1", start_ms=999, end_ms=1500)
            ]
            completion = super().run_structured(prompt, **kwargs)
            self.draft_tool_results.append(
                _tool_map(kwargs["tools"])["update_draft_item"].invoke({
                    **_selected_range("chunk-1"),
                    "claim": "Corrected grounded evidence.",
                })
            )
            return completion

    class AlwaysCompleteEvaluator(Evaluator):
        def run_structured(self, prompt, **kwargs):
            return super().run_structured(prompt, **kwargs).model_copy(
                update={"action": "complete", "advice": "approved"},
            )

    runtime, query, evaluator, chat, _retrieval = _runtime(
        query=CorrectingQuery(), evaluator=AlwaysCompleteEvaluator(),
    )

    result = runtime.kick(_inputs())

    feedback = json.loads(query.draft_tool_results[0])
    assert feedback["error"]["code"] == "INVALID_DRAFT_RANGE"
    assert feedback["error"]["message"].startswith("时间区间不合法：")
    assert json.loads(query.draft_tool_results[1])["accepted"] is True
    assert len(query.contexts) == len(evaluator.contexts) == len(chat.contexts) == 1
    assert result.render_blocks[1].start_ms == 1100
    assert result.render_blocks[1].end_ms == 1900


@pytest.mark.parametrize("start_ms,end_ms", [
    (True, 1900),
    ("1100", 1900),
    (1100.5, 1900),
    (1100, False),
    (1100, "1900"),
    (1100, 1900.5),
    (1500, 1500),
    (1600, 1500),
])
def test_selected_range_rejects_non_integer_or_non_increasing_time(
    start_ms, end_ms,
):
    with pytest.raises(ValueError):
        QueryDraft.model_validate({
            "plan_context": "plan",
            "message": "draft",
            "selected_video_ranges": [_selected_range(
                "chunk-1", start_ms=start_ms, end_ms=end_ms,
            )],
        })


def test_mismatched_candidate_and_evidence_ids_cannot_reach_evaluator():
    class MismatchedRetrieval(Retrieval):
        def search(self, query, **options):
            result = super().search(query, **options)
            result.hits[0].candidate.video_chunk_id = "another-id"
            return result

    runtime, _query, evaluator, chat, _retrieval = _runtime(retrieval=MismatchedRetrieval())
    with pytest.raises(AgentModelInvocationError):
        runtime.kick(_inputs())
    assert evaluator.contexts == chat.contexts == []


def test_selection_order_keeps_exact_real_evidence_and_approved_ranges():
    runtime, _query, _evaluator, _chat, retrieval = _runtime(
        query=Query(selected=("chunk-2", "chunk-1")),
    )
    result = runtime.kick(_inputs())
    assert [hit.candidate.video_chunk_id for hit in result.hits] == ["chunk-2", "chunk-1"]
    assert result.agent_reply.result_count == 2
    assert result.hits[0].candidate == retrieval.hits[1].candidate
    assert result.hits[0].evidence == retrieval.hits[1].evidence
    assert result.hits[0].selection_reason.summary == "Grounded evidence for chunk-2."
    assert result.hits[1].candidate == retrieval.hits[0].candidate
    assert result.hits[1].evidence == retrieval.hits[0].evidence
    assert result.hits[1].selection_reason.summary == "Grounded evidence for chunk-1."
    assert [
        (block.video_chunk_id, block.start_ms, block.end_ms)
        for block in result.render_blocks
        if block.type == "video"
    ] == [
        ("chunk-2", 1100, 1900),
        ("chunk-1", 1100, 1900),
    ]


def test_duplicate_draft_updates_are_deduplicated_by_chunk_id():
    runtime, _query, evaluator, _chat, _retrieval = _runtime(query=Query(
        selected=("chunk-1", "chunk-1"),
    ))

    result = runtime.kick(_inputs())

    assert result.agent_reply.result_count == 1
    assert evaluator.contexts[0]["current_draft"]["video_chunk_ids"] == ["chunk-1"]


def test_series_background_is_injected_once_without_leaking_into_each_hit():
    marker = "series-background-unique-marker"
    background = {"format": "markdown", "content": marker + "x" * 30_000}
    hits = [_hit(f"chunk-{index}") for index in range(1, 11)]
    for hit in hits:
        hit.evidence.important_extra_data = background
        hit.evidence.character_bible = [{"name": "Tomori"}]
    runtime, query, evaluator, chat, _retrieval = _runtime(retrieval=Retrieval(hits))
    inputs = _inputs()
    inputs["requested_top_k"] = 10
    inputs["series_context"] = [{
        "seriesId": "series", "name": "MyGO", "importantExtraData": background,
        "characterBible": [{"name": "Tomori"}], "videos": [],
    }]
    full_result_chars = len(json.dumps(VideoChunkSearchResult(
        query="query-1-0", series_ids=["series"],
        agent_reply=AgentReply(message="retrieval", result_count=len(hits)), hits=hits,
    ).model_dump(mode="json", by_alias=True), ensure_ascii=False))

    result = runtime.kick(inputs)

    assert json.dumps(query.contexts[0], ensure_ascii=False).count(marker) == 1
    assert marker not in json.dumps(query.tool_results[0], ensure_ascii=False)
    evaluator_context = json.dumps(evaluator.contexts[0], ensure_ascii=False)
    assert evaluator_context.count(marker) == 1
    assert len(evaluator_context) < full_result_chars // 4
    assert marker not in json.dumps(chat.contexts[0], ensure_ascii=False)
    tool_card = query.tool_results[0]["items"][0]
    assert tool_card["description"] == "matched action"
    assert "evidence" not in tool_card
    assert result.hits[0].evidence.important_extra_data == background
    assert result.hits[0].evidence.character_bible == [{"name": "Tomori"}]


def test_stored_history_keeps_order_and_ids_without_replaying_full_evidence():
    marker = "historical-background-must-not-enter-prompt"
    evidence_marker = "bounded-history-evidence-marker"
    window_marker = "bounded-history-window-marker"
    historical = VideoChunkSearchResult(
        query="old query", series_ids=["series"],
        agent_reply=AgentReply(message="old answer", result_count=1), hits=[_hit()],
    )
    historical.hits[0].evidence.important_extra_data = {
        "format": "markdown", "content": marker + "x" * 30_000,
    }
    historical.hits[0].evidence.video_compact = evidence_marker + "x" * 3_000
    historical.hits[0].evidence.window_summary = window_marker + "x" * 3_000
    historical.hits[0].selection_reason.matched_evidence = [
        MatchedEvidence(
            kind="CONTEXT", text=f"{evidence_marker}-{index}-" + "x" * 1_100,
            start_ms=1000, end_ms=2000,
        )
        for index in range(6)
    ]
    inputs = _inputs()
    inputs["history"] = [{
        "role": "assistant", "content": "old answer", "seriesIds": ["series"],
        "messageId": "old-message", "status": "COMPLETED", "reference": None,
        "searchResult": historical.model_dump(mode="json", by_alias=True),
    }]
    runtime, query, evaluator, chat, _retrieval = _runtime()

    runtime.kick(inputs)

    projected = query.contexts[0]["inputs"]["history"][0]["searchResult"]
    assert projected["hits"][0]["candidate"]["videoChunkId"] == "chunk-1"
    projected_hit = projected["hits"][0]
    assert len(projected_hit["evidence"]["videoCompact"]) == 2_000
    assert projected_hit["evidence"]["videoCompact"].startswith(evidence_marker)
    assert projected_hit["evidence"]["videoCompact"].endswith("…")
    assert len(projected_hit["evidence"]["windowSummary"]) == 2_000
    assert projected_hit["evidence"]["windowSummary"].startswith(window_marker)
    matched = projected_hit["selectionReason"]["matchedEvidence"]
    assert len(matched) == 4
    assert [item["text"].startswith(f"{evidence_marker}-{index}-")
            for index, item in enumerate(matched)] == [True] * 4
    assert all(len(item["text"]) == 1_000 and item["text"].endswith("…") for item in matched)
    assert marker not in json.dumps(query.contexts[0], ensure_ascii=False)
    assert marker not in json.dumps(evaluator.contexts[0], ensure_ascii=False)
    assert marker not in json.dumps(chat.contexts[0], ensure_ascii=False)


def test_reference_context_uses_compact_evidence_without_repeating_background():
    background_marker = "reference-background-must-not-enter-prompt"
    evidence_marker = "reference-evidence-must-appear-once"
    matched_marker = "reference-matched-evidence-must-appear-once"
    hit = _hit()
    evidence = hit.evidence
    evidence.baseline_visual_caption = evidence_marker
    hit.selection_reason.matched_evidence = [MatchedEvidence(
        kind="CONTEXT", text=matched_marker, start_ms=1000, end_ms=2000,
    )]
    evidence.important_extra_data = {
        "format": "markdown", "content": background_marker + "x" * 30_000,
    }
    inputs = _inputs()
    inputs["reference_context"] = {
        "reference": {"messageId": "old-message", "videoChunkId": "chunk-1"},
        "sourceStatus": "COMPLETED",
        "selectionReason": hit.selection_reason.model_dump(mode="json", by_alias=True),
        "evidence": evidence.model_dump(mode="json", by_alias=True),
    }
    runtime, query, evaluator, chat, _retrieval = _runtime(retrieval=Retrieval([hit]))

    runtime.kick(inputs)

    projected = query.contexts[0]["inputs"]["reference_context"]
    assert projected["evidence"]["videoChunkId"] == "chunk-1"
    assert projected["evidence"]["videoCompact"] == "cup action"
    query_context = json.dumps(query.contexts[0], ensure_ascii=False)
    evaluator_context = json.dumps(evaluator.contexts[0], ensure_ascii=False)
    chat_context = json.dumps(chat.contexts[0], ensure_ascii=False)
    assert background_marker not in query_context + evaluator_context + chat_context
    assert query_context.count(evidence_marker) == 1
    assert evaluator_context.count(evidence_marker) == 0
    assert chat_context.count(evidence_marker) == 0
    assert query_context.count(matched_marker) == 1
    assert evaluator_context.count(matched_marker) == 0
    assert chat_context.count(matched_marker) == 0


def test_multi_turn_context_keeps_one_canonical_evidence_copy():
    marker = "canonical-evidence-copy-marker"
    retrieval = Retrieval()
    retrieval.hits[0].evidence.baseline_visual_caption = marker
    runtime, query, evaluator, chat, _retrieval = _runtime(
        query=Query(search_rounds=(1,)), evaluator=Evaluator(3), retrieval=retrieval,
    )

    runtime.kick(_inputs())

    assert json.dumps(query.contexts[2], ensure_ascii=False).count(marker) == 0
    assert json.dumps(evaluator.contexts[2], ensure_ascii=False).count(marker) == 0
    assert all("retrieval_result" not in turn for turn in query.contexts[2]["turns"])
    assert all("draft_output" not in turn for turn in query.contexts[2]["turns"])


@pytest.mark.parametrize("mutation, expected", [
    (lambda result: setattr(result, "series_ids", []), "changed the fixed Series scope"),
    (lambda result: setattr(result, "series_ids", ["other"]), "changed the fixed Series scope"),
    (lambda result: setattr(result.hits[0].evidence, "analysis_window_start_ms", 1001), "analysis window"),
    (lambda result: setattr(result.hits[0].evidence, "analysis_window_end_ms", 1999), "analysis window"),
    (lambda result: setattr(result.hits[0].evidence, "analysis_window_end_ms", 10_001), "analysis window"),
    (lambda result: result.hits[0].selection_reason.matched_evidence.append(
        MatchedEvidence.model_construct(kind="CONTEXT", text="cup", start_ms=None, end_ms=1000)
    ), "incomplete video time range"),
])
def test_retrieval_contract_corruption_is_rejected_before_evaluation(mutation, expected):
    class CorruptedRetrieval(Retrieval):
        def search(self, query, **options):
            result = super().search(query, **options)
            mutation(result)
            return result

    runtime, _query, evaluator, chat, _retrieval = _runtime(retrieval=CorruptedRetrieval())
    with pytest.raises(AgentModelInvocationError, match=expected):
        runtime.kick(_inputs())
    assert evaluator.contexts == chat.contexts == []


def test_duplicate_chunk_in_one_retrieval_is_rejected():
    class DuplicateRetrieval(Retrieval):
        def search(self, query, **options):
            result = super().search(query, **options)
            result.hits[1] = result.hits[0].model_copy(deep=True)
            return result

    runtime, _query, evaluator, chat, _retrieval = _runtime(retrieval=DuplicateRetrieval())
    with pytest.raises(AgentModelInvocationError, match="duplicate VideoChunk IDs"):
        runtime.kick(_inputs())
    assert evaluator.contexts == chat.contexts == []


def test_first_turn_must_actually_retrieve_even_when_answer_has_no_selected_hits():
    runtime, _query, evaluator, chat, _retrieval = _runtime(query=Query(search_rounds=(), selected=()))
    with pytest.raises(AgentModelInvocationError, match="must retrieve real evidence"):
        runtime.kick(_inputs())
    assert evaluator.contexts == chat.contexts == []


def test_empty_real_retrieval_can_deliver_an_approved_uncertain_answer():
    runtime, _query, _evaluator, _chat, retrieval = _runtime(
        query=Query(selected=()), retrieval=Retrieval([]),
    )
    result = runtime.kick(_inputs())
    assert len(retrieval.calls) == 1
    assert result.hits == []
    assert result.agent_reply.result_count == 0
    assert [block.type for block in result.render_blocks] == ["markdown"]


def test_tool_scope_is_not_an_exposed_model_argument_and_top_k_is_bounded():
    class InspectQuery(Query):
        def run_structured(self, prompt, *, tools, result_type, **kwargs):
            assert "# Video Retrieval Skill" in prompt
            assert set(tools[0].args_schema.model_fields) == {
                "query", "top_k", "channel_weights", "cursor",
            }
            return super().run_structured(
                prompt,
                tools=tools,
                result_type=result_type,
                **kwargs,
            )

    runtime, query, _evaluator, _chat, retrieval = _runtime(
        query=InspectQuery(), maximum_top_k=2, max_model_calls_per_turn=5,
        limits_enabled=True,
    )
    runtime.kick(_inputs())
    assert retrieval.calls == [("query-1-0", "workspace", ["series"], 2)]
    assert retrieval.weights == [(0.0, 0.5, 0.5)]
    assert query.budgets == [5]


def test_kick_run_limits_override_only_the_current_execution():
    runtime, query, evaluator, chat, _retrieval = _runtime(limits_enabled=True)
    runtime.kick(
        _inputs(),
        max_model_calls_per_turn=7,
        max_tool_calls_per_turn=10,
    )
    assert query.budgets == [7]
    assert evaluator.budgets == [7]
    assert chat.model_call_budgets == [7]
    assert runtime._max_model_calls == 6
    assert runtime._max_tool_calls == 24


def test_disabled_limits_ignore_count_but_preserve_run_options():
    runtime, query, evaluator, chat, _retrieval = _runtime()
    runtime.kick(
        _inputs(), max_model_calls_per_turn=1, max_tool_calls_per_turn=1,
    )
    assert query.budgets == evaluator.budgets == chat.model_call_budgets == [None]


@pytest.mark.parametrize(
    (
        "execution_timeout_seconds",
        "evaluator_stage_timeout_seconds",
        "chat_timeout_seconds",
        "expected_query_timeout_seconds",
        "expected_evaluator_timeout_seconds",
        "expected_chat_timeout_seconds",
    ),
    [
        (300, 180, 240, 120, 180, 240),
        (300, 75, 60, 120, 75, 60),
        (45, 180, 240, 45, 45, 45),
    ],
)
def test_role_timeouts_use_stage_or_global_remaining_minimum(
    monkeypatch,
    execution_timeout_seconds,
    evaluator_stage_timeout_seconds,
    chat_timeout_seconds,
    expected_query_timeout_seconds,
    expected_evaluator_timeout_seconds,
    expected_chat_timeout_seconds,
):
    clock = 100.0
    monkeypatch.setattr("server.agent.video_search.runtime.monotonic", lambda: clock)
    monkeypatch.setattr("server.agent.common.state.monotonic", lambda: clock)
    runtime, query, evaluator, chat, _retrieval = _runtime(
        stage_timeout_seconds=120,
        evaluator_stage_timeout_seconds=evaluator_stage_timeout_seconds,
        chat_timeout_seconds=chat_timeout_seconds,
        execution_timeout_seconds=execution_timeout_seconds,
    )

    runtime.kick(_inputs())

    assert query.request_timeouts == [pytest.approx(expected_query_timeout_seconds)]
    assert evaluator.request_timeouts == [pytest.approx(expected_evaluator_timeout_seconds)]
    assert chat.request_timeouts == [pytest.approx(expected_chat_timeout_seconds)]


def test_query_stage_deadline_stops_before_evaluator(monkeypatch):
    clock = 100.0
    monkeypatch.setattr("server.agent.video_search.runtime.monotonic", lambda: clock)
    monkeypatch.setattr("server.agent.common.state.monotonic", lambda: clock)

    class ExpiredQuery(Query):
        def run_structured(self, _prompt, *, result_type, **_kwargs):
            nonlocal clock
            clock = 221.0
            return result_type.model_validate({
                "plan_context": "late", "message": "late",
            })

    runtime, _query, evaluator, chat, _retrieval = _runtime(
        query=ExpiredQuery(), stage_timeout_seconds=120,
        execution_timeout_seconds=300,
    )
    with pytest.raises(AgentModelInvocationError, match="deadline exceeded"):
        runtime.kick(_inputs())
    assert evaluator.contexts == chat.contexts == []


def test_model_selected_weights_are_normalized_recorded_and_returned_to_agent():
    runtime, query, evaluator, _chat, retrieval = _runtime(
        query=Query(channel_weights=(0.6, 0.3, 0.1)),
    )

    result = runtime.kick(_inputs())

    assert retrieval.weights[0] == pytest.approx((0.6, 0.3, 0.1))
    strategy = query.tool_results[0]["retrieval_strategy"]
    assert strategy["requested_weights"] == {
        "transcript": 0.6,
        "semantic": 0.3,
        "visual": 0.1,
    }
    assert strategy["effective_weights"] == pytest.approx({
        "transcript": 0.6,
        "semantic": 0.3,
        "visual": 0.1,
    })
    recorded = evaluator.contexts[0]["retrieval_calls"][0]["retrieval_strategy"]
    assert recorded["requested_weights"] == strategy["requested_weights"]
    assert recorded["effective_weights"] == pytest.approx(
        strategy["effective_weights"]
    )
    trace = result.model_dump(mode="json", by_alias=True)["retrievalTrace"]
    assert trace == [{
        "query": "query-1-0",
        "topK": 8,
        "strategyVersion": "query-adaptive-three-channel-rrf-v2",
        "requestedWeights": {
            "transcript": 0.6,
            "semantic": 0.3,
            "visual": 0.1,
        },
        "effectiveWeights": pytest.approx({
            "transcript": 0.6,
            "semantic": 0.3,
            "visual": 0.1,
        }),
        "resultCount": 2,
        "videoChunkIds": ["chunk-1", "chunk-2"],
    }]


@pytest.mark.parametrize(
    "weights",
    [
        {"transcript": True, "semantic": 0.25, "visual": 0.25},
        {"transcript": 0.25, "semantic": True, "visual": 0.25},
        {"transcript": 0.25, "semantic": 0.25, "visual": False},
        {"transcript": -0.1, "semantic": 0.1, "visual": 1.0},
        {"transcript": 0.0, "semantic": 0.0, "visual": 0.0},
        {"transcript": 0.5, "semantic": 0.25, "visual": 0.25, "unknown": 1.0},
    ],
)
def test_agent_channel_weights_reject_invalid_values(weights):
    with pytest.raises(ValueError):
        RetrievalChannelWeights.model_validate(weights)


def test_legacy_two_channel_trace_weights_default_transcript_to_zero():
    weights = RetrievalChannelWeights.model_validate(
        {"semantic": 0.6, "visual": 0.4}
    )

    assert weights.model_dump() == {
        "transcript": 0.0,
        "semantic": 0.6,
        "visual": 0.4,
    }


def test_query_can_make_more_tool_calls_than_the_removed_budget():
    runtime, _query, evaluator, chat, retrieval = _runtime(
        query=Query(searches_per_round=25),
    )

    runtime.kick(_inputs())

    assert len(retrieval.calls) == 25
    assert len(evaluator.contexts) == len(chat.contexts) == 1


def test_enabled_tool_budget_stops_before_an_extra_search_or_evaluation():
    runtime, _query, evaluator, chat, retrieval = _runtime(
        query=Query(searches_per_round=2), max_tool_calls_per_turn=1,
        limits_enabled=True,
    )
    with pytest.raises(VideoSearchFailure) as captured:
        runtime.kick(_inputs())
    assert captured.value.code is VideoSearchFailureCode.LOOP_LIMIT_EXCEEDED
    assert captured.value.phase is VideoSearchFailurePhase.QUERY
    assert len(retrieval.calls) == 1
    assert evaluator.contexts == chat.contexts == []


def test_kick_can_lower_tool_budget_without_mutating_runtime_default():
    runtime, _query, evaluator, chat, retrieval = _runtime(
        query=Query(searches_per_round=2), limits_enabled=True,
    )
    with pytest.raises(VideoSearchFailure) as captured:
        runtime.kick(_inputs(), max_tool_calls_per_turn=1)
    assert captured.value.code is VideoSearchFailureCode.LOOP_LIMIT_EXCEEDED
    assert captured.value.phase is VideoSearchFailurePhase.QUERY
    assert len(retrieval.calls) == 1
    assert evaluator.contexts == chat.contexts == []
    assert runtime._max_tool_calls == 24


def test_prompt_character_budget_stops_before_the_query_provider():
    runtime, query, evaluator, chat, retrieval = _runtime(max_prompt_chars=100)

    with pytest.raises(VideoSearchFailure) as captured:
        runtime.kick(_inputs())
    assert captured.value.code is VideoSearchFailureCode.CONTEXT_BUDGET_EXCEEDED
    assert captured.value.phase is VideoSearchFailurePhase.QUERY

    assert query.contexts == evaluator.contexts == chat.contexts == retrieval.calls == []


def test_complete_query_message_budget_error_is_classified_for_supervisor():
    class OversizedQuery(Query):
        def run_structured(self, *args, **kwargs):
            raise ModelInputTooLargeError("complete Query messages exceeded the limit")

    runtime, _query, evaluator, chat, retrieval = _runtime(query=OversizedQuery())

    with pytest.raises(VideoSearchFailure) as captured:
        runtime.kick(_inputs())
    assert captured.value.code is VideoSearchFailureCode.CONTEXT_BUDGET_EXCEEDED
    assert captured.value.phase is VideoSearchFailurePhase.QUERY

    assert evaluator.contexts == chat.contexts == retrieval.calls == []


@pytest.mark.parametrize(("reason", "expected_code"), [
    (
        ModelFailureReason.OUTPUT_TOKEN_LIMIT,
        VideoSearchFailureCode.OUTPUT_TOKEN_LIMIT,
    ),
    (
        ModelFailureReason.MODEL_TIMEOUT,
        VideoSearchFailureCode.MODEL_TIMEOUT,
    ),
    (
        ModelFailureReason.REQUIRED_TOOL_MISSING,
        VideoSearchFailureCode.REQUIRED_TOOL_MISSING,
    ),
    (
        ModelFailureReason.INVALID_MODEL_RESPONSE,
        VideoSearchFailureCode.INVALID_MODEL_RESPONSE,
    ),
])
def test_query_model_failure_reason_is_preserved_for_supervisor(
    reason,
    expected_code,
):
    class FailingQuery(Query):
        def run_structured(self, *args, **kwargs):
            raise QueryModelIncompleteError("provider detail", reason=reason)

    runtime, _query, evaluator, chat, retrieval = _runtime(query=FailingQuery())

    with pytest.raises(VideoSearchFailure) as captured:
        runtime.kick(_inputs())

    assert captured.value.code is expected_code
    assert captured.value.phase is VideoSearchFailurePhase.QUERY
    assert isinstance(captured.value.__cause__, AgentModelInvocationError)
    assert evaluator.contexts == chat.contexts == retrieval.calls == []


def test_evaluator_model_failure_is_attributed_to_evaluator_phase():
    class FailingEvaluator(Evaluator):
        def run_structured(self, *args, **kwargs):
            raise QueryModelIncompleteError(
                "provider detail",
                reason=ModelFailureReason.REQUIRED_TOOL_MISSING,
            )

    runtime, _query, _evaluator, chat, _retrieval = _runtime(
        evaluator=FailingEvaluator(),
    )

    with pytest.raises(VideoSearchFailure) as captured:
        runtime.kick(_inputs())

    assert captured.value.code is VideoSearchFailureCode.REQUIRED_TOOL_MISSING
    assert captured.value.phase is VideoSearchFailurePhase.EVALUATOR
    assert chat.contexts == []


def test_chat_model_failure_is_attributed_to_chat_phase():
    class FailingChat(Chat):
        def run_agent_loop(self, *args, **kwargs):
            raise QueryModelIncompleteError(
                "provider detail",
                reason=ModelFailureReason.INVALID_MODEL_RESPONSE,
            )

    runtime, *_ = _runtime(chat=FailingChat())

    with pytest.raises(VideoSearchFailure) as captured:
        runtime.kick(_inputs())

    assert captured.value.code is VideoSearchFailureCode.INVALID_MODEL_RESPONSE
    assert captured.value.phase is VideoSearchFailurePhase.CHAT


def test_retrieval_dependency_failure_is_attributed_to_retrieval_phase():
    class FailingRetrieval(Retrieval):
        def search(self, query, **options):
            raise RuntimeError("private dependency detail")

    runtime, _query, evaluator, chat, _retrieval = _runtime(
        retrieval=FailingRetrieval(),
    )

    with pytest.raises(VideoSearchFailure) as captured:
        runtime.kick(_inputs())

    assert captured.value.code is VideoSearchFailureCode.RETRIEVAL_FAILED
    assert captured.value.phase is VideoSearchFailurePhase.RETRIEVAL
    assert "private dependency detail" not in str(captured.value)
    assert evaluator.contexts == chat.contexts == []


@pytest.mark.parametrize("message", [
    "programming failure",
    "agent loop exhausted max_model_calls before stop",
])
def test_unknown_query_failure_remains_fatal(message):
    class FailingQuery(Query):
        def run_structured(self, *args, **kwargs):
            raise RuntimeError(message)

    runtime, *_ = _runtime(query=FailingQuery())

    with pytest.raises(AgentModelInvocationError) as captured:
        runtime.kick(_inputs())

    assert not isinstance(captured.value, VideoSearchFailure)


def test_parallel_tool_calls_share_one_turn_budget_and_keep_actual_records():
    class ParallelQuery(Query):
        def run_structured(
            self, prompt, *, tools, result_type, max_model_calls,
            request_timeout_seconds, check_control=None, max_input_chars=None,
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(
                    tools[0].invoke,
                    _retrieval_args(f"parallel-{i}", 2),
                ) for i in range(2)]
                for future in futures:
                    future.result(timeout=5)
            return result_type.model_validate({
                "plan_context": "parallel", "message": "draft",
                "selected_video_ranges": [_selected_range("chunk-1")],
            })

    runtime, _query, evaluator, chat, retrieval = _runtime(
        query=ParallelQuery(), max_tool_calls_per_turn=1,
        limits_enabled=True,
    )
    with pytest.raises(VideoSearchFailure) as captured:
        runtime.kick(_inputs())
    assert captured.value.code is VideoSearchFailureCode.LOOP_LIMIT_EXCEEDED
    assert captured.value.phase is VideoSearchFailurePhase.QUERY
    assert len(retrieval.calls) == 1
    assert evaluator.contexts == chat.contexts == []


def test_current_turn_cannot_change_the_scope_even_to_a_subset():
    inputs = _inputs()
    inputs["series_ids"] = ["series", "second-series"]

    class NarrowRetrieval(Retrieval):
        def search(self, query, **options):
            return super().search(query, **{**options, "series_ids": ["series"]})

    runtime, _query, evaluator, chat, _retrieval = _runtime(retrieval=NarrowRetrieval())
    with pytest.raises(AgentModelInvocationError, match="changed the fixed Series scope"):
        runtime.kick(inputs)
    assert evaluator.contexts == chat.contexts == []


def test_chat_publishes_no_output_until_tools_and_final_text_are_ready():
    started, release = Event(), Event()

    class BlockedChat(Chat):
        def run_agent_loop(self, prompt, **kwargs):
            started.set()
            assert release.wait(5)
            return super().run_agent_loop(prompt, **kwargs)

    runtime, *_ = _runtime(chat=BlockedChat())
    events = []

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(runtime.kick, _inputs(), emit=events.append)
        try:
            assert started.wait(5)
            assert not future.done()
            assert [event for event in events if event["type"] != "answer.progress"] == []
        finally:
            release.set()
        result = future.result(timeout=5)
    assert result.agent_reply.message == "## Final answer\n\nGrounded answer."
    assert [event["delta"] for event in events if event["type"] == "answer.delta"] == [
        "## Final answer\n\nGrounded answer.",
    ]


def test_chat_failure_emits_no_unvalidated_evidence_or_text():
    class FailedChat(Chat):
        def run_agent_loop(self, prompt, **kwargs):
            super().run_agent_loop(prompt, **kwargs)
            raise RuntimeError("provider lost")

    chat = FailedChat()
    runtime, *_ = _runtime(chat=chat)
    events = []
    with pytest.raises(AgentModelInvocationError, match="Chat Agent model"):
        runtime.kick(_inputs(), emit=events.append)
    assert [item.video_chunk_id for item in chat.video_items] == ["chunk-1"]
    assert [event for event in events if event["type"] != "answer.progress"] == []


def test_chat_cannot_submit_unapproved_evidence_ids() -> None:
    class InventedEvidenceChat(Chat):
        def run_agent_loop(
            self, prompt, *, tools, **kwargs,
        ):
            feedback = _invoke_json(_tool_map(tools)["update_video_item"], {
                "video_chunk_id": "invented-chunk",
                "claim": "Invented evidence.",
            })
            assert feedback["error"]["code"] == "UNKNOWN_ITEM"
            return super().run_agent_loop(prompt, tools=tools, **kwargs)

    runtime, *_ = _runtime(chat=InventedEvidenceChat())
    events = []
    result = runtime.kick(_inputs(), emit=events.append)

    assert [item.candidate.video_chunk_id for item in result.hits] == ["chunk-1"]


def test_chat_cannot_silently_drop_all_approved_evidence() -> None:
    class TextOnlyChat(Chat):
        def run_agent_loop(self, prompt, *, handle_text, **kwargs):
            assert kwargs["require_tool_call"] is True
            handle_text("Grounded answer without its approved video.")

    runtime, *_ = _runtime(chat=TextOnlyChat())
    events = []

    with pytest.raises(
        AgentModelInvocationError,
        match="without placing approved video evidence",
    ):
        runtime.kick(_inputs(), emit=events.append)

    assert [event for event in events if event["type"] != "answer.progress"] == []


def test_chat_stop_feedback_guides_missing_video_item_and_allows_correction() -> None:
    class CorrectingChat(Chat):
        supports_stop_feedback = True

        def __init__(self):
            super().__init__()
            self.stop_feedback_messages = []

        def run_agent_loop(
            self, prompt, *, tools, handle_text, stop_feedback, **kwargs,
        ):
            assert kwargs["require_tool_call"] is True
            handle_text("Grounded answer.")
            self.stop_feedback_messages.append(stop_feedback())
            assert "update_video_item" in self.stop_feedback_messages[0]
            assert _invoke_json(_tool_map(tools)["update_video_item"], {
                "video_chunk_id": "chunk-1",
                "claim": "Grounded evidence.",
            })["accepted"] is True
            self.stop_feedback_messages.append(stop_feedback())

    chat = CorrectingChat()
    runtime, *_ = _runtime(chat=chat)
    result = runtime.kick(_inputs())

    assert chat.stop_feedback_messages[1] is None
    assert result.agent_reply.message == "Grounded answer."
    assert [block.type for block in result.render_blocks] == ["markdown", "video"]
    assert [hit.candidate.video_chunk_id for hit in result.hits] == ["chunk-1"]


def test_chat_updates_the_same_approved_id_in_place() -> None:
    class DuplicateEvidenceChat(Chat):
        def run_agent_loop(
            self, prompt, *, tools, handle_text, **kwargs,
        ):
            handle_text("Grounded answer.")
            item = {
                "video_chunk_id": "chunk-1",
                "claim": "Grounded evidence.",
            }
            update = _tool_map(tools)["update_video_item"]
            update.invoke(item)
            update.invoke({**item, "claim": "Revised grounded evidence."})

    runtime, *_ = _runtime(chat=DuplicateEvidenceChat())
    events = []

    result = runtime.kick(_inputs(), emit=events.append)

    assert result.hits[0].selection_reason.summary == "Revised grounded evidence."


def test_chat_requires_nonempty_markdown_even_when_video_was_submitted() -> None:
    class VideoOnlyChat(Chat):
        def run_agent_loop(
            self, prompt, *, tools, **kwargs,
        ):
            _tool_map(tools)["update_video_item"].invoke({
                "video_chunk_id": "chunk-1",
                "claim": "Grounded evidence.",
            })

    runtime, *_ = _runtime(chat=VideoOnlyChat())
    events = []

    with pytest.raises(AgentModelInvocationError, match="Chat Agent model"):
        runtime.kick(_inputs(), emit=events.append)

    assert [event for event in events if event["type"] != "answer.progress"] == []


@pytest.mark.parametrize("submitted_ids", [("chunk-1",), ("chunk-1", "chunk-2")])
def test_chat_may_choose_an_approved_subset_in_any_order(submitted_ids) -> None:
    class ChoosingChat(Chat):
        def run_agent_loop(
            self, prompt, *, tools, handle_text, **kwargs,
        ):
            handle_text("Before video.")
            for chunk_id in submitted_ids:
                _tool_map(tools)["update_video_item"].invoke({
                    "video_chunk_id": chunk_id,
                    "claim": f"Chosen {chunk_id}.",
                })
            handle_text("After video.")

    runtime, *_ = _runtime(
        query=Query(selected=("chunk-2", "chunk-1")),
        chat=ChoosingChat(),
    )

    result = runtime.kick(_inputs())

    assert [hit.candidate.video_chunk_id for hit in result.hits] == list(submitted_ids)
    assert [hit.selection_reason.summary for hit in result.hits] == [
        f"Chosen {chunk_id}." for chunk_id in submitted_ids
    ]
    assert [block.type for block in result.render_blocks] == [
        "markdown", *(["video"] * len(submitted_ids)), "markdown",
    ]
    assert result.agent_reply.message == "Before video.\n\nAfter video."


def test_chat_tool_does_not_accept_a_playback_range() -> None:
    class RangeSubmittingChat(Chat):
        def run_agent_loop(self, prompt, *, tools, **kwargs):
            feedback = _invoke_json(_tool_map(tools)["update_video_item"], {
                "video_chunk_id": "chunk-1",
                "start_ms": 1000,
                "end_ms": 2000,
                "claim": "Grounded evidence.",
            })
            assert feedback["error"]["code"] == "INVALID_ARGUMENTS"
            return super().run_agent_loop(prompt, tools=tools, **kwargs)

    runtime, *_ = _runtime(chat=RangeSubmittingChat())
    result = runtime.kick(_inputs())
    assert [item.candidate.video_chunk_id for item in result.hits] == ["chunk-1"]


def test_out_of_chunk_range_can_be_discarded_without_crashing_chat() -> None:
    runtime, query, evaluator, chat, _retrieval = _runtime(
        query=Query(selected_ranges=[
            _selected_range("chunk-1", start_ms=999, end_ms=1500)
        ]),
    )

    result = runtime.kick(_inputs())

    assert len(query.contexts) == 1
    feedback = json.loads(query.draft_tool_results[0])
    assert feedback["error"]["code"] == "INVALID_DRAFT_RANGE"
    assert feedback["error"]["message"].startswith("时间区间不合法：")
    assert evaluator.contexts[0]["current_draft"]["video_chunk_ids"] == []
    assert chat.contexts[0]["approved_turn"]["video_chunk_ids"] == []
    assert result.hits == []


def test_cancel_after_agent_loop_stops_before_output():
    cancellation = Event()

    class CancelChat(Chat):
        def run_agent_loop(self, prompt, **kwargs):
            super().run_agent_loop(prompt, **kwargs)
            cancellation.set()

    runtime, *_ = _runtime(chat=CancelChat())
    events = []

    with pytest.raises(AgentModelInvocationError, match="cancelled"):
        runtime.kick(_inputs(), emit=events.append, cancellation=cancellation)
    assert [event for event in events if event["type"] != "answer.progress"] == []


def test_deadline_after_a_model_call_stops_before_chat(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("server.agent.video_search.runtime.monotonic", lambda: clock[0])
    monkeypatch.setattr("server.agent.common.state.monotonic", lambda: clock[0])

    class SlowEvaluator(Evaluator):
        def run_structured(self, prompt, **kwargs):
            result = super().run_structured(prompt, **kwargs)
            clock[0] = 102.0
            return result

    runtime, _query, evaluator, chat, _retrieval = _runtime(
        evaluator=SlowEvaluator(), execution_timeout_seconds=1,
    )
    with pytest.raises(AgentModelInvocationError, match="deadline exceeded"):
        runtime.kick(_inputs())
    assert len(evaluator.contexts) == 1
    assert chat.contexts == []


def test_cancelled_kick_does_not_call_any_agent_or_tool():
    cancellation = Event()
    cancellation.set()
    runtime, query, evaluator, chat, retrieval = _runtime()
    with pytest.raises(AgentModelInvocationError, match="cancelled"):
        runtime.kick(_inputs(), cancellation=cancellation)
    assert query.contexts == evaluator.contexts == chat.contexts == retrieval.calls == []


def test_shared_graph_concurrent_kicks_keep_tool_scope_context_and_events_separate():
    rendezvous = Barrier(2)
    events = {"a": [], "b": []}

    class ScopedQuery:
        model_name = "scoped-query"

        def run_structured(
            self, prompt, *, tools, result_type, max_model_calls,
            request_timeout_seconds,
            check_control=None, max_input_chars=None,
        ):
            assert request_timeout_seconds > 0
            context = json.loads(prompt.rsplit("\n\n", 1)[1])
            assert context["turns"] == []
            name = context["inputs"]["conversation_id"]
            rendezvous.wait(timeout=5)
            tools_by_name = _tool_map(tools)
            result = _invoke_json(tools_by_name["retrieve"], _retrieval_args(name, 1))
            video_chunk_id = result["items"][0]["video_chunk_id"]
            _read_all(tools_by_name["read_trieve_item"], video_chunk_id)
            tools_by_name["update_draft_item"].invoke({
                "video_chunk_id": video_chunk_id,
                "start_ms": 1100,
                "end_ms": 1900,
                "claim": f"Evidence for {video_chunk_id}",
            })
            return result_type.model_validate({
                "plan_context": name, "message": name,
            })

    class Approve:
        model_name = "approve"

        def run_structured(self, prompt, *, tools, result_type, **_kwargs):
            tools_by_name = _tool_map(tools)
            catalog = _invoke_json(tools_by_name["get_draft_catelog"], {})
            for item in catalog["items"]:
                _read_all(tools_by_name["read_draft_item"], item["video_chunk_id"])
            return result_type.model_validate({"action": "complete", "advice": "ok"})

    class EchoChat:
        model_name = "echo"

        def run_agent_loop(
            self, prompt, *, tools, max_model_calls, handle_text,
            require_tool_call=False,
            request_timeout_seconds=None, check_control=None, max_input_chars=None,
        ):
            context = json.loads(prompt.rsplit("\n\n", 1)[1])
            chunk_ids = context["approved_turn"]["video_chunk_ids"]
            handle_text(context["approved_turn"]["plan_context"])
            for chunk_id in chunk_ids:
                _tool_map(tools)["update_video_item"].invoke({
                    "video_chunk_id": chunk_id,
                    "claim": f"Evidence for {chunk_id}",
                })

    def search(
        query,
        *,
        workspace_id,
        series_ids,
        top_k,
        transcript_weight,
        text_weight,
        vision_weight,
    ):
        assert workspace_id == f"workspace-{query}"
        assert series_ids == [f"series-{query}"]
        assert (transcript_weight, text_weight, vision_weight) == (0.0, 0.5, 0.5)
        hit = _hit(f"chunk-{query}", workspace=workspace_id, series=series_ids[0])
        return VideoChunkSearchResult(query=query, series_ids=series_ids,
            agent_reply=AgentReply(message=query, result_count=1), hits=[hit])

    runtime = AgentRuntime(ScopedQuery(), Approve(), EchoChat(), search)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {
            name: executor.submit(runtime.kick,
                _inputs(name, workspace=f"workspace-{name}", series=f"series-{name}"),
                emit=events[name].append)
            for name in ("a", "b")
        }
        results = {name: future.result(timeout=5) for name, future in futures.items()}
    for name in ("a", "b"):
        assert results[name].agent_reply.message == name
        assert results[name].hits[0].evidence.video_chunk_id == f"chunk-{name}"
        assert all(event["conversationId"] == name for event in events[name])
        evidence = next(event for event in events[name] if event["type"] == "answer.evidence")
        assert evidence["searchResult"]["hits"][0]["evidence"]["workspaceId"] == f"workspace-{name}"


class DisjointRetrieval(Retrieval):
    def search(self, query, **options):
        self.hits = [_hit(f"chunk-{len(self.calls) + 1}")]
        return super().search(query, **options)


@pytest.mark.parametrize("requested,maximum", [(1, 8), (8, 1)])
def test_multiple_searches_can_select_more_unique_hits_than_one_search(requested, maximum):
    runtime, query, evaluator, chat, retrieval = _runtime(
        query=Query(searches_per_round=2, selected=("chunk-1", "chunk-2")),
        retrieval=DisjointRetrieval(), maximum_top_k=maximum,
    )
    inputs = _inputs()
    inputs["requested_top_k"] = requested
    result = runtime.kick(inputs)
    assert len(retrieval.calls) == 2
    assert [call[3] for call in retrieval.calls] == [1, 1]
    assert "selection_limit" not in query.contexts[0]
    assert len(evaluator.contexts) == len(chat.contexts) == 1
    assert result.agent_reply.result_count == 2
    assert [hit.evidence.video_chunk_id for hit in result.hits] == ["chunk-1", "chunk-2"]


def test_multiple_searches_may_choose_one_valid_hit_within_budget():
    runtime, _, evaluator, _, _ = _runtime(
        query=Query(searches_per_round=2, selected=("chunk-2",)),
        retrieval=DisjointRetrieval(), maximum_top_k=1,
    )
    result = runtime.kick(_inputs())
    assert result.agent_reply.result_count == 1
    assert result.hits[0].evidence.video_chunk_id == "chunk-2"
    assert len(evaluator.contexts[0]["retrieval_calls"]) == 2
    assert result.query == "query-1-1"
