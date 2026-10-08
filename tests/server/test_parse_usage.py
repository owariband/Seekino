from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from gateway.usage import TokenUsage, capture_model_usage, model_call_context, track_model_call
from server.api.app import create_app
from server.api.parse_usage import project_usage_overview
from server.model.dto.parseUsageDTO import UsageObservationBatch
from server.service.parse_usage import ParseUsageService
from server.storage.langfuse import LangfuseQueryError
from server.utils.time import datetime_to_unix_time_ms

_NOW = datetime(2026, 10, 8, 0, 0, 30, tzinfo=UTC)


class Store:
    def __init__(self, observations=(), *, status="available", truncated=False, error=None):
        self.status = status
        self.batch = UsageObservationBatch(tuple(observations), truncated)
        self.error = error
        self.queries = []
        self.closed = False

    def query(self, **values):
        self.queries.append(values)
        if self.error is not None:
            raise self.error
        return self.batch

    def close(self):
        self.closed = True


def observation(identity="observation", *, metadata=None, **values):
    return {
        "id": identity, "type": "GENERATION", "traceId": "trace", "model": "model",
        "inputUsage": 100, "outputUsage": 20, "totalUsage": 120,
        "usageDetails": {"input": 100, "output": 20, "total": 120},
        "costDetails": {"input": "0.1", "output": "0.02", "total": "0.12"},
        "totalCost": "0.12", "modelId": "priced-model",
        "metadata": {
            "workspace_id": "workspace", "series_id": "series", "video_id": "video",
            "video_parse_task_id": "task", "run_id": "task", "call_id": identity,
            "provider": "provider", "category": "parse", "operation": "video_parse",
            "usage_details_semantics": "exclusive_buckets", **(metadata or {}),
        }, **values,
    }


def service(store):
    return ParseUsageService(store, clock=lambda: _NOW)


def test_overview_deduplicates_observations_and_counts_sdk_call_once():
    first = observation("first", metadata={"call_id": "sdk-call"}, totalCost="0.00000001",
                        costDetails={"input": "0.00000001", "output": "0", "total": "0.00000001"})
    second = observation("second", model="other-model", metadata={"call_id": "sdk-call"},
                         inputUsage=200, outputUsage=40, totalUsage=240,
                         usageDetails={"input": 200, "output": 40, "total": 240})
    span = observation("span", type="SPAN")
    state = service(Store([first, first, second, span])).overview(workspace_id="workspace")
    assert state.totals.call_count == 1
    assert state.totals.total_tokens == state.totals.known_tokens == 360
    assert state.totals.estimated_cost_usd == Decimal("0.12000001")
    assert len(state.models) == 2 and all(row.call_count == 1 for row in state.models)
    assert len(state.runs) == 1 and state.runs[0].kind == "parse"
    public = project_usage_overview(state).model_dump(by_alias=True)
    assert public["source"] == "langfuse" and public["currency"] == "USD"
    assert public["totals"]["estimatedCostUsd"] == "0.12000001"
    assert "estimatedCostCny" not in public["totals"]


def test_series_and_run_filters_are_exact_and_support_chat_series_arrays():
    chat = observation("chat", metadata={
        "series_id": None, "series_ids": '["series", "another"]', "run_id": "message",
        "video_parse_task_id": None, "video_id": None, "conversation_id": "conversation",
        "assistant_message_id": "message", "category": None,
    })
    other = observation("other", metadata={"series_id": "series-extra", "run_id": "task-extra"})
    foreign = observation("foreign", metadata={"workspace_id": "another-workspace"})
    store = Store([chat, other, foreign])
    state = service(store).overview(workspace_id="workspace", series_id="series", run_id="message")
    assert state.totals.call_count == 1
    assert state.runs[0].kind == "chat" and state.runs[0].series_id is None
    assert service(store).overview(workspace_id="workspace", run_id="mess").totals.call_count == 0
    assert store.queries[0]["workspace_id"] == "workspace"


def test_missing_usage_and_prices_remain_unknown_instead_of_zero():
    unknown = observation("unknown", inputUsage=0, outputUsage=0, totalUsage=0, usageDetails={},
                          totalCost=0, costDetails={}, modelId=None, metadata={"usage_status": "unknown"})
    state = service(Store([observation("known"), unknown])).overview(workspace_id="workspace")
    assert state.totals.call_count == 2
    assert state.totals.total_tokens is None and state.totals.known_tokens == 120
    assert state.totals.estimated_cost_usd is None and state.totals.known_cost_usd == Decimal("0.12")
    assert state.totals.missing_usage_count == state.totals.unpriced_call_count == 1


