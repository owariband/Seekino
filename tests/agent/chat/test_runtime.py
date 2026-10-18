from __future__ import annotations

import json
import logging
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from time import monotonic
from uuid import uuid4

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
from agent.chat.common.tool import as_langchain_tools
from agent.chat.video_search.runtime import AgentRuntime
from agent.chat.video_search.errors import (
    VideoSearchFailure, VideoSearchFailureCode, VideoSearchFailurePhase,
)
from agent.chat.video_search.state import (
    LexicalRetrievalChannelWeights, QueryDraft, RetrievalChannelWeights,
    RetrievalTraceEntry,
)
from agent.chat.video_search.tools import ChatWriteCanvasArguments, UpdateVideoItemArguments


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


class _CanvasReader:
    """Bind a collection while exercising the registered shared read Tool."""

    def __init__(self, tools, collection):
        self.tool = tools["get_context_canvas"]
        self.collection = collection

    def _arguments(self, values):
        values = dict(values)
        if "video_chunk_id" in values:
            values["reference"] = values.pop("video_chunk_id")
        return {"collection": self.collection, **values}

    def invoke(self, values):
        if values.get("type") == "tool_call":
            values = {**values, "name": "get_context_canvas",
                      "args": self._arguments(values["args"])}
        else:
            values = self._arguments(values)
        return self.tool.invoke(values)

    def execute(self, **values):
        return self.tool.execute(**self._arguments(values))


class _CanvasWriter:
    """Send one business operation through the registered atomic batch Tool."""

    def __init__(self, tools, operation):
        self.tools, self.operation = tools, operation
        self.args_schema = tools["write_context_canvas"].args_schema
        self.description = tools["write_context_canvas"].description

    def invoke(self, values):
        collection = "draft" if "draft" in self.operation else "approved"
        current = json.loads(self.tools["get_context_canvas"].invoke({"collection": collection}))
        result = json.loads(self.tools["write_context_canvas"].invoke({
            "batch_id": uuid4().hex, "expected_revision": current["revision"],
            "operations": [{"operation": self.operation, **values}],
        }))
        return json.dumps(result["results"][0] if "results" in result else result)


def _reader(tools, collection):
    return _CanvasReader(tools, collection)


