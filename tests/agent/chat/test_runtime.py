from __future__ import annotations


import json




import subprocess


import sys


from concurrent.futures import ThreadPoolExecutor


from threading import Barrier, Event


from time import monotonic




import pytest


from engine.rag.contracts import (
    AgentReply,
    MatchedEvidence,
    SelectionReason,
    VideoChunkCandidate,
    VideoChunkEvidence,
    VideoChunkSearchHit,
    VideoChunkSearchResult,
)


from engine.rag.retrieval.contracts import EventIndexNotReadyError, EventSearchResult, FusedEventHit


from engine.rag.retrieval.events import EVENT_RETRIEVAL_STRATEGY_VERSION


from gateway.agent_model.errors import (
    ModelFailureReason, ModelInputTooLargeError, QueryModelIncompleteError,
)


from agent.chat.common.errors import AgentExecutionTimeoutError, AgentModelInvocationError


from agent.chat.common.state import RunInputs




from agent.chat.video_search.runtime import AgentRuntime


from agent.chat.video_search.errors import (
    VideoSearchFailure, VideoSearchFailureCode, VideoSearchFailurePhase,
)


from agent.chat.video_search.state import (
    LexicalRetrievalChannelWeights, QueryDraft, RetrievalChannelWeights,
    RetrievalTraceEntry,
)


from agent.chat.video_search.output import build_retrieval_trace, merge_retrieval_calls, retrieval_hit
from agent.chat.video_search.state import QueryReport, RetrievalCall
from agent.chat.video_search.tools.shared import _ToolCallBudget


@pytest.mark.parametrize("statement", [
    "from agent.chat.common.state import AnswerPhase",
    "from server.repository.agent import AgentRepository",
])
def test_chat_primitives_and_repository_import_without_runtime(statement):
    subprocess.run([sys.executable, "-B", "-c", statement + "\n" + """
import sys
assert 'agent.chat.video_search.runtime' not in sys.modules
assert not any(name == 'langgraph' or name.startswith('langgraph.') for name in sys.modules)
"""], check=True)


def _inputs(name="conversation", *, workspace="workspace", series="series") -> RunInputs:
    return {
        "conversation_id": name, "assistant_message_id": f"{name}-answer",
        "tenant_id": workspace, "series_ids": [series], "series_context": [],
        "user_message": f"{name}: find the broken cup", "history": [], "requested_top_k": 8,
    }


def _retrieval_args(
    query: str,
    top_k: int,
    *,
    lexical: float = 0.0,
    semantic: float = 0.5,
    visual: float = 0.5,
) -> dict:
    return {
        "query": query,
        "top_k": top_k,
        "channel_weights": {
            "lexical": lexical,
            "semantic": semantic,
            "visual": visual,
        },
    }


def _tool_map(tools):
    return {tool.name: tool for tool in tools}


def _invoke_json(tool, arguments):
    return json.loads(tool.invoke(arguments))


