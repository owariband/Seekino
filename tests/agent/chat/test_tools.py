from __future__ import annotations

import json
import logging
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
from agent.chat.common.errors import AgentModelInvocationError, AgentToolOutputError
from agent.chat.common.state import ExecutionControl, RunInputs
from agent.chat.common.tool import (
    AgentTool, as_langchain_tools, attach_agent_tool_logging, build_agent_tool,
)
from agent.chat.common.tool_result import (
    JsonToolResult, TextToolResult, ToolError,
)
from agent.chat.video_search.context import build_workspace_checkpoint
from agent.chat.video_search.state import DraftItem
from agent.chat.video_search.skills import (
    CHAT_SKILL_PROMPT, EVALUATOR_SKILL_PROMPT, LEXICAL_RETRIEVAL_SKILL_PROMPT,
    QUERY_SKILL_PROMPT, RETRIEVAL_SKILL_PROMPT,
)
from agent.chat.video_search.tools import (
    DraftReadAudit, LexicalRetrieveArguments, RetrieveArguments,
    build_chat_tools, build_evaluator_tools, build_query_tools,
)
from agent.chat.video_search.workspace import AgentWorkspace
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
    workspace: AgentWorkspace,
    *,
    maximum_result_bytes: int = 1_024,
    maximum_read_chars: int = 16_384,
    maximum_catalog_items: int = 20,
    maximum_tool_calls: int | None = 100,
):
    calls = []
    tools = build_query_tools(
        retrieval.search,
        workspace=workspace,
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
    workspace = AgentWorkspace("workspace", ("series",))
    retrieval = Retrieval([_hit("chunk-1")])
    query_tools = build_query_tools(
        retrieval.search,
        workspace=workspace,
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
        workspace=workspace,
        inputs=inputs,
        control=_control(),
        calls=[],
        maximum_top_k=20,
        maximum_tool_calls=20,
        maximum_result_bytes=1_024,
        maximum_catalog_items=20,
    )
    workspace.approve_draft()
    chat_tools = build_chat_tools(
        workspace=workspace,
        control=_control(),
        maximum_tool_calls=20,
        maximum_result_bytes=1_024,
        maximum_catalog_items=20,
    )

    assert {tool.name for tool in query_tools} == {
        "retrieve",
        "read_trieve_item",
        "update_draft_item",
        "remove_draft_item",
        "get_draft_catelog",
        "read_draft_item",
    }
    assert {tool.name for tool in evaluator_tools} == {
        "retrieve",
        "read_trieve_item",
        "get_draft_catelog",
        "read_draft_item",
    }
    assert {tool.name for tool in chat_tools} == {
        "remove_video_item",
        "update_video_item",
        "get_draft_catelog",
        "read_draft_item",
    }
    assert all(isinstance(tool, AgentTool) for tool in query_tools)
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
        workspace=AgentWorkspace("workspace", ("series",)),
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
        AgentWorkspace("workspace", ("series",)),
    )
    attach_agent_tool_logging(list(tools.values()), role="Query", inputs=_inputs())

    _retrieve(tools, top_k=1)
    tools["read_trieve_item"].invoke({
        "video_chunk_id": "chunk-1", "section": "evidence.text", "end": 10,
    })
    tools["update_draft_item"].invoke({
        "video_chunk_id": "chunk-1",
        "start_seconds": 11,
        "end_seconds": 19,
        "claim": "private claim text",
    })
    feedback = json.loads(tools["update_draft_item"].invoke({
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
    assert '"video_chunk_id":"unknown-chunk"' in caplog.text
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
    workspace = AgentWorkspace("workspace", ("series",))
    tools, calls = _query_tools(
        retrieval,
        workspace,
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

    inserted = json.loads(tools["update_draft_item"].invoke({
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
        tools["read_trieve_item"], "chunk-1", maximum_result_bytes,
    )
    assert detail["candidate"]["videoChunkId"] == "chunk-1"
    assert detail["evidence"]["videoCompact"] == "杯子碎裂。" * 500


def test_read_range_reports_oversize_and_retries_with_smaller_range() -> None:
    maximum_result_bytes = 700
    retrieval = Retrieval([_hit("chunk-1", large_text="杯子碎裂。" * 500)])
    tools, _calls = _query_tools(
        retrieval, AgentWorkspace("workspace", ("series",)),
        maximum_result_bytes=maximum_result_bytes,
    )
    _retrieve(tools, top_k=1)
    tool = tools["read_trieve_item"]
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
    workspace = AgentWorkspace("workspace", ("series",))
    tools, _calls = _query_tools(
        retrieval, workspace, maximum_result_bytes=4_096,
        maximum_read_chars=80,
    )
    _retrieve(tools, top_k=1)
    tools["update_draft_item"].invoke({
        "video_chunk_id": "chunk-1", "start_seconds": 11,
        "end_seconds": 19, "claim": "draft claim",
    })

    for name in ("read_trieve_item", "read_draft_item"):
        tool = tools[name]
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
        retrieval, AgentWorkspace("workspace", ("series",)),
        maximum_result_bytes=700, maximum_read_chars=300,
    )
    _retrieve(tools, top_k=1)
    too_large = json.loads(tools["read_trieve_item"].invoke({
        "video_chunk_id": "chunk-1", "section": "evidence.text",
        "start": 0, "end": 500,
    }))
    suggested_end = too_large["error"]["suggested_end"]
    assert too_large["error"]["code"] == "RANGE_TOO_LARGE"
    assert 0 < suggested_end < 300
    smaller = json.loads(tools["read_trieve_item"].invoke({
        "video_chunk_id": "chunk-1", "section": "evidence.text",
        "start": 0, "end": suggested_end,
    }))
    assert smaller["end"] == suggested_end


def test_read_range_uses_first_snapshot_after_same_id_is_retrieved_again() -> None:
    retrieval = Retrieval([_hit("chunk-1", large_text="first evidence")])
    workspace = AgentWorkspace("workspace", ("series",))
    tools, _calls = _query_tools(retrieval, workspace)
    _retrieve(tools, top_k=1)
    first = json.loads(tools["read_trieve_item"].invoke({
        "video_chunk_id": "chunk-1", "section": "evidence.text",
    }))

    changed = _hit("chunk-1", large_text="later evidence")
    changed.candidate.fused_rank = 9
    changed.selection_reason.summary = "later search ranking"
    retrieval.hits = [changed]
    _retrieve(tools, top_k=1)
    repeated = json.loads(tools["read_trieve_item"].invoke({
        "video_chunk_id": "chunk-1", "section": "evidence.text",
        "start": first["start"], "end": first["end"],
    }))

    assert repeated == first
    assert workspace.retrieval_item("chunk-1").candidate.fused_rank == 9


def test_correctable_tool_error_is_returned_to_agent_with_error_status() -> None:
    tools, _calls = _query_tools(
        Retrieval([]), AgentWorkspace("workspace", ("series",)),
    )
    internal_result = tools["read_trieve_item"].execute(
        video_chunk_id="missing",
    )
    result = tools["read_trieve_item"].invoke({
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
        AgentWorkspace("workspace", ("series",)),
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
    workspace = AgentWorkspace("workspace", ("series",))
    query_tools, _calls = _query_tools(retrieval, workspace)
    _retrieve(query_tools, top_k=1)
    query_tools["update_draft_item"].invoke({
        "video_chunk_id": "chunk-1", "start_seconds": 11,
        "end_seconds": 19, "claim": "draft claim",
    })
    audit = DraftReadAudit()
    evaluator_tools = {tool.name: tool for tool in build_evaluator_tools(
        retrieval.search,
        workspace=workspace,
        inputs=_inputs(),
        control=_control(),
        calls=[],
        maximum_top_k=20,
        maximum_tool_calls=100,
        maximum_result_bytes=700,
        maximum_catalog_items=20,
        audit=audit,
    )}
    evaluator_tools["get_draft_catelog"].invoke({})
    overview = json.loads(evaluator_tools["read_draft_item"].invoke({
        "video_chunk_id": "chunk-1",
    }))
    assert not audit.covers(["chunk-1"])

    section = next(item for item in overview["sections"] if item["name"] == "draft")
    total = section["total_chars"]
    midpoint = total // 2
    for start, end in ((0, midpoint), (midpoint, total)):
        evaluator_tools["read_draft_item"].invoke({
            "video_chunk_id": "chunk-1", "section": "draft",
            "start": start, "end": end,
        })
    assert not audit.covers(["chunk-1"])

    _read_all(evaluator_tools["read_draft_item"], "chunk-1", 700)
    assert audit.covers(["chunk-1"])


def test_draft_tools_upsert_by_chunk_id_read_detail_and_remove() -> None:
    maximum_result_bytes = 700
    retrieval = Retrieval([_hit("chunk-1", large_text="detail" * 500)])
    workspace = AgentWorkspace("workspace", ("series",))
    tools, _calls = _query_tools(
        retrieval, workspace, maximum_result_bytes=maximum_result_bytes,
    )
    retrieved = _retrieve(tools, top_k=1)
    assert retrieved["draft_item_count"] == 0

    first_item = {
        "video_chunk_id": "chunk-1",
        "start_seconds": 11,
        "end_seconds": 19,
        "claim": "first claim",
    }
    inserted = json.loads(tools["update_draft_item"].invoke(first_item))
    repeated = json.loads(tools["update_draft_item"].invoke(first_item))
    updated = json.loads(tools["update_draft_item"].invoke({
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
    catalog = json.loads(tools["get_draft_catelog"].invoke({}))
    assert catalog["total"] == 1
    assert catalog["items"][0] == {
        "video_chunk_id": "chunk-1",
        "episode_order": 1,
        "start_seconds": 12,
        "end_seconds": 18,
        "description": "revised claim",
    }
    detail = _read_all(tools["read_draft_item"], "chunk-1", maximum_result_bytes)
    assert detail["draft"]["claim"] == "revised claim"
    assert detail["evidence"]["videoChunkId"] == "chunk-1"

    out_of_range = tools["update_draft_item"].invoke({
        "video_chunk_id": "chunk-1",
        "start_seconds": 9,
        "end_seconds": 18,
        "claim": "out of range",
    })
    empty_range = tools["update_draft_item"].invoke({
        "video_chunk_id": "chunk-1",
        "start_seconds": 15,
        "end_seconds": 15,
        "claim": "empty range",
    })
    reversed_range = tools["update_draft_item"].invoke({
        "video_chunk_id": "chunk-1",
        "start_seconds": 16,
        "end_seconds": 15,
        "claim": "reversed range",
    })
    negative_range = tools["update_draft_item"].invoke({
        "video_chunk_id": "chunk-1",
        "start_seconds": -1,
        "end_seconds": 15,
        "claim": "negative range",
    })

    for rejected in (
        out_of_range, empty_range, reversed_range, negative_range,
    ):
        failure = json.loads(rejected)
        assert failure["draft_item_count"] == 1
        assert failure["error"]["code"] == "INVALID_DRAFT_RANGE"
        assert "10 <= start_seconds < end_seconds <= 20" in failure["error"]["message"]
        assert "重新调用 update_draft_item" in failure["error"]["retry_hint"]
    assert "start_seconds=9, end_seconds=18" in out_of_range
    assert "start_seconds=15, end_seconds=15" in empty_range
    assert "start_seconds=16, end_seconds=15" in reversed_range
    assert "start_seconds=-1, end_seconds=15" in negative_range
    assert workspace.draft_item("chunk-1") == DraftItem(
        video_chunk_id="chunk-1",
        start_seconds=12,
        end_seconds=18,
        claim="revised claim",
    )

    removed = json.loads(tools["remove_draft_item"].invoke({
        "video_chunk_id": "chunk-1",
    }))
    assert removed == {
        "removed": True,
        "video_chunk_id": "chunk-1",
        "draft_item_count": 0,
    }


def test_update_draft_tool_does_not_mask_unexpected_errors(monkeypatch) -> None:
    retrieval = Retrieval([_hit("chunk-1")])
    workspace = AgentWorkspace("workspace", ("series",))
    tools, _calls = _query_tools(retrieval, workspace)
    _retrieve(tools, top_k=1)

    def fail_upsert(_workspace: AgentWorkspace, **_values) -> bool:
        raise RuntimeError("unexpected storage failure")

    monkeypatch.setattr(AgentWorkspace, "upsert_draft_item", fail_upsert)

    with pytest.raises(RuntimeError, match="unexpected storage failure"):
        tools["update_draft_item"].invoke({
            "video_chunk_id": "chunk-1",
            "start_seconds": 11,
            "end_seconds": 18,
            "claim": "valid range",
        })


def test_draft_catalog_cursor_is_rejected_after_draft_changes() -> None:
    retrieval = Retrieval([_hit("chunk-1"), _hit("chunk-2", episode=2)])
    workspace = AgentWorkspace("workspace", ("series",))
    tools, _calls = _query_tools(
        retrieval, workspace, maximum_catalog_items=1, maximum_result_bytes=1_024,
    )
    _retrieve(tools, top_k=2)
    for chunk_id in ("chunk-1", "chunk-2"):
        tools["update_draft_item"].invoke({
            "video_chunk_id": chunk_id,
            "start_seconds": 11,
            "end_seconds": 19,
            "claim": chunk_id,
        })
    first = json.loads(tools["get_draft_catelog"].invoke({}))
    assert first["next_cursor"]

    tools["update_draft_item"].invoke({
        "video_chunk_id": "chunk-1",
        "start_seconds": 12,
        "end_seconds": 18,
        "claim": "changed",
    })

    feedback = json.loads(tools["get_draft_catelog"].invoke({
        "cursor": first["next_cursor"],
    }))
    assert feedback["error"]["code"] == "INVALID_CATALOG_CURSOR"
    assert "without cursor" in feedback["error"]["retry_hint"]


def test_chat_tools_use_approved_snapshot_and_update_video_in_place() -> None:
    retrieval = Retrieval([_hit("chunk-1"), _hit("chunk-2", episode=2)])
    workspace = AgentWorkspace("workspace", ("series",))
    query_tools, _calls = _query_tools(retrieval, workspace)
    _retrieve(query_tools, top_k=2)
    for chunk_id in ("chunk-1", "chunk-2"):
        query_tools["update_draft_item"].invoke({
            "video_chunk_id": chunk_id,
            "start_seconds": 11,
            "end_seconds": 19,
            "claim": f"draft {chunk_id}",
        })
    workspace.approve_draft()
    workspace.append_markdown("before")
    tools = {tool.name: tool for tool in build_chat_tools(
        workspace=workspace,
        control=_control(),
        maximum_tool_calls=20,
        maximum_result_bytes=1_024,
        maximum_catalog_items=20,
    )}

    assert json.loads(tools["get_draft_catelog"].invoke({}))["total"] == 2
    tools["update_video_item"].invoke({
        "video_chunk_id": "chunk-1", "claim": "first",
    })
    workspace.append_markdown("between")
    tools["update_video_item"].invoke({
        "video_chunk_id": "chunk-2", "claim": "second",
    })
    tools["update_video_item"].invoke({
        "video_chunk_id": "chunk-1", "claim": "first revised",
    })

    rendered = workspace.render_draft()
    assert [block.type for block in rendered.blocks] == [
        "markdown", "video", "markdown", "video",
    ]
    assert rendered.video_items[0].claim == "first revised"
    assert rendered.video_items[1].claim == "second"

    removed = json.loads(tools["remove_video_item"].invoke({
        "video_chunk_id": "chunk-1",
    }))
    assert removed["removed"] is True
    assert [item.video_chunk_id for item in workspace.render_draft().video_items] == [
        "chunk-2",
    ]


def test_workspace_checkpoint_contains_handles_without_full_evidence() -> None:
    large_text = "canonical evidence" * 2_000
    retrieval = Retrieval([_hit("chunk-1", large_text=large_text)])
    workspace = AgentWorkspace("workspace", ("series",))
    query_tools, _calls = _query_tools(retrieval, workspace)
    _retrieve(query_tools, top_k=1)
    query_tools["update_draft_item"].invoke({
        "video_chunk_id": "chunk-1",
        "start_seconds": 11,
        "end_seconds": 19,
        "claim": "bounded claim",
    })

    query_checkpoint = build_workspace_checkpoint(workspace, role="query")
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

    workspace.approve_draft()
    workspace.append_markdown("answer text")
    chat_tools = {tool.name: tool for tool in build_chat_tools(
        workspace=workspace,
        control=_control(),
        maximum_tool_calls=20,
        maximum_result_bytes=1_024,
        maximum_catalog_items=20,
    )}
    chat_tools["update_video_item"].invoke({
        "video_chunk_id": "chunk-1",
        "claim": "rendered claim",
    })

    chat_checkpoint = build_workspace_checkpoint(workspace, role="chat")

    assert chat_checkpoint["render_draft"] == {
        "block_count": 2,
        "blocks": [
            {"type": "markdown", "character_count": 11},
            {"type": "video", "video_chunk_id": "chunk-1"},
        ],
    }


def test_shared_role_tool_budget_counts_every_tool() -> None:
    retrieval = Retrieval([_hit("chunk-1")])
    workspace = AgentWorkspace("workspace", ("series",))
    tools, _calls = _query_tools(
        retrieval, workspace, maximum_tool_calls=1,
    )

    _retrieve(tools, top_k=1)
    with pytest.raises(AgentModelInvocationError, match="Tool-call budget exceeded"):
        tools["read_trieve_item"].invoke({"video_chunk_id": "chunk-1"})


def test_tool_budget_can_be_disabled() -> None:
    retrieval = Retrieval([_hit("chunk-1")])
    workspace = AgentWorkspace("workspace", ("series",))
    tools, _calls = _query_tools(retrieval, workspace, maximum_tool_calls=None)

    _retrieve(tools, top_k=1)
    for _ in range(101):
        response = tools["read_trieve_item"].invoke({
            "video_chunk_id": "chunk-1",
        })
        assert json.loads(response)["id"] == "chunk-1"