def _writer(tools, operation):
    return _CanvasWriter(tools, operation)


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
        series_id=series, tenant_id=workspace, series_name="series name",
        series_description=None, series_status="READY", important_extra_data=None,
        character_bible=[], video_id=f"video-{chunk_id}", episode_order=1,
        original_filename="episode.mp4", original_source_uri="episode.mp4",
        inference_proxy_uri=None, content_hash="hash", duration_seconds=100,
        upload_status="COMPLETED", parse_status="COMPLETED", video_chunk_id=chunk_id,
        sequence_no=0, start_seconds=10, end_seconds=20, analysis_window_start_seconds=0,
        analysis_window_end_seconds=30, chunk_media_ref="chunk.mp4",
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
        number = context["turn_number"]
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
        ranges = (
            [_selected_range(chunk_id) for chunk_id in self.selected]
            if self.selected_ranges is None
            else self.selected_ranges
        )
        for item in ranges:
            _read_all(_reader(tools_by_name, "retrieval"), item["video_chunk_id"])
            self.draft_tool_results.append(
                _writer(tools_by_name, "upsert_draft_item").invoke({
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
            page = _invoke_json(_reader(tools_by_name, "draft"), {"cursor": cursor})
            item_ids.extend(item["video_chunk_id"] for item in page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        for video_chunk_id in item_ids:
            _read_all(_reader(tools_by_name, "draft"), video_chunk_id)
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
        assert [tool.name for tool in tools] == ["get_context_canvas", "write_context_canvas"]
        tools_by_name = _tool_map(tools)
        update_video_item = _writer(tools_by_name, "upsert_video_item")
        assert update_video_item.description
        assert update_video_item.args_schema is ChatWriteCanvasArguments
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
    kwargs.setdefault("retrieval_channel", "lexical")
    return AgentRuntime(query, evaluator, chat, retrieval.search, **kwargs), query, evaluator, chat, retrieval


@pytest.mark.parametrize("context_messages", [False, True])
@pytest.mark.parametrize("image_source", ["none", "current", "history", "both"])
def test_roles_forward_current_and_historical_images_outside_text_context(
    monkeypatch, context_messages, image_source,
):
    runtime, query, evaluator, chat, _ = _runtime(evaluator=Evaluator(2))
    captured = {role: [] for role in ("query", "evaluator", "chat")}
    for role, model, method in (
        ("query", query, "run_structured"),
        ("evaluator", evaluator, "run_structured"),
        ("chat", chat, "run_agent_loop"),
    ):
        model.supports_context_messages = context_messages
        model.supports_context_checkpoints = True
        model.supports_vision_input = False
        original = getattr(model, method)

        def capture(prompt, *, _role=role, _original=original, **options):
            checkpoint = options.pop("context_checkpoint")
            media = {key: options.pop(key) for key in ("image_urls", "initial_messages")
                     if key in options}
            captured[_role].append((prompt, media, checkpoint()))
            return _original(prompt, **options)

        monkeypatch.setattr(model, method, capture)

    current = [f"data:image/png;base64,current-{index}" for index in range(6)]
    history_images = ["data:image/png;base64,old-first", "data:image/png;base64,old-second"]
    inputs = _inputs()
    inputs["history"] = [
        {"role": "USER", "content": "Earlier first image.",
         **({"imageUrls": history_images[:1]} if image_source in {"history", "both"} else {})},
        {"role": "ASSISTANT", "content": "Earlier explanation."},
        {"role": "user", "content": "Earlier second image.",
         **({"imageUrls": history_images[1:]} if image_source in {"history", "both"} else {})},
    ]
    if image_source in {"current", "both"}:
        inputs["image_urls"] = current
    result = runtime.kick(inputs)

    assert result.agent_reply.result_count == 1
    assert [len(captured[role]) for role in captured] == [2, 2, 1]
    for role, calls in captured.items():
        for prompt, media, checkpoint in calls:
            assert "data:image" not in prompt
            assert media.get("image_urls") == (current if image_source in {"current", "both"} else None)
            assert checkpoint["role"] == role
            messages = media.get("initial_messages")
            expected_history = [
                {"role": "user", "content": item["content"], "imageUrls": item["imageUrls"]}
                for item in inputs["history"] if item.get("imageUrls")
            ]
            if not context_messages and image_source not in {"history", "both"}:
                assert messages is None
                continue
            if not context_messages:
                assert messages == [*expected_history, {"role": "user", "content": inputs["user_message"]}]
                continue
            assert messages[0]["role"] == "system"
            assert messages[1]["role"] == "user" and "锚点" in messages[1]["content"]
            anchor = json.loads(messages[1]["content"].split("\n", 1)[1])
            assert anchor["user_message"] == inputs["user_message"]
            assert messages[-1]["content"].startswith("<initial_context_snapshot>")
            assert messages[2:-1] == expected_history
            assert all("data:image" not in item["content"] for item in messages)
        if role == "query":
            context = json.loads(calls[-1][0].rsplit("\n\n", 1)[1])
            assert context["original_plan"] == "plan-1"
            assert context["latest_evaluator_advice"] == "find broken cup, not picked-up cup"
        if role == "query" and context_messages:
            anchor = json.loads(calls[-1][1]["initial_messages"][1]["content"].split("\n", 1)[1])
            assert anchor["original_plan"] == "plan-1"
            assert anchor["latest_evaluator_advice"] == "find broken cup, not picked-up cup"


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_query_native_image_history_keeps_one_task_body_within_text_budget(provider):
    from types import SimpleNamespace

    from agent.chat.common.state import ExecutionControl
    from agent.chat.video_search.query import run_query
    from agent.chat.video_search.video_search_canvas import VideoSearchContextCanvas
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
    advice = "Check the earlier image against the original plan."
    previous = SimpleNamespace(
        plan_context="original-plan",
        review=SimpleNamespace(advice=advice),
        context=lambda: {"plan_context": "original-plan", "review": {"advice": advice}},
    )

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
            assert context["original_plan"] == "original-plan"
            assert context["latest_evaluator_advice"] == advice
            assert "original-plan" in rendered and advice in rendered
            return kwargs["result_type"](plan_context="updated-plan", message="Checked image.")

    try:
        result = run_query(
            QueryModel(), inputs, [previous], tools=[], max_model_calls=1,
            control=ExecutionControl(monotonic() + 5), max_prompt_chars=100_000,
            canvas=VideoSearchContextCanvas("workspace", ("series",)),
        )
        assert result.plan_context == "updated-plan"
    finally:
        native.close()


def test_runtime_defaults_to_the_v1_transcript_contract() -> None:
    runtime = AgentRuntime(Query(), Evaluator(), Chat(), Retrieval().search)

    assert runtime._retrieval_channel == "transcript"
    assert "channel_weights.transcript" in runtime._retrieval_skill_prompt
    assert "channel_weights.lexical" not in runtime._retrieval_skill_prompt


def test_runtime_rejects_nonpositive_read_character_limit() -> None:
    with pytest.raises(ValueError, match="max_read_chars_per_call"):
        _runtime(max_read_chars_per_call=0)


def test_successful_handoff_exposes_last_review_and_frozen_canonical_evidence():
    from agent.utils.errors import AgentToolFeedbackError

    class PartialRetrieval(Retrieval):
        def search(self, *args, **kwargs):
            result = super().search(*args, **kwargs)
            result.partial = True
            result.warnings = ["one video unavailable"]
            result.searched_video_ids = ["video-chunk-1", "video-chunk-2"]
            return result

    runtime, _, _, _, _ = _runtime(evaluator=Evaluator(2), retrieval=PartialRetrieval())
    handoffs = []

    def handoff(canvas, turn):
        handoffs.append((canvas, turn))
        assert turn.number == 2
        assert turn.rendered_answer.answer_markdown == "## Final answer\n\nGrounded answer."
        assert canvas.get("review", "latest") == {
            "turn_number": 2, "action": "complete", "advice": "approved",
            "selected_video_ranges": [_selected_range("chunk-1")],
            "partial": True, "warnings": ["one video unavailable"],
            "searched_video_ids": ["video-chunk-1", "video-chunk-2"],
        }
        assert canvas.draft_item("chunk-1", approved=True).start_seconds == 11
        assert canvas.approved_hit("chunk-1").evidence.start_seconds == 10
        with pytest.raises(AgentToolFeedbackError, match="read-only"):
            canvas.mutate(lambda draft: draft["review"]["latest"].update(action="continue"))
        with pytest.raises(AgentToolFeedbackError, match="read-only"):
            canvas.mutate(lambda draft: draft["approved"]["chunk-1"].update(start_seconds=10))

    result = runtime.kick(_inputs(), on_handoff=handoff)

    assert len(handoffs) == 1
    assert result.render_blocks[1].start_seconds == 11
    assert result.render_blocks[1].end_seconds == 19
    assert result.hits[0].evidence.start_seconds == 10
    assert result.hits[0].evidence.end_seconds == 20


def test_cancelled_graph_completion_does_not_handoff(monkeypatch):
    runtime, _, _, _, _ = _runtime()
    cancellation = Event()
    invoke = runtime._graph.invoke
    handoffs = []

    def cancelled_completion(*args, **kwargs):
        result = invoke(*args, **kwargs)
        cancellation.set()
        return result

    monkeypatch.setattr(runtime._graph, "invoke", cancelled_completion)
    with pytest.raises(AgentModelInvocationError, match="cancelled"):
        runtime.kick(_inputs(), cancellation=cancellation,
                     on_handoff=lambda *values: handoffs.append(values))
    assert handoffs == []


@pytest.mark.parametrize("evaluator_scope_known", [True, False])
def test_handoff_includes_evaluator_retrieval_coverage_without_changing_standalone_output(evaluator_scope_known):
    class CoverageRetrieval(Retrieval):
        def search(self, query, **kwargs):
            result = super().search(query, **kwargs)
            result.searched_video_ids = ["video-chunk-1"]
            result.warnings = ["query warning"]
            if query == "eval-only":
                result.hits = [_hit("chunk-eval")]
                result.partial = True
                result.warnings = ["query warning", "eval warning"]
                result.searched_video_ids = ["video-chunk-eval"] if evaluator_scope_known else None
            return result

    class ExtraRetrievalEvaluator(Evaluator):
        def run_structured(self, prompt, **kwargs):
            completion = super().run_structured(prompt, **kwargs)
            _invoke_json(_tool_map(kwargs["tools"])["retrieve"], _retrieval_args("eval-only", 1))
            return completion

    runtime, _, _, _, _ = _runtime(
        evaluator=ExtraRetrievalEvaluator(), retrieval=CoverageRetrieval([_hit("chunk-1")]),
    )
    handoffs = []
    result = runtime.kick(_inputs(), on_handoff=lambda *values: handoffs.append(values))

    canvas, turn = handoffs[0]
    coverage = {
        "partial": True, "warnings": ["query warning", "eval warning"],
        "searched_video_ids": ["video-chunk-1", "video-chunk-eval"] if evaluator_scope_known else None,
    }
    assert len(turn.retrieval_calls) == 2
    assert canvas.retrieval_snapshot("chunk-eval").evidence.video_chunk_id == "chunk-eval"
    assert [hit.evidence.video_chunk_id for hit in turn.retrieval_result.hits] == ["chunk-1"]
    assert {key: getattr(turn.retrieval_result, key) for key in coverage} == coverage
    assert {key: canvas.get("review", "latest")[key] for key in coverage} == coverage
    assert result.partial is False
    assert result.warnings == ["query warning"]
    assert result.searched_video_ids == ["video-chunk-1"]


@pytest.mark.parametrize("runtime_bytes,caller_bytes", [(1200, 800), (800, 1200), (800, None)])
def test_supervisor_read_tools_preserve_paging_and_use_the_narrower_budget(runtime_bytes, caller_bytes):
    from agent.chat.common.state import ExecutionControl
    from agent.chat.video_search.video_search_canvas import VideoSearchContextCanvas

    runtime, _, _, _, _ = _runtime(
        max_tool_result_bytes=runtime_bytes, max_catalog_items_per_page=1,
        max_read_chars_per_call=40,
    )
    canvas = VideoSearchContextCanvas("workspace", ("series",))
    canvas.record_retrieval(VideoChunkSearchResult(
        query="query", series_ids=["series"],
        agent_reply=AgentReply(message="evidence", result_count=2),
        hits=[_hit("chunk-1"), _hit("chunk-2")],
    ))
    tools = _tool_map(as_langchain_tools(runtime.build_supervisor_read_tools(
        canvas, ExecutionControl(None), maximum_result_bytes=caller_bytes,
    )))
    assert set(tools) == {"get_context_canvas"}
    first = tools["get_context_canvas"].invoke({"collection": "retrieval"})
    page = json.loads(first)
    assert len(page["items"]) == 1
    assert page["next_cursor"] is not None
    second = tools["get_context_canvas"].invoke({
        "collection": "retrieval", "cursor": page["next_cursor"],
    })
    assert json.loads(second)["items"][0]["video_chunk_id"] == "chunk-2"
    detail = tools["get_context_canvas"].invoke({
        "collection": "retrieval", "reference": "chunk-1",
        "section": "evidence.text", "start": 0, "end": 40,
    })
    assert json.loads(detail)["end"] - json.loads(detail)["start"] <= 40
    limit = min(runtime_bytes, caller_bytes) if caller_bytes is not None else runtime_bytes
    assert all(len(value.encode("utf-8")) <= limit for value in (first, second, detail))


@pytest.mark.parametrize("value", [0, float("inf"), float("nan")])
def test_runtime_rejects_invalid_chat_timeout(value) -> None:
    with pytest.raises(ValueError, match="chat_timeout_seconds"):
        _runtime(chat_timeout_seconds=value)


@pytest.mark.parametrize("total_timeout_enabled", [True, False])
def test_runtime_obeys_outer_deadline_before_starting_video_graph(total_timeout_enabled):
    runtime, query, _, _, _ = _runtime(total_timeout_enabled=total_timeout_enabled)
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
    assert result.hits[0].evidence.start_seconds == 10
    assert result.hits[0].evidence.end_seconds == 20
    assert result.render_blocks[1].start_seconds == 11
    assert result.render_blocks[1].end_seconds == 19
    assert "write_context_canvas" in query.prompts[0]
    assert "get_context_canvas" in query.prompts[0]
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
            catalog = _invoke_json(_reader(tools_by_name, "draft"), {})
            for item in catalog["items"]:
                _read_all(_reader(tools_by_name, "draft"), item["video_chunk_id"])
            return result_type.model_validate({
                "action": (
                    "complete"
                    if context["inputs"]["conversation_id"] == "auto"
                    and context["turn_number"] == 4
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


@pytest.mark.parametrize("sections_to_read", [(), ("draft", "evidence.transcript", "evidence.events")])
def test_evaluator_complete_is_trusted_without_full_section_reads(caplog, sections_to_read) -> None:
    caplog.set_level(logging.INFO)

    class BlindEvaluator:
        model_name = "blind-evaluator"

        def __init__(self) -> None:
            self.calls = 0

        def run_structured(self, _prompt, *, tools, result_type, **_kwargs):
            self.calls += 1
            if sections_to_read:
                reader = _reader(_tool_map(tools), "draft")
                catalog = _invoke_json(reader, {})
                for item in catalog["items"]:
                    for section in sections_to_read:
                        _invoke_json(reader, {
                            "video_chunk_id": item["video_chunk_id"], "section": section,
                        })
            return result_type.model_validate({
                "action": "complete",
                "advice": "approved without reading",
            })

    evaluator = BlindEvaluator()
    runtime, query, _evaluator, chat, _retrieval = _runtime(
        evaluator=evaluator, limits_enabled=True,
    )

    result = runtime.kick(_inputs())

    assert evaluator.calls == 1
    assert len(query.contexts) == len(chat.contexts) == 1
    assert result.agent_reply.result_count == 1
    assert (
        'review_action=complete review_source=evaluator '
        'evaluator_action=complete evaluator_advice="approved without reading"'
    ) in caplog.text
    assert "review_source=draft_read_guard" not in caplog.text


def test_next_query_reads_previous_calls_drafts_and_reviews_from_canvas():
    history_reads = []

    class HistoryQuery(Query):
        def run_structured(self, prompt, **kwargs):
            context = json.loads(prompt.rsplit("\n\n", 1)[1])
            if context["turn_number"] == 3:
                reader = _reader(_tool_map(kwargs["tools"]), "history")
                catalog = _invoke_json(reader, {})
                assert catalog["total"] == 2
                for item in catalog["items"]:
                    history_reads.append(_read_all(reader, item["reference"])["history"])
            return super().run_structured(prompt, **kwargs)

    runtime, query, evaluator, chat, retrieval = _runtime(
        query=HistoryQuery(search_rounds=(1, 2), searches_per_round=2), evaluator=Evaluator(3),
    )
    result = runtime.kick(_inputs())
    third = query.contexts[2]
    assert third["original_plan"] == "plan-1"
    assert "turns" not in third
    assert third["run_records"]["history"]["item_count"] == 2
    assert third["latest_evaluator_advice"] == "find broken cup, not picked-up cup"
    for number, record in enumerate(history_reads, 1):
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
    assert history_reads[1]["evidence_source_turns"] == {"chunk-1": 2}
    assert "query" not in result.model_dump(mode="json", by_alias=True)
    assert [
        entry.model_dump(mode="json", by_alias=True)
        for entry in result.retrieval_trace
    ] == [
        {
            "query": f"query-{turn}-{call}",
            "topK": 8,
            "strategyVersion": "query-adaptive-three-channel-rrf-v2",
            "requestedWeights": {
                "lexical": 0.0,
                "semantic": 0.5,
                "visual": 0.5,
            },
            "effectiveWeights": {
                "lexical": 0.0,
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
    # Later records and rendering do not change previously delivered navigation.
    assert query.contexts[1]["run_records"]["history"]["item_count"] == 1


@pytest.mark.parametrize("mutation, expected", [
    (lambda hit: setattr(hit.evidence, "tenant_id", "other"), "outside the fixed scope"),
    (lambda hit: setattr(hit.evidence, "series_id", "other"), "outside the fixed scope"),
    (lambda hit: setattr(hit.evidence, "end_seconds", 101), "invalid video time range"),
    (lambda hit: setattr(hit.evidence, "start_seconds", -1), "invalid video time range"),
    (lambda hit: setattr(hit.evidence, "end_seconds", 10), "invalid video time range"),
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
                _reader(_tool_map(kwargs["tools"]), "retrieval"),
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
                _selected_range("chunk-1", start_seconds=9, end_seconds=15)
            ]
            completion = super().run_structured(prompt, **kwargs)
            self.draft_tool_results.append(
                _writer(_tool_map(kwargs["tools"]), "upsert_draft_item").invoke({
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
    assert result.render_blocks[1].start_seconds == 11
    assert result.render_blocks[1].end_seconds == 19


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
        (block.video_chunk_id, block.start_seconds, block.end_seconds)
        for block in result.render_blocks
        if block.type == "video"
    ] == [
        ("chunk-2", 11, 19),
        ("chunk-1", 11, 19),
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
        kind="CONTEXT", text=matched_marker, start_seconds=10, end_seconds=20,
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
    assert "turns" not in query.contexts[2]
    assert query.contexts[2]["run_records"]["history"]["item_count"] == 2


@pytest.mark.parametrize("mutation, expected", [
    (lambda result: setattr(result, "series_ids", []), "changed the fixed Series scope"),
    (lambda result: setattr(result, "series_ids", ["other"]), "changed the fixed Series scope"),
    (lambda result: setattr(result.hits[0].evidence, "analysis_window_start_seconds", 11), "analysis window"),
    (lambda result: setattr(result.hits[0].evidence, "analysis_window_end_seconds", 19), "analysis window"),
    (lambda result: setattr(result.hits[0].evidence, "analysis_window_end_seconds", 101), "analysis window"),
    (lambda result: result.hits[0].selection_reason.matched_evidence.append(
        MatchedEvidence.model_construct(kind="CONTEXT", text="cup", start_seconds=None, end_seconds=10)
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


def test_event_only_query_and_evaluator_complete_with_canonical_evidence_and_full_trace():
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
                version=1, timeline_index=9, event_type="RELATION_CHANGE", fused_rank=1,
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
                "event_types": ["RELATION_CHANGE"],
            })
            assert page["result_count"] == 1
            _read_all(_reader(tools_by_name, "retrieval"), "chunk-1")
            _writer(tools_by_name, "upsert_draft_item").invoke({
                **_selected_range("chunk-1"), "claim": "The canonical Event supports a relationship change.",
            })
            return result_type(plan_context="search Events directly", message="grounded draft")

    class EventEvaluator(Evaluator):
        def run_structured(self, prompt, *, tools, **options):
            context = json.loads(prompt.rsplit("\n\n", 1)[1])
            assert context["retrieval_calls"][0]["granularity"] == "event"
            assert marker not in prompt
            _invoke_json(_tool_map(tools)["search_video_events"], {
                **_retrieval_args("check relationship change", 1, semantic=1, visual=0),
                "event_types": ["RELATION_CHANGE"], "video_chunk_ids": ["chunk-1"],
            })
            return super().run_structured(prompt, tools=tools, **options)

    runtime, query, evaluator, _chat, retrieval = _runtime(
        query=EventQuery(), evaluator=EventEvaluator(), event_retrieve=event_search,
    )
    result = runtime.kick(_inputs())
    assert retrieval.calls == [] and len(searches) == 2
    assert result.hits[0].evidence.video_chunk_id == "chunk-1"
    trace = result.model_dump(mode="json", by_alias=True)["retrievalTrace"]
    assert [entry["role"] for entry in trace] == ["query", "evaluator"]
    assert all(entry["granularity"] == "event" for entry in trace)
    assert all(entry["strategyVersion"] == EVENT_RETRIEVAL_STRATEGY_VERSION for entry in trace)
    assert all(entry["strategyVersion"] == entry["eventTrace"]["strategy_version"] for entry in trace)
    assert all(entry["eventTrace"]["discarded"][0]["reason"] == marker for entry in trace)
    assert trace[0]["eventTrace"]["matched_events"][0]["timeline_index"] == 9
    assert trace[1]["effectiveFilters"]["video_chunk_ids"] == ["chunk-1"]
    assert marker not in json.dumps(query.contexts + evaluator.contexts)


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
        with pytest.raises(AgentModelInvocationError, match="must retrieve real evidence"):
            runtime.kick(_inputs())
        assert evaluator.contexts == chat.contexts == retrieval.calls == []
        return
    result = runtime.kick(_inputs())
    trace = result.model_dump(mode="json", by_alias=True)["retrievalTrace"]
    assert len(trace) == 2 and len(retrieval.calls) == 1
    assert trace[0]["query"] == "specific quote" and trace[0]["status"] == "error"
    assert trace[0]["granularity"] == "event" and trace[0]["role"] == "query"
    assert trace[0]["strategyVersion"] == EVENT_RETRIEVAL_STRATEGY_VERSION
    assert trace[0]["effectiveFilters"] == {"speaker": "speaker", "event_types": ["DIALOGUE"], "video_chunk_ids": []}
    assert trace[0]["error"]["message"] == str(failure)
    assert trace[0]["resultCount"] == 0
    assert trace[1]["query"] == "query-1-0" and "status" not in trace[1]


def test_empty_real_retrieval_can_deliver_an_approved_uncertain_answer():
    runtime, _query, _evaluator, _chat, retrieval = _runtime(
        query=Query(selected=()), retrieval=Retrieval([]),
    )
    result = runtime.kick(_inputs())
    assert len(retrieval.calls) == 1
    assert result.hits == []
    assert result.agent_reply.result_count == 0
    assert [block.type for block in result.render_blocks] == ["markdown"]


def test_tool_scope_is_fixed_and_model_top_k_ignores_legacy_limits():
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

    runtime, query, evaluator, _chat, retrieval = _runtime(
        query=InspectQuery(), maximum_top_k=2, max_model_calls_per_turn=5,
        limits_enabled=True,
    )
    inputs = _inputs()
    inputs["requested_top_k"] = 3
    runtime.kick(inputs)
    assert retrieval.calls == [("query-1-0", "workspace", ["series"], 8)]
    assert retrieval.weights == [(0.0, 0.5, 0.5)]
    assert query.budgets == [5]
    assert "requested_top_k" not in query.contexts[0]["inputs"]
    assert "requested_top_k" not in evaluator.contexts[0]["inputs"]


def test_model_can_retrieve_more_than_100_hits_and_complete_with_full_trace():
    chunk_ids = [f"chunk-{number}" for number in range(1, 102)]
    runtime, query, evaluator, chat, retrieval = _runtime(
        query=Query(top_k=101, selected=(chunk_ids[-1],)),
        retrieval=Retrieval([_hit(chunk_id) for chunk_id in chunk_ids]),
        maximum_top_k=2,
    )
    inputs = _inputs()
    inputs["requested_top_k"] = 3

    result = runtime.kick(inputs)

    assert retrieval.calls == [("query-1-0", "workspace", ["series"], 101)]
    assert query.tool_results[0]["requested_top_k"] == 101
    assert query.tool_results[0]["result_count"] == 101
    assert query.tool_results[0]["total"] == 101
    assert query.tool_results[0]["next_cursor"] is not None
    assert len(evaluator.contexts) == len(chat.contexts) == 1
    assert result.agent_reply.result_count == 1
    assert result.hits[0].evidence.video_chunk_id == chunk_ids[-1]
    trace = json.loads(result.model_dump_json(by_alias=True))["retrievalTrace"]
    assert len(trace) == 1
    assert trace[0]["topK"] == 101
    assert trace[0]["resultCount"] == 101
    assert trace[0]["videoChunkIds"] == chunk_ids


def test_kick_run_limits_override_only_the_current_execution():
    runtime, query, evaluator, chat, _retrieval = _runtime(limits_enabled=True)
    runtime.kick(
        _inputs(),
        max_model_calls_per_turn=7,
        max_tool_calls_per_turn=12,
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
    monkeypatch.setattr("agent.chat.video_search.runtime.monotonic", lambda: clock)
    monkeypatch.setattr("agent.chat.common.state.monotonic", lambda: clock)
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
    monkeypatch.setattr("agent.chat.video_search.runtime.monotonic", lambda: clock)
    monkeypatch.setattr("agent.chat.common.state.monotonic", lambda: clock)

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


def test_ten_minute_deadline_stops_repeated_query_evaluator_turns_before_chat(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("agent.chat.video_search.runtime.monotonic", lambda: clock[0])
    monkeypatch.setattr("agent.chat.common.state.monotonic", lambda: clock[0])

    class SlowEvaluator(Evaluator):
        def run_structured(self, prompt, **kwargs):
            result = super().run_structured(prompt, **kwargs)
            clock[0] += 100
            return result

    runtime, query, evaluator, chat, _retrieval = _runtime(
        evaluator=SlowEvaluator(99), total_timeout_enabled=True,
        limits_enabled=False, execution_timeout_seconds=600,
    )

    with pytest.raises(AgentExecutionTimeoutError, match="deadline exceeded"):
        runtime.kick(_inputs(), max_loop_turns=100)

    assert clock[0] == 700.0
    assert [context["turn_number"] for context in query.contexts] == list(range(1, 7))
    assert len(evaluator.contexts) == 6
    assert evaluator.request_timeouts == [pytest.approx(value) for value in [480, 480, 400, 300, 200, 100]]
    assert query.budgets == evaluator.budgets == [None] * 6
    assert chat.contexts == []


def test_disabled_total_timeout_keeps_finite_stages_after_more_than_three_turns(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("agent.chat.video_search.runtime.monotonic", lambda: clock[0])
    monkeypatch.setattr("agent.chat.common.state.monotonic", lambda: clock[0])

    class SlowQuery(Query):
        def run_structured(self, prompt, **kwargs):
            result = super().run_structured(prompt, **kwargs)
            clock[0] += 2
            return result

    class SlowEvaluator(Evaluator):
        def run_structured(self, prompt, **kwargs):
            result = super().run_structured(prompt, **kwargs)
            clock[0] += 2
            return result

    runtime, query, evaluator, chat, _retrieval = _runtime(
        query=SlowQuery(), evaluator=SlowEvaluator(4),
        total_timeout_enabled=False, limits_enabled=False,
        execution_timeout_seconds=1, stage_timeout_seconds=5,
        evaluator_stage_timeout_seconds=7, chat_timeout_seconds=9,
    )

    result = runtime.kick(_inputs())

    assert result.agent_reply.result_count == 1
    assert clock[0] == 116.0
    assert len(query.contexts) == len(evaluator.contexts) == 4
    assert query.request_timeouts == [pytest.approx(5)] * 4
    assert evaluator.request_timeouts == [pytest.approx(7)] * 4
    assert chat.request_timeouts == [pytest.approx(9)]
    assert query.budgets == evaluator.budgets == [None] * 4
    assert chat.model_call_budgets == [None]


def test_disabled_total_timeout_still_clamps_stages_to_parent_deadline(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("agent.chat.video_search.runtime.monotonic", lambda: clock[0])
    monkeypatch.setattr("agent.chat.common.state.monotonic", lambda: clock[0])

    class SlowQuery(Query):
        def run_structured(self, prompt, **kwargs):
            result = super().run_structured(prompt, **kwargs)
            clock[0] += 2
            return result

    class SlowEvaluator(Evaluator):
        def run_structured(self, prompt, **kwargs):
            result = super().run_structured(prompt, **kwargs)
            clock[0] += 2
            return result

    runtime, query, evaluator, chat, _retrieval = _runtime(
        query=SlowQuery(), evaluator=SlowEvaluator(),
        total_timeout_enabled=False, execution_timeout_seconds=1,
        stage_timeout_seconds=10, evaluator_stage_timeout_seconds=10,
        chat_timeout_seconds=10,
    )

    runtime.kick(_inputs(), deadline=108.0)

    assert query.request_timeouts == [pytest.approx(8)]
    assert evaluator.request_timeouts == [pytest.approx(6)]
    assert chat.request_timeouts == [pytest.approx(4)]


@pytest.mark.parametrize("phase", ["query", "evaluator", "chat"])
def test_disabled_total_timeout_still_enforces_each_stage_deadline(monkeypatch, phase):
    clock = [100.0]
    monkeypatch.setattr("agent.chat.video_search.runtime.monotonic", lambda: clock[0])
    monkeypatch.setattr("agent.chat.common.state.monotonic", lambda: clock[0])

    class SlowQuery(Query):
        def run_structured(self, prompt, **kwargs):
            result = super().run_structured(prompt, **kwargs)
            if phase == "query":
                clock[0] += 6
            return result

    class SlowEvaluator(Evaluator):
        def run_structured(self, prompt, **kwargs):
            result = super().run_structured(prompt, **kwargs)
            if phase == "evaluator":
                clock[0] += 8
            return result

    class SlowChat(Chat):
        def run_agent_loop(self, prompt, **kwargs):
            super().run_agent_loop(prompt, **kwargs)
            if phase == "chat":
                clock[0] += 10

    runtime, query, evaluator, chat, _retrieval = _runtime(
        query=SlowQuery(), evaluator=SlowEvaluator(), chat=SlowChat(),
        total_timeout_enabled=False, execution_timeout_seconds=1,
        stage_timeout_seconds=5, evaluator_stage_timeout_seconds=7,
        chat_timeout_seconds=9,
    )

    with pytest.raises(AgentModelInvocationError, match="deadline exceeded"):
        runtime.kick(_inputs())

    assert query.request_timeouts == [pytest.approx(5)]
    assert evaluator.request_timeouts == ([] if phase == "query" else [pytest.approx(7)])
    assert chat.request_timeouts == ([pytest.approx(9)] if phase == "chat" else [])


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


def test_model_selected_weights_are_normalized_recorded_and_returned_to_agent():
    runtime, query, evaluator, _chat, retrieval = _runtime(
        query=Query(channel_weights=(0.6, 0.3, 0.1)),
    )

    result = runtime.kick(_inputs())

    assert retrieval.weights[0] == pytest.approx((0.6, 0.3, 0.1))
    strategy = query.tool_results[0]["retrieval_strategy"]
    assert strategy["requested_weights"] == {
        "lexical": 0.6,
        "semantic": 0.3,
        "visual": 0.1,
    }
    assert strategy["effective_weights"] == pytest.approx({
        "lexical": 0.6,
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
            "lexical": 0.6,
            "semantic": 0.3,
            "visual": 0.1,
        },
        "effectiveWeights": pytest.approx({
            "lexical": 0.6,
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


def test_old_prompt_character_threshold_does_not_block_any_role():
    runtime, query, evaluator, chat, retrieval = _runtime(max_prompt_chars=100)

    result = runtime.kick(_inputs())

    assert result.agent_reply.result_count == 1
    assert len(query.contexts) == len(evaluator.contexts) == len(chat.contexts) == 1
    assert all(len(model.prompts[0]) > 100 for model in (query, evaluator))
    assert len(json.dumps(chat.contexts[0])) > 100
    assert len(retrieval.calls) == 1


def test_many_rounds_keep_prompt_small_and_old_turns_recoverable():
    restored = []

    class LongRunQuery(Query):
        def run_structured(self, prompt, **kwargs):
            context = json.loads(prompt.rsplit("\n\n", 1)[1])
            if context["turn_number"] == 20:
                restored.append(_read_all(
                    _reader(_tool_map(kwargs["tools"]), "history"), "2",
                )["history"])
            return super().run_structured(prompt, **kwargs)

    runtime, query, evaluator, _chat, _retrieval = _runtime(
        query=LongRunQuery(search_rounds=(1,)), evaluator=Evaluator(20),
        max_prompt_chars=100,
    )
    runtime.kick(_inputs())

    assert len(query.prompts) == len(evaluator.prompts) == 20
    assert query.contexts[-1]["original_plan"] == "plan-1"
    assert query.contexts[-1]["run_records"]["history"]["item_count"] == 19
    assert restored[0]["number"] == 2
    assert restored[0]["plan_context"] == "plan-2"
    assert restored[0]["draft"]["message"] == "draft-2"
    assert restored[0]["review"]["advice"] == query.contexts[-1]["latest_evaluator_advice"]
    # Changing the round/count has bounded overhead; old rounds are not re-serialized.
    assert len(query.prompts[-1]) - len(query.prompts[1]) < 100
    assert len(evaluator.prompts[-1]) - len(evaluator.prompts[1]) < 100


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
        ModelFailureReason.LOOP_LIMIT,
        VideoSearchFailureCode.LOOP_LIMIT_EXCEEDED,
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
            feedback = _invoke_json(_writer(_tool_map(tools), "upsert_video_item"), {
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
            assert "write_context_canvas" in self.stop_feedback_messages[0]
            assert _invoke_json(_writer(_tool_map(tools), "upsert_video_item"), {
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
            update = _writer(_tool_map(tools), "upsert_video_item")
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
            _writer(_tool_map(tools), "upsert_video_item").invoke({
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
                _writer(_tool_map(tools), "upsert_video_item").invoke({
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
            feedback = _invoke_json(_writer(_tool_map(tools), "upsert_video_item"), {
                "video_chunk_id": "chunk-1",
                "start_seconds": 10,
                "end_seconds": 20,
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
            _selected_range("chunk-1", start_seconds=9, end_seconds=15)
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


@pytest.mark.parametrize("total_timeout_enabled", [True, False])
def test_cancel_after_agent_loop_stops_before_output(total_timeout_enabled):
    cancellation = Event()

    class CancelChat(Chat):
        def run_agent_loop(self, prompt, **kwargs):
            super().run_agent_loop(prompt, **kwargs)
            cancellation.set()

    runtime, *_ = _runtime(
        chat=CancelChat(), total_timeout_enabled=total_timeout_enabled,
    )
    events = []

    with pytest.raises(AgentModelInvocationError, match="cancelled"):
        runtime.kick(_inputs(), emit=events.append, cancellation=cancellation)
    assert [event for event in events if event["type"] != "answer.progress"] == []


def test_deadline_after_a_model_call_stops_before_chat(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("agent.chat.video_search.runtime.monotonic", lambda: clock[0])
    monkeypatch.setattr("agent.chat.common.state.monotonic", lambda: clock[0])

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
    handoff_rendezvous = Barrier(2)
    events = {"a": [], "b": []}
    handoffs = {}

    class ScopedQuery:
        model_name = "scoped-query"

        def run_structured(
            self, prompt, *, tools, result_type, max_model_calls,
            request_timeout_seconds,
            check_control=None, max_input_chars=None,
        ):
            assert request_timeout_seconds > 0
            context = json.loads(prompt.rsplit("\n\n", 1)[1])
            assert context["run_records"]["history"]["item_count"] == 0
            name = context["inputs"]["conversation_id"]
            rendezvous.wait(timeout=5)
            tools_by_name = _tool_map(tools)
            result = _invoke_json(tools_by_name["retrieve"], _retrieval_args(name, 1))
            video_chunk_id = result["items"][0]["video_chunk_id"]
            _read_all(_reader(tools_by_name, "retrieval"), video_chunk_id)
            _writer(tools_by_name, "upsert_draft_item").invoke({
                "video_chunk_id": video_chunk_id,
                "start_seconds": 11,
                "end_seconds": 19,
                "claim": f"Evidence for {video_chunk_id}",
            })
            return result_type.model_validate({
                "plan_context": name, "message": name,
            })

    class Approve:
        model_name = "approve"

        def run_structured(self, prompt, *, tools, result_type, **_kwargs):
            tools_by_name = _tool_map(tools)
            catalog = _invoke_json(_reader(tools_by_name, "draft"), {})
            for item in catalog["items"]:
                _read_all(_reader(tools_by_name, "draft"), item["video_chunk_id"])
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
                _writer(_tool_map(tools), "upsert_video_item").invoke({
                    "video_chunk_id": chunk_id,
                    "claim": f"Evidence for {chunk_id}",
                })

    def search(
        query,
        *,
        tenant_id,
        series_ids,
        top_k,
        lexical_weight,
        text_weight,
        vision_weight,
    ):
        assert tenant_id == f"workspace-{query}"
        assert series_ids == [f"series-{query}"]
        assert (lexical_weight, text_weight, vision_weight) == (0.0, 0.5, 0.5)
        hit = _hit(f"chunk-{query}", workspace=tenant_id, series=series_ids[0])
        return VideoChunkSearchResult(query=query, series_ids=series_ids,
            agent_reply=AgentReply(message=query, result_count=1), hits=[hit])

    runtime = AgentRuntime(
        ScopedQuery(), Approve(), EchoChat(), search,
        retrieval_channel="lexical",
    )

    def handoff(canvas, turn):
        from agent.chat.common.state import ExecutionControl

        name = turn.plan_context
        handoff_rendezvous.wait(timeout=5)
        tools = _tool_map(as_langchain_tools(runtime.build_supervisor_read_tools(
            canvas, ExecutionControl(None),
        )))
        page = _invoke_json(tools["get_context_canvas"], {"collection": "retrieval"})
        assert [item["video_chunk_id"] for item in page["items"]] == [f"chunk-{name}"]
        foreign = _invoke_json(tools["get_context_canvas"], {
            "collection": "retrieval", "reference": f"chunk-{'b' if name == 'a' else 'a'}",
        })
        assert foreign["error"]["code"] == "UNKNOWN_ITEM"
        handoffs[name] = canvas

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {
            name: executor.submit(runtime.kick,
                _inputs(name, workspace=f"workspace-{name}", series=f"series-{name}"),
                emit=events[name].append, on_handoff=handoff)
            for name in ("a", "b")
        }
        results = {name: future.result(timeout=5) for name, future in futures.items()}
    assert handoffs["a"].id != handoffs["b"].id
    for name in ("a", "b"):
        assert results[name].agent_reply.message == name
        assert results[name].hits[0].evidence.video_chunk_id == f"chunk-{name}"
        assert all(event["conversationId"] == name for event in events[name])
        evidence = next(event for event in events[name] if event["type"] == "answer.evidence")
        assert evidence["searchResult"]["hits"][0]["evidence"]["tenantId"] == f"workspace-{name}"


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
    assert [call[3] for call in retrieval.calls] == [8, 8]
    assert "selection_limit" not in query.contexts[0]
    assert len(evaluator.contexts) == len(chat.contexts) == 1
    assert result.agent_reply.result_count == 2
    assert [hit.evidence.video_chunk_id for hit in result.hits] == ["chunk-1", "chunk-2"]


def test_multiple_searches_may_choose_one_valid_hit():
    runtime, _, evaluator, _, _ = _runtime(
        query=Query(searches_per_round=2, selected=("chunk-2",)),
        retrieval=DisjointRetrieval(), maximum_top_k=1,
    )
    result = runtime.kick(_inputs())
    assert result.agent_reply.result_count == 1
    assert result.hits[0].evidence.video_chunk_id == "chunk-2"
    assert len(evaluator.contexts[0]["retrieval_calls"]) == 2
    assert [entry.query for entry in result.retrieval_trace] == ["query-1-0", "query-1-1"]
    assert "query" not in result.model_dump(mode="json", by_alias=True)


def test_role_checkpoints_recover_exact_canvas_state_after_discarding_messages() -> None:
    marker = "exact-evidence-survives-model-history-reset"
    retrieval = Retrieval([_hit()])
    retrieval.hits[0].evidence.retrieval_text = marker
    checkpoints = {}

    class RecoveringQuery(Query):
        supports_context_checkpoints = True

        def run_structured(self, prompt, *, context_checkpoint, **kwargs):
            completion = super().run_structured(prompt, **kwargs)
            checkpoints["query"] = context_checkpoint()
            assert marker not in json.dumps(checkpoints["query"])
            # A newly installed message window holds navigation only.
            self.contexts.clear()
            sections = _read_all(_reader(_tool_map(kwargs["tools"]), "retrieval"), "chunk-1")
            assert sections["evidence.text"]["retrievalText"] == marker
            return completion

    class RecoveringEvaluator(Evaluator):
        supports_context_checkpoints = True

        def run_structured(self, prompt, *, context_checkpoint, **kwargs):
            checkpoints["evaluator"] = context_checkpoint()
            assert marker not in json.dumps(checkpoints["evaluator"])
            return super().run_structured(prompt, **kwargs)

    class RecoveringChat(Chat):
        supports_context_checkpoints = True

        def run_agent_loop(self, prompt, *, context_checkpoint, **kwargs):
            checkpoints["chat"] = context_checkpoint()
            sections = _read_all(_reader(_tool_map(kwargs["tools"]), "approved"), "chunk-1")
            assert sections["evidence.text"]["retrievalText"] == marker
            super().run_agent_loop(prompt, **kwargs)
            rendered = context_checkpoint()
            assert rendered["render_draft"]["block_count"] == 3

    runtime, *_ = _runtime(query=RecoveringQuery(), evaluator=RecoveringEvaluator(),
                          chat=RecoveringChat(), retrieval=retrieval)
    events = []
    result = runtime.kick(_inputs(), emit=events.append)
    assert len({value["canvas_id"] for value in checkpoints.values()}) == 1
    assert result.hits[0].evidence.retrieval_text == marker
    assert [block.type for block in result.render_blocks] == ["markdown", "video", "markdown"]
    assert [event["type"] for event in events[-5:]] == [
        "answer.evidence", "answer.block", "answer.block", "answer.block", "answer.delta",
    ]


def test_gateway_chat_keeps_markdown_and_video_from_the_same_model_turn() -> None:
    import httpx
    from openai import OpenAI
    from gateway.openai.agent import OpenAIAgentModel

    requests, receipts = [], []
    read_revision = None

    def respond(request):
        nonlocal read_revision
        payload = json.loads(request.content)
        requests.append(payload)
        number = len(requests)
        if number == 1:
            content, name, arguments = None, "get_context_canvas", {"collection": "approved"}
        elif number == 2:
            catalog = json.loads(payload["messages"][-1]["content"])
            read_revision = catalog["revision"]
            content, name, arguments = "Before video.", "write_context_canvas", {
                "batch_id": "same-turn-video", "expected_revision": read_revision,
                "operations": [{"operation": "upsert_video_item", "video_chunk_id": "chunk-1",
                                "claim": "Grounded evidence."}],
            }
        else:
            assert number == 3, "a normal mixed text/tool turn must not require a retry"
            receipt = json.loads(payload["messages"][-1]["content"])
            receipts.append(receipt)
            assert "error" not in receipt, receipt
            content, name, arguments = "After video.", None, None
        message = {"role": "assistant", "content": content}
        if name is not None:
            message["tool_calls"] = [{
                "id": f"call-{number}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }]
        return httpx.Response(200, json={
            "id": f"response-{number}", "object": "chat.completion", "created": 0,
            "model": "chat-canvas-test", "choices": [{
                "index": 0, "finish_reason": "tool_calls" if name else "stop", "message": message,
            }], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        })

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        client = OpenAI(api_key="offline-test-key", base_url="https://canvas.test/v1",
                        http_client=http, max_retries=0)
        model = OpenAIAgentModel(
            "chat-canvas-test", base_url="https://canvas.test/v1", api_key="offline-test-key",
            max_output_tokens=2048, request_timeout_seconds=30, client=client,
        )
        runtime, *_ = _runtime(chat=model)
        events = []
        result = runtime.kick(_inputs(), emit=events.append)

    assert len(requests) == 3
    assert receipts[0]["revision"] == read_revision + 2
    assert receipts[0]["replayed"] is False
    assert [block.type for block in result.render_blocks] == ["markdown", "video", "markdown"]
    assert result.render_blocks[0].markdown == "Before video."
    assert result.render_blocks[1].video_chunk_id == "chunk-1"
    assert result.render_blocks[2].markdown == "After video."
    assert [event["type"] for event in events[-5:]] == [
        "answer.evidence", "answer.block", "answer.block", "answer.block", "answer.delta",
    ]


def _approved_chat_canvas_tools():
    from agent.chat.common.state import ExecutionControl
    from agent.chat.video_search.tools.registry import build_chat_tools
    from agent.chat.video_search.video_search_canvas import VideoSearchContextCanvas

    canvas = VideoSearchContextCanvas("workspace", ("series",))
    canvas.record_retrieval(VideoChunkSearchResult(
        query="cup", series_ids=["series"],
        agent_reply=AgentReply(message="retrieved", result_count=2),
        hits=[_hit(), _hit("chunk-2")],
    ))
    for reference in ("chunk-1", "chunk-2"):
        canvas.upsert_draft_item(video_chunk_id=reference, start_seconds=11,
                                    end_seconds=19, claim="approved claim")
    canvas.approve_draft()
    tools = _tool_map(build_chat_tools(
        canvas=canvas, control=ExecutionControl(deadline=monotonic() + 30),
        maximum_tool_calls=None, maximum_result_bytes=4096, maximum_catalog_items=1,
    ))
    return canvas, tools


def test_chat_video_batch_merges_continuous_text_and_replays_without_mutating() -> None:
    canvas, tools = _approved_chat_canvas_tools()
    catalog = _invoke_json(tools["get_context_canvas"], {"collection": "approved"})
    original_revision = catalog["revision"]
    assert catalog["next_cursor"]
    canvas.append_markdown("First paragraph.")
    canvas.append_markdown("Second paragraph.")
    assert canvas.revision == original_revision + 2
    invalid_cursor = _invoke_json(tools["get_context_canvas"], {
        "collection": "approved", "cursor": catalog["next_cursor"],
    })
    assert invalid_cursor["error"]["code"] == "INVALID_CATALOG_CURSOR"
    arguments = {
        "batch_id": "continuous-text", "expected_revision": original_revision,
        "operations": [{"operation": "upsert_video_item", "video_chunk_id": "chunk-1",
                        "claim": "approved video"}],
    }
    receipt = _invoke_json(tools["write_context_canvas"], arguments)
    assert receipt["revision"] == original_revision + 3
    assert [block.type for block in canvas.render_blocks()] == ["markdown", "markdown", "video"]
    canvas.append_markdown("Third paragraph.")
    before_replay = canvas.snapshot()
    replay = _invoke_json(tools["write_context_canvas"], arguments)
    assert replay == {**receipt, "replayed": True}
    assert canvas.snapshot() == before_replay
    assert canvas.video_item_count() == 1


@pytest.mark.parametrize("change", ["host_video", "model_video", "retrieval", "draft"])
def test_chat_text_merge_rejects_old_revision_after_another_change(change) -> None:
    from agent.chat.video_search.state import ChatVideoBlock

    canvas, tools = _approved_chat_canvas_tools()
    original_revision = canvas.revision
    canvas.append_markdown("Before concurrent change.")
    if change == "host_video":
        canvas.upsert_video_item(ChatVideoBlock(video_chunk_id="chunk-1", claim="concurrent video"))
    elif change == "model_video":
        receipt = _invoke_json(tools["write_context_canvas"], {
            "batch_id": "concurrent-video", "expected_revision": canvas.revision,
            "operations": [{"operation": "upsert_video_item", "video_chunk_id": "chunk-1",
                            "claim": "concurrent video"}],
        })
        assert "error" not in receipt
    elif change == "retrieval":
        hit = _hit()
        hit.selection_reason.summary = "new retrieval result"
        canvas.record_retrieval(VideoChunkSearchResult(
            query="another cup", series_ids=["series"],
            agent_reply=AgentReply(message="retrieved", result_count=1), hits=[hit],
        ))
    else:
        canvas.upsert_draft_item(video_chunk_id="chunk-1", start_seconds=11,
                                    end_seconds=19, claim="concurrent draft")
    refreshed_revision = canvas.revision
    canvas.append_markdown("After concurrent change.")
    before = canvas.snapshot()
    rejected = _invoke_json(tools["write_context_canvas"], {
        "batch_id": "stale-video", "expected_revision": original_revision,
        "operations": [{"operation": "upsert_video_item", "video_chunk_id": "chunk-2",
                        "claim": "must not overwrite the new snapshot"}],
    })
    assert rejected["error"]["code"] == "STALE_REVISION"
    assert canvas.snapshot() == before
    accepted = _invoke_json(tools["write_context_canvas"], {
        "batch_id": "refreshed-video", "expected_revision": refreshed_revision,
        "operations": [{"operation": "upsert_video_item", "video_chunk_id": "chunk-2",
                        "claim": "based on the refreshed snapshot"}],
    })
    assert "error" not in accepted
    assert canvas.draft_item("chunk-1", approved=True).claim == "approved claim"


def test_markdown_revision_merge_does_not_apply_to_query_writes() -> None:
    from agent.utils.context_canvas import CanvasRevisionConflict

    canvas, _tools = _approved_chat_canvas_tools()
    original_revision = canvas.revision
    canvas.append_markdown("Chat text.")
    before = canvas.snapshot()
    with pytest.raises(CanvasRevisionConflict):
        canvas.write(role="query", batch_id="stale-query", expected_revision=original_revision,
                        operations=[{"operation": "upsert_draft_item", "video_chunk_id": "chunk-1",
                                     "claim": "Query must refresh"}])
    assert canvas.snapshot() == before
