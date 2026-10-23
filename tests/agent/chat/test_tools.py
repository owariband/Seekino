from __future__ import annotations

import json
import logging
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from time import monotonic

import pytest
from langchain_core.messages import ToolMessage
from pydantic import BaseModel

from engine.rag.contracts import (
    AgentReply,
    SelectionReason,
    VideoChunkCandidate,
    VideoChunkEvidence,
    VideoChunkSearchHit,
    VideoChunkSearchResult,
)
from engine.rag.retrieval.contracts import EventIndexNotReadyError, EventSearchResult, FusedEventHit
from engine.rag.retrieval.events import EVENT_RETRIEVAL_STRATEGY_VERSION
from agent.chat.common.errors import AgentModelInvocationError, AgentToolOutputError
from agent.chat.common.state import ExecutionControl, RunInputs
from agent.chat.common.tool import (
    AgentTool, as_langchain_tools, attach_agent_tool_logging, build_agent_tool,
)
from agent.chat.common.tool_result import (
    JsonToolResult, TextToolResult, ToolError,
)
from agent.chat.video_search.context import build_retrieval_checkpoint, retrieval_evidence_text
from agent.chat.video_search.skills import (
    LEXICAL_RETRIEVAL_SKILL_PROMPT,
    QUERY_SKILL_PROMPT, RETRIEVAL_SKILL_PROMPT,
)
from agent.chat.video_search.tools import (
    LexicalRetrieveArguments, RetrieveArguments,
    build_query_tools, build_supervisor_tools,
)
from agent.chat.video_search.tools.shared import _ToolCallBudget
from agent.utils.tool import AgentTool as SharedAgentTool
from agent.utils.errors import AgentToolFeedbackError


def _inputs() -> RunInputs:
    return {
        "conversation_id": "conversation",
        "assistant_message_id": "answer",
        "tenant_id": "workspace",
        "series_ids": ["series"],
        "series_context": [],
        "user_message": "find the broken cup",
        "history": [],
        "requested_top_k": 20,
    }


def _hit(chunk_id: str, *, episode: int = 1, large_text: str = "") -> VideoChunkSearchHit:
    evidence = VideoChunkEvidence(
        series_id="series",
        tenant_id="workspace",
        series_name="Series",
        series_description=None,
        series_status="READY",
        important_extra_data=None,
        character_bible=[],
        video_id=f"video-{episode}",
        episode_order=episode,
        original_filename=f"episode-{episode}.mp4",
        original_source_uri=f"episode-{episode}.mp4",
        inference_proxy_uri=None,
        content_hash=f"hash-{chunk_id}",
        duration_seconds=100,
        upload_status="COMPLETED",
        parse_status="COMPLETED",
        video_chunk_id=chunk_id,
        sequence_no=episode - 1,
        start_seconds=10,
        end_seconds=20,
        analysis_window_start_seconds=0,
        analysis_window_end_seconds=30,
        chunk_media_ref=f"{chunk_id}.mp4",
        analysis_window_media_ref=f"{chunk_id}-window.mp4",
        transcript=[],
        baseline_visual_caption=large_text or "a cup breaks",
        keyframe_uris=[],
        all_time_events=[{"event_type": "DIALOGUE", "start_seconds": 12, "end_seconds": 18, "speaker": "speaker", "text_variants": [{"language": "ja", "role": "ORIGINAL", "text": f"quote-{index}"}]} for index in range(3)],
        all_time_event_text=large_text or "a cup breaks",
        retrieval_text=large_text or "a cup breaks",
        video_compact=large_text or "a cup breaks",
        context_before_compact=large_text,
        context_after_compact=large_text,
        window_summary=large_text or "a cup breaks",
        model_provider="ark",
        model_name="parser",
        pipeline_version="video-parse-v2",
        is_fallback=False,
        parse_provenance=None,
        parsed_at=1_789_724_800,
    )
    return VideoChunkSearchHit(
        candidate=VideoChunkCandidate(
            video_chunk_id=chunk_id,
            fused_rank=episode,
            fused_score=1 / episode,
        ),
        selection_reason=SelectionReason(
            summary=f"matched {chunk_id}",
            matched_evidence=[],
            limitations=[],
        ),
        evidence=evidence,
    )


class Retrieval:
    def __init__(self, hits: list[VideoChunkSearchHit]) -> None:
        self.hits = hits
        self.calls = 0

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
    ) -> VideoChunkSearchResult:
        self.calls += 1
        assert tenant_id == "workspace"
        assert series_ids == ["series"]
        assert lexical_weight + text_weight + vision_weight == pytest.approx(1)
        hits = [item.model_copy(deep=True) for item in self.hits[:top_k]]
        return VideoChunkSearchResult(
            query=query,
            series_ids=series_ids,
            agent_reply=AgentReply(message="retrieved", result_count=len(hits)),
            hits=hits,
        )


def _control() -> ExecutionControl:
    return ExecutionControl(monotonic() + 60)


