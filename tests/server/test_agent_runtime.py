from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier, Event

import pytest

from gateway.agent_model import ModelInputTooLargeError
from server.agent import AgentContextBudgetError, AgentModelInvocationError, AgentRuntime
from server.agent.context import project_search_result
from server.agent.state import (
    ExecutionControl, ModelContextBudget, RetrievalCall, RunInputs, context_json,
)
from server.agent.tools import build_retrieval_tool
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
        parse_provenance=None, parsed_at=datetime(2026, 9, 15),
    )
    return VideoChunkSearchHit(
        candidate=VideoChunkCandidate(video_chunk_id=chunk_id, fused_rank=1, fused_score=1),
        selection_reason=SelectionReason(summary="matched action", matched_evidence=[], limitations=[]),
        evidence=evidence,
    )


class Retrieval:
    def __init__(self, hits=None):
        self.hits = hits if hits is not None else [_hit(), _hit("chunk-2")]
        self.calls = []

    def search(self, query, *, workspace_id, series_ids, top_k):
        self.calls.append((query, workspace_id, series_ids, top_k))
        hits = [hit.model_copy(deep=True) for hit in self.hits[:top_k]]
        return VideoChunkSearchResult(
            query=query, series_ids=series_ids,
            agent_reply=AgentReply(message="retrieval", result_count=len(hits)), hits=hits,
        )


class Query:
    model_name = "query-test"
    provider_name = "fake"

    def __init__(self, *, search_rounds=(1,), selected=("chunk-1",), searches_per_round=1):
        self.search_rounds = search_rounds
        self.selected = selected
        self.searches_per_round = searches_per_round
        self.contexts = []
        self.tool_results = []
        self.budgets = []

    def run_query(self, prompt, *, tools, result_type, max_model_calls, check_control=None):
        context = json.loads(prompt.rsplit("\n\n", 1)[1])
        self.contexts.append(context)
        self.budgets.append(max_model_calls)
        number = context["turn_number"]
        if number in self.search_rounds:
            for index in range(self.searches_per_round):
                self.tool_results.append(
                    tools[0].invoke({"query": f"query-{number}-{index}", "top_k": 8})
                )
        return result_type.model_validate({
            "plan_context": f"plan-{number}", "message": f"draft-{number}",
            "selected_video_chunk_ids": list(self.selected),
        })


class Evaluator:
    model_name = "evaluator-test"
    provider_name = "fake"

    def __init__(self, approve_at=1):
        self.approve_at = approve_at
        self.contexts = []

    def generate(self, inputs, *, schema_name, result_type):
        assert schema_name == "anivag_answer_evaluation"
        self.contexts.append(json.loads(inputs[0].text.rsplit("\n\n", 1)[1]))
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

    def run_agent_loop(
        self, prompt, *, function_name, function_description, result_type,
        max_model_calls, handle_text, handle_tool_call,
        request_timeout_seconds=None, check_control=None,
    ):
        assert function_name == "submit_video_item"
        assert function_description
        context = json.loads(prompt.rsplit("\n\n", 1)[1])
        self.contexts.append(context)
        assert request_timeout_seconds is not None
        assert request_timeout_seconds > 0
        self.request_timeouts.append(request_timeout_seconds)
        assert max_model_calls > 0
        self.model_call_budgets.append(max_model_calls)
        assert check_control is not None
        chunk_ids = [
            hit["candidate"]["videoChunkId"]
            for hit in context["approved_turn"]["selected_evidence"]["hits"]
        ]
        text_blocks = (
            ["## Final answer", "Grounded answer."]
            if chunk_ids else ["## Final answer\n\nGrounded answer."]
        )
        check_control()
        self.text_blocks.append(text_blocks[0])
        handle_text(text_blocks[0])
        for chunk_id in chunk_ids:
            check_control()
            item = result_type.model_validate({
                "video_chunk_id": chunk_id,
                "claim": f"Grounded evidence for {chunk_id}.",
            })
            self.video_items.append(item)
            self.tool_results.append(json.loads(handle_tool_call(item)))
        if len(text_blocks) == 2:
            check_control()
            self.text_blocks.append(text_blocks[1])
            handle_text(text_blocks[1])


