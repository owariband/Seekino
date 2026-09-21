from __future__ import annotations

import json
import logging
from time import monotonic

import pytest
from langchain_core.messages import ToolMessage

from server.agent.context import build_workspace_checkpoint
from server.agent.errors import AgentModelInvocationError
from server.agent.state import (
    DraftItem, ExecutionControl, RunInputs,
)
from server.agent.skills import (
    CHAT_SKILL_PROMPT, EVALUATOR_SKILL_PROMPT, QUERY_SKILL_PROMPT,
    RETRIEVAL_SKILL_PROMPT,
)
from server.agent.tools import (
    DraftReadAudit, attach_agent_tool_logging, build_chat_tools, build_evaluator_tools,
    build_query_tools,
)
from server.agent.workspace import AgentWorkspace
from server.schema import (
    AgentReply, SelectionReason, VideoChunkCandidate, VideoChunkEvidence,
    VideoChunkSearchHit, VideoChunkSearchResult,
)


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
        duration_ms=10_000,
        upload_status="COMPLETED",
        parse_status="COMPLETED",
        video_chunk_id=chunk_id,
        sequence_no=episode - 1,
        start_ms=1_000,
        end_ms=2_000,
        analysis_window_start_ms=0,
        analysis_window_end_ms=3_000,
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
        transcript_weight,
        text_weight,
        vision_weight,
    ) -> VideoChunkSearchResult:
        self.calls += 1
        assert workspace_id == "workspace"
        assert series_ids == ["series"]
        assert transcript_weight + text_weight + vision_weight == pytest.approx(1)
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
        maximum_catalog_items=maximum_catalog_items,
    )
    return {tool.name: tool for tool in tools}, calls