def _query_tools(
    retrieval: Retrieval,
    *,
    maximum_result_bytes: int = 4_096,
    maximum_read_chars: int = 16_384,
    maximum_catalog_items: int = 20,
    maximum_tool_calls: int | None = 100,
):
    calls = []
    tools = build_query_tools(
        retrieval.search,
        inputs=_inputs(),
        control=_control(),
        calls=calls,
        maximum_top_k=20,
        maximum_tool_calls=maximum_tool_calls,
        maximum_result_bytes=maximum_result_bytes,
        maximum_read_chars=maximum_read_chars,
        maximum_catalog_items=maximum_catalog_items,
        retrieval_channel="lexical",
    )
    return {tool.name: tool for tool in tools}, calls


def _retrieve(tools, *, top_k: int = 20) -> dict:
    return json.loads(tools["retrieve"].invoke({
        "query": "broken cup",
        "top_k": top_k,
        "channel_weights": {
            "lexical": 0,
            "semantic": 0.7,
            "visual": 0.3,
        },
    }))


def _event_result(query: str, *, count: int = 3) -> EventSearchResult:
    return EventSearchResult(
        chunk_result=VideoChunkSearchResult(
            query=query, series_ids=["series"], hits=[_hit("chunk-1")],
            agent_reply=AgentReply(message="matched Events", result_count=1),
        ),
        matches=tuple(FusedEventHit(
            event_id=f"event-{index}", parse_task_id="task", video_chunk_id="chunk-1",
            version=1, timeline_index=index, event_type="DIALOGUE", speaker="speaker",
            lexical_fields=({"field": "dialogue", "raw_text": f"quote-{index}", "language": "ja", "role": "ORIGINAL"},),
            matched_fields=({"field": "dialogue", "raw_text": f"quote-{index}", "language": "ja", "role": "ORIGINAL"},),
            fused_rank=index + 1, fused_score=1 / (index + 1),
            channel_ranks={"lexical": index + 1}, channel_scores={"lexical": 5.0},
            start_ms=12, end_ms=18,
        ) for index in range(count)),
        trace={"strategy_version": EVENT_RETRIEVAL_STRATEGY_VERSION,
               "channels": {"lexical": [{"event_id": "unselected-event", "score": 0.4}]},
               "discarded": [{"event_id": "stale-event", "reason": "source changed"}]},
    )


@pytest.mark.parametrize("builder,role", [(build_query_tools, "query"), (build_supervisor_tools, "supervisor")])
def test_event_search_pages_events_counts_chunks_and_keeps_full_trace(builder, role) -> None:
    received = []
    found = _event_result("quote")
    large_field = found.matches[0].lexical_fields[0].model_copy(update={"raw_text": "long original dialogue " * 2_000})
    found = found.model_copy(update={"matches": (
        found.matches[0].model_copy(update={"lexical_fields": (large_field,), "matched_fields": (large_field,)}),
        *found.matches[1:],
    )})

    def search(query, **options):
        received.append((query, options))
        return found

    calls = []
    tools = {tool.name: tool for tool in builder(
        Retrieval([]).search, inputs=_inputs(), control=_control(), calls=calls,
        maximum_top_k=1, maximum_tool_calls=None, maximum_result_bytes=4_096,
        maximum_catalog_items=1, event_retrieve=search,
    )}
    initial = {"query": "quote", "top_k": 5, "speaker": "speaker", "video_chunk_ids": ["chunk-1"],
               "channel_weights": {"lexical": 0.8, "semantic": 0.2, "visual": 0}}
    page = json.loads(tools["search_video_events"].invoke(initial))
    events = list(page["items"])
    assert page["result_count"] == 1 and page["event_count"] == page["total"] == 3
    assert page["effective_filters"]["event_types"] == ["DIALOGUE"]
    assert page["retrieval_strategy"]["version"] == EVENT_RETRIEVAL_STRATEGY_VERSION
    while page["next_cursor"]:
        raw = tools["search_video_events"].invoke({"cursor": page["next_cursor"]})
        assert len(raw.encode()) <= 4_096
        page = json.loads(raw)
        events.extend(page["items"])
    assert [item["matched_event"]["timeline_index"] for item in events] == [0, 1, 2]
    assert events[0]["matched_event"]["lexical_fields"][0]["role"] == "ORIGINAL"
    assert events[0]["matched_event"]["matched_fields"][0]["language"] == "ja"
    assert len(events[0]["matched_event"]["matched_fields"][0]["text_excerpt"]) <= 160
    assert received == [("quote", {
        "tenant_id": "workspace", "series_ids": ["series"], "top_k": 5,
        "lexical_weight": 0.8, "text_weight": 0.2, "vision_weight": 0,
        "event_types": ["DIALOGUE"], "speaker": "speaker", "video_chunk_ids": ["chunk-1"],
    })]
    assert calls[0].result.hits[0].evidence.video_chunk_id == "chunk-1"
    assert len(calls) == 1 and calls[0].role == role
    assert calls[0].strategy_version == EVENT_RETRIEVAL_STRATEGY_VERSION
    assert calls[0].event_trace == {**found.trace, "matched_events": [item.model_dump(mode="json") for item in found.matches]}
    assert "event_trace" not in calls[0].context()
    feedback = json.loads(tools["search_video_events"].invoke({**initial, "event_types": ["ACTION"]}))
    assert feedback["error"]["code"] == "INVALID_ARGUMENTS"
    assert len(received) == 1
    from gateway.codex.protocol import CodexToolBridge, dynamic_tool_specs
    native_tool = tools["search_video_events"].as_langchain_tool()
    assert dynamic_tool_specs([native_tool])[0]["inputSchema"]["properties"]["event_types"]
    native_result = CodexToolBridge([native_tool]).handle("item/tool/call", {
        "tool": "search_video_events", "callId": "event-native-call", "arguments": initial,
    })
    assert native_result["success"] is True
    assert json.loads(native_result["contentItems"][0]["text"])["event_count"] == 3


