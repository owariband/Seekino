from __future__ import annotations

import json
import logging
import subprocess
import sys
from time import monotonic
from uuid import uuid4

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
from agent.chat.common.errors import AgentModelInvocationError, AgentToolOutputError
from agent.chat.common.state import ExecutionControl, RunInputs
from agent.chat.common.tool import (
    AgentTool, as_langchain_tools, attach_agent_tool_logging, build_agent_tool,
)
from agent.chat.common.tool_result import (
    JsonToolResult, TextToolResult, ToolError,
)
from agent.chat.video_search.context import build_canvas_checkpoint
from agent.chat.video_search.state import DraftItem
from agent.chat.video_search.skills import (
    CHAT_SKILL_PROMPT, EVALUATOR_SKILL_PROMPT, LEXICAL_RETRIEVAL_SKILL_PROMPT,
    QUERY_SKILL_PROMPT, RETRIEVAL_SKILL_PROMPT,
)
from agent.chat.video_search.tools import (
    DraftReadAudit, LexicalRetrieveArguments, RetrieveArguments,
    build_chat_tools, build_evaluator_tools, build_query_tools,
)
from agent.chat.video_search.video_search_canvas import VideoSearchContextCanvas
from agent.utils.tool import AgentTool as SharedAgentTool
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


def _inputs() -> RunInputs:
    return {
        "conversation_id": "conversation",
        "assistant_message_id": "answer",
        "workspace_id": "workspace",
        "series_ids": ["series"],
        "series_context": [],
        "user_message": "find the broken cup",
        "history": [],
        "requested_top_k": 20,
    }


def _hit(chunk_id: str, *, episode: int = 1, large_text: str = "") -> VideoChunkSearchHit:
    evidence = VideoChunkEvidence(
        series_id="series",
        workspace_id="workspace",
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
        all_time_events=[],
        all_time_event_text=large_text or "a cup breaks",
        retrieval_text=large_text or "a cup breaks",
        video_compact=large_text or "a cup breaks",
        context_before_compact=large_text,
        context_after_compact=large_text,
        window_summary=large_text or "a cup breaks",
        model_provider="ark",
        model_name="parser",
        pipeline_version="v1",
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
        workspace_id,
        series_ids,
        top_k,
        lexical_weight,
        text_weight,
        vision_weight,
    ) -> VideoChunkSearchResult:
        self.calls += 1
        assert workspace_id == "workspace"
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
    canvas: VideoSearchContextCanvas,
    *,
    maximum_result_bytes: int = 1_024,
    maximum_read_chars: int = 16_384,
    maximum_catalog_items: int = 20,
    maximum_tool_calls: int | None = 100,
):
    calls = []
    tools = build_query_tools(
        retrieval.search,
        canvas=canvas,
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


def _read_all(tool, video_chunk_id: str, maximum_result_bytes: int) -> dict:
    overview = json.loads(tool.invoke({"video_chunk_id": video_chunk_id}))
    detail: dict = {}
    for section in overview["sections"]:
        name, total = section["name"], section["total_chars"]
        start, parts = 0, []
        while start < total:
            raw = tool.invoke({
                "video_chunk_id": video_chunk_id,
                "section": name,
                "start": start,
                "end": total,
            })
            assert len(raw.encode("utf-8")) <= maximum_result_bytes
            page = json.loads(raw)
            if "error" in page:
                assert page["error"]["code"] == "RANGE_TOO_LARGE"
                end = page["error"]["suggested_end"]
                page = json.loads(tool.invoke({
                    "video_chunk_id": video_chunk_id,
                    "section": name,
                    "start": start,
                    "end": end,
                }))
            parts.append(page["content"])
            start = page["end"]
        value = json.loads("".join(parts))
        if name.startswith("evidence."):
            detail.setdefault("evidence", {}).update(value)
        else:
            detail[name] = value
    return detail


def test_role_registries_expose_only_the_exact_tools() -> None:
    inputs = _inputs()
    canvas = VideoSearchContextCanvas("workspace", ("series",))
    retrieval = Retrieval([_hit("chunk-1")])
    query_tools = build_query_tools(
        retrieval.search,
        canvas=canvas,
        inputs=inputs,
        control=_control(),
        calls=[],
        maximum_top_k=20,
        maximum_tool_calls=20,
        maximum_result_bytes=1_024,
        maximum_catalog_items=20,
    )
    evaluator_tools = build_evaluator_tools(
        retrieval.search,
        canvas=canvas,
        inputs=inputs,
        control=_control(),
        calls=[],
        maximum_top_k=20,
        maximum_tool_calls=20,
        maximum_result_bytes=1_024,
        maximum_catalog_items=20,
    )
    canvas.approve_draft()
    chat_tools = build_chat_tools(
        canvas=canvas,
        control=_control(),
        maximum_tool_calls=20,
        maximum_result_bytes=1_024,
        maximum_catalog_items=20,
    )

    assert {tool.name for tool in query_tools} == {"retrieve", "get_context_canvas", "write_context_canvas"}
    assert {tool.name for tool in evaluator_tools} == {"retrieve", "get_context_canvas"}
    assert {tool.name for tool in chat_tools} == {"get_context_canvas", "write_context_canvas"}
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
    assert f"end<={feedback.suggested_end}" in feedback.retry_hint
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
        EVALUATOR_SKILL_PROMPT,
        CHAT_SKILL_PROMPT,
    )

    assert all("作品无关" in skill for skill in skills)
    assert all("不得写入或依赖任何具体动画" in skill for skill in skills)
    assert all("丰川祥子" not in skill for skill in skills)
    for skill in (QUERY_SKILL_PROMPT, EVALUATOR_SKILL_PROMPT, CHAT_SKILL_PROMPT):
        assert skill.startswith("媒体时间合同：")
        assert "*_seconds / *Seconds" in skill
        assert "整数毫秒" in skill
        assert "保留原始毫秒值" in skill
        assert "必须除以 1000" in skill
        assert "必须拒绝把原始毫秒数直接标为秒" in skill
        assert "start/end 是字符偏移" in skill


