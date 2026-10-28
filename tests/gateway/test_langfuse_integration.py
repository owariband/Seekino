import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest, ExportTraceServiceResponse
from opentelemetry.sdk.trace.export import SpanExportResult

from gateway.config import TracingConfig
from gateway import tracing as tracing_module
from gateway.tracing import LangfuseTracing, _ProcessExporter
from gateway.usage import TokenUsage, capture_model_usage, track_model_call


@contextmanager
def backend(monkeypatch, *, blocked=False, status=200, rejected=False):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    requests = []
    received, release = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append((self.path, dict(self.headers), payload))
            received.set()
            if blocked:
                release.wait(10)
            try:
                body = ExportTraceServiceResponse()
                if rejected:
                    body.partial_success.rejected_spans = 1
                payload = body.SerializeToString()
                self.send_response(status)
                self.send_header("Content-Type", "application/x-protobuf")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests, received
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(2)


def tracing_for(base_url, **configuration):
    return LangfuseTracing(TracingConfig(enabled=True, base_url=base_url, **configuration), environ={
        "LANGFUSE_PUBLIC_KEY": "pk-local-test", "LANGFUSE_SECRET_KEY": "sk-local-test",
    })


def test_offline_otlp_payload_headers_hierarchy_and_time(monkeypatch) -> None:
    with backend(monkeypatch) as (url, requests, received):
        tracing = tracing_for(url, close_timeout_seconds=3)
        calls = []
        with capture_model_usage(calls.append), tracing.scope({"run_id": "run", "session_id": "session"}):
            with track_model_call(provider="fake", model="requested", operation="generate") as tracker:
                tracker.model = "actual"
                tracker.usage = TokenUsage(10, 3, 13, 4, 2)
        tracing.close()
        report = tracing.close_report
        assert report["exported"] == 2
        assert report["failed"] == report["dropped"] == report["unconfirmed"] == 0
        assert not report["forced"]
        assert report["worker_exited"] and report["ipc_thread_exited"]
        assert received.is_set()
        spans = []
        for path, headers, payload in requests:
            assert path == "/api/public/otel/v1/traces"
            assert headers["Content-Type"] == "application/x-protobuf"
            assert headers["Authorization"].startswith("Basic ")
            assert headers["x-langfuse-ingestion-version"] == "4"
            request = ExportTraceServiceRequest.FromString(payload)
            spans.extend(span for resource in request.resource_spans for scope in resource.scope_spans for span in scope.spans)
        generation, root = spans
        attributes = {item.key: item.value.string_value for item in generation.attributes}
        assert generation.parent_span_id == root.span_id
        assert generation.trace_id == root.trace_id
        assert generation.start_time_unix_nano == calls[0].started_at * 1_000_000
        assert generation.end_time_unix_nano == calls[0].ended_at * 1_000_000
        assert attributes["langfuse.observation.model.name"] == "actual"
        assert attributes["session.id"] == "session"
        assert attributes["langfuse.trace.metadata.run_id"] == "run"
        assert json.loads(attributes["langfuse.observation.usage_details"]) == {"input": 6, "input_cached_tokens": 4, "output": 3, "total": 13}
        assert attributes["langfuse.observation.metadata.usage_details_semantics"] == "exclusive_buckets"
        assert not any("input" == key or "output" == key for key in attributes)


def test_blocked_http_close_reclaims_resources_and_reports_unconfirmed(monkeypatch) -> None:
    with backend(monkeypatch, blocked=True) as (url, requests, received):
        tracing = tracing_for(url, export_timeout_seconds=10, close_timeout_seconds=0.5, queue_size=16)
        with tracing.scope({}):
            with track_model_call(provider="fake", model="model", operation="generate"):
                result = "model-result"
        assert received.wait(5)
        started = time.monotonic()
        tracing.close()
        elapsed = time.monotonic() - started
        report = tracing.close_report
        assert result == "model-result"
        assert report["forced"] and report["unconfirmed"] >= 1
        assert report["dropped"] == 0
        assert report["worker_exited"] and report["ipc_thread_exited"]
        assert elapsed < 0.7
        tracing.close()
        assert tracing.close_report is report