def test_event_index_not_ready_is_recoverable_and_chunk_search_remains_available() -> None:
    def unavailable(*_args, **_kwargs):
        raise EventIndexNotReadyError("scope has no Event receipt")

    calls = []
    tools = {tool.name: tool for tool in build_query_tools(
        Retrieval([_hit("chunk-1")]).search, inputs=_inputs(), control=_control(), calls=calls,
        maximum_top_k=20, maximum_tool_calls=None, maximum_result_bytes=4_096,
        maximum_catalog_items=10, retrieval_channel="lexical", event_retrieve=unavailable,
    )}
    feedback = json.loads(tools["search_video_events"].invoke({
        "query": "quote", "top_k": 1, "channel_weights": {"lexical": 1, "semantic": 0, "visual": 0},
    }))
    assert feedback["error"]["code"] == "EVENT_INDEX_NOT_READY"
    assert len(calls) == 1
    assert calls[0].status == "error" and calls[0].error["code"] == "EVENT_INDEX_NOT_READY"
    assert _retrieve(tools)["result_count"] == 1
    assert len(calls) == 2 and calls[1].granularity is None


@pytest.mark.parametrize("corruption", ["scope", "speaker", "time", "strategy", "schema", "missing_event", "negative_index"])
def test_event_results_are_validated_before_recording_evidence(corruption) -> None:
    found = _event_result("quote", count=1)
    if corruption == "scope":
        found.chunk_result.hits[0].evidence.tenant_id = "outside"
    elif corruption == "speaker":
        found = found.model_copy(update={"matches": (found.matches[0].model_copy(update={"speaker": "other"}),)})
    elif corruption == "time":
        found = found.model_copy(update={"matches": (found.matches[0].model_copy(update={"start_ms": 40, "end_ms": 50}),)})
    elif corruption == "strategy":
        found.trace["strategy_version"] = "query-adaptive-three-channel-rrf-v2"
    elif corruption == "missing_event":
        found.chunk_result.hits[0].evidence.all_time_events = []
    elif corruption == "negative_index":
        found = found.model_copy(update={"matches": (found.matches[0].model_copy(update={"timeline_index": -1}),)})
    else:
        found = {"invalid": "event result"}
    calls = []
    tools = build_query_tools(
        Retrieval([]).search, inputs=_inputs(), control=_control(), calls=calls,
        maximum_top_k=20, maximum_tool_calls=None, maximum_result_bytes=4_096,
        maximum_catalog_items=10, event_retrieve=lambda *_args, **_kwargs: found,
    )
    tool = next(tool for tool in tools if tool.name == "search_video_events")
    with pytest.raises(AgentModelInvocationError):
        tool.invoke({"query": "quote", "speaker": "speaker", "top_k": 1,
                     "channel_weights": {"lexical": 1, "semantic": 0, "visual": 0}})
    assert calls == []


def test_role_registries_expose_only_the_exact_tools() -> None:
    inputs = _inputs()
    retrieval = Retrieval([_hit("chunk-1")])
    query_tools = build_query_tools(
        retrieval.search,
        inputs=inputs,
        control=_control(),
        calls=[],
        maximum_top_k=20,
        maximum_tool_calls=20,
        maximum_result_bytes=1_024,
        maximum_catalog_items=20,
    )
    supervisor_tools = build_supervisor_tools(
        retrieval.search,
        inputs=inputs,
        control=_control(),
        calls=[],
        maximum_top_k=20,
        maximum_tool_calls=20,
        maximum_result_bytes=1_024,
        maximum_catalog_items=20,
    )
    assert {tool.name for tool in query_tools} == {"retrieve", "read_retrieval_evidence"}
    assert {tool.name for tool in supervisor_tools} == {"retrieve", "read_retrieval_evidence"}
    assert all(isinstance(tool, SharedAgentTool) for tool in query_tools)
    assert {tool.name for tool in as_langchain_tools(query_tools)} == {
        tool.name for tool in query_tools
    }