def _read_all(tool, video_chunk_id):
    content, cursor = [], None
    while True:
        page = _invoke_json(tool, {"video_chunk_id": video_chunk_id, "cursor": cursor})
        content.extend(item["text"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            return "".join(content)


def _hit(chunk_id="chunk-1", *, workspace="workspace", series="series"):
    evidence = VideoChunkEvidence(
        series_id=series, tenant_id=workspace, series_name="series name",
        series_description=None, series_status="READY", important_extra_data=None,
        character_bible=[], video_id=f"video-{chunk_id}", episode_order=1,
        original_filename="episode.mp4", original_source_uri="episode.mp4",
        inference_proxy_uri=None, content_hash="hash", duration_seconds=100,
        upload_status="COMPLETED", parse_status="COMPLETED", video_chunk_id=chunk_id,
        sequence_no=0, start_seconds=10, end_seconds=20, analysis_window_start_seconds=0,
        analysis_window_end_seconds=30, chunk_media_ref="chunk.mp4",
        analysis_window_media_ref="window.mp4", transcript=[], baseline_visual_caption=None,
        keyframe_uris=[], all_time_events=[{"event_type": "ACTION", "start_seconds": 12, "end_seconds": 18, "description": "cup action"}], all_time_event_text="cup action",
        retrieval_text="cup action", video_compact="cup action", context_before_compact="",
        context_after_compact="", window_summary="cup action", model_provider="ark",
        model_name="parser", pipeline_version="video-parse-v2", is_fallback=False,
        parse_provenance=None, parsed_at=1_789_724_800,
    )
    return VideoChunkSearchHit(
        candidate=VideoChunkCandidate(video_chunk_id=chunk_id, fused_rank=1, fused_score=1),
        selection_reason=SelectionReason(summary="matched action", matched_evidence=[], limitations=[]),
        evidence=evidence,
    )


def test_final_range_requires_explicit_sources_when_restoring_cross_chunk_history():
    from agent.chat.video_search.output import video_range_sources

    left, right = _hit("chunk-1"), _hit("chunk-2")
    right.evidence.video_id = left.evidence.video_id
    right.evidence.sequence_no = 1
    right.evidence.start_seconds, right.evidence.end_seconds = 20, 30
    evidence = {hit.evidence.video_chunk_id: hit.evidence.model_dump(by_alias=True)
                for hit in (left, right)}
    with pytest.raises(ValueError, match="not covered"):
        video_range_sources(evidence, "chunk-1", 12, 28)
    assert video_range_sources(evidence, "chunk-1", 12, 28, allow_adjacent=True) == ["chunk-1", "chunk-2"]
    assert video_range_sources(
        evidence, "chunk-2", 12, 28, source_video_chunk_ids=["chunk-2", "chunk-1"],
    ) == ["chunk-1", "chunk-2"]


@pytest.mark.parametrize("field,value", [
    ("startSeconds", "bad"), ("startSeconds", None), ("endSeconds", True),
    ("tenantId", "other"), ("seriesId", "other"), ("videoId", "other"),
    ("videoChunkId", "mismatched"), ("sequenceNo", 2), ("durationSeconds", 25),
])
def test_final_range_rejects_corrupt_or_unrelated_saved_sources(field, value):
    from agent.chat.video_search.output import video_range_sources

    left, right = _hit("chunk-1"), _hit("chunk-2")
    right.evidence.video_id = left.evidence.video_id
    right.evidence.sequence_no = 1
    right.evidence.start_seconds, right.evidence.end_seconds = 20, 30
    evidence = {hit.evidence.video_chunk_id: hit.evidence.model_dump(by_alias=True)
                for hit in (left, right)}
    evidence["chunk-2"][field] = value
    with pytest.raises(ValueError):
        video_range_sources(evidence, "chunk-1", 12, 28, source_video_chunk_ids=["chunk-1", "chunk-2"])


@pytest.mark.parametrize("sources", [[], ["missing"], ["chunk-1", "chunk-1"], "chunk-1"])
def test_final_range_rejects_invalid_explicit_source_ids(sources):
    from agent.chat.video_search.output import video_range_sources

    evidence = {"chunk-1": _hit().evidence.model_dump(by_alias=True)}
    with pytest.raises(ValueError):
        video_range_sources(evidence, "chunk-1", 12, 18, source_video_chunk_ids=sources)


def _selected_range(chunk_id: str, *, start_seconds: int = 11, end_seconds: int = 19) -> dict:
    return {
        "video_chunk_id": chunk_id,
        "start_seconds": start_seconds,
        "end_seconds": end_seconds,
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
        tenant_id,
        series_ids,
        top_k,
        lexical_weight,
        text_weight,
        vision_weight,
    ):
        self.calls.append((query, tenant_id, series_ids, top_k))
        self.weights.append((lexical_weight, text_weight, vision_weight))
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
        top_k=8,
    ):
        self.search_rounds = search_rounds
        self.selected = selected
        self.selected_ranges = selected_ranges
        self.searches_per_round = searches_per_round
        self.channel_weights = channel_weights
        self.top_k = top_k
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
        number = context.get("turn_number", 1)
        tools_by_name = _tool_map(tools)
        if number in self.search_rounds:
            for index in range(self.searches_per_round):
                self.tool_results.append(
                    _invoke_json(tools_by_name["retrieve"], _retrieval_args(
                        f"query-{number}-{index}",
                        self.top_k,
                        lexical=self.channel_weights[0],
                        semantic=self.channel_weights[1],
                        visual=self.channel_weights[2],
                    ))
                )
        return result_type.model_validate({
            "plan_context": f"plan-{number}", "message": f"draft-{number}",
        })


class Evaluator:
    def __init__(self, *args, **kwargs):
        self.contexts = []


class Chat:
    def __init__(self):
        self.contexts = []


def _runtime(query=None, evaluator=None, chat=None, retrieval=None, **kwargs):
    query, evaluator, chat, retrieval = (
        query or Query(), evaluator or Evaluator(), chat or Chat(), retrieval or Retrieval()
    )
    kwargs.setdefault("retrieval_channel", "lexical")
    return AgentRuntime(query, retrieval.search, **kwargs), query, evaluator, chat, retrieval


@pytest.mark.parametrize("start_seconds,end_seconds", [
    (True, 19),
    ("11", 19),
    (11.5, 19),
    (11, False),
    (11, "19"),
    (11, 19.5),
    (15, 15),
    (16, 15),
])
def test_selected_range_rejects_non_integer_or_non_increasing_time(
    start_seconds, end_seconds,
):
    with pytest.raises(ValueError):
        QueryDraft.model_validate({
            "plan_context": "plan",
            "message": "draft",
            "selected_video_ranges": [_selected_range(
                "chunk-1", start_seconds=start_seconds, end_seconds=end_seconds,
            )],
        })


def test_execution_control_without_deadline_keeps_cancellation_checks():
    from agent.chat.common.state import ExecutionControl

    cancellation = Event()
    control = ExecutionControl(None, cancellation)
    control.check()
    assert control.remaining_seconds() is None

    cancellation.set()

    with pytest.raises(AgentModelInvocationError, match="cancelled"):
        control.check()
    with pytest.raises(AgentModelInvocationError, match="cancelled"):
        control.remaining_seconds()


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


@pytest.mark.parametrize("requested", [
    {"transcript": 0.2, "semantic": 0.5, "visual": 0.3},
    {"semantic": 0.5, "visual": 0.5},
])
def test_historical_retrieval_trace_keeps_asr_weights(requested):
    trace = RetrievalTraceEntry.model_validate({
        "query": "original phrase",
        "topK": 5,
        "strategyVersion": "query-adaptive-three-channel-rrf-v2",
        "requestedWeights": requested,
        "effectiveWeights": requested,
        "resultCount": 0,
        "videoChunkIds": [],
    })

    encoded = trace.model_dump(mode="json", by_alias=True)
    assert encoded["requestedWeights"]["transcript"] == requested.get("transcript", 0.0)
    assert "lexical" not in encoded["requestedWeights"]
    assert encoded["effectiveWeights"] == encoded["requestedWeights"]


def test_lexical_retrieval_trace_is_distinct_from_asr_history():
    weights = {"lexical": 0.6, "semantic": 0.3, "visual": 0.1}
    trace = RetrievalTraceEntry.model_validate({
        "query": "scene details",
        "topK": 5,
        "strategyVersion": "lexical-three-channel-rrf-v3",
        "requestedWeights": weights,
        "effectiveWeights": weights,
        "resultCount": 1,
        "videoChunkIds": ["chunk-1"],
    })

    assert isinstance(trace.requested_weights, LexicalRetrievalChannelWeights)
    encoded = trace.model_dump(mode="json", by_alias=True)
    assert encoded["requestedWeights"] == weights
    assert encoded["effectiveWeights"] == weights
    assert "transcript" not in encoded["requestedWeights"]

    with pytest.raises(ValueError, match="same channels"):
        RetrievalTraceEntry.model_validate({
            **encoded,
            "effectiveWeights": {"transcript": 0.6, "semantic": 0.3, "visual": 0.1},
        })


@pytest.mark.parametrize("weights", [
    {"lexical": True, "semantic": 0.5, "visual": 0.5},
    {"lexical": 0.0, "semantic": 0.0, "visual": 0.0},
    {"lexical": 0.5, "semantic": 0.5, "visual": 0.0, "transcript": 0.0},
])
def test_lexical_trace_weights_reject_invalid_values(weights):
    with pytest.raises(ValueError):
        LexicalRetrievalChannelWeights.model_validate(weights)


def test_query_report_contains_all_real_evidence_without_mutable_drafts():
    runtime, query, evaluator, chat, retrieval = _runtime()
    events = []

    report = runtime.kick(_inputs(), emit=events.append)

    assert isinstance(report, QueryReport)
    assert report.completion.message == "draft-1"
    assert [hit.evidence.video_chunk_id for hit in report.retrieval_result.hits] == ["chunk-1", "chunk-2"]
    assert len(query.contexts) == len(retrieval.calls) == len(report.retrieval_calls) == 1
    assert evaluator.contexts == chat.contexts == []
    assert report.frame_receipts == ()
    assert [
        hit.candidate.video_chunk_id for hit in report.retrieval_calls[0].result.hits
    ] == ["chunk-1", "chunk-2"]
    assert [event["type"] for event in events] == ["answer.progress"]
    assert runtime.model_name == query.model_name


def test_query_report_keeps_deep_canonical_snapshots():
    runtime, *_ = _runtime(query=Query(selected=("chunk-2", "chunk-1")))
    report = runtime.kick(_inputs())
    original = retrieval_hit(report.retrieval_calls, "chunk-1").evidence.window_summary
    report.retrieval_result.hits[0].evidence.window_summary = "changed report copy"
    assert retrieval_hit(report.retrieval_calls, "chunk-1").evidence.window_summary == original


def test_empty_real_retrieval_returns_an_uncertain_report_without_video_items():
    runtime, *_ = _runtime(query=Query(selected=()), retrieval=Retrieval([]))
    report = runtime.kick(_inputs())
    assert report.retrieval_result.hits == []
    assert len(report.retrieval_calls) == 1


def test_query_cannot_return_a_report_without_real_retrieval():
    runtime, *_ = _runtime(query=Query(search_rounds=(), selected=()))
    with pytest.raises(VideoSearchFailure) as captured:
        runtime.kick(_inputs())
    assert captured.value.code == VideoSearchFailureCode.REQUIRED_TOOL_MISSING
    assert captured.value.phase == VideoSearchFailurePhase.QUERY
    assert captured.value.retryable


@pytest.mark.parametrize("mutation", [
    lambda hit: setattr(hit.evidence, "tenant_id", "other"),
    lambda hit: setattr(hit.evidence, "series_id", "other"),
    lambda hit: setattr(hit.evidence, "video_chunk_id", "mismatch"),
    lambda hit: setattr(hit.evidence, "end_seconds", 200),
])
def test_invalid_retrieved_evidence_cannot_reach_a_query_report(mutation):
    class CorruptRetrieval(Retrieval):
        def search(self, query, **kwargs):
            result = super().search(query, **kwargs)
            mutation(result.hits[0])
            return result

    runtime, *_ = _runtime(retrieval=CorruptRetrieval([_hit()]))
    with pytest.raises(AgentModelInvocationError):
        runtime.kick(_inputs())


def test_query_tool_scope_is_fixed_even_when_result_narrows_it():
    class NarrowRetrieval(Retrieval):
        def search(self, query, **kwargs):
            return super().search(query, **{**kwargs, "series_ids": ["series"]})

    runtime, *_ = _runtime(retrieval=NarrowRetrieval())
    inputs = _inputs()
    inputs["series_ids"] = ["series", "second"]
    with pytest.raises(AgentModelInvocationError, match="changed the fixed Series scope"):
        runtime.kick(inputs)


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_query_rejects_invalid_explicit_model_budget(value):
    runtime, *_ = _runtime()
    with pytest.raises(ValueError, match="max_model_calls_per_turn"):
        runtime.kick(_inputs(), max_model_calls_per_turn=value)


def test_explicit_query_limits_apply_to_one_execution_without_mutating_defaults():
    runtime, query, *_ = _runtime(limits_enabled=True, max_model_calls_per_turn=7)
    runtime.kick(_inputs(), max_model_calls_per_turn=2)
    runtime.kick(_inputs())
    assert query.budgets == [2, 7]


def test_disabled_default_count_limits_keep_explicit_override_available():
    runtime, query, *_ = _runtime(limits_enabled=False, max_model_calls_per_turn=1)
    runtime.kick(_inputs())
    runtime.kick(_inputs(), max_model_calls_per_turn=3)
    assert query.budgets == [None, 3]


def test_shared_parent_tool_budget_is_not_reset_for_each_query():
    runtime, _, _, _, retrieval = _runtime(query=Query(selected=()))
    budget = _ToolCallBudget(1, "Supervisor")
    runtime.kick(_inputs(), tool_budget=budget)
    with pytest.raises(VideoSearchFailure) as captured:
        runtime.kick(_inputs("second"), tool_budget=budget)
    assert captured.value.code == VideoSearchFailureCode.LOOP_LIMIT_EXCEEDED
    assert len(retrieval.calls) == 1


def test_parallel_query_tools_share_one_budget():
    class ParallelQuery(Query):
        def run_structured(self, prompt, *, tools, result_type, **kwargs):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(tools[0].invoke, _retrieval_args(f"q-{i}", 2)) for i in range(2)]
                for future in futures:
                    future.result(timeout=5)
            return result_type(plan_context="parallel", message="report")

    runtime, _, _, _, retrieval = _runtime(query=ParallelQuery(), limits_enabled=True, max_tool_calls_per_turn=1)
    with pytest.raises(VideoSearchFailure) as captured:
        runtime.kick(_inputs())
    assert captured.value.code == VideoSearchFailureCode.LOOP_LIMIT_EXCEEDED
    assert len(retrieval.calls) == 1


