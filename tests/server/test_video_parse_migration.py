from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import os
from uuid import uuid4

import pytest
from sqlalchemy import CheckConstraint, inspect, text

from scripts.migrate_video_parse_chunk_tasks import ADDED, MARKER, MODELS, TABLES, migrate, plan_rows
from server.config.parse_profile import default_video_parse_v2_config
from server.database import Base, DatabaseEnvironment, create_database_engine, create_schema

NOW = datetime(2026, 10, 14, tzinfo=UTC)


def _rows():
    rows = {name: [] for name in TABLES}
    config = default_video_parse_v2_config().model_dump(mode="json")
    config.pop("indexing")
    config.update(chunk_duration=10, interval_duration=30)
    for name in list(config["execution"]):
        if name.endswith("_concurrency") or name.endswith("_execution_timeout_seconds"):
            config["execution"].pop(name)
    config["execution"]["max_concurrent_model_calls"] = 4
    config["execution"]["max_retry_count"] = 3
    base = {"lease_owner": None, "lease_version": 8, "lease_expire_time": None,
            "retry_count": 2, "next_execute_time": NOW, "finish_time": None}
    rows["video_parse_task"] = [{**base, "id": "parent", "video_id": "video", "parse_config": config,
        "status": "INDEXING_FAILED", "chunk_count": 3, "attempt_count": 7, "detail": {"keep": "audit"}}]
    rows["video"] = [{"id": "video", "series_id": "series", "duration_ms": 30000}]
    rows["series"] = [{"id": "series", "tenant_id": "test"}]
    for i in range(3):
        rows["video_chunk"].append({"id": f"chunk-{i}", "video_id": "video", "sequence_no": i, "start_ms": i * 10000,
                                     "end_ms": (i + 1) * 10000})
        rows["chunk_parse_task"].append({**base, "id": f"task-{i}", "video_task_id": "parent",
                                         "chunk_id": f"chunk-{i}", "status": "COMPLETE"})
        rows["chunk_parse_content"].append({"id": f"content-{i}", "chunk_task_id": f"task-{i}",
            "version": 4, "content": {"persons": [], "timeline": []}, "detail": {"scope": [0], "checkpoint": "preserve"}})
    rows["interval_parse_task"] = [{**base, "id": "interval", "video_task_id": "parent",
        "chunk_task_id": "task-0", "chunk_task_ids": [f"task-{i}" for i in range(3)],
        "chunk_count": 3, "start_sequence_no": 0, "status": "INDEXING_FAILED", "ready_mask": 7}]
    rows["interval_parse_content"] = [{"interval_task_id": "interval", "content": {"updates": []}}]
    return rows


def _targets():
    return {"parent": {"generation": 7, "sparse_enabled": False,
                       "contract_fingerprint": "a" * 64, "evidence": "frozen deployment snapshot"}}


def test_migration_preserves_failure_scope_and_committed_results_without_retry():
    rows = _rows()
    before = deepcopy(rows)
    plan = plan_rows(rows, targets=_targets(), published=lambda expected, *_: expected["video_chunk_id"] == "chunk-0", now=NOW)
    assert plan["differences"] == []
    assert rows == before
    changes = plan["changes"]
    assert changes["interval_parse_task"]["interval"]["status"] == "COMPLETE"
    assert changes["chunk_parse_task"]["task-0"]["status"] == "COMPLETE"
    assert changes["chunk_parse_task"]["task-1"]["status"] == "FAILED"
    assert changes["chunk_parse_task"]["task-1"]["phase"] == 3
    parent = changes["video_parse_task"]["parent"]
    assert parent["status"] == "INDEXING_FAILED"
    assert parent["parse_config"]["models"] == before["video_parse_task"][0]["parse_config"]["models"]
    assert parent["parse_config"]["execution"]["max_model_calls"] == before["video_parse_task"][0]["parse_config"]["execution"]["max_model_calls"]
    assert "max_concurrent_model_calls" not in parent["parse_config"]["execution"]
    assert parent["detail"][MARKER]["intervals"]["interval"]["retry_count"] == 2