def _runtime(query=None, evaluator=None, chat=None, retrieval=None, **kwargs):
    query, evaluator, chat, retrieval = (
        query or Query(), evaluator or Evaluator(), chat or Chat(), retrieval or Retrieval()
    )
    return AgentRuntime(query, evaluator, chat, retrieval.search, **kwargs), query, evaluator, chat, retrieval


@pytest.mark.parametrize("approve_at", [1, 2, 3])
def test_approval_immediately_stops_loop_and_streams_once(approve_at):
    runtime, query, evaluator, chat, retrieval = _runtime(evaluator=Evaluator(approve_at))
    events = []
    result = runtime.kick(_inputs(), emit=events.append)
    assert len(query.contexts) == len(evaluator.contexts) == approve_at
    assert len(chat.contexts) == len(retrieval.calls) == 1
    assert 0 < chat.request_timeouts[0] <= 180
    assert chat.model_call_budgets == [6]
    assert result.agent_reply.message == "## Final answer\n\nGrounded answer."
    assert result.agent_reply.result_count == 1
    assert result.hits[0].evidence.start_ms == 1000
    assert result.hits[0].evidence.end_ms == 2000
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
    assert chat.tool_results == [{"accepted": True, "video_chunk_id": "chunk-1"}]
    assert all(event["conversationId"] == "conversation" for event in events)
    assert all(event["messageId"] == "conversation-answer" for event in events)
    assert runtime.model_name == "chat-test"


def test_third_rejection_is_failure_and_never_starts_final_chat():
    runtime, query, evaluator, chat, _retrieval = _runtime(evaluator=Evaluator(99))
    events = []
    with pytest.raises(AgentModelInvocationError, match="after 3 turns"):
        runtime.kick(_inputs(), emit=events.append)
    assert len(query.contexts) == len(evaluator.contexts) == 3
    assert chat.contexts == []
    assert [event["phase"] for event in events] == ["RETRIEVING", "EVALUATING"] * 3


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
        assert record["query_draft"]["message"] == f"draft-{number}"
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
    assert len(retrieval.calls) == 4
    assert len(evaluator.contexts[1]["retrieval_calls"]) == 2
    assert evaluator.contexts[1]["retrieval_calls"][0]["video_chunk_ids"] == [
        "chunk-1", "chunk-2",
    ]
    assert evaluator.contexts[2]["retrieval_calls"] == []
    assert evaluator.contexts[2]["evidence_source_turns"] == {"chunk-1": 2}
    assert len(evaluator.contexts[2]["available_evidence"]["hits"]) == 2
    assert "selected_output" not in evaluator.contexts[2]
    approved = chat.contexts[0]["approved_turn"]
    assert approved["number"] == 3
    assert approved["draft"]["message"] == "draft-3"
    assert len(approved["selected_evidence"]["hits"]) == 1
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


def test_unknown_selected_id_fails_before_evaluator_or_chat():
    runtime, _query, evaluator, chat, _retrieval = _runtime(query=Query(selected=("invented",)))
    with pytest.raises(AgentModelInvocationError, match="outside retrieved evidence"):
        runtime.kick(_inputs())
    assert evaluator.contexts == chat.contexts == []


def test_mismatched_candidate_and_evidence_ids_cannot_reach_evaluator():
    class MismatchedRetrieval(Retrieval):
        def search(self, query, *, workspace_id, series_ids, top_k):
            result = super().search(query, workspace_id=workspace_id, series_ids=series_ids, top_k=top_k)
            result.hits[0].candidate.video_chunk_id = "another-id"
            return result

    runtime, _query, evaluator, chat, _retrieval = _runtime(retrieval=MismatchedRetrieval())
    with pytest.raises(AgentModelInvocationError):
        runtime.kick(_inputs())
    assert evaluator.contexts == chat.contexts == []