@pytest.mark.parametrize("total_timeout_enabled", [True, False])
def test_parent_deadline_is_enforced_before_query(total_timeout_enabled):
    runtime, query, *_ = _runtime(total_timeout_enabled=total_timeout_enabled)
    with pytest.raises(AgentExecutionTimeoutError):
        runtime.kick(_inputs(), deadline=monotonic() - 1)
    assert query.contexts == []


def test_stage_timeout_limits_query_even_without_total_deadline(monkeypatch):
    now = [10.0]
    monkeypatch.setattr("agent.chat.video_search.runtime.monotonic", lambda: now[0])
    monkeypatch.setattr("agent.chat.common.state.monotonic", lambda: now[0])

    class ExpiringQuery(Query):
        def run_structured(self, *args, **kwargs):
            result = super().run_structured(*args, **kwargs)
            now[0] += 11
            return result

    runtime, *_ = _runtime(query=ExpiringQuery(), total_timeout_enabled=False, stage_timeout_seconds=10)
    with pytest.raises(AgentExecutionTimeoutError):
        runtime.kick(_inputs())


def test_cancelled_query_never_returns_report_or_final_answer():
    cancellation = Event()

    class CancellingQuery(Query):
        def run_structured(self, *args, **kwargs):
            result = super().run_structured(*args, **kwargs)
            cancellation.set()
            return result

    runtime, *_ = _runtime(query=CancellingQuery())
    events = []
    with pytest.raises(AgentModelInvocationError, match="cancelled"):
        runtime.kick(_inputs(), cancellation=cancellation, emit=events.append)
    assert all(event["type"] == "answer.progress" for event in events)