def test_tool_results_keep_runtime_status_out_of_model_content() -> None:
    success = JsonToolResult(success=True, payload={"items": []})
    text = TextToolResult(success=True, content="bounded text")
    error = ToolError(
        code="RETRYABLE_FAILURE",
        message="Call could not be completed.",
        retryable=True,
        phase="query",
        retry_hint="Try again.",
    )

    assert json.loads(success.model_content()) == {"items": []}
    assert text.model_content() == "bounded text"
    assert error.as_dict() == {
        "code": "RETRYABLE_FAILURE",
        "message": "Call could not be completed.",
        "retryable": True,
        "phase": "query",
        "retry_hint": "Try again.",
    }
    assert "success" not in success.model_content()


def test_agent_tool_adapter_enforces_the_result_byte_budget() -> None:
    class NoArguments(BaseModel):
        pass

    tool = build_agent_tool(
        func=lambda: TextToolResult(success=True, content="too large"),
        name="bounded",
        description="Return a bounded result.",
        args_schema=NoArguments,
        maximum_result_bytes=4,
    )

    with pytest.raises(AgentToolOutputError, match="exceeds 4 UTF-8 bytes"):
        tool.invoke({})


def test_shared_tool_can_run_when_chat_imports_are_forbidden() -> None:
    subprocess.run([sys.executable, "-B", "-c", """
import importlib.abc
import json
import sys

class NoChatImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "agent.chat" or fullname.startswith("agent.chat."):
            raise AssertionError(f"Shared tools imported {fullname}")

sys.meta_path.insert(0, NoChatImports())
from agent.utils.tool import build_agent_tool
from agent.utils.tool_result import JsonToolResult
from pydantic import BaseModel

class Arguments(BaseModel):
    text: str

tool = build_agent_tool(
    func=lambda text: JsonToolResult(success=True, payload={"text": text}),
    name="shared", description="Return shared text.", args_schema=Arguments,
    maximum_result_bytes=128,
)
assert json.loads(tool.invoke({"text": "通用工具"})) == {"text": "通用工具"}
assert not any(name == "agent.chat" or name.startswith("agent.chat.")
               for name in sys.modules)
"""], check=True)


def test_shared_types_keep_chat_constructor_and_feedback_compatible(caplog) -> None:
    from agent.chat.common import errors as chat_errors, tool_result as chat_results
    from agent.utils import errors as shared_errors, tool_result as shared_results
    from agent.utils.tool import build_agent_tool as build_shared_tool

    for name in ("AgentToolInvocationError", "AgentToolOutputError", "AgentToolFeedbackError"):
        assert getattr(chat_errors, name) is getattr(shared_errors, name)
    for name in ("ToolResult", "JsonToolResult", "TextToolResult", "ToolError"):
        assert getattr(chat_results, name) is getattr(shared_results, name)

    class Arguments(BaseModel):
        query: str

    def feedback(query: str):
        if query == "unknown":
            raise chat_errors.UnknownToolItemError("Missing item.", "Retrieve it first.")
        raise shared_errors.AgentToolFeedbackError("RETRY", "Too large.", "Read less.")

    tool = AgentTool(
        func=feedback, name="compatible", description="Return correctable feedback.",
        args_schema=Arguments, maximum_result_bytes=1024, draft_item_count=lambda: 3,
    )
    for arguments, code in (
        ({"query": "shared"}, "RETRY"), ({"query": "unknown"}, "UNKNOWN_ITEM"),
        ({}, "INVALID_ARGUMENTS"),
    ):
        result = tool.invoke({
            "type": "tool_call", "id": code, "name": tool.name, "args": arguments,
        })
        assert isinstance(result, ToolMessage)
        assert result.status == "error"
        assert result.tool_call_id == code
        payload = json.loads(result.content)
        assert payload["draft_item_count"] == 3
        assert payload["error"]["code"] == code
        assert payload["error"]["retryable"] is True

    caplog.set_level(logging.INFO, logger="agent.chat.common.tool")
    shared = build_shared_tool(
        func=lambda query: shared_results.JsonToolResult(success=True, payload={"content": query}),
        name="shared", description="Return shared text.", args_schema=Arguments,
        maximum_result_bytes=1024,
    )
    attach_agent_tool_logging([shared], role="Shared", inputs=_inputs())
    assert json.loads(shared.invoke({"query": "private evidence"}))["content"] == "private evidence"
    assert "event=agent_tool_started role=Shared conversation_id=conversation message_id=answer" in caplog.text
    assert "event=agent_tool_finished" in caplog.text
    assert '"query_chars":16' in caplog.text
    assert '"content_chars":16' in caplog.text
    assert "private evidence" not in caplog.text


