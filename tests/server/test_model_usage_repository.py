import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta, timezone
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, event, inspect, insert
from sqlalchemy.exc import IntegrityError

from gateway.usage import TokenUsage
from server.database import DatabaseEnvironment, create_database_engine, create_session_factory
from server.model.dto.parseUsageDTO import ModelUsageState
from server.model.parse_usage import ModelUsageRecord
from server.repository.model_usage import MAX_USAGE_RECORDS, ModelUsageConflictError, ModelUsageRepository

TEST_DATABASE_URL = os.getenv("ANIVAG_TEST_DATABASE_URL")
NOW = datetime(2026, 10, 8, 1, 2, 3, 456000, tzinfo=UTC)


@pytest.fixture(params=["sqlite", pytest.param("mysql", marks=pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="ANIVAG_TEST_DATABASE_URL is not configured",
))])
def storage(request):
    engine = create_database_engine(
        TEST_DATABASE_URL if request.param == "mysql" else "sqlite+pysqlite:///:memory:",
        environment=DatabaseEnvironment.TEST,
    )
    if request.param == "mysql":
        assert engine.dialect.name == "mysql"
    ModelUsageRecord.__table__.create(engine, checkfirst=True)
    factory = create_session_factory(engine)
    workspace = f"usage-test-{uuid4()}"
    try:
        yield ModelUsageRepository(factory), factory, workspace
    finally:
        with factory.begin() as session:
            session.execute(delete(ModelUsageRecord).where(
                ModelUsageRecord.workspace_id.in_((workspace, workspace + "-other", workspace.upper())),
            ))
        engine.dispose()


def call(workspace, **changes):
    values = dict(
        call_id=str(uuid4()), workspace_id=workspace, task_type="video_parse", run_id="run-A",
        started_at=NOW, created_at=NOW,
        usage={"tokens": asdict(TokenUsage(100, 20, 120, 30, None)), "sdk_model_usage": []},
        metadata={
            "model_name": "actual-model", "provider": "test", "src": None,
            "operation": "chat", "status": "completed", "request_id": None,
            "ended_at": None, "granularity": "provider_request", "usage_source": "provider_usage",
            "usage_scope": None, "context": {"series_id": "series-A"},
        },
    )
    values.update(changes)
    return ModelUsageState(**values)


def query(repository, workspace, **changes):
    return repository.query(workspace_id=workspace, from_start_time=NOW,
                            to_start_time=NOW + timedelta(days=1), **changes)


def test_append_is_immutable_and_replay_does_not_overwrite(storage):
    repository, factory, workspace = storage
    original = call(workspace)
    stored = repository.record_call(original)
    replay = repository.record_call(replace(
        original, created_at=NOW + timedelta(seconds=1),
        usage={"tokens": None, "sdk_model_usage": []},
    ))
    assert stored == replay == original
    assert len(query(repository, workspace).records) == 1
    with pytest.raises(TypeError):
        stored.usage["tokens"]["input_tokens"] = 0
    with factory() as session:
        row = session.get(ModelUsageRecord, original.call_id)
        assert row.usage["tokens"]["reasoning_tokens"] is None
        assert row.metadata_["src"] is None


@pytest.mark.parametrize("changes", [
    {"workspace_id": "different"}, {"task_type": "chat_retrieval"}, {"run_id": "run-B"},
])
def test_call_id_scope_conflicts_are_rejected(storage, changes):
    repository, _, workspace = storage
    original = call(workspace)
    repository.record_call(original)
    with pytest.raises(ModelUsageConflictError):
        repository.record_call(replace(original, **changes))
    assert query(repository, workspace).records == (original,)


def test_utc_millisecond_and_unknown_json_round_trip(storage):
    repository, _, workspace = storage
    local = NOW.astimezone(timezone(timedelta(hours=8))).replace(microsecond=456789)
    stored = repository.record_call(call(
        workspace, started_at=local, created_at=local,
        usage={"tokens": None, "sdk_model_usage": [{"model_name": "sdk", "tokens": None}]},
    ))
    assert stored.started_at == stored.created_at == NOW
    assert stored.usage["tokens"] is None
    assert stored.usage["sdk_model_usage"][0]["tokens"] is None


def test_exact_workspace_run_series_and_half_open_window(storage):
    repository, _, workspace = storage
    scalar = call(workspace, call_id=str(UUID(int=1)))
    array_metadata = dict(scalar.metadata)
    array_metadata["context"] = {"series_ids": ["series-A", "series-B"]}
    array = call(workspace, call_id=str(UUID(int=2)), metadata=array_metadata)
    for row in (
        scalar, array, call(workspace + "-other"), call(workspace.upper()),
        call(workspace, run_id="run-AA"), call(workspace, run_id="run-a"),
        call(workspace, started_at=NOW - timedelta(milliseconds=1)),
        call(workspace, started_at=NOW + timedelta(days=1)),
    ):
        repository.record_call(row)
    for context in ({"series_id": "series-AA"}, {"series_ids": ["series-AA"]},
                    {"series_id": "series-a"}, {"series_ids": ["series-a"]}):
        metadata = dict(scalar.metadata)
        metadata["context"] = context
        repository.record_call(call(workspace, metadata=metadata))
    batch = query(repository, workspace, series_id="series-A", run_id="run-A")
    assert batch.records == (scalar, array)
    assert batch.truncated is False


