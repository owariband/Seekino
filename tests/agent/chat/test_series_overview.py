from __future__ import annotations

import json

from agent.chat.video_search.tools import build_retrieval_tools
from agent.chat.video_search.tools.series_overview import GetSeriesOverviewArguments
from tests.agent.chat.test_tools import Retrieval, _control, _hit, _inputs


def _tools(*, role="supervisor", series_overview=None):
    return build_retrieval_tools(
        Retrieval([_hit("chunk-1")]).search, role=role, inputs=_inputs(), control=_control(),
        calls=[], maximum_tool_calls=None, maximum_result_bytes=4_096,
        maximum_catalog_items=20, retrieval_channel="lexical",
        series_overview=series_overview,
    )


def _overview_result():
    return {"series": [{
        "series": "Course", "displayName": "机器学习",
        "videos": [
            {"episode": 1, "title": "课程标题", "description": "课程简介",
             "summarized": True, "hasOutline": True},
        ],
    }]}


def test_get_series_overview_schema_has_optional_series():
    assert set(GetSeriesOverviewArguments.model_fields) == {"series"}


def test_get_series_overview_returns_series_and_videos_without_video_id():
    received = []

    def series_overview(series, *, tenant_id, series_ids):
        received.append((series, tenant_id, series_ids))
        return _overview_result()

    tools = {tool.name: tool for tool in _tools(series_overview=series_overview)}
    assert "get_series_overview" in tools
    result = json.loads(tools["get_series_overview"].invoke({}))
    assert result[0]["series"] == "Course"
    assert result[0]["displayName"] == "机器学习"
    assert result[0]["videos"][0]["episode"] == 1
    assert "video_id" not in result[0]
    assert "video_id" not in result[0]["videos"][0]
    assert received == [(None, "workspace", ["series"])]


def test_get_series_overview_reports_unknown_series():
    def series_overview(series, *, tenant_id, series_ids):
        return {"error": {
            "code": "UNKNOWN_SERIES", "message": "unknown series",
            "hint": "Valid series: Course",
        }}

    tools = {tool.name: tool for tool in _tools(series_overview=series_overview)}
    result = json.loads(tools["get_series_overview"].invoke({"series": "Missing"}))
    assert result["error"]["code"] == "UNKNOWN_SERIES"
    assert "Course" in result["error"]["retry_hint"]


def test_get_series_overview_only_for_supervisor_when_callable_injected():
    def series_overview(series, *, tenant_id, series_ids):
        return _overview_result()

    supervisor = {tool.name for tool in _tools(role="supervisor", series_overview=series_overview)}
    query = {tool.name for tool in _tools(role="query", series_overview=series_overview)}
    without_callable = {tool.name for tool in _tools(role="supervisor", series_overview=None)}
    assert "get_series_overview" in supervisor
    assert "get_series_overview" not in query
    assert "get_series_overview" not in without_callable
    assert "retrieve" in supervisor and "retrieve" in query