def test_shared_output_helpers_bound_utf8_and_reject_stale_pages() -> None:
    from agent.utils.errors import AgentToolFeedbackError, AgentToolInvocationError
    from agent.utils.tool_output import page_items, range_text

    content = "杯" * 100
    range_options = dict(
        resource_id="evidence", section="text", start=0,
        maximum_bytes=180, maximum_chars=100,
    )
    with pytest.raises(AgentToolFeedbackError) as captured:
        range_text(content, end=len(content), **range_options)
    feedback = captured.value
    assert feedback.code == "RANGE_TOO_LARGE"
    assert 0 < feedback.suggested_end < len(content)
    assert f"char_end={feedback.suggested_end}" in feedback.retry_hint
    assert "char_start set to the returned end" in feedback.retry_hint
    raw = range_text(content, end=feedback.suggested_end, **range_options)
    payload = json.loads(raw)
    assert payload["content"] == content[:feedback.suggested_end]
    assert payload["end"] == feedback.suggested_end
    assert payload["total_chars"] == len(content)
    assert len(raw) < len(raw.encode("utf-8")) <= 180

    items = [{"text": "杯" * 20}, {"text": "尾页"}]
    page_options = dict(
        namespace="shared", resource_id="catalog", maximum_items=1, maximum_bytes=512,
    )
    first_raw = page_items(items, cursor=None, **page_options)
    first = json.loads(first_raw)
    assert first["items"] == items[:1]
    assert first["total"] == 2
    assert len(first_raw.encode("utf-8")) <= 512
    second = json.loads(page_items(items, cursor=first["next_cursor"], **page_options))
    assert second["items"] == items[1:]
    assert second["next_cursor"] is None
    with pytest.raises(AgentToolInvocationError, match="Stale or mismatched"):
        page_items([*items, {"text": "changed"}], cursor=first["next_cursor"], **page_options)


def test_agent_skills_are_explicitly_work_agnostic() -> None:
    skills = (
        RETRIEVAL_SKILL_PROMPT,
        LEXICAL_RETRIEVAL_SKILL_PROMPT,
        QUERY_SKILL_PROMPT,
    )

    assert all("作品无关" in skill for skill in skills)
    assert all("不得写入或依赖任何具体动画" in skill for skill in skills)
    assert all("丰川祥子" not in skill for skill in skills)
    for skill in (QUERY_SKILL_PROMPT,):
        assert skill.startswith("媒体时间合同：")
        assert "*_seconds / *Seconds" in skill
        assert "整数毫秒" in skill
        assert "inspect_video_frames 使用毫秒" in skill
        assert "必须除以 1000" in skill
        assert "ID 和游标续读" in skill


def test_retrieve_keeps_transcript_and_lexical_weight_contracts_separate() -> None:
    seen: list[tuple[str, float]] = []

    def search(query, *, transcript_weight=None, lexical_weight=None, **kwargs):
        del kwargs
        if transcript_weight is not None:
            seen.append(("transcript", transcript_weight))
        if lexical_weight is not None:
            seen.append(("lexical", lexical_weight))
        return VideoChunkSearchResult(
            query=query,
            series_ids=["series"],
            agent_reply=AgentReply(message="none", result_count=0),
            hits=[],
        )

    common = dict(
        inputs=_inputs(),
        control=_control(),
        calls=[],
        maximum_top_k=20,
        maximum_tool_calls=10,
        maximum_result_bytes=1_024,
        maximum_catalog_items=20,
    )
    transcript = build_query_tools(search, **common)[0]
    assert transcript.args_schema is RetrieveArguments
    json.loads(transcript.invoke({
        "query": "spoken phrase",
        "top_k": 5,
        "channel_weights": {"transcript": 1, "semantic": 0, "visual": 0},
    }))

    lexical = build_query_tools(search, retrieval_channel="lexical", **common)[0]
    assert lexical.args_schema is LexicalRetrieveArguments
    json.loads(lexical.invoke({
        "query": "structured terms",
        "top_k": 5,
        "channel_weights": {"lexical": 1, "semantic": 0, "visual": 0},
    }))

    assert seen == [("transcript", 1.0), ("lexical", 1.0)]


