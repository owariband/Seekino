from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from gateway.usage import SDKModelUsage, TokenUsage, capture_model_usage, model_call_context, track_model_call
from server.api.app import create_app
from server.api.parse_usage import project_usage_overview
from server.database import DatabaseEnvironment, create_database_engine, create_session_factory
from server.model.dto.parseUsageDTO import ModelCallUsageBatch, ModelUsageState
from server.model.parse_usage import ModelUsageRecord
from server.repository.model_usage import ModelUsageRepository
from server.service.parse_usage import ParseUsageService
from server.utils.time import datetime_to_unix_time_ms, unix_time_ms_to_datetime

_NOW = datetime(2026, 10, 8, 0, 0, 30, tzinfo=UTC)


class Repository:
    def __init__(self, records=(), *, truncated=False, error=None):
        self.batch = ModelCallUsageBatch(tuple(records), truncated)
        self.error = error
        self.queries = []
        self.appended = []

    def query(self, **values):
        self.queries.append(values)
        if self.error is not None:
            raise self.error
        return self.batch

    def record_call(self, state):
        if self.error is not None:
            raise self.error
        self.appended.append(state)
        return state


def service(repository):
    return ParseUsageService(repository, clock=lambda: _NOW)


def video_scope(capture):
    return capture.capture_video(
        SimpleNamespace(video_parse_task_id="task", lease_version=8),
        SimpleNamespace(workspace_id="workspace", series_id="series", task=SimpleNamespace(video_id="video")),
        sequence_no=4, category="embedding",
    )


def chat_context():
    return {"workspace_id": "workspace", "assistant_message_id": "message", "run_id": "message",
            "conversation_id": "conversation", "series_ids": ("series", "other"), "stage": "agent_chat"}


def test_capture_video_combines_observers_and_persists_one_explicit_snapshot(tmp_path):
    engine = create_database_engine(f"sqlite+pysqlite:///{tmp_path / 'usage.db'}", environment=DatabaseEnvironment.TEST)
    ModelUsageRecord.__table__.create(engine)
    repository = ModelUsageRepository(create_session_factory(engine))
    capture, outer = service(repository), []
    try:
        with capture_model_usage(outer.append), video_scope(capture), video_scope(capture):
            with model_call_context({"prompt": "private", "stage": "index", "src": "actual-source"}):
                with track_model_call(provider="provider", model="actual", operation="text_embedding") as tracker:
                    tracker.usage = TokenUsage(100, 0, 100, 30, None)
        assert len(outer) == 1
        batch = repository.query(workspace_id="workspace", from_start_time=unix_time_ms_to_datetime(outer[0].started_at - 1),
                                 to_start_time=unix_time_ms_to_datetime(outer[0].started_at + 1))
        assert len(batch.records) == 1
        row = batch.records[0]
        assert row.task_type == "video_parse" and row.run_id == "task"
        assert row.metadata["src"] == "actual-source" and row.metadata["model_name"] == "actual"
        assert row.metadata["context"]["stage"] == "index" and row.metadata["context"]["sequence_no"] == 4
        assert "prompt" not in row.metadata["context"] and "video_parse_task_id" not in row.metadata["context"]
        assert row.usage["tokens"]["cached_input_tokens"] == 30
    finally:
        engine.dispose()


def test_chat_thread_inherits_owner_and_sdk_cost_is_not_persisted():
    repository = Repository()
    capture = service(repository)

    def invoke():
        with model_call_context({"stage": "embedding", "series_ids": ["series"], "query": "private"}):
            with track_model_call(provider="sdk", model="parent", operation="chat", granularity="sdk_turn_run") as tracker:
                tracker.sdk_model_usage = (SDKModelUsage("actual-child", TokenUsage(10, 2, 12, None, None), 1.5),)

    async def execute():
        with capture.capture_chat(chat_context()), capture.capture_chat(chat_context()):
            await asyncio.to_thread(invoke)

    asyncio.run(execute())
    assert len(repository.appended) == 1
    row = repository.appended[0]
    assert row.task_type == "chat_retrieval" and row.run_id == "message"
    assert row.metadata["context"]["series_ids"] == ("series",)
    assert "assistant_message_id" not in row.metadata["context"] and "query" not in row.metadata["context"]
    assert set(row.usage["sdk_model_usage"][0]) == {"model_name", "tokens"}