def test_selection_order_and_duplicate_ids_keep_exact_real_evidence():
    runtime, _query, _evaluator, _chat, retrieval = _runtime(
        query=Query(selected=("chunk-2", "chunk-1", "chunk-2")),
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
    tool_evidence = query.tool_results[0]["hits"][0]["evidence"]
    assert tool_evidence["videoCompact"] == "cup action"
    assert "importantExtraData" not in tool_evidence
    assert "characterBible" not in tool_evidence
    assert "originalSourceUri" not in tool_evidence
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
    assert evaluator_context.count(evidence_marker) == 1
    assert chat_context.count(evidence_marker) == 1
    assert query_context.count(matched_marker) == 1
    assert evaluator_context.count(matched_marker) == 1
    assert chat_context.count(matched_marker) == 1


def test_multi_turn_context_keeps_one_canonical_evidence_copy():
    marker = "canonical-evidence-copy-marker"
    retrieval = Retrieval()
    retrieval.hits[0].evidence.baseline_visual_caption = marker
    runtime, query, evaluator, _chat, _retrieval = _runtime(
        query=Query(search_rounds=(1,)), evaluator=Evaluator(3), retrieval=retrieval,
    )

    runtime.kick(_inputs())

    assert json.dumps(query.contexts[2], ensure_ascii=False).count(marker) == 1
    assert json.dumps(evaluator.contexts[2], ensure_ascii=False).count(marker) == 1
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
        def search(self, query, *, workspace_id, series_ids, top_k):
            result = super().search(query, workspace_id=workspace_id, series_ids=series_ids, top_k=top_k)
            mutation(result)
            return result

    runtime, _query, evaluator, chat, _retrieval = _runtime(retrieval=CorruptedRetrieval())
    with pytest.raises(AgentModelInvocationError, match=expected):
        runtime.kick(_inputs())
    assert evaluator.contexts == chat.contexts == []


def test_duplicate_chunk_in_one_retrieval_is_rejected():
    class DuplicateRetrieval(Retrieval):
        def search(self, query, *, workspace_id, series_ids, top_k):
            result = super().search(query, workspace_id=workspace_id, series_ids=series_ids, top_k=top_k)
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
        def run_query(self, prompt, *, tools, result_type, max_model_calls, check_control=None):
            assert set(tools[0].args_schema.model_fields) == {"query", "top_k"}
            return super().run_query(prompt, tools=tools, result_type=result_type, max_model_calls=max_model_calls)

    runtime, query, _evaluator, _chat, retrieval = _runtime(
        query=InspectQuery(), maximum_top_k=2, max_model_calls_per_turn=5,
    )
    runtime.kick(_inputs())
    assert retrieval.calls == [("query-1-0", "workspace", ["series"], 2)]
    assert query.budgets == [5]


def test_tool_budget_stops_before_an_extra_search_or_evaluation():
    runtime, _query, evaluator, chat, retrieval = _runtime(
        query=Query(searches_per_round=2), max_tool_calls_per_turn=1,
    )
    with pytest.raises(AgentModelInvocationError, match="tool-call budget"):
        runtime.kick(_inputs())
    assert len(retrieval.calls) == 1
    assert evaluator.contexts == chat.contexts == []


def test_prompt_character_budget_stops_before_the_query_provider():
    runtime, query, evaluator, chat, retrieval = _runtime(max_prompt_chars=100)

    with pytest.raises(AgentModelInvocationError, match="context character budget"):
        runtime.kick(_inputs())

    assert query.contexts == evaluator.contexts == chat.contexts == retrieval.calls == []


def test_complete_query_message_budget_error_keeps_its_specific_semantics():
    class OversizedQuery(Query):
        def run_query(self, *args, **kwargs):
            raise ModelInputTooLargeError("complete Query messages exceeded the limit")

    runtime, _query, evaluator, chat, retrieval = _runtime(query=OversizedQuery())

    with pytest.raises(AgentContextBudgetError, match="complete Query messages"):
        runtime.kick(_inputs())

    assert evaluator.contexts == chat.contexts == retrieval.calls == []


def test_tool_context_uses_the_same_cumulative_character_budget():
    hit = _hit()
    retrieval = Retrieval([hit])
    preview = retrieval.search(
        "same-query", workspace_id="workspace", series_ids=["series"], top_k=1,
    )
    retrieval.calls.clear()
    result_chars = len(context_json(project_search_result(preview)))
    context_budget = ModelContextBudget(result_chars * 2 - 1)
    calls: list[RetrievalCall] = []
    tool = build_retrieval_tool(
        retrieval.search, inputs=_inputs(), control=ExecutionControl(float("inf")),
        calls=calls, maximum_top_k=1, max_tool_calls=2,
        context_budget=context_budget,
    )

    tool.invoke({"query": "same-query", "top_k": 1})
    with pytest.raises(AgentModelInvocationError, match="retrieval tool result"):
        tool.invoke({"query": "same-query", "top_k": 1})

    assert len(retrieval.calls) == 2
    assert len(calls) == 1


def test_parallel_tool_calls_share_one_turn_budget_and_keep_actual_records():
    class ParallelQuery(Query):
        def run_query(self, prompt, *, tools, result_type, max_model_calls, check_control=None):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(tools[0].invoke, {"query": f"parallel-{i}", "top_k": 2})
                           for i in range(2)]
                for future in futures:
                    future.result(timeout=5)
            return result_type.model_validate({
                "plan_context": "parallel", "message": "draft", "selected_video_chunk_ids": ["chunk-1"],
            })

    runtime, _query, evaluator, chat, retrieval = _runtime(
        query=ParallelQuery(), max_tool_calls_per_turn=1,
    )
    with pytest.raises(AgentModelInvocationError, match="tool-call budget"):
        runtime.kick(_inputs())
    assert len(retrieval.calls) == 1
    assert evaluator.contexts == chat.contexts == []


