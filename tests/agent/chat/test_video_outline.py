from __future__ import annotations

import json

from agent.chat.video_search.tools import build_retrieval_tools
from tests.agent.chat.test_tools import Retrieval, _control, _hit, _inputs


def _tools(*, role="supervisor", video_outline=None):
    return build_retrieval_tools(
        Retrieval([_hit("chunk-1")]).search, role=role, inputs=_inputs(), control=_control(),
        calls=[], maximum_tool_calls=None, maximum_result_bytes=4_096,
        maximum_catalog_items=20, retrieval_channel="lexical", video_outline=video_outline,
    )


def _outline(*, available=True, video_id="video-1"):
    if not available:
        return {"available": False, "reason": "该视频尚未生成章节大纲。"}
    return {
        "video_id": video_id, "title": "课程标题", "description": "课程简介",
        "sections": [
            {"start_ms": 0, "end_ms": 10_000, "title": "开场", "summary": "老师介绍课程目标。",
             "video_chunk_ids": ["chunk-1", "chunk-2"]},
            {"start_ms": 10_000, "end_ms": 20_000, "title": "正题", "summary": "讲解核心算法。",
             "video_chunk_ids": ["chunk-3"]},
        ],
    }


def test_get_video_outline_returns_sections_with_video_chunk_ids():
    received = []

    def video_outline(video_id, *, tenant_id, series_ids):
        received.append((video_id, tenant_id, series_ids))
        return _outline()

    tools = {tool.name: tool for tool in _tools(video_outline=video_outline)}
    assert "get_video_outline" in tools
    result = json.loads(tools["get_video_outline"].invoke({"video_id": "video-1"}))
    assert result["video_id"] == "video-1"
    assert result["title"] == "课程标题"
    assert result["sections"][0]["video_chunk_ids"] == ["chunk-1", "chunk-2"]
    assert received == [("video-1", "workspace", ["series"])]


def test_get_video_outline_unavailable_outline_returns_available_false():
    def video_outline(video_id, *, tenant_id, series_ids):
        return _outline(available=False)

    tools = {tool.name: tool for tool in _tools(video_outline=video_outline)}
    result = json.loads(tools["get_video_outline"].invoke({"video_id": "video-1"}))
    assert result == {"available": False, "reason": "该视频尚未生成章节大纲。"}


def test_get_video_outline_rejects_out_of_scope_video():
    def video_outline(video_id, *, tenant_id, series_ids):
        return None

    tools = {tool.name: tool for tool in _tools(video_outline=video_outline)}
    result = json.loads(tools["get_video_outline"].invoke({"video_id": "video-1"}))
    assert result["error"]["code"] == "OUT_OF_SCOPE_VIDEO"


def test_get_video_outline_only_for_supervisor_when_callable_injected():
    def video_outline(video_id, *, tenant_id, series_ids):
        return _outline()

    supervisor = {tool.name for tool in _tools(role="supervisor", video_outline=video_outline)}
    query = {tool.name for tool in _tools(role="query", video_outline=video_outline)}
    without_callable = {tool.name for tool in _tools(role="supervisor", video_outline=None)}
    assert "get_video_outline" in supervisor
    assert "get_video_outline" not in query
    assert "get_video_outline" not in without_callable
    assert "retrieve" in supervisor and "retrieve" in query