def test_retrieve_tool_keeps_v1_transcript_and_v2_lexical_contracts_separate() -> None:
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
        canvas=VideoSearchContextCanvas("workspace", ("series",)),
        inputs=_inputs(),
        control=_control(),
        calls=[],
        maximum_top_k=20,
        maximum_tool_calls=10,
        maximum_result_bytes=1_024,
        maximum_catalog_items=20,
    )
    v1 = build_query_tools(search, **common)[0]
    assert v1.args_schema is RetrieveArguments
    json.loads(v1.invoke({
        "query": "spoken phrase",
        "top_k": 5,
        "channel_weights": {"transcript": 1, "semantic": 0, "visual": 0},
    }))

    v2 = build_query_tools(search, retrieval_channel="lexical", **common)[0]
    assert v2.args_schema is LexicalRetrieveArguments
    json.loads(v2.invoke({
        "query": "structured terms",
        "top_k": 5,
        "channel_weights": {"lexical": 1, "semantic": 0, "visual": 0},
    }))

    assert seen == [("transcript", 1.0), ("lexical", 1.0)]


def test_agent_tool_trace_records_safe_arguments_results_and_errors(caplog) -> None:
    caplog.set_level(logging.INFO, logger="agent.chat.common.tool")
    tools, _calls = _query_tools(
        Retrieval([_hit("chunk-1", large_text="private evidence " * 100)]),
        VideoSearchContextCanvas("workspace", ("series",)),
    )
    attach_agent_tool_logging(list(tools.values()), role="Query", inputs=_inputs())

    _retrieve(tools, top_k=1)
    _reader(tools, "retrieval").invoke({
        "video_chunk_id": "chunk-1", "section": "evidence.text", "end": 10,
    })
    _writer(tools, "upsert_draft_item").invoke({
        "video_chunk_id": "chunk-1",
        "start_seconds": 11,
        "end_seconds": 19,
        "claim": "private claim text",
    })
    feedback = json.loads(_writer(tools, "upsert_draft_item").invoke({
        "video_chunk_id": "unknown-chunk",
        "start_seconds": 11,
        "end_seconds": 19,
        "claim": "private claim text",
    }))
    assert feedback["error"]["code"] == "UNKNOWN_ITEM"

    assert "event=agent_tool_started role=Query conversation_id=conversation message_id=answer" in caplog.text
    assert '"top_k":1' in caplog.text
    assert '"item_video_chunk_ids":["chunk-1"]' in caplog.text
    assert '"content_chars":' in caplog.text
    assert "tool=write_context_canvas" in caplog.text
    assert "status=error" in caplog.text
    assert '"error_code":"UNKNOWN_ITEM"' in caplog.text
    assert "broken cup" not in caplog.text
    assert "private claim text" not in caplog.text
    assert "private evidence" not in caplog.text