@pytest.mark.parametrize("field,value", [("inputUsage", -1), ("outputUsage", True), ("totalUsage", 1)])
def test_invalid_or_inconsistent_counters_are_not_accepted(field, value):
    row = observation("invalid", **{field: value})
    if field != "totalUsage":
        row["usageDetails"] = {}
    state = service(Store([row])).overview(workspace_id="workspace")
    assert state.totals.total_tokens is None
    assert state.totals.missing_usage_count == 1


def test_exclusive_input_buckets_and_subsets_are_not_double_counted():
    row = observation(
        inputUsage=None, outputUsage=None, totalUsage=None,
        usageDetails={"input": 70, "input_cached_tokens": 20, "input_cache_creation_5m_tokens": 10,
                      "input_cache_creation_1h_tokens": 5, "output": 20, "total": 125},
        costDetails={"input": "0.07", "input_cached_tokens": "0.01", "input_cache_creation_5m_tokens": "0.01",
                     "input_cache_creation_1h_tokens": "0.01", "output": "0.02", "total": "0.12"},
        metadata={"usage_subsets": '{"cached_input_tokens":20,"reasoning_tokens":5}'},
    )
    totals = service(Store([row])).overview(workspace_id="workspace").totals
    assert totals.input_tokens == 105 and totals.output_tokens == 20 and totals.total_tokens == 125
    assert totals.cached_input_tokens == 20 and totals.cache_write_input_tokens == 15
    assert totals.reasoning_tokens == 5 and totals.known_tokens == 125
    assert totals.estimated_cost_usd == Decimal("0.12")


@pytest.mark.parametrize("metadata,cost_details", [
    ({"usage_details_semantics": None}, {"input": "0.1", "output": "0.02", "total": "0.12"}),
    ({"usage_pricing_status": "incomplete"}, {"input": "0.1", "output": "0.02", "total": "0.12"}),
    ({}, {"input": "0.1", "total": "0.1"}),
])
def test_old_inclusive_or_partial_bucket_prices_are_unpriced(metadata, cost_details):
    row = observation(metadata=metadata, costDetails=cost_details)
    totals = service(Store([row])).overview(workspace_id="workspace").totals
    assert totals.estimated_cost_usd is None and totals.known_cost_usd == 0
    assert totals.unpriced_call_count == 1


def test_explicit_sdk_usd_cost_can_price_a_run_with_incomplete_buckets():
    row = observation(metadata={"usage_pricing_status": "incomplete", "cost_source": "sdk_estimate"},
                      costDetails={"total": "0.00000314"}, totalCost=0)
    totals = service(Store([row])).overview(workspace_id="workspace").totals
    assert totals.estimated_cost_usd == Decimal("0.00000314")
    assert totals.unpriced_call_count == 0


def test_zero_usage_and_zero_cost_require_an_explicit_basis():
    row = observation(inputUsage=0, outputUsage=0, totalUsage=0,
                      usageDetails={"input": 0, "output": 0, "total": 0},
                      costDetails={"input": 0, "output": 0, "total": 0}, totalCost=0)
    totals = service(Store([row])).overview(workspace_id="workspace").totals
    assert totals.total_tokens == 0 and totals.estimated_cost_usd == 0
    row["modelId"] = None
    assert service(Store([row])).overview(workspace_id="workspace").totals.estimated_cost_usd is None


def test_priced_model_does_not_turn_unknown_usage_into_free_consumption():
    row = observation(inputUsage=0, outputUsage=0, totalUsage=0,
                      costDetails={"input": 0, "output": 0, "total": 0}, totalCost=0,
                      metadata={"usage_status": "unknown"})
    assert service(Store([row])).overview(workspace_id="workspace").totals.estimated_cost_usd is None
    row["metadata"]["cost_source"] = "sdk_estimate"
    row["costDetails"] = {"total": "0.004"}
    assert service(Store([row])).overview(workspace_id="workspace").totals.estimated_cost_usd == Decimal("0.004")


@pytest.mark.parametrize("cost", ["NaN", "Infinity", "-1", True])
def test_invalid_usd_costs_are_unknown(cost):
    row = observation(totalCost=cost, costDetails={"total": cost})
    totals = service(Store([row])).overview(workspace_id="workspace").totals
    assert totals.estimated_cost_usd is None and totals.unpriced_call_count == 1


def test_truncated_result_reports_known_subtotals_without_complete_totals():
    state = service(Store([observation()], truncated=True)).overview(workspace_id="workspace")
    assert state.truncated and state.totals.total_tokens is None
    assert state.totals.known_tokens == 120 and state.totals.estimated_cost_usd is None
    assert state.totals.known_cost_usd == Decimal("0.12")
    assert state.models[0].total_tokens is None


def test_missing_id_is_not_counted_as_a_unique_model_observation():
    state = service(Store([observation(id=None)])).overview(workspace_id="workspace")
    assert state.truncated and state.totals.call_count == 0


