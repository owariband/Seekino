from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json

import httpx
import pytest

from gateway.config import TracingConfig
from server.storage.langfuse import LangfuseObservationStore, LangfuseQueryError


START = datetime(2026, 10, 1, tzinfo=UTC)
END = START + timedelta(days=1)
ENV = {
    "LANGFUSE_BASE_URL": "https://langfuse.example/prefix",
    "LANGFUSE_PUBLIC_KEY": "test-public",
    "LANGFUSE_SECRET_KEY": "test-secret",
}


def test_paginated_scoped_reads_keep_usage_and_share_cached_result():
    requests = []

    def respond(request):
        requests.append(request)
        assert request.url.path == "/prefix/api/public/v2/observations"
        assert request.headers["Authorization"].startswith("Basic ")
        filters = json.loads(request.url.params["filter"])
        assert {condition["column"] for condition in filters} == {"metadata", "startTime", "environment", "type"}
        assert filters[0] == {"type": "stringObject", "column": "metadata", "key": "tenant_id", "operator": "=", "value": "workspace-a"}
        assert request.url.params["fields"] == "core,basic,metadata,model,usage"
        page = "second" if request.url.params.get("cursor") else "first"
        return httpx.Response(200, json={
            "data": [{"id": page, "metadata": {"tenant_id": "workspace-a"}, "costDetails": {"total": 0.001}, "totalUsage": 100}],
            "meta": {"cursor": None if page == "second" else "next"},
        })

    store = LangfuseObservationStore(TracingConfig(enabled=True), environ=ENV, transport=httpx.MockTransport(respond))
    try:
        result = store.query(tenant_id="workspace-a", from_start_time=START, to_start_time=END)
        assert [row["id"] for row in result.observations] == ["first", "second"]
        assert result.observations[0]["costDetails"] == {"total": Decimal("0.001")}
        assert not result.truncated
        assert store.query(tenant_id="workspace-a", from_start_time=START, to_start_time=END) is result
        assert len(requests) == 2
    finally:
        store.close()
    with pytest.raises(LangfuseQueryError, match="关闭"):
        store.query(tenant_id="workspace-a", from_start_time=START, to_start_time=END)


def test_cache_is_scoped_by_workspace_and_time_window():
    requests = []

    def respond(request):
        requests.append(request)
        workspace = json.loads(request.url.params["filter"])[0]["value"]
        return httpx.Response(200, json={"data": [{"id": workspace, "metadata": {"tenant_id": workspace}}], "meta": {"cursor": None}})

    store = LangfuseObservationStore(TracingConfig(enabled=True), environ=ENV, transport=httpx.MockTransport(respond))
    try:
        for workspace, end in (("one", END), ("two", END), ("one", END + timedelta(hours=1))):
            result = store.query(tenant_id=workspace, from_start_time=START, to_start_time=end)
            assert result.observations[0]["id"] == workspace
        assert len(requests) == 3
    finally:
        store.close()


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_remote_errors_are_sanitized_and_not_cached(status):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, text="private-provider-error-and-secret")

    store = LangfuseObservationStore(TracingConfig(enabled=True), environ=ENV, transport=httpx.MockTransport(respond))
    try:
        for _ in range(2):
            with pytest.raises(LangfuseQueryError) as error:
                store.query(tenant_id="one", from_start_time=START, to_start_time=END)
            assert "private-provider" not in str(error.value)
            assert "test-secret" not in str(error.value)
        assert len(requests) == 2
    finally:
        store.close()


def test_foreign_workspace_rows_fail_closed():
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"data": [{"metadata": {"tenant_id": "other"}}]}))
    store = LangfuseObservationStore(TracingConfig(enabled=True), environ=ENV, transport=transport)
    try:
        with pytest.raises(LangfuseQueryError, match="范围"):
            store.query(tenant_id="one", from_start_time=START, to_start_time=END)
    finally:
        store.close()


def test_numeric_usd_preserves_wire_decimal_precision():
    payload = '{"data":[{"id":"one","metadata":{"tenant_id":"one"},"totalCost":0.12345678901234567890123}],"meta":{"cursor":null}}'
    transport = httpx.MockTransport(lambda _: httpx.Response(200, text=payload))
    store = LangfuseObservationStore(TracingConfig(enabled=True), environ=ENV, transport=transport)
    try:
        result = store.query(tenant_id="one", from_start_time=START, to_start_time=END)
        assert result.observations[0]["totalCost"] == Decimal("0.12345678901234567890123")
    finally:
        store.close()


def test_repeated_cursor_marks_partial_and_terminates():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [{"id": "same", "metadata": {"tenant_id": "one"}}], "meta": {"cursor": "same-cursor"}})

    store = LangfuseObservationStore(TracingConfig(enabled=True), environ=ENV, transport=httpx.MockTransport(respond))
    try:
        result = store.query(tenant_id="one", from_start_time=START, to_start_time=END)
        assert result.truncated
        assert len(requests) == 2
    finally:
        store.close()


def test_disabled_and_unconfigured_stores_make_no_requests():
    for settings, environ, status in ((TracingConfig(), ENV, "disabled"), (TracingConfig(enabled=True), {}, "not_configured")):
        store = LangfuseObservationStore(settings, environ=environ)
        assert store.status == status
        with pytest.raises(LangfuseQueryError):
            store.query(tenant_id="one", from_start_time=START, to_start_time=END)
        store.close()


def test_rejects_unbounded_or_naive_windows_before_http():
    store = LangfuseObservationStore(TracingConfig(), environ={})
    try:
        for start, end in ((START, START), (START, START + timedelta(days=32)), (START.replace(tzinfo=None), END)):
            with pytest.raises(ValueError, match="bounded"):
                store.query(tenant_id="one", from_start_time=start, to_start_time=end)
    finally:
        store.close()