def test_retrieve_returns_catalog_and_read_trieve_item_ranges_full_evidence() -> None:
    maximum_result_bytes = 700
    lexical_hit = _hit("chunk-1", episode=1, large_text="杯子碎裂。" * 500)
    lexical_hit.candidate.lexical_rank = 3
    retrieval = Retrieval([
        lexical_hit,
        _hit("chunk-2", episode=2, large_text="人物离开。" * 500),
    ])
    canvas = VideoSearchContextCanvas("workspace", ("series",))
    tools, calls = _query_tools(
        retrieval,
        canvas,
        maximum_result_bytes=maximum_result_bytes,
        maximum_catalog_items=1,
        maximum_tool_calls=1_000,
    )

    first = _retrieve(tools, top_k=2)

    assert retrieval.calls == 1
    assert len(calls) == 1
    assert first["total"] == 2
    assert first["draft_item_count"] == 0
    assert len(first["items"]) == 1
    assert first["items"][0]["video_chunk_id"] == "chunk-1"
    assert first["items"][0]["lexical_rank"] == 3
    assert first["items"][0]["transcript_rank"] is None
    assert "杯子碎裂" not in json.dumps(first, ensure_ascii=False)
    assert "evidence" not in first["items"][0]
    assert first["next_cursor"]

    inserted = json.loads(_writer(tools, "upsert_draft_item").invoke({
        "video_chunk_id": "chunk-1",
        "start_seconds": 11,
        "end_seconds": 19,
        "claim": "first claim",
    }))
    assert inserted["draft_item_count"] == 1

    second_raw = tools["retrieve"].invoke({"cursor": first["next_cursor"]})
    second = json.loads(second_raw)
    assert len(second_raw.encode("utf-8")) <= maximum_result_bytes
    assert retrieval.calls == 1
    assert [item["video_chunk_id"] for item in second["items"]] == ["chunk-2"]
    assert second["draft_item_count"] == 1
    assert second["next_cursor"] is None

    invalid_cursor = json.loads(tools["retrieve"].invoke({"cursor": "invalid"}))
    assert invalid_cursor["error"]["code"] == "INVALID_CATALOG_CURSOR"
    assert invalid_cursor["draft_item_count"] == 1

    detail = _read_all(
        _reader(tools, "retrieval"), "chunk-1", maximum_result_bytes,
    )
    assert detail["candidate"]["videoChunkId"] == "chunk-1"
    assert detail["evidence"]["videoCompact"] == "杯子碎裂。" * 500


def test_read_range_reports_oversize_and_retries_with_smaller_range() -> None:
    maximum_result_bytes = 700
    retrieval = Retrieval([_hit("chunk-1", large_text="杯子碎裂。" * 500)])
    tools, _calls = _query_tools(
        retrieval, VideoSearchContextCanvas("workspace", ("series",)),
        maximum_result_bytes=maximum_result_bytes,
    )
    _retrieve(tools, top_k=1)
    tool = _reader(tools, "retrieval")
    overview = json.loads(tool.invoke({"video_chunk_id": "chunk-1"}))
    total = next(
        section["total_chars"] for section in overview["sections"]
        if section["name"] == "evidence.text"
    )

    oversized = json.loads(tool.invoke({
        "video_chunk_id": "chunk-1", "section": "evidence.text",
        "start": 0, "end": total,
    }))
    assert "ok" not in oversized
    assert oversized["error"]["code"] == "RANGE_TOO_LARGE"
    suggested_end = oversized["error"]["suggested_end"]
    assert 0 < suggested_end < total
    assert len(json.dumps(oversized, ensure_ascii=False).encode("utf-8")) <= maximum_result_bytes

    smaller_raw = tool.invoke({
        "video_chunk_id": "chunk-1", "section": "evidence.text",
        "start": 0, "end": suggested_end,
    })
    smaller = json.loads(smaller_raw)
    assert smaller["end"] == suggested_end
    assert smaller["total_chars"] == total
    assert len(smaller["content"]) == suggested_end
    assert len(smaller_raw.encode("utf-8")) <= maximum_result_bytes

    empty_tail = json.loads(tool.invoke({
        "video_chunk_id": "chunk-1", "section": "evidence.text",
        "start": total, "end": total,
    }))
    assert empty_tail["error"]["code"] == "INVALID_RANGE"


def test_read_tools_enforce_character_limit_with_retryable_end() -> None:
    retrieval = Retrieval([_hit("chunk-1", large_text="detail" * 500)])
    canvas = VideoSearchContextCanvas("workspace", ("series",))
    tools, _calls = _query_tools(
        retrieval, canvas, maximum_result_bytes=4_096,
        maximum_read_chars=80,
    )
    _retrieve(tools, top_k=1)
    _writer(tools, "upsert_draft_item").invoke({
        "video_chunk_id": "chunk-1", "start_seconds": 11,
        "end_seconds": 19, "claim": "draft claim",
    })

    for collection in ("retrieval", "draft"):
        tool = _reader(tools, collection)
        too_large = json.loads(tool.invoke({
            "video_chunk_id": "chunk-1", "section": "evidence.text",
            "start": 0, "end": 100,
        }))
        assert too_large["error"]["code"] == "RANGE_TOO_LARGE"
        assert too_large["error"]["suggested_end"] == 80
        smaller = json.loads(tool.invoke({
            "video_chunk_id": "chunk-1", "section": "evidence.text",
            "start": 0, "end": 80,
        }))
        assert smaller["end"] == 80
        assert len(smaller["content"]) == 80