def test_cancelled_kick_does_not_start_query():
    cancellation = Event()
    cancellation.set()
    runtime, query, *_ = _runtime()
    with pytest.raises(AgentModelInvocationError, match="cancelled"):
        runtime.kick(_inputs(), cancellation=cancellation)
    assert query.contexts == []


@pytest.mark.parametrize("reason,code", [
    (ModelFailureReason.OUTPUT_TOKEN_LIMIT, VideoSearchFailureCode.OUTPUT_TOKEN_LIMIT),
    (ModelFailureReason.MODEL_TIMEOUT, VideoSearchFailureCode.MODEL_TIMEOUT),
    (ModelFailureReason.REQUIRED_TOOL_MISSING, VideoSearchFailureCode.REQUIRED_TOOL_MISSING),
    (ModelFailureReason.INVALID_MODEL_RESPONSE, VideoSearchFailureCode.INVALID_MODEL_RESPONSE),
])
def test_query_provider_failure_classification_is_preserved(reason, code):
    class FailedQuery(Query):
        def run_structured(self, *args, **kwargs):
            raise QueryModelIncompleteError("failed", reason=reason)

    runtime, *_ = _runtime(query=FailedQuery())
    with pytest.raises(VideoSearchFailure) as captured:
        runtime.kick(_inputs())
    assert captured.value.code == code
    assert captured.value.phase == VideoSearchFailurePhase.QUERY