def test_parallel_chat_scopes_do_not_cross_workspace_or_run(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    engine = create_database_engine(f"sqlite+pysqlite:///{tmp_path / 'parallel.db'}", environment=DatabaseEnvironment.TEST)
    ModelUsageRecord.__table__.create(engine)
    repository = ModelUsageRepository(create_session_factory(engine))
    capture = ParseUsageService(repository)
    barrier = Barrier(2)

    def invoke(index):
        with capture.capture_chat({"workspace_id": f"workspace-{index}", "assistant_message_id": f"message-{index}"}):
            with track_model_call(provider="p", model="m", operation="chat") as tracker:
                barrier.wait(timeout=5)
                tracker.usage = TokenUsage(10 + index, 0, 10 + index, None, None)
                return tracker.call_id, tracker.started_at

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            calls = list(executor.map(invoke, range(2)))
        for index, (call_id, started_at) in enumerate(calls):
            rows = repository.query(workspace_id=f"workspace-{index}",
                                    from_start_time=unix_time_ms_to_datetime(started_at - 1),
                                    to_start_time=unix_time_ms_to_datetime(started_at + 1)).records
            assert len(rows) == 1 and rows[0].call_id == call_id
            assert rows[0].run_id == f"message-{index}" and rows[0].task_type == "chat_retrieval"
            assert rows[0].usage["tokens"]["total_tokens"] == 10 + index
    finally:
        engine.dispose()


def test_failed_call_keeps_confirmed_usage_and_write_failure_preserves_business(caplog):
    repository = Repository()
    capture = service(repository)
    with pytest.raises(RuntimeError, match="provider failed"):
        with capture.capture_chat(chat_context()):
            with track_model_call(provider="p", model="m", operation="chat") as tracker:
                tracker.usage = TokenUsage(10, 2, 12, 0, 0)
                raise RuntimeError("provider failed")
    assert repository.appended[0].metadata["status"] == "failed"
    assert repository.appended[0].usage["tokens"]["total_tokens"] == 12
    capture = service(Repository(error=RuntimeError("secret credential")))
    with capture.capture_chat(chat_context()):
        with track_model_call(provider="p", model="m", operation="chat"):
            business_result = "answer"
    assert business_result == "answer"
    assert "append failed" in caplog.text and "secret credential" not in caplog.text


@pytest.mark.parametrize("changes", [
    {"run_id": "other"}, {"workspace_id": "other"}, {"assistant_message_id": "other"},
    {"video_parse_task_id": "task"},
])
def test_conflicting_context_is_rejected_without_recording(changes, caplog):
    repository = Repository()
    capture = service(repository)
    with capture.capture_chat(chat_context()), model_call_context(changes):
        with track_model_call(provider="p", model="m", operation="chat"):
            pass
    assert not repository.appended and "append failed" in caplog.text


def test_v1_and_standalone_retrieval_do_not_fabricate_an_owner():
    repository = Repository()
    capture = service(repository)
    lease = SimpleNamespace(workspace_id="workspace", series_id="series", video_id="video", job_id="job",
                            attempt_count=2, lease_version=7)
    with capture.capture(lease):
        with track_model_call(provider="p", model="m", operation="video_parse"):
            pass
    with model_call_context({"workspace_id": "workspace", "series_ids": ["series"]}):
        with track_model_call(provider="p", model="m", operation="text_embedding"):
            pass
    # Even an outer Chat subscriber must ignore a legacy V1 job context.
    with capture.capture_chat(chat_context()), capture.capture(lease):
        with track_model_call(provider="p", model="m", operation="video_parse"):
            pass
    assert not repository.appended


@pytest.mark.parametrize("sequence_no", [-1, True, "1"])
def test_capture_video_keeps_sequence_validation(sequence_no):
    with pytest.raises(ValueError, match="sequence_no"):
        with service(Repository()).capture_video(SimpleNamespace(), SimpleNamespace(), sequence_no=sequence_no, category="parse"):
            pass


def test_capture_video_keeps_category_validation():
    with pytest.raises(ValueError, match="category"):
        with service(Repository()).capture_video(SimpleNamespace(), SimpleNamespace(), sequence_no=0, category="other"):
            pass


def recorded(identity="call", *, tokens=None, unknown=False, sdk=(), metadata=None, **changes):
    values = dict(
        call_id=identity, workspace_id="workspace", task_type="video_parse", run_id="task",
        started_at=_NOW, created_at=_NOW,
        usage={"tokens": None if unknown else asdict(tokens or TokenUsage(100, 20, 120, 30, 5)),
               "sdk_model_usage": [{"model_name": model, "tokens": asdict(usage) if usage is not None else None}
                                   for model, usage in sdk]},
        metadata={"model_name": "actual", "provider": "provider", "src": None, "operation": "video_parse",
                  "status": "completed", "request_id": None, "ended_at": None,
                  "granularity": "provider_request", "usage_source": "provider_usage", "usage_scope": None,
                  "context": {"series_id": "series", "video_id": "video", "category": "parse"},
                  **(metadata or {})},
    )
    values.update(changes)
    return ModelUsageState(**values)


def test_sql_overview_sdk_details_replace_parent_and_call_count_is_distinct():
    sdk = recorded(sdk=(
        ("child-A", TokenUsage(10, 2, 12, None, None)),
        ("child-B", TokenUsage(20, 4, 24, None, None)),
        ("child-A", TokenUsage(5, 1, 6, None, None)),
    ))
    state = service(Repository([sdk, sdk])).overview(workspace_id="workspace")
    assert state.totals.call_count == 1
    assert state.totals.total_tokens == state.totals.known_tokens == 42
    assert {row.model: (row.call_count, row.total_tokens) for row in state.models} == {
        "child-A": (1, 18), "child-B": (1, 24),
    }
    assert state.runs[0].task_type == "video_parse" and state.runs[0].run_id == "task"
    public = project_usage_overview(state).model_dump(by_alias=True)
    assert public["source"] == "local" and public["status"] == "available"
    assert public["runs"][0]["taskType"] == "video_parse"
    assert "currency" not in public and "kind" not in public["runs"][0]
    assert not {"estimatedCostUsd", "knownCostUsd", "unpricedCallCount"}.intersection(public["totals"])


def test_subset_unknown_only_nulls_the_affected_subset():
    state = service(Repository([recorded(tokens=TokenUsage(100, 20, 120, None, None))])).overview(workspace_id="workspace")
    assert state.totals.input_tokens == 100 and state.totals.output_tokens == 20
    assert state.totals.total_tokens == 120 and state.totals.missing_usage_count == 0
    assert state.totals.cached_input_tokens is state.totals.reasoning_tokens is None
    assert state.totals.cache_write_input_tokens is None


def test_cache_reasoning_and_modality_subsets_are_not_added_again():
    tokens = TokenUsage(100, 20, 120, 30, 5, text_input_tokens=50, image_input_tokens=10,
                        audio_input_tokens=20, cached_audio_input_tokens=5, cache_write_input_tokens=15,
                        cache_write_5m_input_tokens=10, cache_write_1h_input_tokens=5)
    totals = service(Repository([recorded(tokens=tokens)])).overview(workspace_id="workspace").totals
    assert totals.total_tokens == totals.known_tokens == 120
    assert totals.cached_input_tokens == 30 and totals.cache_write_input_tokens == 15
    assert totals.reasoning_tokens == 5


def test_missing_sdk_usage_is_counted_once_per_call_and_does_not_use_parent_total():
    partial = recorded("partial", sdk=(("child-A", None), ("child-B", None), ("known", TokenUsage(10, 2, 12, 0, 0))))
    state = service(Repository([recorded("complete"), partial])).overview(workspace_id="workspace")
    assert state.totals.call_count == 2 and state.totals.missing_usage_count == 1
    assert state.totals.input_tokens is state.totals.output_tokens is state.totals.total_tokens is None
    assert state.totals.known_tokens == 132
    assert state.runs[0].missing_usage_count == 1
    assert next(row for row in state.models if row.model == "known").total_tokens == 12


def test_main_loop_is_a_known_subtotal_at_all_affected_group_levels():
    partial = recorded("partial", metadata={"usage_scope": "main_loop"})
    complete = recorded("complete", run_id="other")
    state = service(Repository([partial, complete])).overview(workspace_id="workspace")
    for totals in (state.totals, state.models[0], next(row for row in state.runs if row.run_id == "task")):
        assert totals.input_tokens is totals.output_tokens is totals.total_tokens is None
        assert totals.missing_usage_count == 1
    assert state.totals.known_tokens == 240 and state.models[0].known_tokens == 240
    assert next(row for row in state.runs if row.run_id == "task").known_tokens == 120
    assert next(row for row in state.runs if row.run_id == "other").total_tokens == 120


def test_truncated_window_nulls_complete_totals_but_does_not_invent_missing_calls():
    state = service(Repository([recorded()], truncated=True)).overview(workspace_id="workspace")
    assert state.truncated
    for totals in (state.totals, state.models[0], state.runs[0]):
        assert totals.call_count == 1 and totals.missing_usage_count == 0
        assert totals.input_tokens is totals.output_tokens is totals.total_tokens is None
        assert totals.cached_input_tokens is totals.reasoning_tokens is None
        assert totals.known_tokens == 120


def test_failed_call_still_counts_confirmed_tokens_and_unknown_call_does_not_become_zero():
    state = service(Repository([recorded("failed", metadata={"status": "failed"}), recorded("unknown", unknown=True)])).overview(workspace_id="workspace")
    assert state.totals.call_count == 2 and state.totals.missing_usage_count == 1
    assert state.totals.total_tokens is None and state.totals.known_tokens == 120


def test_two_task_types_with_the_same_run_id_remain_separate_runs():
    chat = recorded("chat", task_type="chat_retrieval", metadata={"context": {"series_ids": ["series", "other"]}})
    state = service(Repository([recorded(), chat])).overview(workspace_id="workspace")
    assert len(state.runs) == 2 and {run.task_type for run in state.runs} == {"video_parse", "chat_retrieval"}
    assert next(run for run in state.runs if run.task_type == "chat_retrieval").series_id is None


def test_database_query_failure_is_unavailable_and_does_not_expose_credentials(caplog):
    from sqlalchemy.exc import OperationalError

    state = service(Repository(error=OperationalError("secret payload", {}, Exception("secret credential")))).overview(workspace_id="workspace")
    assert state.status == "unavailable" and state.totals.call_count is None
    assert not state.models and not state.runs and not state.truncated
    assert "secret" not in state.message and "secret" not in caplog.text
    assert project_usage_overview(state).source == "local"


def test_empty_recorded_window_is_available_with_zero_calls():
    state = service(Repository()).overview(workspace_id="workspace")
    assert state.status == "available" and state.totals.call_count == state.totals.total_tokens == 0
    assert state.totals.known_tokens == state.totals.missing_usage_count == 0


def test_default_window_and_exact_filters_are_passed_to_repository():
    repository = Repository()
    state = service(repository).overview(workspace_id="workspace", series_id="series", run_id="task")
    expected_end = datetime(2026, 10, 8, 0, 1, tzinfo=UTC)
    assert state.to_start_time == datetime_to_unix_time_ms(expected_end)
    assert state.to_start_time - state.from_start_time == 30 * 86_400_000
    assert repository.queries[0] == dict(workspace_id="workspace", from_start_time=expected_end - timedelta(days=30),
                                      to_start_time=expected_end, series_id="series", run_id="task")
    exact = service(repository).overview(workspace_id="workspace", to_start_time=datetime_to_unix_time_ms(_NOW))
    assert exact.to_start_time == datetime_to_unix_time_ms(_NOW)


@pytest.mark.parametrize("start,end", [(0, 32 * 86_400_000), (100, 100), (101, 100), (-1, 100), (True, 100)])
def test_invalid_windows_are_rejected_before_query(start, end):
    repository = Repository()
    with pytest.raises(ValueError):
        service(repository).overview(workspace_id="workspace", from_start_time=start, to_start_time=end)
    assert not repository.queries


@pytest.mark.parametrize("field,value", [("workspace_id", " w"), ("series_id", "s "), ("run_id", " r")])
def test_overview_identity_whitespace_is_rejected_before_query(field, value):
    repository = Repository()
    arguments = {"workspace_id": "workspace", field: value}
    with pytest.raises(ValueError):
        service(repository).overview(**arguments)
    assert not repository.queries


def test_sql_api_preserves_get_route_and_parameter_contract(tmp_path):
    from uuid import uuid4

    engine = create_database_engine(f"sqlite+pysqlite:///{tmp_path / 'api.db'}", environment=DatabaseEnvironment.TEST)
    ModelUsageRecord.__table__.create(engine)
    repository = ModelUsageRepository(create_session_factory(engine))
    repository.record_call(recorded(str(uuid4())))
    application = create_app(services=SimpleNamespace(parse_usage=service(repository)))
    try:
        with TestClient(application) as client:
            response = client.get("/api/usage/overview", params={"workspaceId": "workspace", "seriesId": "series", "runId": "task"})
            assert response.status_code == 200
            public = response.json()
            assert public["source"] == "local" and public["totals"]["totalTokens"] == 120
            assert public["runs"][0]["taskType"] == "video_parse"
            assert "currency" not in public and "estimatedCostUsd" not in public["totals"]
            assert client.get("/api/usage/overview", params={"workspaceId": "foreign"}).json()["totals"]["callCount"] == 0
            assert client.get("/api/jobs/usage/get", params={"workspaceId": "workspace", "jobId": "job"}).status_code == 404
            for params in ({}, {"workspaceId": ""}, {"workspaceId": "x" * 65}, {"workspaceId": "workspace", "fromStartTime": -1}):
                assert client.get("/api/usage/overview", params=params).status_code == 422
            from fastapi.openapi.utils import get_openapi
            query_properties = get_openapi(title="usage-test", version="1", routes=application.routes)["paths"]["/api/usage/overview"]["get"]["parameters"]
            assert {item["name"] for item in query_properties} == {"workspaceId", "fromStartTime", "toStartTime", "seriesId", "runId"}
    finally:
        engine.dispose()


def test_sql_api_reports_partial_sdk_usage_and_keeps_task_types_separate(tmp_path):
    from uuid import uuid4

    engine = create_database_engine(f"sqlite+pysqlite:///{tmp_path / 'partial-api.db'}", environment=DatabaseEnvironment.TEST)
    ModelUsageRecord.__table__.create(engine)
    repository = ModelUsageRepository(create_session_factory(engine))
    try:
        repository.record_call(recorded(str(uuid4())))
        repository.record_call(recorded(
            str(uuid4()), task_type="chat_retrieval", run_id="task",
            sdk=(("known-child", TokenUsage(10, 2, 12, 0, 0)), ("unknown-child", None)),
            metadata={"operation": "chat", "granularity": "sdk_turn_run"},
        ))
        application = create_app(services=SimpleNamespace(parse_usage=service(repository)))
        with TestClient(application) as client:
            response = client.get("/api/usage/overview", params={
                "workspaceId": "workspace", "seriesId": "series", "runId": "task",
                "fromStartTime": datetime_to_unix_time_ms(_NOW),
                "toStartTime": datetime_to_unix_time_ms(_NOW + timedelta(milliseconds=1)),
            })
        assert response.status_code == 200
        public = response.json()
        assert public["source"] == "local" and public["status"] == "available"
        assert public["totals"]["callCount"] == 2
        assert public["totals"]["totalTokens"] is None
        assert public["totals"]["knownTokens"] == 132
        assert public["totals"]["missingUsageCount"] == 1
        assert {row["model"]: (row["callCount"], row["totalTokens"], row["knownTokens"])
                for row in public["models"]} == {
            "actual": (1, 120, 120), "known-child": (1, 12, 12), "unknown-child": (1, None, 0),
        }
        assert {(row["taskType"], row["runId"]): (row["callCount"], row["totalTokens"], row["knownTokens"])
                for row in public["runs"]} == {
            ("video_parse", "task"): (1, 120, 120), ("chat_retrieval", "task"): (1, None, 12),
        }
    finally:
        engine.dispose()


def test_usage_api_database_failure_returns_unavailable_without_fabricating_zero():
    from sqlalchemy.exc import SQLAlchemyError

    repository = Repository(error=SQLAlchemyError("private-database-credential"))
    application = create_app(services=SimpleNamespace(parse_usage=service(repository)))
    with TestClient(application) as client:
        response = client.get("/api/usage/overview", params={"workspaceId": "workspace"})
    assert response.status_code == 200
    public = response.json()
    assert public["source"] == "local" and public["status"] == "unavailable"
    assert public["message"] and "private-database-credential" not in response.text
    assert public["totals"]["callCount"] is None
    assert public["totals"]["inputTokens"] is public["totals"]["outputTokens"] is public["totals"]["totalTokens"] is None
    assert public["models"] == public["runs"] == []
    assert len(repository.queries) == 1


@pytest.mark.parametrize("params,status", [
    ({"fromStartTime": 0, "toStartTime": 0}, 400),
    ({"fromStartTime": 2, "toStartTime": 1}, 400),
    ({"fromStartTime": 0, "toStartTime": 32 * 86_400_000}, 400),
    ({"runId": " task"}, 400),
    ({"fromStartTime": "not-a-time"}, 422),
    ({"seriesId": ""}, 422),
])
def test_usage_api_rejects_invalid_filters_before_database_query(params, status):
    repository = Repository()
    application = create_app(services=SimpleNamespace(parse_usage=service(repository)))
    with TestClient(application) as client:
        response = client.get("/api/usage/overview", params={"workspaceId": "workspace", **params})
    assert response.status_code == status
    assert response.json()["code"] == ("INVALID_REQUEST" if status == 400 else "VALIDATION_ERROR")
    assert repository.queries == []