def test_read_range_uses_smaller_of_character_and_byte_limits() -> None:
    retrieval = Retrieval([_hit("chunk-1", large_text="杯子碎裂。" * 500)])
    tools, _calls = _query_tools(
        retrieval, VideoSearchContextCanvas("workspace", ("series",)),
        maximum_result_bytes=700, maximum_read_chars=300,
    )
    _retrieve(tools, top_k=1)
    too_large = json.loads(_reader(tools, "retrieval").invoke({
        "video_chunk_id": "chunk-1", "section": "evidence.text",
        "start": 0, "end": 500,
    }))
    suggested_end = too_large["error"]["suggested_end"]
    assert too_large["error"]["code"] == "RANGE_TOO_LARGE"
    assert 0 < suggested_end < 300
    smaller = json.loads(_reader(tools, "retrieval").invoke({
        "video_chunk_id": "chunk-1", "section": "evidence.text",
        "start": 0, "end": suggested_end,
    }))
    assert smaller["end"] == suggested_end


def test_read_range_uses_first_snapshot_after_same_id_is_retrieved_again() -> None:
    retrieval = Retrieval([_hit("chunk-1", large_text="first evidence")])
    canvas = VideoSearchContextCanvas("workspace", ("series",))
    tools, _calls = _query_tools(retrieval, canvas)
    _retrieve(tools, top_k=1)
    first = json.loads(_reader(tools, "retrieval").invoke({
        "video_chunk_id": "chunk-1", "section": "evidence.text",
    }))

    changed = _hit("chunk-1", large_text="later evidence")
    changed.candidate.fused_rank = 9
    changed.selection_reason.summary = "later search ranking"
    retrieval.hits = [changed]
    _retrieve(tools, top_k=1)
    repeated = json.loads(_reader(tools, "retrieval").invoke({
        "video_chunk_id": "chunk-1", "section": "evidence.text",
        "start": first["start"], "end": first["end"],
    }))

    assert repeated["content"] == first["content"]
    assert repeated["revision"] > first["revision"]
    assert canvas.retrieval_item("chunk-1").candidate.fused_rank == 9