def test_backlog_is_bounded_and_does_not_retry_model(monkeypatch, caplog) -> None:
    with backend(monkeypatch, blocked=True) as (url, requests, received):
        tracing = tracing_for(url, close_timeout_seconds=0.5, queue_size=1)
        calls = []
        with capture_model_usage(calls.append), tracing.scope({}):
            for _ in range(100):
                with track_model_call(provider="fake", model="model", operation="generate"):
                    pass
        tracing.close()
        report = tracing.close_report
        assert len(calls) == 100
        assert report["dropped"] > 0
        assert report["worker_exited"] and report["ipc_thread_exited"]
        assert "queue rejected" in caplog.text


@pytest.mark.parametrize("status", [401, 500])
def test_backend_errors_are_transport_failures_and_keep_usage(monkeypatch, status) -> None:
    with backend(monkeypatch, status=status) as (url, requests, received):
        tracing = tracing_for(url, close_timeout_seconds=3)
        calls = []
        with capture_model_usage(calls.append), tracing.scope({}):
            with track_model_call(provider="fake", model="model", operation="generate") as tracker:
                tracker.usage = TokenUsage(1, 1, 2, None, None)
        tracing.close()
        report = tracing.close_report
        assert report["failed"] == 2
        assert report["exported"] == 0
        assert len(calls) == 1 and calls[0].usage.total_tokens == 2
        assert report["worker_exited"] and report["ipc_thread_exited"]


def test_http_success_with_collector_rejection_is_failed_export(monkeypatch) -> None:
    with backend(monkeypatch, rejected=True) as (url, requests, received):
        tracing = tracing_for(url, close_timeout_seconds=3)
        with tracing.scope({}):
            with track_model_call(provider="fake", model="model", operation="generate"):
                pass
        tracing.close()
        assert tracing.close_report["failed"] == 2
        assert tracing.close_report["exported"] == 0


def test_concurrent_close_does_not_block_export_or_repeat_close(monkeypatch) -> None:
    with backend(monkeypatch, blocked=True) as (url, requests, received):
        tracing = tracing_for(url, close_timeout_seconds=0.5)
        with tracing.scope({}):
            with track_model_call(provider="fake", model="model", operation="generate"):
                pass
        assert received.wait(5)
        exporter = tracing._exporter
        waiting, continue_close = threading.Event(), threading.Event()
        original_join = exporter._thread.join

        def join(timeout):
            waiting.set()
            assert continue_close.wait(1)
            original_join(timeout)

        monkeypatch.setattr(exporter._thread, "join", join)
        closing = threading.Thread(target=tracing.close)
        closing.start()
        try:
            assert waiting.wait(1)
            started = time.monotonic()
            assert exporter.export([object()]) is SpanExportResult.FAILURE
            exporter.shutdown()
            assert time.monotonic() - started < 0.1
        finally:
            continue_close.set()
            closing.join(2)
        assert not closing.is_alive()
        assert tracing.close_report["dropped"] == 1
        assert tracing.close_report["exported"] == 0
        assert tracing.close_report["worker_exited"] and tracing.close_report["ipc_thread_exited"]


def test_ipc_thread_start_failure_reclaims_spawned_process_and_sockets(monkeypatch) -> None:
    processes, sockets = [], []
    original_popen = tracing_module.subprocess.Popen
    original_socketpair = tracing_module.socket.socketpair

    def popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process

    def socketpair():
        pair = original_socketpair()
        sockets.extend(pair)
        return pair

    def fail_start(self):
        raise RuntimeError("thread creation failed")

    monkeypatch.setattr(tracing_module.subprocess, "Popen", popen)
    monkeypatch.setattr(tracing_module.socket, "socketpair", socketpair)
    monkeypatch.setattr(tracing_module.Thread, "start", fail_start)
    with pytest.raises(RuntimeError, match="thread creation failed"):
        _ProcessExporter(TracingConfig(close_timeout_seconds=0.5), "pk", "sk", "http://127.0.0.1:1")
    assert len(processes) == 1 and processes[0].poll() is not None
    assert all(connection.fileno() == -1 for connection in sockets)


@pytest.mark.skipif(os.environ.get("SEEKINO_LANGFUSE_INTEGRATION") != "1",
                    reason="Explicit real Langfuse project authorization and credentials required")