def test_migration_repeated_plan_keeps_budgets_and_lease_versions():
    rows = _rows()
    first = plan_rows(rows, targets=_targets(), published=lambda *_: False, now=NOW)
    for name, changes in first["changes"].items():
        for row in rows[name]:
            row.update(changes[row["id"]])
    rows["chunk_parse_task"][1]["retry_count"] = 3
    rows["chunk_parse_task"][1]["lease_version"] = 19
    repeat = plan_rows(rows, targets={}, published=lambda *_: False, now=NOW)
    assert repeat["differences"] == []
    assert all(not value for value in repeat["changes"].values())
    assert rows["chunk_parse_task"][1]["retry_count"] == 3
    assert rows["chunk_parse_task"][1]["lease_version"] == 19


@pytest.mark.parametrize("defect", ["snapshot", "missing_content", "missing_refine", "membership", "live_lease", "es_unavailable"])
def test_migration_reports_differences_instead_of_inventing_success(defect):
    rows = _rows()
    targets = _targets()
    verifier = lambda *_: True
    if defect == "snapshot":
        targets = {}
    elif defect == "missing_content":
        rows["chunk_parse_content"].pop()
    elif defect == "missing_refine":
        rows["interval_parse_content"] = []
    elif defect == "membership":
        rows["interval_parse_task"][0]["chunk_task_ids"] = ["task-0"]
    elif defect == "live_lease":
        rows["chunk_parse_task"][0].update(lease_owner="old", lease_expire_time=NOW + timedelta(seconds=1))
    else:
        verifier = None
    assert plan_rows(rows, targets=targets, published=verifier, now=NOW)["differences"]


def test_interrupted_understanding_recovers_once_and_does_not_consume_parent_budget():
    rows = _rows()
    rows["chunk_parse_content"] = []
    rows["interval_parse_content"] = []
    rows["interval_parse_task"][0]["status"] = "WAITING"
    rows["video_parse_task"][0]["status"] = "PARSING"
    for child in rows["chunk_parse_task"]:
        child.update(status="UNDERSTANDING", lease_owner="old", lease_expire_time=NOW - timedelta(seconds=1))
    plan = plan_rows(rows, targets=_targets(), published=None, now=NOW)
    assert plan["differences"] == []
    child = plan["changes"]["chunk_parse_task"]["task-0"]
    assert child["phase"] == 1 and child["lease_version"] == 9
    assert child["retry_count"] == 3
    assert child["next_execute_time"] > NOW
    assert plan["changes"]["video_parse_task"]["parent"]["prepare_retry_count"] == 0



@pytest.mark.parametrize("kind", ["parse", "refine"])
def test_explicit_failure_with_results_is_reported_without_implicit_retry(kind):
    rows = _rows()
    if kind == "parse":
        rows["chunk_parse_task"][0]["status"] = "FAILED"
    else:
        rows["interval_parse_task"][0]["status"] = "PARSING_FAILED"
    result = plan_rows(rows, targets=_targets(), published=lambda *_: True, now=NOW)
    assert any("failure conflicts" in error for error in result["differences"])


def test_missing_planned_window_blocks_index_and_recovers_preparation():
    rows = _rows()
    rows["video_parse_task"][0]["status"] = "PARSING"
    rows["interval_parse_task"] = []
    rows["interval_parse_content"] = []
    result = plan_rows(rows, targets=_targets(), published=lambda *_: True, now=NOW)
    assert not result["differences"]
    assert result["changes"]["video_parse_task"]["parent"]["status"] == "PREPARING"
    assert all((child["phase"], child["status"]) == (2, "WAITING")
               for child in result["changes"]["chunk_parse_task"].values())


def test_old_index_retry_budget_and_later_backoff_survive_migration():
    rows = _rows()
    interval = rows["interval_parse_task"][0]
    interval.update(status="INDEXING", lease_owner="expired", lease_expire_time=NOW - timedelta(seconds=1),
                    retry_count=1, next_execute_time=NOW + timedelta(hours=1))
    result = plan_rows(rows, targets=_targets(), published=lambda *_: False, now=NOW)
    assert not result["differences"]
    for child in result["changes"]["chunk_parse_task"].values():
        assert child["retry_count"] == 2
        assert child["next_execute_time"] == NOW + timedelta(hours=1)