@pytest.mark.parametrize("build_tools", [build_query_tools, build_supervisor_tools])
@pytest.mark.parametrize("channel", ["transcript", "lexical"])
@pytest.mark.parametrize("requested,available", [(8, 8), (101, 101), (8, 2), (8, 0)])
def test_retrieve_honors_agent_quantity_and_pages_all_hits(
    build_tools, channel, requested, available,
) -> None:
    inputs = {**_inputs(), "requested_top_k": 3}
    retrieval = Retrieval([_hit(f"chunk-{index}") for index in range(available)])
    searched_top_k = []

    def search(query, *, top_k, **kwargs):
        searched_top_k.append(top_k)
        if channel == "transcript":
            kwargs["lexical_weight"] = kwargs.pop("transcript_weight")
        return retrieval.search(query, top_k=top_k, **kwargs)

    calls = []
    maximum_result_bytes = 4_096
    tool = build_tools(
        search, inputs=inputs, control=_control(), calls=calls,
        maximum_top_k=2, maximum_tool_calls=None,
        maximum_result_bytes=maximum_result_bytes, maximum_catalog_items=3,
        retrieval_channel=channel,
    )[0]
    raw = tool.invoke({
        "query": "broken cup", "top_k": requested,
        "channel_weights": {channel: 0, "semantic": 0.7, "visual": 0.3},
    })
    found_ids = []
    while True:
        assert len(raw.encode("utf-8")) <= maximum_result_bytes
        page = json.loads(raw)
        assert page["requested_top_k"] == requested
        assert page["result_count"] == page["total"] == available
        assert page["warnings"] == []
        assert page["partial"] is False
        assert len(page["items"]) <= 3
        assert all("evidence" in item for item in page["items"])
        found_ids.extend(item["video_chunk_id"] for item in page["items"])
        if page["next_cursor"] is None:
            break
        raw = tool.invoke({"cursor": page["next_cursor"]})

    assert found_ids == [f"chunk-{index}" for index in range(available)]
    assert searched_top_k == [requested]
    assert retrieval.calls == len(calls) == 1
    assert calls[0].top_k == requested
    if available:
        assert calls[0].result.hits[-1].evidence.video_chunk_id == found_ids[-1]


@pytest.mark.parametrize("schema,channel", [
    (RetrieveArguments, "transcript"), (LexicalRetrieveArguments, "lexical"),
])
@pytest.mark.parametrize("top_k", [0, -1, True, 1.5, "8"])
def test_retrieve_quantity_must_be_a_positive_integer(schema, channel, top_k) -> None:
    with pytest.raises(ValueError):
        schema.model_validate({
            "query": "broken cup", "top_k": top_k,
            "channel_weights": {channel: 1, "semantic": 0, "visual": 0},
        })


def test_agent_tool_trace_records_safe_arguments_results_and_errors(caplog) -> None:
    caplog.set_level(logging.INFO, logger="agent.chat.common.tool")
    tools, _calls = _query_tools(
        Retrieval([_hit("chunk-1", large_text="private evidence " * 100)]),
    )
    attach_agent_tool_logging(list(tools.values()), role="Query", inputs=_inputs())

    _retrieve(tools, top_k=1)
    tools["read_retrieval_evidence"].invoke({"video_chunk_id": "chunk-1"})
    feedback = json.loads(tools["read_retrieval_evidence"].invoke({"video_chunk_id": "unknown-chunk"}))
    assert feedback["error"]["code"] == "UNKNOWN_ITEM"

    assert "event=agent_tool_started role=Query conversation_id=conversation message_id=answer" in caplog.text
    assert '"top_k":1' in caplog.text
    assert '"item_video_chunk_ids":["chunk-1"]' in caplog.text
    assert '"video_chunk_id":"chunk-1"' in caplog.text
    assert "tool=read_retrieval_evidence" in caplog.text
    assert "status=error" in caplog.text
    assert '"error_code":"UNKNOWN_ITEM"' in caplog.text
    assert "broken cup" not in caplog.text
    assert "private claim text" not in caplog.text
    assert "private evidence" not in caplog.text


@pytest.mark.parametrize("outcome", ["success", "feedback", "invalid", "exception"])
def test_tool_activity_reports_real_calls_and_handled_failures(outcome) -> None:
    class Arguments(BaseModel):
        query: str

    executed, activity = [], []

    def search(query):
        executed.append(query)
        if outcome == "exception":
            raise RuntimeError("search unavailable")
        return JsonToolResult(
            success=outcome != "feedback", payload={"message": "private tool output"},
        )

    tool = build_agent_tool(
        func=search, name="search_web", description="Search external material.",
        args_schema=Arguments, maximum_result_bytes=1024,
    )
    attach_agent_tool_logging(
        [tool], role="Supervisor", inputs=_inputs(),
        on_activity=lambda *event: activity.append(event),
    )
    arguments = {} if outcome == "invalid" else {"query": "the actual search"}
    invocation = {"type": "tool_call", "id": "provider-call", "name": tool.name, "args": arguments}
    if outcome == "exception":
        with pytest.raises(RuntimeError, match="search unavailable"):
            tool.invoke(invocation)
    else:
        receipt = tool.invoke(invocation)
        assert receipt.status == ("success" if outcome == "success" else "error")

    assert len(activity) == 2
    call_id, name, status, inputs = activity[0]
    assert isinstance(call_id, str) and call_id
    assert (name, status, inputs) == ("search_web", "inProgress", arguments)
    assert activity[1] == (
        call_id, "search_web", "completed" if outcome == "success" else "failed", None,
    )
    assert executed == ([] if outcome == "invalid" else ["the actual search"])
    assert "private tool output" not in repr(activity)