def test_current_turn_cannot_change_the_scope_even_to_a_subset():
    inputs = _inputs()
    inputs["series_ids"] = ["series", "second-series"]

    class NarrowRetrieval(Retrieval):
        def search(self, query, *, workspace_id, series_ids, top_k):
            return super().search(query, workspace_id=workspace_id, series_ids=["series"], top_k=top_k)

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
            self, prompt, *, result_type, handle_tool_call, **kwargs,
        ):
            handle_tool_call(result_type.model_validate({
                "video_chunk_id": "invented-chunk",
                "claim": "Invented evidence.",
            }))

    runtime, *_ = _runtime(chat=InventedEvidenceChat())
    events = []

    with pytest.raises(
        AgentModelInvocationError,
        match="outside approved evidence",
    ):
        runtime.kick(_inputs(), emit=events.append)

    assert [event for event in events if event["type"] != "answer.progress"] == []


def test_chat_cannot_submit_the_same_approved_id_twice() -> None:
    class DuplicateEvidenceChat(Chat):
        def run_agent_loop(
            self, prompt, *, result_type, handle_text, handle_tool_call, **kwargs,
        ):
            handle_text("Grounded answer.")
            item = result_type.model_validate({
                "video_chunk_id": "chunk-1",
                "claim": "Grounded evidence.",
            })
            handle_tool_call(item)
            handle_tool_call(item)

    runtime, *_ = _runtime(chat=DuplicateEvidenceChat())
    events = []

    with pytest.raises(AgentModelInvocationError, match="more than once"):
        runtime.kick(_inputs(), emit=events.append)

    assert [event for event in events if event["type"] != "answer.progress"] == []


def test_chat_requires_nonempty_markdown_even_when_video_was_submitted() -> None:
    class VideoOnlyChat(Chat):
        def run_agent_loop(
            self, prompt, *, result_type, handle_tool_call, **kwargs,
        ):
            handle_tool_call(result_type.model_validate({
                "video_chunk_id": "chunk-1",
                "claim": "Grounded evidence.",
            }))

    runtime, *_ = _runtime(chat=VideoOnlyChat())
    events = []

    with pytest.raises(AgentModelInvocationError, match="Chat Agent model"):
        runtime.kick(_inputs(), emit=events.append)

    assert [event for event in events if event["type"] != "answer.progress"] == []