@pytest.mark.parametrize("defect", ["version", "visibility", "refine_status", "parent_status", "repeat_parse"])
def test_repeat_verifies_publication_and_dependencies_without_resetting(defect):
    rows = _rows()
    first = plan_rows(rows, targets=_targets(), published=lambda *_: True, now=NOW)
    for name, changes in first["changes"].items():
        for row in rows[name]:
            row.update(changes[row["id"]])
    if defect == "version":
        rows["chunk_parse_task"][0]["indexed_content_version"] = 99
    elif defect == "refine_status":
        rows["interval_parse_task"][0]["status"] = "WAITING"
    elif defect == "parent_status":
        rows["video_parse_task"][0]["status"] = "COMPLETE"
    elif defect == "repeat_parse":
        rows["chunk_parse_task"][0].update(phase=1, status="QUEUED")
    repeated = plan_rows(rows, targets={}, published=lambda *_: defect != "visibility", now=NOW)
    assert repeated["differences"]
    assert not any(repeated["changes"].values())

@pytest.fixture
def migration_engine():
    url = os.getenv("ANIVAG_MIGRATION_TEST_DATABASE_URL")
    if not url:
        pytest.skip("ANIVAG_MIGRATION_TEST_DATABASE_URL is not configured")
    engine = create_database_engine(url, environment=DatabaseEnvironment.TEST)
    assert engine.url.database and engine.url.database.endswith("_migration_test"), "requires dedicated disposable migration test database"
    create_schema(engine)
    Base.metadata.drop_all(engine)
    create_schema(engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def _seed_legacy(engine):
    """Build the actual previous physical shape, with non-empty retained results."""
    rows = _rows()
    # Keep valid UUID identities for real database adapters and ES contract checks.
    ids = {value: str(uuid4()) for value in ["parent", "video", "series", "interval",
           *(f"chunk-{i}" for i in range(3)), *(f"task-{i}" for i in range(3)), *(f"content-{i}" for i in range(3))]}
    def remap(value):
        if isinstance(value, str):
            return ids.get(value, value)
        if isinstance(value, list):
            return [remap(item) for item in value]
        if isinstance(value, dict):
            return {key: remap(item) for key, item in value.items()}
        return value
    rows = remap(rows)
    with engine.connect() as connection:
        for table in MODELS:
            name = table.name
            for check in inspect(connection).get_check_constraints(name):
                if any(column in check["sqltext"] for column in ADDED[name]) or check["name"].endswith("_status") or check["name"].endswith("_lease_state"):
                    connection.exec_driver_sql(f"ALTER TABLE `{name}` DROP CHECK `{check['name']}`")
            for index in inspect(connection).get_indexes(name):
                if set(index["column_names"]) & ADDED[name]:
                    if index["name"] == "ix_chunk_parse_task_parent_status":
                        connection.exec_driver_sql(f"ALTER TABLE `{name}` DROP INDEX `{index['name']}`, ADD INDEX `{index['name']}` (video_task_id, status, chunk_id)")
                    else:
                        connection.exec_driver_sql(f"ALTER TABLE `{name}` DROP INDEX `{index['name']}`")
            for column in ADDED[name]:
                connection.exec_driver_sql(f"ALTER TABLE `{name}` DROP COLUMN `{column}`")
        connection.exec_driver_sql("ALTER TABLE chunk_parse_task ADD INDEX ix_chunk_parse_task_due(status, next_execute_time)")
        connection.commit()
    from sqlalchemy import MetaData, Table
    metadata = MetaData()
    with engine.begin() as connection:
        for name in ("series", "video", "video_parse_task", "video_chunk", "chunk_parse_task", "chunk_parse_content", "interval_parse_task", "interval_parse_content"):
            table = Table(name, metadata, autoload_with=connection)
            for row in rows[name]:
                values = {key: value for key, value in row.items() if key in table.c}
                # Fill unrelated required columns with well-formed historical values.
                defaults = {"create_time": 1, "update_time": 1, "name": "Migration fixture",
                    "status": "READY", "character_bible": [], "series_id": ids["series"],
                    "video_id": ids["video"], "episode_order": 1, "upload_sequence": 1,
                    "filename": "test.mp4", "source_uri": "fixture://source", "hash": "b" * 64,
                    "size_bytes": 4, "duration_ms": 30000, "upload_status": "COMPLETED",
                    "parse_status": "COMPLETE", "upload_progress": 100, "parse_progress": 100,
                    "pipeline_version": "video-parse-v2", "version": 1, "max_attempts": 3,
                    "processing_duration_ms": 0, "processing_timing_complete": True,
                    "is_test": True, "is_deleted": False, "window_start_ms": row.get("start_ms", 0),
                    "window_end_ms": row.get("end_ms", 30000), "chunk_media_ref": "fixture://chunk",
                    "analysis_window_media_ref": "fixture://window", "transcript": [], "keyframe_uris": []}
                for col in table.c:
                    if col.name not in values and not col.nullable and not col.server_default:
                        values[col.name] = defaults[col.name]
                connection.execute(table.insert().values(**values))
    targets = {ids["parent"]: _targets()["parent"]}
    return targets, ids


def test_real_mysql_dry_run_apply_repeat_and_results_preserved(migration_engine):
    targets, ids = _seed_legacy(migration_engine)
    before = inspect(migration_engine).get_columns("chunk_parse_task")
    dry = migrate(migration_engine, targets=targets, published=lambda expected, *_: expected["video_chunk_id"] == ids["chunk-0"], now=NOW)
    assert not dry["applied"] and not dry["differences"]
    assert repr(inspect(migration_engine).get_columns("chunk_parse_task")) == repr(before)
    with pytest.raises(ValueError, match="writers-stopped"):
        migrate(migration_engine, targets=targets, apply=True)
    apply = migrate(migration_engine, targets=targets, published=lambda expected, *_: expected["video_chunk_id"] == ids["chunk-0"],
                    now=NOW, apply=True, writers_stopped=True)
    assert apply["applied"] and apply["preservedContentDigest"] == dry["preservedContentDigest"]
    create_schema(migration_engine)  # New worker startup must accept the final physical contract.
    repeat = migrate(migration_engine, targets={}, published=lambda expected, *_: expected["video_chunk_id"] == ids["chunk-0"], now=NOW,
                     apply=True, writers_stopped=True)
    assert repeat["applied"] and not repeat["differences"]
    assert not any(repeat["updates"].values())
    assert repeat["schemaPreparation"] == [] and repeat["schemaFinalization"] == []
    with migration_engine.connect() as connection:
        assert connection.scalar(text("SELECT COUNT(*) FROM chunk_parse_task WHERE phase=3 AND status='FAILED'")) == 2
        assert connection.scalar(text("SELECT COUNT(*) FROM interval_parse_task WHERE status='COMPLETE'")) == 1
        assert connection.scalar(text("SELECT COUNT(*) FROM chunk_parse_content")) == 3


@pytest.mark.parametrize("interruption", ["after_prepare_ddl", "after_row_commit"])
def test_real_mysql_interrupted_ddl_resumes_without_resetting_budget(migration_engine, monkeypatch, interruption):
    import scripts.migrate_video_parse_chunk_tasks as module
    targets, _ids = _seed_legacy(migration_engine)
    original_schema = module._schema
    stopped = False
    def interrupted(connection, *, finalize, apply):
        nonlocal stopped
        if apply and not stopped and interruption == "after_row_commit" and finalize:
            stopped = True
            raise RuntimeError("simulated process interruption")
        result = original_schema(connection, finalize=finalize, apply=apply)
        if apply and not stopped and interruption == "after_prepare_ddl" and not finalize:
            stopped = True
            raise RuntimeError("simulated process interruption")
        return result
    monkeypatch.setattr(module, "_schema", interrupted)
    with pytest.raises(RuntimeError, match="simulated process interruption"):
        migrate(migration_engine, targets=targets, published=lambda *_: False,
                apply=True, writers_stopped=True, now=NOW)
    if interruption == "after_row_commit":
        with migration_engine.begin() as connection:
            connection.execute(text("UPDATE chunk_parse_task SET retry_count=3, lease_version=19 WHERE status='FAILED'"))
    result = migrate(migration_engine, targets=targets, published=lambda *_: False,
                     apply=True, writers_stopped=True, now=NOW)
    assert result["applied"] and not result["differences"]
    create_schema(migration_engine)
    with migration_engine.connect() as connection:
        assert connection.scalar(text("SELECT COUNT(*) FROM chunk_parse_content")) == 3
        if interruption == "after_row_commit":
            assert connection.scalar(text("SELECT COUNT(*) FROM chunk_parse_task WHERE retry_count=3 AND lease_version=19")) == 3