def test_real_langfuse_v2_observations_read_back() -> None:
    """Opt-in synthetic records; no model, media, or customer content is sent."""
    import httpx

    base_url = os.environ["LANGFUSE_BASE_URL"].rstrip("/")
    public_key = os.environ["LANGFUSE_PUBLIC_KEY"]
    secret_key = os.environ["LANGFUSE_SECRET_KEY"]
    trace_id, run_id = uuid.uuid4().hex, uuid.uuid4().hex
    before = datetime.now(timezone.utc) - timedelta(seconds=1)
    tracing = LangfuseTracing(TracingConfig(enabled=True, base_url=base_url,
                                           environment="integration-test", release="synthetic-probe"), environ={
        "LANGFUSE_PUBLIC_KEY": public_key, "LANGFUSE_SECRET_KEY": secret_key,
    })
    calls = []
    try:
        with capture_model_usage(calls.append), tracing.scope({
            "trace_id": trace_id, "parent_span_id": "1" * 16,
            "run_id": run_id, "session_id": run_id, "cost_cny": "1.25", "cost_currency": "CNY",
        }):
            with track_model_call(provider="synthetic", model="seekino-synthetic-model", operation="generate") as tracker:
                tracker.usage = TokenUsage(10, 3, 13, 4, 2)
            with track_model_call(provider="synthetic", model="seekino-synthetic-embedding", operation="text_embedding") as tracker:
                tracker.usage = TokenUsage(0, 0, 0, None, None)
            with track_model_call(provider="synthetic", model="seekino-synthetic-aggregate", operation="agent",
                                  granularity="sdk_turn_run", usage_source="synthetic_result"):
                pass
    finally:
        tracing.close()
    assert tracing.close_report["exported"] == 4
    assert tracing.close_report["failed"] == tracing.close_report["dropped"] == tracing.close_report["unconfirmed"] == 0
    parameters = {
        "traceId": trace_id, "fromStartTime": before.isoformat(),
        "toStartTime": (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),
        "fields": "core,basic,model,usage,metadata,metrics,trace_context,io", "limit": 100,
    }
    rows = []
    deadline = time.monotonic() + 30
    with httpx.Client(timeout=5, auth=(public_key, secret_key)) as client:
        while time.monotonic() < deadline:
            response = client.get(f"{base_url}/api/public/v2/observations", params=parameters)
            response.raise_for_status()
            result = response.json()
            rows = result["data"]
            assert not result.get("meta", {}).get("cursor"), "Unexpected duplicate or additional observations"
            if len(rows) == 4:
                break
            time.sleep(0.5)
    assert len(rows) == 4, "HTTP export acknowledgements alone do not prove backend records are readable"
    by_name = {row["name"]: row for row in rows}
    root, generation = by_name["seekino.execute"], by_name["generate"]
    assert root["parentObservationId"] == "1" * 16
    assert all(row["traceId"] == trace_id and row["sessionId"] == run_id for row in rows)
    assert all(row["environment"] == "integration-test" and row["release"] == "synthetic-probe" for row in rows)
    for call in calls:
        observation = by_name[call.operation]
        assert observation["parentObservationId"] == root["id"]
        assert observation["metadata"]["call_id"] == call.call_id
        assert observation["metadata"]["status"] == call.status
        start = datetime.fromisoformat(observation["startTime"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(observation["endTime"].replace("Z", "+00:00"))
        assert abs(start.timestamp() * 1000 - call.started_at) <= 1
        assert abs(end.timestamp() * 1000 - call.ended_at) <= 1
    assert generation["model"] == "seekino-synthetic-model"
    assert generation["usageDetails"] == {"input": 10, "output": 3, "total": 13}
    assert by_name["text_embedding"]["type"] == "EMBEDDING"
    assert by_name["text_embedding"]["usageDetails"] == {"input": 0, "output": 0, "total": 0}
    aggregate = by_name["agent"]
    assert aggregate["metadata"]["usage_status"] == "unknown"
    assert aggregate["metadata"]["granularity"] == "sdk_turn_run"
    assert "usage_source" not in aggregate["metadata"]
    assert aggregate.get("usageDetails") in (None, {})
    for row in rows:
        assert row["metadata"]["cost_cny"] == "1.25"
        assert row["metadata"]["cost_currency"] == "CNY"
        assert row.get("input") in (None, "") and row.get("output") in (None, "")
        assert not row.get("costDetails")