def test_series_sql_requires_a_string_or_an_array_of_exact_members(storage):
    repository, _, workspace = storage
    for context in ({"series_ids": "series-A"}, {"series_id": ["series-A"]},
                    {"series_ids": {"nested": "series-A"}}, {"series_id": None, "series_ids": None}):
        metadata = dict(call(workspace).metadata)
        metadata["context"] = context
        repository.record_call(call(workspace, metadata=metadata))
    assert not query(repository, workspace, series_id="series-A").records


def test_sql_filters_precede_the_real_20k_limit_and_extra_row_is_excluded(storage):
    repository, factory, workspace = storage
    metadata = dict(call(workspace).metadata)
    metadata["context"] = {"series_ids": ["series-AA"]}
    # These sort before the desired call. All SQL filters must run before LIMIT.
    rows = [dict(
        call_id=str(UUID(int=i + 10)), workspace_id=workspace,
        task_type="video_parse", run_id="run-A", started_at=NOW,
        usage={"tokens": None, "sdk_model_usage": []}, metadata=metadata, created_at=NOW,
    ) for i in range(MAX_USAGE_RECORDS + 1)]
    with factory.begin() as session:
        session.execute(insert(ModelUsageRecord.__table__), rows)
    target = call(workspace, started_at=NOW + timedelta(milliseconds=1))
    repository.record_call(target)
    batch = query(repository, workspace, series_id="series-A", run_id="run-A")
    assert batch.records == (target,) and not batch.truncated
    batch = query(repository, workspace)
    assert batch.truncated and len(batch.records) == MAX_USAGE_RECORDS
    assert batch.records[0].call_id == str(UUID(int=10))
    assert batch.records[-1].call_id == str(UUID(int=MAX_USAGE_RECORDS + 9))
    assert target.call_id not in {row.call_id for row in batch.records}


@pytest.mark.parametrize("changes", [
    {"call_id": "not-a-uuid"}, {"workspace_id": " w"}, {"workspace_id": "w "},
    {"workspace_id": "w" * 65}, {"run_id": "r "}, {"run_id": "r" * 257},
    {"task_type": "retrieval"}, {"started_at": NOW.replace(tzinfo=None)},
    {"usage": {"tokens": {}, "sdk_model_usage": []}},
    {"usage": {"tokens": None, "sdk_model_usage": "[]"}},
    {"metadata": {"context": {}}},
])
def test_invalid_id_time_or_json_is_rejected(storage, changes):
    repository, _, workspace = storage
    with pytest.raises(ValueError):
        repository.record_call(call(workspace, **changes))
    assert not query(repository, workspace).records


def test_json_snapshot_does_not_share_mutable_caller_values():
    values = {"tokens": None, "sdk_model_usage": []}
    row = call("w", usage=values)
    values["sdk_model_usage"].append({"model_name": "later", "tokens": None})
    assert row.usage["sdk_model_usage"] == ()
    with pytest.raises(ValueError, match="JSON"):
        call("w", usage={"tokens": object()})


@pytest.mark.parametrize("name,value", [("workspace_id", " w"), ("run_id", "r "), ("series_id", " s")])
def test_query_rejects_whitespace_identities(storage, name, value):
    repository, _, workspace = storage
    arguments = dict(workspace_id=workspace, from_start_time=NOW, to_start_time=NOW + timedelta(days=1))
    arguments[name] = value
    with pytest.raises(ValueError):
        repository.query(**arguments)


def test_other_integrity_errors_are_not_reported_as_success(storage):
    repository, factory, workspace = storage
    engine = factory.kw["bind"]

    def fail(_connection, _cursor, statement, parameters, _context, _many):
        if statement.lstrip().upper().startswith("INSERT"):
            raise IntegrityError(statement, parameters, Exception("unrelated integrity failure"))

    event.listen(engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(IntegrityError):
            repository.record_call(call(workspace))
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    assert not query(repository, workspace).records


@pytest.mark.parametrize("conflict", [False, True])
def test_mysql_real_schema_and_concurrent_call_id_replay(storage, conflict):
    repository, factory, workspace = storage
    engine = factory.kw["bind"]
    if engine.dialect.name != "mysql":
        pytest.skip("concurrent PK/UTC semantics require real TEST MySQL")
    columns = {column["name"]: column for column in inspect(engine).get_columns("model_usage")}
    assert set(columns) == set(ModelUsageRecord.__table__.columns.keys())
    assert all(not column["nullable"] for column in columns.values())
    assert columns["started_at"]["type"].fsp == columns["created_at"]["type"].fsp == 3
    original = call(workspace)
    other = replace(original, workspace_id=workspace + "-other") if conflict else original
    barrier = Barrier(2)

    def synchronize(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("INSERT INTO MODEL_USAGE"):
            barrier.wait(timeout=10)

    event.listen(engine, "before_cursor_execute", synchronize)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(repository.record_call, row) for row in (original, other)]
            results = []
            errors = []
            for future in futures:
                try:
                    results.append(future.result())
                except ModelUsageConflictError as error:
                    errors.append(error)
    finally:
        event.remove(engine, "before_cursor_execute", synchronize)
    if conflict:
        assert len(results) == len(errors) == 1
        assert results[0] in (original, other)
        assert len(query(repository, workspace).records) + len(query(repository, workspace + "-other").records) == 1
    else:
        assert results == [original, original]
        assert query(repository, workspace).records == (original,)