def test_correctable_tool_error_is_returned_to_agent_with_error_status() -> None:
    tools, _calls = _query_tools(
        Retrieval([]), VideoSearchContextCanvas("workspace", ("series",)),
    )
    internal_result = _reader(tools, "retrieval").execute(
        video_chunk_id="missing",
    )
    result = _reader(tools, "retrieval").invoke({
        "type": "tool_call",
        "name": "read_trieve_item",
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
        VideoSearchContextCanvas("workspace", ("series",)),
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


def test_draft_audit_requires_full_coverage_of_every_section() -> None:
    retrieval = Retrieval([_hit("chunk-1", large_text="detail" * 500)])
    canvas = VideoSearchContextCanvas("workspace", ("series",))
    query_tools, _calls = _query_tools(retrieval, canvas)
    _retrieve(query_tools, top_k=1)
    _writer(query_tools, "upsert_draft_item").invoke({
        "video_chunk_id": "chunk-1", "start_seconds": 11,
        "end_seconds": 19, "claim": "draft claim",
    })
    audit = DraftReadAudit()
    evaluator_tools = {tool.name: tool for tool in build_evaluator_tools(
        retrieval.search,
        canvas=canvas,
        inputs=_inputs(),
        control=_control(),
        calls=[],
        maximum_top_k=20,
        maximum_tool_calls=100,
        maximum_result_bytes=700,
        maximum_catalog_items=20,
        audit=audit,
    )}
    _reader(evaluator_tools, "draft").invoke({})
    overview = json.loads(_reader(evaluator_tools, "draft").invoke({
        "video_chunk_id": "chunk-1",
    }))
    assert not audit.covers(["chunk-1"])

    section = next(item for item in overview["sections"] if item["name"] == "draft")
    total = section["total_chars"]
    midpoint = total // 2
    for start, end in ((0, midpoint), (midpoint, total)):
        _reader(evaluator_tools, "draft").invoke({
            "video_chunk_id": "chunk-1", "section": "draft",
            "start": start, "end": end,
        })
    assert not audit.covers(["chunk-1"])

    _read_all(_reader(evaluator_tools, "draft"), "chunk-1", 700)
    assert audit.covers(["chunk-1"])


def test_draft_tools_upsert_by_chunk_id_read_detail_and_remove() -> None:
    maximum_result_bytes = 700
    retrieval = Retrieval([_hit("chunk-1", large_text="detail" * 500)])
    canvas = VideoSearchContextCanvas("workspace", ("series",))
    tools, _calls = _query_tools(
        retrieval, canvas, maximum_result_bytes=maximum_result_bytes, maximum_tool_calls=None,
    )
    retrieved = _retrieve(tools, top_k=1)
    assert retrieved["draft_item_count"] == 0

    first_item = {
        "video_chunk_id": "chunk-1",
        "start_seconds": 11,
        "end_seconds": 19,
        "claim": "first claim",
    }
    inserted = json.loads(_writer(tools, "upsert_draft_item").invoke(first_item))
    repeated = json.loads(_writer(tools, "upsert_draft_item").invoke(first_item))
    updated = json.loads(_writer(tools, "upsert_draft_item").invoke({
        "video_chunk_id": "chunk-1",
        "start_seconds": 12,
        "end_seconds": 18,
        "claim": "revised claim",
    }))

    assert inserted["operation"] == "inserted"
    assert inserted["draft_item_count"] == 1
    assert repeated["operation"] == "already_present"
    assert repeated["draft_item_count"] == 1
    assert updated["operation"] == "updated"
    assert updated["draft_item_count"] == 1
    catalog = json.loads(_reader(tools, "draft").invoke({}))
    assert catalog["total"] == 1
    assert catalog["items"][0] == {
        "reference": "chunk-1",
        "video_chunk_id": "chunk-1",
        "episode_order": 1,
        "start_seconds": 12,
        "end_seconds": 18,
        "description": "revised claim",
    }
    detail = _read_all(_reader(tools, "draft"), "chunk-1", maximum_result_bytes)
    assert detail["draft"]["claim"] == "revised claim"
    assert detail["evidence"]["videoChunkId"] == "chunk-1"

    out_of_range = _writer(tools, "upsert_draft_item").invoke({
        "video_chunk_id": "chunk-1",
        "start_seconds": 9,
        "end_seconds": 18,
        "claim": "out of range",
    })
    empty_range = _writer(tools, "upsert_draft_item").invoke({
        "video_chunk_id": "chunk-1",
        "start_seconds": 15,
        "end_seconds": 15,
        "claim": "empty range",
    })
    reversed_range = _writer(tools, "upsert_draft_item").invoke({
        "video_chunk_id": "chunk-1",
        "start_seconds": 16,
        "end_seconds": 15,
        "claim": "reversed range",
    })
    negative_range = _writer(tools, "upsert_draft_item").invoke({
        "video_chunk_id": "chunk-1",
        "start_seconds": -1,
        "end_seconds": 15,
        "claim": "negative range",
    })

    for rejected in (
        out_of_range, empty_range, reversed_range, negative_range,
    ):
        failure = json.loads(rejected)
        assert canvas.draft_item_count() == 1
        assert failure["error"]["code"] == "INVALID_DRAFT_RANGE"
        assert "10 <= start_seconds < end_seconds <= 20" in failure["error"]["message"]
        assert "重新调用 write_context_canvas" in failure["error"]["retry_hint"]
    assert "start_seconds=9, end_seconds=18" in out_of_range
    assert "start_seconds=15, end_seconds=15" in empty_range
    assert "start_seconds=16, end_seconds=15" in reversed_range
    assert "start_seconds=-1, end_seconds=15" in negative_range
    assert canvas.draft_item("chunk-1") == DraftItem(
        video_chunk_id="chunk-1",
        start_seconds=12,
        end_seconds=18,
        claim="revised claim",
    )

    removed = json.loads(_writer(tools, "remove_draft_item").invoke({
        "video_chunk_id": "chunk-1",
    }))
    assert removed == {
        "removed": True,
        "video_chunk_id": "chunk-1",
        "draft_item_count": 0,
    }


def test_update_draft_tool_does_not_mask_unexpected_errors(monkeypatch) -> None:
    retrieval = Retrieval([_hit("chunk-1")])
    canvas = VideoSearchContextCanvas("workspace", ("series",))
    tools, _calls = _query_tools(retrieval, canvas)
    _retrieve(tools, top_k=1)

    def fail_upsert(*_args, **_values) -> bool:
        raise RuntimeError("unexpected storage failure")

    monkeypatch.setattr(VideoSearchContextCanvas, "_upsert_draft", fail_upsert)

    with pytest.raises(RuntimeError, match="unexpected storage failure"):
        _writer(tools, "upsert_draft_item").invoke({
            "video_chunk_id": "chunk-1",
            "start_seconds": 11,
            "end_seconds": 18,
            "claim": "valid range",
        })


def test_draft_catalog_cursor_is_rejected_after_draft_changes() -> None:
    retrieval = Retrieval([_hit("chunk-1"), _hit("chunk-2", episode=2)])
    canvas = VideoSearchContextCanvas("workspace", ("series",))
    tools, _calls = _query_tools(
        retrieval, canvas, maximum_catalog_items=1, maximum_result_bytes=1_024,
    )
    _retrieve(tools, top_k=2)
    for chunk_id in ("chunk-1", "chunk-2"):
        _writer(tools, "upsert_draft_item").invoke({
            "video_chunk_id": chunk_id,
            "start_seconds": 11,
            "end_seconds": 19,
            "claim": chunk_id,
        })
    first = json.loads(_reader(tools, "draft").invoke({}))
    assert first["next_cursor"]

    _writer(tools, "upsert_draft_item").invoke({
        "video_chunk_id": "chunk-1",
        "start_seconds": 12,
        "end_seconds": 18,
        "claim": "changed",
    })

    feedback = json.loads(_reader(tools, "draft").invoke({
        "cursor": first["next_cursor"],
    }))
    assert feedback["error"]["code"] == "INVALID_CATALOG_CURSOR"
    assert "without a cursor" in feedback["error"]["retry_hint"]


def test_chat_tools_use_approved_snapshot_and_update_video_in_place() -> None:
    retrieval = Retrieval([_hit("chunk-1"), _hit("chunk-2", episode=2)])
    canvas = VideoSearchContextCanvas("workspace", ("series",))
    query_tools, _calls = _query_tools(retrieval, canvas)
    _retrieve(query_tools, top_k=2)
    for chunk_id in ("chunk-1", "chunk-2"):
        _writer(query_tools, "upsert_draft_item").invoke({
            "video_chunk_id": chunk_id,
            "start_seconds": 11,
            "end_seconds": 19,
            "claim": f"draft {chunk_id}",
        })
    canvas.approve_draft()
    canvas.append_markdown("before")
    tools = {tool.name: tool for tool in build_chat_tools(
        canvas=canvas,
        control=_control(),
        maximum_tool_calls=20,
        maximum_result_bytes=1_024,
        maximum_catalog_items=20,
    )}

    assert json.loads(_reader(tools, "approved").invoke({}))["total"] == 2
    _writer(tools, "upsert_video_item").invoke({
        "video_chunk_id": "chunk-1", "claim": "first",
    })
    canvas.append_markdown("between")
    _writer(tools, "upsert_video_item").invoke({
        "video_chunk_id": "chunk-2", "claim": "second",
    })
    _writer(tools, "upsert_video_item").invoke({
        "video_chunk_id": "chunk-1", "claim": "first revised",
    })

    rendered = canvas.render_draft()
    assert [block.type for block in rendered.blocks] == [
        "markdown", "video", "markdown", "video",
    ]
    assert rendered.video_items[0].claim == "first revised"
    assert rendered.video_items[1].claim == "second"

    removed = json.loads(_writer(tools, "remove_video_item").invoke({
        "video_chunk_id": "chunk-1",
    }))
    assert removed["removed"] is True
    assert [item.video_chunk_id for item in canvas.render_draft().video_items] == [
        "chunk-2",
    ]


def test_canvas_checkpoint_contains_handles_without_full_evidence() -> None:
    large_text = "canonical evidence" * 2_000
    retrieval = Retrieval([_hit("chunk-1", large_text=large_text)])
    canvas = VideoSearchContextCanvas("workspace", ("series",))
    query_tools, _calls = _query_tools(retrieval, canvas)
    _retrieve(query_tools, top_k=1)
    _writer(query_tools, "upsert_draft_item").invoke({
        "video_chunk_id": "chunk-1",
        "start_seconds": 11,
        "end_seconds": 19,
        "claim": "bounded claim",
    })

    query_checkpoint = build_canvas_checkpoint(canvas, role="query")
    rendered_query = json.dumps(query_checkpoint, ensure_ascii=False)

    assert query_checkpoint["retrieval_catalog"] == {
        "item_count": 1,
        "visible_video_chunk_ids": ["chunk-1"],
        "omitted_item_count": 0,
    }
    assert query_checkpoint["draft_catalog"] == [{
        "video_chunk_id": "chunk-1",
        "start_seconds": 11,
        "end_seconds": 19,
    }]
    assert large_text not in rendered_query
    assert "bounded claim" not in rendered_query

    canvas.approve_draft()
    canvas.append_markdown("answer text")
    chat_tools = {tool.name: tool for tool in build_chat_tools(
        canvas=canvas,
        control=_control(),
        maximum_tool_calls=20,
        maximum_result_bytes=1_024,
        maximum_catalog_items=20,
    )}
    _writer(chat_tools, "upsert_video_item").invoke({
        "video_chunk_id": "chunk-1",
        "claim": "rendered claim",
    })

    chat_checkpoint = build_canvas_checkpoint(canvas, role="chat")

    assert chat_checkpoint["render_draft"] == {
        "block_count": 2,
        "blocks": [
            {"type": "markdown", "character_count": 11},
            {"type": "video", "video_chunk_id": "chunk-1"},
        ],
    }


def test_shared_role_tool_budget_counts_every_tool() -> None:
    retrieval = Retrieval([_hit("chunk-1")])
    canvas = VideoSearchContextCanvas("workspace", ("series",))
    tools, _calls = _query_tools(
        retrieval, canvas, maximum_tool_calls=1,
    )

    _retrieve(tools, top_k=1)
    with pytest.raises(AgentModelInvocationError, match="Tool-call budget exceeded"):
        _reader(tools, "retrieval").invoke({"video_chunk_id": "chunk-1"})


def test_tool_budget_can_be_disabled() -> None:
    retrieval = Retrieval([_hit("chunk-1")])
    canvas = VideoSearchContextCanvas("workspace", ("series",))
    tools, _calls = _query_tools(retrieval, canvas, maximum_tool_calls=None)

    _retrieve(tools, top_k=1)
    for _ in range(101):
        response = _reader(tools, "retrieval").invoke({
            "video_chunk_id": "chunk-1",
        })
        assert json.loads(response)["id"] == "chunk-1"


def test_video_search_canvas_uses_shared_storage_and_atomic_operations() -> None:
    from agent.utils.context_canvas import ContextCanvas

    canvas = VideoSearchContextCanvas("workspace", ("series",))
    tools, _calls = _query_tools(Retrieval([_hit("chunk-1"), _hit("chunk-2")]), canvas)
    _retrieve(tools, top_k=2)
    assert isinstance(canvas, ContextCanvas)
    assert not hasattr(canvas, "_draft_items")
    before = canvas.snapshot()
    arguments = {
        "batch_id": "atomic-error", "expected_revision": canvas.revision,
        "operations": [
            {"operation": "upsert_draft_item", "video_chunk_id": "chunk-1",
             "start_seconds": 11, "end_seconds": 19, "claim": "kept"},
            {"operation": "upsert_draft_item", "video_chunk_id": "chunk-2",
             "start_seconds": 1, "end_seconds": 2, "claim": "invalid"},
        ],
    }
    failed = json.loads(tools["write_context_canvas"].invoke(arguments))
    assert failed["error"]["code"] == "INVALID_DRAFT_RANGE"
    assert canvas.snapshot() == before
    arguments["operations"][1].update(start_seconds=12, end_seconds=18)
    receipt = json.loads(tools["write_context_canvas"].invoke(arguments))
    assert len(receipt["results"]) == 2
    assert canvas.snapshot().collections["draft"]["chunk-1"]["claim"] == "kept"


def test_canvas_write_omitted_fields_replay_and_stale_revision() -> None:
    canvas = VideoSearchContextCanvas("workspace", ("series",))
    tools, _calls = _query_tools(Retrieval([_hit("chunk-1"), _hit("chunk-2")]), canvas)
    _retrieve(tools, top_k=2)
    for reference in ("chunk-1", "chunk-2"):
        canvas.upsert_draft_item(video_chunk_id=reference, start_seconds=11,
                                    end_seconds=19, claim=reference)
    arguments = {"batch_id": "partial", "expected_revision": canvas.revision,
                 "operations": [{"operation": "upsert_draft_item", "video_chunk_id": "chunk-1",
                                 "claim": "corrected"}]}
    receipt = json.loads(tools["write_context_canvas"].invoke(arguments))
    assert receipt["replayed"] is False
    assert canvas.draft_item("chunk-1").start_seconds == 11
    assert canvas.draft_item("chunk-2").claim == "chunk-2"
    canvas.append_markdown("Host streamed text.")
    revision = canvas.revision
    replay = json.loads(tools["write_context_canvas"].invoke(arguments))
    assert replay["replayed"] is True
    assert canvas.revision == revision
    stale = json.loads(tools["write_context_canvas"].invoke({**arguments, "batch_id": "stale"}))
    assert stale["error"]["code"] == "STALE_REVISION"
    assert canvas.revision == revision
    reused = json.loads(tools["write_context_canvas"].invoke({
        **arguments, "operations": [{"operation": "remove_draft_item", "video_chunk_id": "chunk-1"}],
    }))
    assert reused["error"]["code"] == "BATCH_ID_REUSED"
    assert canvas.draft_item("chunk-1").claim == "corrected"


def test_canvas_role_permissions_and_approved_evidence_are_frozen() -> None:
    from agent.utils.errors import AgentToolFeedbackError

    canvas = VideoSearchContextCanvas("workspace", ("series",))
    retrieval = Retrieval([_hit("chunk-1", large_text="original evidence"), _hit("chunk-2")])
    query, _calls = _query_tools(retrieval, canvas)
    _retrieve(query, top_k=2)
    canvas.upsert_draft_item(video_chunk_id="chunk-1", start_seconds=11,
                                end_seconds=19, claim="approved claim")
    canvas.approve_draft()
    canvas.upsert_draft_item(video_chunk_id="chunk-1", start_seconds=12,
                                end_seconds=18, claim="later claim")
    retrieval.hits = [_hit("chunk-1", large_text="later evidence")]
    _retrieve(query, top_k=1)
    chat = {tool.name: tool for tool in build_chat_tools(
        canvas=canvas, control=_control(), maximum_tool_calls=None,
        maximum_result_bytes=1024, maximum_catalog_items=20,
    )}
    assert canvas.draft_item("chunk-1", approved=True).claim == "approved claim"
    assert canvas.approved_hit("chunk-1").evidence.retrieval_text == "original evidence"
    for collection in ("draft", "retrieval", "evidence"):
        response = json.loads(chat["get_context_canvas"].invoke({"collection": collection}))
        assert response["error"]["code"] == "FORBIDDEN_COLLECTION"
    unapproved = json.loads(chat["get_context_canvas"].invoke({
        "collection": "approved", "reference": "chunk-2",
    }))
    assert unapproved["error"]["code"] == "UNKNOWN_ITEM"
    denied = json.loads(chat["write_context_canvas"].invoke({
        "batch_id": "role-write", "expected_revision": canvas.revision,
        "operations": [{"operation": "remove_draft_item", "video_chunk_id": "chunk-1"}],
    }))
    assert denied["error"]["code"] == "INVALID_ARGUMENTS"
    with pytest.raises(AgentToolFeedbackError, match="read-only"):
        canvas.mutate(lambda draft: draft["approved"]["chunk-1"].update(claim="changed"))


def test_canvas_audit_rejects_skipped_catalog_page_and_changed_revision() -> None:
    canvas = VideoSearchContextCanvas("workspace", ("series",))
    retrieval = Retrieval([_hit("chunk-1"), _hit("chunk-2")])
    query, _calls = _query_tools(retrieval, canvas)
    _retrieve(query, top_k=2)
    for reference in ("chunk-1", "chunk-2"):
        canvas.upsert_draft_item(video_chunk_id=reference, start_seconds=11,
                                    end_seconds=19, claim=reference)
    options = dict(canvas=canvas, inputs=_inputs(), control=_control(), calls=[],
                   maximum_top_k=20, maximum_tool_calls=None, maximum_result_bytes=1024,
                   maximum_catalog_items=1)
    previous = {tool.name: tool for tool in build_evaluator_tools(retrieval.search, **options)}
    first = json.loads(previous["get_context_canvas"].invoke({"collection": "draft"}))
    assert first["next_cursor"]
    audit = DraftReadAudit()
    current = {tool.name: tool for tool in build_evaluator_tools(retrieval.search, audit=audit, **options)}
    current["get_context_canvas"].invoke({"collection": "draft", "cursor": first["next_cursor"]})
    for reference in ("chunk-1", "chunk-2"):
        _read_all(_reader(current, "draft"), reference, 1024)
    assert not audit.covers(["chunk-1", "chunk-2"], revision=canvas.revision)
    current["get_context_canvas"].invoke({"collection": "draft"})
    assert audit.covers(["chunk-1", "chunk-2"], revision=canvas.revision)
    canvas.upsert_draft_item(video_chunk_id="chunk-1", start_seconds=11,
                                end_seconds=19, claim="changed")
    assert not audit.covers(["chunk-1", "chunk-2"], revision=canvas.revision)
    current["get_context_canvas"].invoke({"collection": "draft"})
    assert not audit.covers(["chunk-1", "chunk-2"], revision=canvas.revision)


def test_canvas_catalog_cursor_cannot_cross_kick_or_role() -> None:
    canvases = [VideoSearchContextCanvas("workspace", ("series",)) for _ in range(2)]
    maps = []
    retrieval = Retrieval([_hit("chunk-1"), _hit("chunk-2")])
    for canvas in canvases:
        tools, _calls = _query_tools(retrieval, canvas, maximum_catalog_items=1)
        _retrieve(tools, top_k=2)
        maps.append(tools)
    first = json.loads(maps[0]["get_context_canvas"].invoke({"collection": "retrieval"}))
    assert first["next_cursor"]
    other = json.loads(maps[1]["get_context_canvas"].invoke({
        "collection": "retrieval", "cursor": first["next_cursor"],
    }))
    assert other["error"]["code"] == "INVALID_CATALOG_CURSOR"
    evaluator = {tool.name: tool for tool in build_evaluator_tools(
        retrieval.search, canvas=canvases[0], inputs=_inputs(), control=_control(), calls=[],
        maximum_top_k=20, maximum_tool_calls=None, maximum_result_bytes=1024, maximum_catalog_items=1,
    )}
    denied = json.loads(evaluator["get_context_canvas"].invoke({
        "collection": "retrieval", "cursor": first["next_cursor"],
    }))
    assert denied["error"]["code"] == "INVALID_CATALOG_CURSOR"
    assert "write_context_canvas" not in evaluator


def test_canvas_write_output_budget_failure_keeps_state_and_replay_log() -> None:
    canvas = VideoSearchContextCanvas("workspace", ("series",))
    tools, _calls = _query_tools(Retrieval([_hit("chunk-1")]), canvas)
    _retrieve(tools, top_k=1)
    before = canvas.snapshot()
    arguments = dict(role="query", batch_id="budget", expected_revision=canvas.revision,
                     operations=[{"operation": "upsert_draft_item", "video_chunk_id": "chunk-1",
                                  "start_seconds": 11, "end_seconds": 19, "claim": "grounded"}])
    with pytest.raises(AgentToolOutputError):
        canvas.write(**arguments, maximum_result_bytes=10)
    assert canvas.snapshot() == before
    receipt = canvas.write(**arguments, maximum_result_bytes=1024)
    assert receipt["replayed"] is False