def test_context_budget_failure_is_not_returned_as_an_uncertain_answer():
    class LargeQuery(Query):
        def run_structured(self, *args, **kwargs):
            raise ModelInputTooLargeError("too large")

    runtime, *_ = _runtime(query=LargeQuery())
    with pytest.raises(VideoSearchFailure) as captured:
        runtime.kick(_inputs())
    assert captured.value.code == VideoSearchFailureCode.CONTEXT_BUDGET_EXCEEDED


def test_unknown_query_failure_keeps_original_cause():
    class BrokenQuery(Query):
        def run_structured(self, *args, **kwargs):
            raise RuntimeError("unexpected failure")

    runtime, *_ = _runtime(query=BrokenQuery())
    with pytest.raises(AgentModelInvocationError) as captured:
        runtime.kick(_inputs())
    assert isinstance(captured.value.__cause__, RuntimeError)


def test_shared_query_graph_runs_concurrently_without_crossing_evidence_or_scope():
    started = Barrier(2)

    class ScopedQuery:
        model_name = "scoped"
        provider_name = "fake"

        def run_structured(self, prompt, *, tools, result_type, **kwargs):
            context = json.loads(prompt.rsplit("\n\n", 1)[1])
            name = context["inputs"]["conversation_id"]
            started.wait(timeout=5)
            available = _tool_map(tools)
            available["retrieve"].invoke(_retrieval_args(name, 1))
            identifier = f"chunk-{name}"
            _read_all(available["read_retrieval_evidence"], identifier)
            return result_type(plan_context=name, message=f"report for {name}")

    def search(query, *, tenant_id, series_ids, **kwargs):
        assert tenant_id == f"workspace-{query}"
        assert series_ids == [f"series-{query}"]
        hit = _hit(f"chunk-{query}", workspace=tenant_id, series=series_ids[0])
        return VideoChunkSearchResult(query=query, series_ids=series_ids,
            agent_reply=AgentReply(message=query, result_count=1), hits=[hit])

    runtime = AgentRuntime(ScopedQuery(), search, retrieval_channel="lexical")
    events = {name: [] for name in ("a", "b")}
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {name: executor.submit(runtime.kick,
            _inputs(name, workspace=f"workspace-{name}", series=f"series-{name}"),
            emit=events[name].append) for name in events}
        reports = {name: future.result(timeout=5) for name, future in futures.items()}
    assert reports["a"].retrieval_calls is not reports["b"].retrieval_calls
    for name, report in reports.items():
        assert [hit.evidence.video_chunk_id for hit in report.retrieval_result.hits] == [f"chunk-{name}"]
        assert report.completion.message == f"report for {name}"
        assert all(event["conversationId"] == name for event in events[name])