@pytest.mark.parametrize("submitted_ids", [("chunk-1",), ("chunk-1", "chunk-2")])
def test_chat_may_choose_an_approved_subset_in_any_order(submitted_ids) -> None:
    class ChoosingChat(Chat):
        def run_agent_loop(
            self, prompt, *, result_type, handle_text, handle_tool_call, **kwargs,
        ):
            handle_text("Before video.")
            for chunk_id in submitted_ids:
                handle_tool_call(result_type.model_validate({
                    "video_chunk_id": chunk_id,
                    "claim": f"Chosen {chunk_id}.",
                }))
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
    monkeypatch.setattr("server.agent.runtime.monotonic", lambda: clock[0])
    monkeypatch.setattr("server.agent.state.monotonic", lambda: clock[0])

    class SlowEvaluator(Evaluator):
        def generate(self, inputs, *, schema_name, result_type):
            result = super().generate(inputs, schema_name=schema_name, result_type=result_type)
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

        def run_query(self, prompt, *, tools, result_type, max_model_calls, check_control=None):
            context = json.loads(prompt.rsplit("\n\n", 1)[1])
            assert context["turns"] == []
            name = context["inputs"]["conversation_id"]
            rendezvous.wait(timeout=5)
            result = tools[0].invoke({"query": name, "top_k": 1})
            return result_type.model_validate({
                "plan_context": name, "message": name,
                "selected_video_chunk_ids": [result["hits"][0]["candidate"]["videoChunkId"]],
            })

    class Approve:
        model_name = "approve"

        def generate(self, inputs, *, schema_name, result_type):
            return result_type.model_validate({"action": "complete", "advice": "ok"})

    class EchoChat:
        model_name = "echo"

        def run_agent_loop(
            self, prompt, *, function_name, function_description, result_type,
            max_model_calls, handle_text, handle_tool_call,
            request_timeout_seconds=None, check_control=None,
        ):
            context = json.loads(prompt.rsplit("\n\n", 1)[1])
            chunk_ids = [
                hit["candidate"]["videoChunkId"]
                for hit in context["approved_turn"]["selected_evidence"]["hits"]
            ]
            handle_text(context["approved_turn"]["plan_context"])
            for chunk_id in chunk_ids:
                handle_tool_call(result_type.model_validate({
                    "video_chunk_id": chunk_id,
                    "claim": f"Evidence for {chunk_id}",
                }))

    def search(query, *, workspace_id, series_ids, top_k):
        assert workspace_id == f"workspace-{query}"
        assert series_ids == [f"series-{query}"]
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
    def search(self, query, *, workspace_id, series_ids, top_k):
        self.hits = [_hit(f"chunk-{len(self.calls) + 1}")]
        return super().search(query, workspace_id=workspace_id, series_ids=series_ids, top_k=top_k)


@pytest.mark.parametrize("requested,maximum", [(1, 8), (8, 1)])
def test_multiple_searches_cannot_expand_final_selected_hit_budget(requested, maximum):
    runtime, query, evaluator, chat, retrieval = _runtime(
        query=Query(searches_per_round=2, selected=("chunk-1", "chunk-2")),
        retrieval=DisjointRetrieval(), maximum_top_k=maximum,
    )
    inputs = _inputs()
    inputs["requested_top_k"] = requested
    with pytest.raises(AgentModelInvocationError, match="final selected-hit budget"):
        runtime.kick(inputs)
    assert len(retrieval.calls) == 2
    assert query.contexts[0]["selection_limit"] == 1
    assert evaluator.contexts == chat.contexts == []


def test_multiple_searches_may_choose_one_valid_hit_within_budget():
    runtime, _, evaluator, _, _ = _runtime(
        query=Query(searches_per_round=2, selected=("chunk-2", "chunk-2")),
        retrieval=DisjointRetrieval(), maximum_top_k=1,
    )
    result = runtime.kick(_inputs())
    assert result.agent_reply.result_count == 1
    assert result.hits[0].evidence.video_chunk_id == "chunk-2"
    assert len(evaluator.contexts[0]["available_evidence"]["hits"]) == 2
    assert result.query == "query-1-1"