def _retrieve(tools, *, top_k: int = 20) -> dict:
    return json.loads(tools["retrieve"].invoke({
        "query": "broken cup",
        "top_k": top_k,
        "channel_weights": {
            "transcript": 0,
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
            if page.get("ok") is False:
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


def test_agent_skills_are_explicitly_work_agnostic() -> None:
    skills = (
        RETRIEVAL_SKILL_PROMPT,
        QUERY_SKILL_PROMPT,
        EVALUATOR_SKILL_PROMPT,
        CHAT_SKILL_PROMPT,
    )

    assert all("作品无关" in skill for skill in skills)
    assert all("不得写入或依赖任何具体动画" in skill for skill in skills)
    assert all("丰川祥子" not in skill for skill in skills)


def test_agent_tool_trace_records_safe_arguments_results_and_errors(caplog) -> None:
    caplog.set_level(logging.INFO, logger="server.agent.tools")
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
        "start_ms": 1_100,
        "end_ms": 1_900,
        "claim": "private claim text",
    })
    feedback = json.loads(tools["update_draft_item"].invoke({
        "video_chunk_id": "unknown-chunk",
        "start_ms": 1_100,
        "end_ms": 1_900,
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
    retrieval = Retrieval([
        _hit("chunk-1", episode=1, large_text="杯子碎裂。" * 500),
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
    assert len(first["items"]) == 1
    assert first["items"][0]["video_chunk_id"] == "chunk-1"
    assert "杯子碎裂" not in json.dumps(first, ensure_ascii=False)
    assert "evidence" not in first["items"][0]
    assert first["next_cursor"]

    second_raw = tools["retrieve"].invoke({"cursor": first["next_cursor"]})
    second = json.loads(second_raw)
    assert len(second_raw.encode("utf-8")) <= maximum_result_bytes
    assert retrieval.calls == 1
    assert [item["video_chunk_id"] for item in second["items"]] == ["chunk-2"]
    assert second["next_cursor"] is None

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
    assert oversized["ok"] is False
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
    result = tools["read_trieve_item"].invoke({
        "type": "tool_call",
        "name": "read_trieve_item",
        "id": "call-1",
        "args": {"video_chunk_id": "missing"},
    })

    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert result.tool_call_id == "call-1"
    assert json.loads(result.content)["error"]["code"] == "UNKNOWN_ITEM"


def test_draft_audit_requires_full_coverage_of_every_section() -> None:
    retrieval = Retrieval([_hit("chunk-1", large_text="detail" * 500)])
    workspace = AgentWorkspace("workspace", ("series",))
    query_tools, _calls = _query_tools(retrieval, workspace)
    _retrieve(query_tools, top_k=1)
    query_tools["update_draft_item"].invoke({
        "video_chunk_id": "chunk-1", "start_ms": 1_100,
        "end_ms": 1_900, "claim": "draft claim",
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
    _retrieve(tools, top_k=1)

    inserted = json.loads(tools["update_draft_item"].invoke({
        "video_chunk_id": "chunk-1",
        "start_ms": 1_100,
        "end_ms": 1_900,
        "claim": "first claim",
    }))
    updated = json.loads(tools["update_draft_item"].invoke({
        "video_chunk_id": "chunk-1",
        "start_ms": 1_200,
        "end_ms": 1_800,
        "claim": "revised claim",
    }))

    assert inserted["operation"] == "inserted"
    assert updated["operation"] == "updated"
    assert updated["draft_item_count"] == 1
    catalog = json.loads(tools["get_draft_catelog"].invoke({}))
    assert catalog["total"] == 1
    assert catalog["items"][0] == {
        "video_chunk_id": "chunk-1",
        "episode_order": 1,
        "start_ms": 1_200,
        "end_ms": 1_800,
        "description": "revised claim",
    }
    detail = _read_all(tools["read_draft_item"], "chunk-1", maximum_result_bytes)
    assert detail["draft"]["claim"] == "revised claim"
    assert detail["evidence"]["videoChunkId"] == "chunk-1"

    out_of_range = tools["update_draft_item"].invoke({
        "video_chunk_id": "chunk-1",
        "start_ms": 900,
        "end_ms": 1_800,
        "claim": "out of range",
    })
    empty_range = tools["update_draft_item"].invoke({
        "video_chunk_id": "chunk-1",
        "start_ms": 1_500,
        "end_ms": 1_500,
        "claim": "empty range",
    })
    reversed_range = tools["update_draft_item"].invoke({
        "video_chunk_id": "chunk-1",
        "start_ms": 1_600,
        "end_ms": 1_500,
        "claim": "reversed range",
    })
    negative_range = tools["update_draft_item"].invoke({
        "video_chunk_id": "chunk-1",
        "start_ms": -1,
        "end_ms": 1_500,
        "claim": "negative range",
    })

    for rejected in (
        out_of_range, empty_range, reversed_range, negative_range,
    ):
        failure = json.loads(rejected)
        assert failure["error"]["code"] == "INVALID_DRAFT_RANGE"
        assert "1000 <= start_ms < end_ms <= 2000" in failure["error"]["message"]
        assert "重新调用 update_draft_item" in failure["error"]["retry_hint"]
    assert "start_ms=900, end_ms=1800" in out_of_range
    assert "start_ms=1500, end_ms=1500" in empty_range
    assert "start_ms=1600, end_ms=1500" in reversed_range
    assert "start_ms=-1, end_ms=1500" in negative_range
    assert workspace.draft_item("chunk-1") == DraftItem(
        video_chunk_id="chunk-1",
        start_ms=1_200,
        end_ms=1_800,
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
            "start_ms": 1_100,
            "end_ms": 1_800,
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
            "start_ms": 1_100,
            "end_ms": 1_900,
            "claim": chunk_id,
        })
    first = json.loads(tools["get_draft_catelog"].invoke({}))
    assert first["next_cursor"]

    tools["update_draft_item"].invoke({
        "video_chunk_id": "chunk-1",
        "start_ms": 1_200,
        "end_ms": 1_800,
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
            "start_ms": 1_100,
            "end_ms": 1_900,
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
        "start_ms": 1_100,
        "end_ms": 1_900,
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
        "start_ms": 1_100,
        "end_ms": 1_900,
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