def _call(hit, *, query="query", searched=None, partial=False, warnings=(), role="query", status=None):
    weights = LexicalRetrievalChannelWeights(lexical=1, semantic=0, visual=0)
    result = VideoChunkSearchResult(
        query=query, series_ids=["series"],
        agent_reply=AgentReply(message="retrieval", result_count=1), hits=[hit],
        searched_video_ids=searched, partial=partial, warnings=list(warnings),
    )
    return RetrievalCall(query=query, top_k=3, requested_weights=weights,
        effective_weights=weights, result=result, role=role, status=status)


def test_direct_and_delegated_calls_merge_coverage_and_keep_each_trace():
    first = _call(_hit(), searched=["video-a"], role="supervisor")
    second = _call(_hit("chunk-2"), query="second", searched=["video-b"], partial=True, warnings=["partial"])
    merged = merge_retrieval_calls(_inputs(), [first, second])
    assert merged.searched_video_ids == ["video-a", "video-b"]
    assert merged.partial and merged.warnings == ["partial"]
    assert len(merged.hits) == 2
    trace = build_retrieval_trace([first, second])
    assert [entry.role for entry in trace] == ["supervisor", "query"]
    assert [entry.query for entry in trace] == ["query", "second"]


def test_merge_rejects_conflicting_canonical_evidence_for_same_chunk():
    changed = _hit()
    changed.evidence.window_summary = "different source version"
    with pytest.raises(AgentModelInvocationError, match="source evidence changed"):
        merge_retrieval_calls(_inputs(), [_call(_hit()), _call(changed)])


def test_failed_call_remains_in_trace_and_marks_partial_without_claiming_coverage():
    good = _call(_hit(), searched=["video-a"])
    bad = _call(_hit("ignored"), query="failed", status="error")
    merged = merge_retrieval_calls(_inputs(), [good, bad])
    assert merged.partial
    assert [hit.evidence.video_chunk_id for hit in merged.hits] == ["chunk-1"]
    assert build_retrieval_trace([good, bad])[1].status == "error"


