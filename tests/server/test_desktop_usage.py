"""The desktop usage view projects the existing ledger for the native settings."""
from contextlib import ExitStack
import json
from types import SimpleNamespace

import pytest

from server.desktop.usage import overview
from server.model.dto.parseUsageDTO import (
    UsageCostSummary,
    UsageModelRow,
    UsageOverviewState,
    UsageRunRow,
    UsageTotals,
)


def state(*, status="available", message=None, truncated=False, cost=None):
    cost = cost or UsageCostSummary(estimated_amount="0.18", known_amount="0.18", priced_call_count=2)
    totals = UsageTotals(
        call_count=2, input_tokens=100, output_tokens=20, cached_input_tokens=None,
        cache_write_input_tokens=None, reasoning_tokens=None, total_tokens=120,
        known_tokens=120, missing_usage_count=0, cost=cost,
    )
    model = UsageModelRow(
        call_count=2, input_tokens=100, output_tokens=20, cached_input_tokens=None,
        cache_write_input_tokens=None, reasoning_tokens=None, total_tokens=120,
        known_tokens=120, missing_usage_count=0, cost=cost,
        provider="chat_api", model="fixture-vision", category="parse", operation="understand",
        src="desktop_source",
    )
    run = UsageRunRow(
        call_count=2, input_tokens=100, output_tokens=20, cached_input_tokens=None,
        cache_write_input_tokens=None, reasoning_tokens=None, total_tokens=120,
        known_tokens=120, missing_usage_count=0, cost=cost,
        run_id="parse-task", task_type="video_parse", series_id="series", video_id="video",
    )
    return UsageOverviewState(
        status=status, message=message, from_start_time=1000, to_start_time=2000,
        fetched_at=3000, truncated=truncated, totals=totals, models=(model,), runs=(run,),
    )


def test_projects_totals_models_and_runs_in_camel_case():
    result = overview(SimpleNamespace(usage_overview=lambda: state()))
    json.dumps(result, ensure_ascii=False, allow_nan=False)
    assert result["status"] == "available" and result["truncated"] is False
    assert result["fromStartTime"] == 1000 and result["fetchedAt"] == 3000
    assert result["totals"] == {
        "callCount": 2, "inputTokens": 100, "outputTokens": 20, "cachedInputTokens": None,
        "cacheWriteInputTokens": None, "reasoningTokens": None, "totalTokens": 120,
        "knownTokens": 120, "missingUsageCount": 0,
        "cost": {
            "currency": "CNY", "estimatedAmount": "0.18", "knownAmount": "0.18",
            "pricedCallCount": 2, "unpricedCallCount": 0, "excludedCallCount": 0,
        },
    }
    assert result["models"][0] == {
        "callCount": 2, "inputTokens": 100, "outputTokens": 20, "cachedInputTokens": None,
        "cacheWriteInputTokens": None, "reasoningTokens": None, "totalTokens": 120,
        "knownTokens": 120, "missingUsageCount": 0,
        "cost": result["totals"]["cost"], "provider": "chat_api", "model": "fixture-vision",
        "category": "parse", "operation": "understand", "src": "desktop_source",
    }
    assert result["runs"][0]["runId"] == "parse-task"
    assert result["runs"][0]["taskType"] == "video_parse"


def test_projects_unavailable_ledger_without_inventing_zero_usage():
    result = overview(SimpleNamespace(usage_overview=lambda: state(
        status="unavailable", message="模型消耗暂时无法读取，请稍后重试。",
        cost=UsageCostSummary(),
    )))
    assert result["status"] == "unavailable" and result["message"]
    assert result["totals"]["callCount"] == 2
    assert result["totals"]["cost"]["estimatedAmount"] is None


def test_desktop_library_exposes_the_existing_usage_service():
    pytest.importorskip("av")
    from server.desktop.runtime import DesktopLibrary

    expected = state()
    calls = []

    def service_overview(*, tenant_id):
        calls.append(tenant_id)
        return expected

    library = DesktopLibrary(SimpleNamespace(usage=SimpleNamespace(overview=service_overview)), ExitStack())
    try:
        assert library.usage_overview() is expected
        assert calls == ["local-default"]
    finally:
        library.close()


def test_task_detail_filters_repository_before_grouping_segment_and_model_usage(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from uuid import uuid4
    from server.desktop.database import open_desktop_database
    from server.repository.model_usage import ModelUsageRepository
    from server.service.parse_usage import ParseUsageService
    from tests.server.test_parse_usage import recorded

    db = open_desktop_database(tmp_path / "usage.sqlite3")
    now = datetime.now(timezone.utc)
    task_id = str(uuid4())
    repo = ModelUsageRepository(db.session_factory)
    service = ParseUsageService(repo)
    def write(kind, run, sequence, unknown=False):
        context = {} if sequence is None else {"sequence_no":sequence}
        timestamp = dict(started_at=now, created_at=now)
        row = recorded(str(uuid4()), task_type=kind, run_id=run, tenant_id="local-default",
                       unknown=unknown, metadata={"context":context}, **timestamp)
        repo.record_call(row)
    try:
        write("video_parse", task_id, 0)
        write("video_parse", task_id, 1, unknown=True)
        write("video_parse", task_id, None)
        write("video_parse", str(uuid4()), 4)
        write("chat_retrieval", task_id, None)
        requests = []
        query = repo.query
        def observe(**filters):
            requests.append(filters)
            return query(**filters)
        monkeypatch.setattr(repo, "query", observe)
        library = SimpleNamespace(usage_overview=lambda job: service.overview(tenant_id="local-default",
                            run_id=job, include_parse_segments=True))
        result = overview(library, task_id)
        assert len(requests) == 1
        assert requests[0]["run_id"] == task_id and requests[0]["task_type"] == "video_parse"
        assert result["totals"]["callCount"] == 3 and result["totals"]["totalTokens"] is None
        assert [(v["sequence"],v["totalTokens"]) for v in result["segments"]] == [(0,120),(1,None),(None,120)]
        assert [v["callCount"] for v in result["models"]] == [3]
    finally:
        db.close()
