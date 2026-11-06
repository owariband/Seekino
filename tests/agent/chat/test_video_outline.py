from __future__ import annotations

import json

import pytest

from agent.chat.common.errors import UnknownToolItemError
from agent.chat.video_search.output import retrieval_hit
from agent.chat.video_search.tools import build_retrieval_tools
from agent.chat.video_search.tools.video_outline import (
    GetVideoOutlineArguments, VideoOutlineItemArguments,
)
from tests.agent.chat.test_tools import Retrieval, _control, _hit, _inputs


def _tools(*, role="supervisor", video_outline=None, outline_evidence=None, calls=None):
    return build_retrieval_tools(
        Retrieval([_hit("chunk-1")]).search, role=role, inputs=_inputs(), control=_control(),
        calls=[] if calls is None else calls, maximum_tool_calls=None, maximum_result_bytes=4_096,
        maximum_catalog_items=20, retrieval_channel="lexical",
        video_outline=video_outline, outline_evidence=outline_evidence,
    )


def _outline_result(*, series="Course", episode=1, available=True):
    if not available:
        return {"videos": [
            {"series": series, "episode": episode, "available": False,
             "reason": "该视频尚未生成章节大纲。"},
        ]}
    return {"videos": [{
        "series": series, "episode": episode, "available": True,
        "title": "课程标题", "description": "课程简介",
        "sections": [
            {"start_ms": 0, "end_ms": 10_000, "title": "开场", "summary": "老师介绍课程目标。",
             "video_chunk_ids": ["chunk-1", "chunk-2"]},
            {"start_ms": 10_000, "end_ms": 20_000, "title": "正题", "summary": "讲解核心算法。",
             "video_chunk_ids": ["chunk-3"]},
        ],
    }]}


def test_get_video_outline_schema_addresses_by_series_and_episode():
    assert set(GetVideoOutlineArguments.model_fields) == {"videos"}
    assert set(VideoOutlineItemArguments.model_fields) == {"series", "episode"}


def test_get_video_outline_batches_series_and_episode_without_video_id():
    received = []

    def video_outline(videos, *, tenant_id, series_ids):
        received.append((videos, tenant_id, series_ids))
        return _outline_result()

    tools = {tool.name: tool for tool in _tools(video_outline=video_outline)}
    assert "get_video_outline" in tools
    result = json.loads(tools["get_video_outline"].invoke({
        "videos": [{"series": "Course", "episode": 1}],
    }))
    assert result[0]["series"] == "Course"
    assert result[0]["episode"] == 1
    assert result[0]["available"] is True
    assert result[0]["title"] == "课程标题"
    assert result[0]["sections"][0]["video_chunk_ids"] == ["chunk-1", "chunk-2"]
    assert "video_id" not in result[0]
    assert received == [([{"series": "Course", "episode": 1}], "workspace", ["series"])]


def test_get_video_outline_unavailable_outline_returns_available_false():
    def video_outline(videos, *, tenant_id, series_ids):
        return _outline_result(available=False)

    tools = {tool.name: tool for tool in _tools(video_outline=video_outline)}
    result = json.loads(tools["get_video_outline"].invoke({
        "videos": [{"series": "Course", "episode": 1}],
    }))
    assert result == [{"series": "Course", "episode": 1, "available": False,
                       "reason": "该视频尚未生成章节大纲。"}]


def test_get_video_outline_reports_resolution_error():
    def video_outline(videos, *, tenant_id, series_ids):
        return {"error": {
            "code": "UNKNOWN_SERIES", "message": "unknown series",
            "hint": "Valid series: Course",
        }}

    tools = {tool.name: tool for tool in _tools(video_outline=video_outline)}
    result = json.loads(tools["get_video_outline"].invoke({
        "videos": [{"series": "Missing", "episode": 1}],
    }))
    assert result["error"]["code"] == "UNKNOWN_SERIES"
    assert "Course" in result["error"]["retry_hint"]


def test_outline_chunk_ids_are_readable_but_not_retrieval_hits():
    calls = []

    def video_outline(videos, *, tenant_id, series_ids):
        return _outline_result()

    def outline_evidence(video_chunk_id, *, tenant_id):
        return _hit(video_chunk_id).evidence

    tools = {tool.name: tool for tool in _tools(
        video_outline=video_outline, outline_evidence=outline_evidence, calls=calls,
    )}
    tools["get_video_outline"].invoke({"videos": [{"series": "Course", "episode": 1}]})
    # Outline chunk ids are readable on demand...
    read = json.loads(tools["read_retrieval_evidence"].invoke({"video_chunk_id": "chunk-2"}))
    assert read["video_chunk_id"] == "chunk-2"
    # ...but they never enter the retrieval registry, so submit stays gated.
    with pytest.raises(UnknownToolItemError):
        retrieval_hit(calls, "chunk-2")


def test_get_video_outline_only_for_supervisor_when_callable_injected():
    def video_outline(videos, *, tenant_id, series_ids):
        return _outline_result()

    supervisor = {tool.name for tool in _tools(role="supervisor", video_outline=video_outline)}
    query = {tool.name for tool in _tools(role="query", video_outline=video_outline)}
    without_callable = {tool.name for tool in _tools(role="supervisor", video_outline=None)}
    assert "get_video_outline" in supervisor
    assert "get_video_outline" not in query
    assert "get_video_outline" not in without_callable
    assert "retrieve" in supervisor and "retrieve" in query