def test_runtime_close_only_closes_query_model():
    query = Query()
    closed = []
    query.close = lambda: closed.append(True)
    runtime, *_ = _runtime(query=query)
    runtime.close()
    assert closed == [True]
@pytest.mark.parametrize("failure", [EventIndexNotReadyError("Event scope is not published"), RuntimeError("Event backend unavailable")])
@pytest.mark.parametrize("fallback", [False, True])
def test_failed_event_attempt_is_traced_but_does_not_satisfy_real_retrieval(failure, fallback):
    class FallbackQuery(Query):
        def run_structured(self, prompt, *, tools, result_type, **options):
            result = _invoke_json(_tool_map(tools)["search_video_events"], {
                **_retrieval_args("specific quote", 2, lexical=1, semantic=0, visual=0),
                "speaker": "speaker",
            })
            assert result["error"]["code"] in {"EVENT_INDEX_NOT_READY", "EVENT_RETRIEVAL_FAILED"}
            if fallback:
                return super().run_structured(prompt, tools=tools, result_type=result_type, **options)
            return result_type(plan_context="Event lookup failed", message="Coverage is unavailable")

    def fail(*_args, **_kwargs):
        raise failure

    runtime, _query, evaluator, chat, retrieval = _runtime(query=FallbackQuery(), event_retrieve=fail)
    if not fallback:
        with pytest.raises(VideoSearchFailure) as captured:
            runtime.kick(_inputs())
        assert captured.value.code == VideoSearchFailureCode.RETRIEVAL_FAILED
        assert captured.value.phase == VideoSearchFailurePhase.RETRIEVAL
        assert captured.value.retryable
        assert evaluator.contexts == chat.contexts == retrieval.calls == []
        return
    result = runtime.kick(_inputs())
    trace = [
        item.model_dump(mode="json", by_alias=True)
        for item in build_retrieval_trace(result.retrieval_calls)
    ]
    assert len(trace) == 2 and len(retrieval.calls) == 1
    assert trace[0]["query"] == "specific quote" and trace[0]["status"] == "error"
    assert trace[0]["granularity"] == "event" and trace[0]["role"] == "query"
    assert trace[0]["strategyVersion"] == EVENT_RETRIEVAL_STRATEGY_VERSION
    assert trace[0]["effectiveFilters"] == {"speaker": "speaker", "event_types": ["DIALOGUE"], "video_chunk_ids": []}
    assert trace[0]["error"]["message"] == str(failure)
    assert trace[0]["resultCount"] == 0
    assert trace[1]["query"] == "query-1-0" and "status" not in trace[1]