@pytest.mark.parametrize("status", ["disabled", "not_configured"])
def test_inactive_langfuse_does_not_query_or_report_zero_consumption(status):
    store = Store(status=status)
    state = service(store).overview(workspace_id="workspace")
    assert state.status == status and state.message
    assert state.totals.call_count is None and state.totals.estimated_cost_usd is None
    assert not store.queries


def test_query_failure_is_unavailable_and_does_not_expose_remote_credentials():
    state = service(Store(error=LangfuseQueryError("secret credential"))).overview(workspace_id="workspace")
    assert state.status == "unavailable" and state.totals.call_count is None
    assert "secret credential" not in state.message
    assert not state.models and not state.runs


def test_default_window_uses_next_minute_and_exact_window_reaches_store():
    store = Store()
    state = service(store).overview(workspace_id="workspace")
    expected_end = datetime(2026, 10, 8, 0, 1, tzinfo=UTC)
    assert state.to_start_time == datetime_to_unix_time_ms(expected_end)
    assert state.to_start_time - state.from_start_time == 30 * 86_400_000
    assert store.queries[0]["to_start_time"] == expected_end
    explicit_end = datetime_to_unix_time_ms(_NOW)
    exact = service(store).overview(workspace_id="workspace", to_start_time=explicit_end)
    assert exact.to_start_time == explicit_end


@pytest.mark.parametrize("start,end", [(0, 32 * 86_400_000), (100, 100), (101, 100), (-1, 100), (True, 100)])
def test_invalid_windows_are_rejected_before_query(start, end):
    store = Store()
    with pytest.raises(ValueError):
        service(store).overview(workspace_id="workspace", from_start_time=start, to_start_time=end)
    assert not store.queries


def test_unknown_metadata_is_not_replaced_with_a_guessed_provider_or_model():
    row = observation(model=None, metadata={"provider": None, "category": None, "operation": None})
    model = service(Store([row])).overview(workspace_id="workspace").models[0]
    assert model.provider is model.model is model.category is model.operation is None


def test_capture_preserves_v1_and_v2_context_without_recording_sql_usage():
    traced = []

    class Tracing:
        @contextmanager
        def scope(self, context):
            with model_call_context(context), capture_model_usage(traced.append, combine=True):
                yield

    store = Store()
    capture = ParseUsageService(store, tracing=Tracing())
    lease = SimpleNamespace(workspace_id="workspace", series_id="series", video_id="video", job_id="job",
                            attempt_count=2, lease_version=7)
    with capture.capture(lease):
        with track_model_call(provider="provider", model="model", operation="video_parse") as call:
            call.usage = TokenUsage(100, 20, 120, 0, 0)
    v2_lease = SimpleNamespace(video_parse_task_id="task", lease_version=8)
    task_input = SimpleNamespace(workspace_id="workspace", series_id="series", task=SimpleNamespace(video_id="video"))
    with capture.capture_video(v2_lease, task_input, sequence_no=4, category="embedding"):
        with track_model_call(provider="provider", model="model", operation="text_embedding"):
            pass
    assert traced[0].context["job_id"] == "job" and traced[0].context["attempt_count"] == 2
    assert traced[1].context["video_parse_task_id"] == "task" and traced[1].context["sequence_no"] == 4
    assert traced[1].context["category"] == "embedding" and not store.queries
    capture.close()
    assert store.closed


@pytest.mark.parametrize("sequence_no", [-1, True, "1"])
def test_capture_video_keeps_sequence_validation(sequence_no):
    with pytest.raises(ValueError, match="sequence_no"):
        with service(Store()).capture_video(
            SimpleNamespace(), SimpleNamespace(), sequence_no=sequence_no, category="parse",
        ):
            pass


def test_overview_api_uses_usd_contract_and_retires_old_cny_route():
    application = create_app(services=SimpleNamespace(parse_usage=service(Store([observation()]))))
    with TestClient(application) as client:
        response = client.get("/api/usage/overview", params={"workspaceId": "workspace", "seriesId": "series", "runId": "task"})
        assert response.status_code == 200
        data = response.json()
        assert data["currency"] == "USD" and data["totals"]["estimatedCostUsd"] == "0.12"
        assert data["models"][0]["model"] == "model" and data["runs"][0]["runId"] == "task"
        assert client.get("/api/jobs/usage/get", params={"workspaceId": "workspace", "jobId": "job"}).status_code == 404
        assert client.get("/api/usage/overview").status_code == 422
        assert client.get("/api/usage/overview", params={"workspaceId": ""}).status_code == 422
        assert client.get("/api/usage/overview", params={"workspaceId": "x" * 65}).status_code == 422
        assert client.get("/api/usage/overview", params={"workspaceId": "workspace", "fromStartTime": -1}).status_code == 422