def test_tool_activity_keeps_parallel_calls_separate() -> None:
    class Arguments(BaseModel):
        query: str

    entered = Barrier(2)
    activity = []

    def publish(*event):
        activity.append(event)
        if event[2] == "inProgress":
            entered.wait(timeout=5)

    tool = build_agent_tool(
        func=lambda query: JsonToolResult(success=True, payload={"query": query}),
        name="search_web", description="Search external material.",
        args_schema=Arguments, maximum_result_bytes=1024,
    )
    attach_agent_tool_logging(
        [tool], role="Supervisor", inputs=_inputs(), on_activity=publish,
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = list(executor.map(lambda query: tool.invoke({"query": query}), ("first", "second")))

    assert len(receipts) == 2
    started = {event[0]: event for event in activity if event[2] == "inProgress"}
    completed = {event[0]: event for event in activity if event[2] == "completed"}
    assert len(started) == 2 and started.keys() == completed.keys()
    assert {event[3]["query"] for event in started.values()} == {"first", "second"}
    assert all(event[3] is None for event in completed.values())


@pytest.mark.parametrize("failed_stage", ["start", "end"])
@pytest.mark.parametrize("success", [False, True])
def test_tool_activity_publish_errors_preserve_business_results(caplog, failed_stage, success) -> None:
    class Arguments(BaseModel):
        query: str

    executed, activity = [], []
    payload = {"message": "the original business result"}
    if not success:
        payload = {"error": {"code": "BUSINESS_UNAVAILABLE", "message": "the original business error"}}

    def publish(_call_id, _tool, status, _arguments):
        activity.append(status)
        if (status == "inProgress") == (failed_stage == "start"):
            raise RuntimeError("activity publication failed")

    def search(query):
        executed.append(query)
        return JsonToolResult(success=success, payload=payload)

    tool = build_agent_tool(
        func=search, name="search_web", description="Search external material.",
        args_schema=Arguments, maximum_result_bytes=1024,
    )
    attach_agent_tool_logging([tool], role="Supervisor", inputs=_inputs(), on_activity=publish)

    receipt = tool.invoke({
        "type": "tool_call", "id": "provider-call", "name": tool.name,
        "args": {"query": "the actual search"},
    })

    assert executed == ["the actual search"]
    assert receipt.tool_call_id == "provider-call"
    assert receipt.status == ("success" if success else "error")
    assert json.loads(receipt.content) == payload
    assert activity == ["inProgress", "completed" if success else "failed"]
    assert any(record.name == "agent.chat.common.tool" and record.levelno >= logging.WARNING
               and record.exc_info and record.exc_info[0] is RuntimeError
               and "search_web" in record.getMessage()
               for record in caplog.records)


def test_correctable_tool_error_is_returned_to_agent_with_error_status() -> None:
    tools, _calls = _query_tools(
        Retrieval([]),
    )
    internal_result = tools["read_retrieval_evidence"].execute(
        video_chunk_id="missing",
    )
    result = tools["read_retrieval_evidence"].invoke({
        "type": "tool_call",
        "name": "read_retrieval_evidence",
        "id": "call-1",
        "args": {"video_chunk_id": "missing"},
    })

    assert internal_result.success is False
    assert json.loads(internal_result.model_content())["error"]["code"] == "UNKNOWN_ITEM"
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert result.tool_call_id == "call-1"
    assert json.loads(result.content)["error"]["code"] == "UNKNOWN_ITEM"


def test_success_and_validation_results_preserve_tool_call_id_and_status() -> None:
    tools, _calls = _query_tools(
        Retrieval([_hit("chunk-1")]),
    )
    success = tools["retrieve"].invoke({
        "type": "tool_call",
        "name": "retrieve",
        "id": "call-success",
        "args": {
            "query": "broken cup",
            "top_k": 1,
            "channel_weights": {
                "lexical": 0,
                "semantic": 0.7,
                "visual": 0.3,
            },
        },
    })
    invalid = tools["retrieve"].invoke({
        "type": "tool_call",
        "name": "retrieve",
        "id": "call-invalid",
        "args": {"query": "missing required arguments"},
    })

    assert isinstance(success, ToolMessage)
    assert success.status == "success"
    assert success.tool_call_id == "call-success"
    assert json.loads(success.content)["result_count"] == 1
    assert isinstance(invalid, ToolMessage)
    assert invalid.status == "error"
    assert invalid.tool_call_id == "call-invalid"
    invalid_payload = json.loads(invalid.content)
    assert invalid_payload["error"]["code"] == "INVALID_ARGUMENTS"
    assert invalid_payload["error"]["retryable"] is True


def test_shared_role_tool_budget_counts_every_tool() -> None:
    retrieval = Retrieval([_hit("chunk-1")])
    tools, _calls = _query_tools(
        retrieval, maximum_tool_calls=1,
    )

    _retrieve(tools, top_k=1)
    with pytest.raises(AgentModelInvocationError, match="Tool-call budget exceeded"):
        tools["read_retrieval_evidence"].invoke({"video_chunk_id": "chunk-1"})


def test_atomic_tools_share_the_injected_budget_across_supervisor_and_query() -> None:
    retrieval = Retrieval([_hit("chunk-1")])
    budget = _ToolCallBudget(2, "Supervisor")
    calls, event_calls = [], []

    def events(query, **_options):
        event_calls.append(query)
        return _event_result(query)

    tools = {tool.name: tool for tool in build_supervisor_tools(
        retrieval.search, inputs=_inputs(), control=_control(), calls=calls,
        maximum_top_k=20, maximum_tool_calls=1, maximum_result_bytes=4_096,
        maximum_catalog_items=20, retrieval_channel="lexical", event_retrieve=events,
        budget=budget,
    )}
    assert "write_context_canvas" not in tools
    _retrieve(tools, top_k=1)
    assert calls[0].role == "supervisor"
    query_tools = {tool.name: tool for tool in build_query_tools(
        retrieval.search, inputs=_inputs(), control=_control(), calls=calls,
        maximum_top_k=20, maximum_tool_calls=1, maximum_result_bytes=4_096,
        maximum_catalog_items=20, budget=budget,
    )}
    assert "error" not in json.loads(query_tools["read_retrieval_evidence"].invoke({"video_chunk_id": "chunk-1"}))
    with pytest.raises(AgentModelInvocationError, match="Supervisor Agent Tool-call budget"):
        tools["search_video_events"].invoke({
            "query": "quote", "top_k": 1,
            "channel_weights": {"lexical": 1, "semantic": 0, "visual": 0},
        })
    assert event_calls == []
    assert budget.used == 3


def test_tool_budget_can_be_disabled() -> None:
    retrieval = Retrieval([_hit("chunk-1")])
    tools, _calls = _query_tools(retrieval, maximum_tool_calls=None)

    _retrieve(tools, top_k=1)
    for _ in range(101):
        response = tools["read_retrieval_evidence"].invoke({
            "video_chunk_id": "chunk-1",
        })
        assert json.loads(response)["video_chunk_id"] == "chunk-1"




def test_direct_retrieval_returns_source_evidence_and_full_unicode_continuation():
    hit = _hit("chunk-1", large_text="真实对白\n\"与动作\"" * 300)
    tools, calls = _query_tools(Retrieval([hit]), maximum_result_bytes=4096, maximum_read_chars=211, maximum_tool_calls=None)
    page = _retrieve(tools, top_k=1)
    assert page["items"][0]["evidence_truncated"]
    assert "quote-0" in page["items"][0]["evidence"]
    parts, cursor = [], None
    while True:
        raw = tools["read_retrieval_evidence"].invoke({"video_chunk_id": "chunk-1", "cursor": cursor})
        assert len(raw.encode()) <= 4096
        page = json.loads(raw)
        assert all(len(item["text"]) <= 211 for item in page["items"])
        parts.extend(item["text"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert "".join(parts) == retrieval_evidence_text(hit)
    assert len(calls) == 1


def test_evidence_cursor_cannot_cross_id_or_invocation():
    tools, _ = _query_tools(Retrieval([_hit("chunk-1", large_text="a" * 10000), _hit("chunk-2")]))
    _retrieve(tools)
    page = json.loads(tools["read_retrieval_evidence"].invoke({"video_chunk_id": "chunk-1"}))
    bad = json.loads(tools["read_retrieval_evidence"].invoke({"video_chunk_id": "chunk-2", "cursor": page["next_cursor"]}))
    assert bad["error"]["code"] == "INVALID_EVIDENCE_CURSOR"
    other, _ = _query_tools(Retrieval([]))
    missing = json.loads(other["read_retrieval_evidence"].invoke({"video_chunk_id": "chunk-1", "cursor": page["next_cursor"]}))
    assert missing["error"]["code"] == "UNKNOWN_ITEM"


def test_conflicting_retrieval_cannot_replace_accepted_source_evidence():
    retrieval = Retrieval([_hit("chunk-1")])
    tools, calls = _query_tools(retrieval)
    _retrieve(tools)
    original = calls[0].result.hits[0].evidence.model_copy(deep=True)
    retrieval.hits[0].evidence.window_summary = "conflicting version"
    with pytest.raises(AgentModelInvocationError, match="source evidence changed"):
        _retrieve(tools)
    assert len(calls) == 1 and calls[0].result.hits[0].evidence == original


def test_retrieval_checkpoint_keeps_bounded_handles_without_copying_evidence():
    tools, calls = _query_tools(Retrieval([_hit(f"chunk-{index}", large_text="private evidence") for index in range(205)]), maximum_tool_calls=None)
    _retrieve(tools, top_k=205)
    checkpoint = build_retrieval_checkpoint(calls, [{"inspection_id": "frame-1", "video_chunk_id": "chunk-1", "images": ["huge-image"]}])
    assert len(checkpoint["retrieved_video_chunk_ids"]) == 200
    assert checkpoint["omitted_video_chunk_count"] == 5
    assert "private evidence" not in json.dumps(checkpoint)
    assert "huge-image" not in json.dumps(checkpoint)