def test_event_only_query_reports_canonical_evidence_and_full_trace():
    marker = "FULL_CANDIDATE_TRACE_" + "x" * 30_000
    searches = []

    def event_search(query, **options):
        searches.append((query, options))
        return EventSearchResult(
            chunk_result=VideoChunkSearchResult(
                query=query, series_ids=["series"], hits=[_hit()],
                agent_reply=AgentReply(message="Event evidence", result_count=1),
            ),
            matches=(FusedEventHit(
                event_id="event-1", parse_task_id="task", video_chunk_id="chunk-1",
                version=1, timeline_index=0, event_type="ACTION", fused_rank=1,
                fused_score=0.5, channel_ranks={"semantic": 1}, channel_scores={"semantic": 0.9},
                lexical_fields=({"field": "event_description", "raw_text": "a relationship changes"},),
                start_ms=12, end_ms=18,
            ),),
            trace={"strategy_version": EVENT_RETRIEVAL_STRATEGY_VERSION,
                   "channels": {"semantic": [{"event_id": "event-1", "score": 0.9}]},
                   "discarded": [{"event_id": "stale", "reason": marker}]},
        )

    class EventQuery(Query):
        def run_structured(self, prompt, *, tools, result_type, **_options):
            self.prompts.append(prompt)
            self.contexts.append(json.loads(prompt.rsplit("\n\n", 1)[1]))
            tools_by_name = _tool_map(tools)
            page = _invoke_json(tools_by_name["search_video_events"], {
                **_retrieval_args("relationship change", 3, lexical=0, semantic=1, visual=0),
                "event_types": ["ACTION"],
            })
            assert page["result_count"] == 1
            _read_all(tools_by_name["read_retrieval_evidence"], "chunk-1")
            return result_type(plan_context="search Events directly", message="grounded draft")

    runtime, query, evaluator, _chat, retrieval = _runtime(
        query=EventQuery(), event_retrieve=event_search,
    )
    result = runtime.kick(_inputs())
    assert retrieval.calls == [] and len(searches) == 1
    assert result.retrieval_result.hits[0].evidence.video_chunk_id == "chunk-1"
    trace = [
        item.model_dump(mode="json", by_alias=True)
        for item in build_retrieval_trace(result.retrieval_calls)
    ]
    assert [entry["role"] for entry in trace] == ["query"]
    assert all(entry["granularity"] == "event" for entry in trace)
    assert all(entry["strategyVersion"] == EVENT_RETRIEVAL_STRATEGY_VERSION for entry in trace)
    assert all(entry["strategyVersion"] == entry["eventTrace"]["strategy_version"] for entry in trace)
    assert all(entry["eventTrace"]["discarded"][0]["reason"] == marker for entry in trace)
    assert trace[0]["eventTrace"]["matched_events"][0]["timeline_index"] == 0
    assert marker not in json.dumps(query.contexts + evaluator.contexts)


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_query_native_image_history_keeps_one_task_body_within_text_budget(provider):
    from types import SimpleNamespace

    from agent.chat.common.state import ExecutionControl
    from agent.chat.video_search.query import run_query
    from gateway.claude_code.agent import ClaudeAgentModel
    from gateway.codex.agent import CodexAgentModel
    from openai_codex import ImageInput as CodexImageInput, TextInput as CodexTextInput

    options = dict(model="native-budget-test", request_timeout_seconds=5, max_output_tokens=100,
                   max_input_chars=100_000)
    native = (CodexAgentModel(**options, max_concurrency=1) if provider == "codex"
              else ClaudeAgentModel(**options))
    inputs = _inputs()
    inputs["series_context"] = [{"description": "NATIVE_TASK_PAYLOAD:" + "x" * 8000}]
    historical = "data:image/png;base64," + "A" * 200_000
    current = [f"https://example.test/current-{index}.png" for index in range(6)]
    inputs["history"] = [{"role": "USER", "content": "Earlier image.", "imageUrls": [historical]}]
    inputs["image_urls"] = current
    class QueryModel:
        model_name = "native-query-test"
        supports_context_messages = False

        def run_structured(self, prompt, **kwargs):
            native.max_input_chars = len(prompt) + 1000
            messages = kwargs["initial_messages"]
            assert messages[-1] == {"role": "user", "content": inputs["user_message"]}
            media_options = dict(initial_messages=messages, image_urls=kwargs["image_urls"])
            if provider == "codex":
                rendered, sdk_inputs = native._prepare_input(
                    prompt, max_input_chars=native.max_input_chars, **media_options,
                )
                images = [item.url for item in sdk_inputs if isinstance(item, CodexImageInput)]
                texts = [item.text for item in sdk_inputs if isinstance(item, CodexTextInput)]
            else:
                rendered, media = native._prepare_input(prompt, **media_options)
                images = [block["image_url"]["url"] for block in media if block["type"] == "image_url"]
                texts = [rendered, *[block["text"] for block in media if block["type"] == "text"]]
            assert len(rendered) <= native.max_input_chars
            assert rendered.count("NATIVE_TASK_PAYLOAD:") == 1
            assert "<initial_context_snapshot>" not in rendered
            assert images == [historical, *current]
            assert all("data:image" not in text for text in texts)
            assert "conversation message 1:" in texts[1] and "conversation message 2:" in texts[2]
            context = json.loads(prompt.rsplit("\n\n", 1)[1])
            return kwargs["result_type"](plan_context="updated-plan", message="Checked image.")

    try:
        result = run_query(
            QueryModel(), inputs, tools=[], max_model_calls=1,
            control=ExecutionControl(monotonic() + 5), max_prompt_chars=100_000,
            calls=[], frame_session=None,
        )
        assert result.plan_context == "updated-plan"
    finally:
        native.close()


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
            start_seconds=10, end_seconds=20,
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
