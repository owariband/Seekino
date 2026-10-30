from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import os
from uuid import uuid4

import pytest
from sqlalchemy import CheckConstraint, MetaData, Table, inspect, select, text

from engine.common.config import VideoParseRuntimeConfig
from engine.parsev2.validation import ChunkResultUpdate, ValidatedIntervalRefinement
from gateway.config import load_config
from scripts.migrate_video_parse_chunk_tasks import ADDED, MARKER, MODELS, RETIRED_COLUMNS, TABLES, _json_hash, migrate, plan_rows
from server.config.media import ImageUploadConfig
from server.config.parse_profile import ParseConfig as ServerParseConfig, default_video_parse_v2_config
from server.config.upload import UploadServiceConfig
from server.constants.parse import CAPACITY_ACQUIRED_KEY, MAX_INDEX_GENERATION, PREPARE_FAILURES_KEY, RUNTIME_DETAIL_KEY
from server.database import Base, DatabaseEnvironment, SchemaUpgradeRequiredError, create_database_engine, create_schema

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


def _upload_config():
    config = default_video_parse_v2_config().model_dump(mode="json")
    config["indexing"].pop("index_generation")
    config["models"]["ocr"] = deepcopy(config["models"]["understanding"])
    config["models"]["refine"] = {**config["models"]["understanding"], "model": "historical-refine"}
    config["models"]["prefix_context"] = deepcopy(config["models"]["understanding"])
    config.update(ocr_enabled=True, compact_enabled=True,
                  prefix_context={"series_compact_max_chars": 900, "video_compact_max_chars": 700, "max_tool_calls": 2})
    return config


def _upload_row(config):
    return {"id": "upload", "parse_config": None, "video_parse_config": deepcopy(config),
            "status": "UPLOADING", "video_id": None, "initial_task_id": None,
            "completion_token": None, "version": 5, "request_fingerprint": "c" * 64,
            "idempotency_key": "historical-request", "detail": {"audit": "preserve"},
            "active_slot_key": "series:1", "expire_time": NOW + timedelta(days=1)}


def _historical_upload_config():
    config = _upload_config()
    config.pop("indexing")
    for name in list(config["execution"]):
        if name.endswith("_concurrency") or name.endswith("_execution_timeout_seconds"):
            config["execution"].pop(name)
    config["execution"]["max_concurrent_model_calls"] = 4
    return config


def _server_profile():
    gateway = load_config()
    return ServerParseConfig.from_defaults(
        runtime=VideoParseRuntimeConfig(), gateway=gateway, upload=UploadServiceConfig(),
        images=ImageUploadConfig(), embedding_model=None,
        embedding_dimensions=gateway.resolve_embedding_binding().dimensions, embedding_fps=None,
    )


def test_upload_migration_preserves_actual_v2_configuration_and_request_identity():
    rows = {name: [] for name in TABLES}
    config = _upload_config()
    upload = _upload_row(config)
    upload["parse_config"] = _server_profile().model_dump(mode="json")
    rows["upload_session"] = [upload]
    before = deepcopy(rows)

    plan = plan_rows(rows, targets={}, published=None, now=NOW)

    assert not plan["differences"] and rows == before
    change = plan["changes"]["upload_session"]["upload"]
    assert change == {"parse_config": config}
    assert "index_generation" not in change["parse_config"]["indexing"]
    assert plan["uploads"][0]["source"] == "video_parse_config"
    upload.update(change)
    # A restart after row commit must also work before the retired column is dropped.
    repeat = plan_rows(rows, targets={}, published=None, now=NOW)
    assert not repeat["differences"] and not any(repeat["changes"].values())
    upload.pop("video_parse_config")
    final = plan_rows(rows, targets={}, published=None, now=NOW)
    assert not final["differences"] and not any(final["changes"].values())
    assert {key: value for key, value in upload.items() if key != "parse_config"} == {
        key: value for key, value in before["upload_session"][0].items()
        if key not in {"parse_config", "video_parse_config"}}


def test_upload_migration_binds_original_task_generation_without_using_later_reparse():
    rows = _rows()
    upload = _upload_row(_upload_config())
    upload.update(status="COMPLETED", video_id="video", initial_task_id="parent")
    rows["upload_session"] = [upload]
    later = deepcopy(rows["video_parse_task"][0])
    later.update(id="later-task", status="PENDING", detail={}, parse_config=default_video_parse_v2_config().model_dump(mode="json"))
    later["parse_config"].update(chunk_duration=10, interval_duration=30)
    later["parse_config"]["indexing"]["index_generation"] = 9
    rows["video_parse_task"].append(later)
    before = deepcopy(rows)

    plan = plan_rows(rows, targets=_targets(), published=lambda *_: False, now=NOW)

    assert not plan["differences"] and rows == before
    assert plan["changes"]["upload_session"]["upload"]["parse_config"] == {
        **upload["video_parse_config"],
        "indexing": {**upload["video_parse_config"]["indexing"], "index_generation": 7},
    }
    for name, changes in plan["changes"].items():
        for row in rows[name]:
            row.update(changes.get(row["id"], {}))
    repeat = plan_rows(rows, targets={}, published=lambda *_: False, now=NOW)
    assert not repeat["differences"] and not any(repeat["changes"].values())


@pytest.mark.parametrize("column", ["video_parse_config", "parse_config"])
def test_historical_upload_uses_initial_frozen_contract_without_changing_existing_inputs_or_tasks(column):
    rows = _rows()
    first = plan_rows(rows, targets=_targets(), published=lambda *_: False, now=NOW)
    for name, changes in first["changes"].items():
        for row in rows[name]:
            row.update(changes.get(row["id"], {}))
    initial = rows["video_parse_task"][0]["parse_config"]
    initial["execution"].update(refine_concurrency=14, index_execution_timeout_seconds=919)
    original = _historical_upload_config()
    original["execution"]["parse_concurrency"] = 4
    original["models"]["understanding"]["model"] = "original-upload-model"
    original["visual_input"].update(mode="ordered_frames", max_frames_per_chunk=77)
    upload = _upload_row(None)
    upload.update(status="COMPLETED", video_id="video", initial_task_id="parent")
    upload[column] = original
    rows["upload_session"] = [upload]
    before = deepcopy(rows)

    plan = plan_rows(rows, targets={}, published=lambda *_: False, now=NOW)

    assert not plan["differences"] and rows == before
    assert all(not changes for name, changes in plan["changes"].items() if name != "upload_session")
    result = plan["changes"]["upload_session"]["upload"]["parse_config"]
    expected = deepcopy(original)
    expected["execution"].pop("max_concurrent_model_calls")
    for name, value in initial["execution"].items():
        expected["execution"].setdefault(name, value)
    expected["indexing"] = deepcopy(initial["indexing"])
    assert result == expected
    upload["parse_config"] = result
    repeat = plan_rows(rows, targets={}, published=lambda *_: False, now=NOW)
    assert not repeat["differences"] and not any(repeat["changes"].values())
    upload.pop("video_parse_config")
    final = plan_rows(rows, targets={}, published=lambda *_: False, now=NOW)
    assert not final["differences"] and not any(final["changes"].values())
    assert {name: value for name, value in rows.items() if name != "upload_session"} == {
        name: value for name, value in before.items() if name != "upload_session"}


@pytest.mark.parametrize("defect", ["no_initial_task", "unknown_initial_task", "other_video", "embedding", "max_height"])
def test_historical_upload_cannot_borrow_an_unrelated_initial_contract(defect):
    rows = _rows()
    upload = _upload_row(_historical_upload_config())
    upload.update(status="COMPLETED", video_id="video", initial_task_id="parent")
    rows["upload_session"] = [upload]
    if defect == "no_initial_task":
        upload.update(status="EXPIRED", video_id=None, initial_task_id=None)
    elif defect == "unknown_initial_task":
        upload["initial_task_id"] = "absent"
    elif defect == "other_video":
        upload["video_id"] = "different"
    elif defect == "embedding":
        upload["video_parse_config"]["models"]["embedding"]["model"] = "unrelated-embedding"
    else:
        upload["video_parse_config"]["visual_input"]["max_height"] += 1
    before = deepcopy(rows)

    plan = plan_rows(rows, targets=_targets(), published=lambda *_: False, now=NOW)

    assert rows == before and any(item.startswith("upload upload:") for item in plan["differences"])
    assert not plan["changes"]["upload_session"]


def test_complete_upload_snapshot_retires_obsolete_model_limit_without_a_task_or_manifest():
    rows = {name: [] for name in TABLES}
    config = _upload_config()
    upload = _upload_row(config)
    upload["video_parse_config"]["execution"]["max_concurrent_model_calls"] = 4
    rows["upload_session"] = [upload]
    before = deepcopy(rows)

    plan = plan_rows(rows, targets={}, published=None, now=NOW)

    assert not plan["differences"] and rows == before
    assert plan["changes"]["upload_session"]["upload"] == {"parse_config": config}


@pytest.mark.parametrize("defect", [None, "evidence", "digest", "execution", "indexing"])
def test_historical_upload_without_task_requires_an_audited_matching_snapshot(defect):
    rows = {name: [] for name in TABLES}
    original = _historical_upload_config()
    upload = _upload_row(original)
    upload["status"] = "EXPIRED"
    rows["upload_session"] = [upload]
    execution = {key: value for key, value in _upload_config()["execution"].items()
                 if key not in original["execution"]}
    execution.update(refine_concurrency=14, index_execution_timeout_seconds=919)
    target = {**_targets()["parent"], "source_config_digest": _json_hash(original), "execution": execution}
    if defect == "evidence":
        target["evidence"] = " "
    elif defect == "digest":
        target["source_config_digest"] = "0" * 64
    elif defect == "execution":
        target["execution"].pop("refine_concurrency")
    elif defect == "indexing":
        target.pop("contract_fingerprint")
    before = deepcopy(rows)

    plan = plan_rows(rows, targets={"upload": target}, published=None, now=NOW)

    assert rows == before
    if defect:
        assert plan["differences"] and not plan["changes"]["upload_session"]
        return
    assert not plan["differences"]
    expected = deepcopy(original)
    expected["execution"].pop("max_concurrent_model_calls")
    expected["execution"].update(execution)
    expected["indexing"] = {"index_generation": 7, "sparse_enabled": False, "contract_fingerprint": "a" * 64}
    assert plan["changes"]["upload_session"]["upload"] == {"parse_config": expected}
    upload["parse_config"] = expected
    repeat = plan_rows(rows, targets={"upload": target}, published=None, now=NOW)
    assert not repeat["differences"] and not any(repeat["changes"].values())
    upload.pop("video_parse_config")
    final = plan_rows(rows, targets={}, published=None, now=NOW)
    assert not final["differences"] and not any(final["changes"].values())


@pytest.mark.parametrize("defect", ["initial_generation", "unknown_initial_task", "other_video", "two_v2_configs", "two_v2_generations", "generation_overflow", "invalid_v2", "missing_config", "active_completion"])
def test_upload_migration_blocks_ambiguous_configuration_and_active_completion(defect):
    rows = _rows()
    upload = _upload_row(_upload_config())
    rows["upload_session"] = [upload]
    if defect == "initial_generation":
        upload.update(video_id="video", initial_task_id="parent")
        upload["video_parse_config"]["indexing"]["index_generation"] = 8
    elif defect == "unknown_initial_task":
        upload.update(video_id="video", initial_task_id="absent")
    elif defect == "other_video":
        upload.update(video_id="different", initial_task_id="parent")
    elif defect == "two_v2_configs":
        upload["parse_config"] = deepcopy(upload["video_parse_config"])
        upload["parse_config"]["models"]["refine"]["model"] = "different-refine"
    elif defect == "two_v2_generations":
        upload["video_parse_config"]["indexing"]["index_generation"] = 7
        upload["parse_config"] = deepcopy(upload["video_parse_config"])
        upload["parse_config"]["indexing"]["index_generation"] = 8
    elif defect == "generation_overflow":
        upload["video_parse_config"]["indexing"]["index_generation"] = MAX_INDEX_GENERATION + 1
    elif defect == "invalid_v2":
        upload["video_parse_config"]["execution"]["parse_concurrency"] = 0
    elif defect == "missing_config":
        upload["video_parse_config"] = None
    else:
        upload.update(status="COMPLETING", completion_token="active")
    before = deepcopy(rows)

    plan = plan_rows(rows, targets=_targets(), published=lambda *_: False, now=NOW)

    assert rows == before
    assert any(item.startswith("upload upload:") for item in plan["differences"])
    assert not plan["changes"]["upload_session"]


@pytest.mark.parametrize("missing", [None, "parse_concurrency", "indexing"])
def test_upload_migration_converts_profile_only_with_explicit_frozen_inputs(missing):
    rows = {name: [] for name in TABLES}
    profile = _server_profile()
    config = profile.model_dump(mode="json")
    config["indexing"].pop("index_generation")
    if missing == "parse_concurrency":
        config["execution"].pop(missing)
    elif missing == "indexing":
        config.pop(missing)
    upload = _upload_row(None)
    upload["parse_config"] = config
    rows["upload_session"] = [upload]
    before = deepcopy(rows)

    plan = plan_rows(rows, targets={}, published=None, now=NOW)

    assert rows == before
    if missing is not None:
        assert plan["differences"] and not plan["changes"]["upload_session"]
    else:
        assert not plan["differences"]
        expected = profile.to_video_parse_v2().model_dump(mode="json")
        expected["indexing"].pop("index_generation")
        assert plan["changes"]["upload_session"]["upload"] == {"parse_config": expected}
        upload.update(plan["changes"]["upload_session"]["upload"])
        repeat = plan_rows(rows, targets={}, published=None, now=NOW)
        assert not repeat["differences"] and not any(repeat["changes"].values())


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
    assert parent["parse_config"]["indexing"]["index_generation"] == 7
    assert "target_index_generation" not in parent
    assert all(not {"indexed_generation", "execution_deadline"} & value.keys()
               for value in changes["chunk_parse_task"].values())
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


@pytest.mark.parametrize("changed_field", ["persons", "timeline"])
def test_migration_accepts_persisted_refine_default_nulls_without_rewriting_content(changed_field):
    rows = _rows()
    rows["video_parse_task"][0]["status"] = "COMPLETE"
    rows["interval_parse_task"][0]["status"] = "COMPLETE"
    result = ValidatedIntervalRefinement(updates=[
        ChunkResultUpdate(sequence_no=0, **{changed_field: []}),
    ])
    persisted = result.model_dump(mode="json")
    omitted_field = "timeline" if changed_field == "persons" else "persons"
    assert persisted["updates"][0][omitted_field] is None
    rows["interval_parse_content"][0]["content"] = persisted
    before = deepcopy(rows)

    plan = plan_rows(rows, targets=_targets(), published=lambda *_: True, now=NOW)

    assert rows == before
    assert plan["differences"] == []
    assert plan["changes"]["video_parse_task"]["parent"]["status"] == "COMPLETE"
    assert plan["changes"]["interval_parse_task"]["interval"]["status"] == "COMPLETE"
    assert all(child["phase"] == 3 and child["status"] == "COMPLETE"
               and child["indexed_content_version"] == 4
               for child in plan["changes"]["chunk_parse_task"].values())


@pytest.mark.parametrize("updates", [
    [{"sequence_no": 0, "persons": None, "timeline": None}],
    [{"sequence_no": 0, "persons": [], "timeline": None, "unknown": None}],
    [{"sequence_no": 0, "persons": [], "timeline": None, "unknown": []}],
    [{"sequence_no": 3, "persons": [], "timeline": None}],
    [{"sequence_no": 0, "persons": [], "timeline": None}] * 2,
], ids=["null-only", "unknown-null", "unknown-value", "nonmember", "duplicate"])
def test_migration_refine_null_compatibility_keeps_invalid_results_blocked(updates):
    rows = _rows()
    rows["interval_parse_content"][0]["content"] = {"updates": deepcopy(updates)}
    before = deepcopy(rows)

    plan = plan_rows(rows, targets=_targets(), published=lambda *_: True, now=NOW)

    assert rows == before
    assert any("invalid Refine result: interval" in difference for difference in plan["differences"])


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
    assert plan["changes"]["video_parse_task"]["parent"]["detail"][RUNTIME_DETAIL_KEY][PREPARE_FAILURES_KEY] == 0


@pytest.mark.parametrize("legacy_count,detail_count", [(3, None), (None, 2), (3, 3)])
def test_parent_detail_migration_preserves_prepare_budget_and_releases_capacity(legacy_count, detail_count):
    rows = _rows()
    parent = rows["video_parse_task"][0]
    if legacy_count is not None:
        parent["prepare_retry_count"] = legacy_count
        parent["scheduler_slot"] = 2
    runtime = {CAPACITY_ACQUIRED_KEY: True, "accepted": True}
    if detail_count is not None:
        runtime[PREPARE_FAILURES_KEY] = detail_count
    parent["detail"][RUNTIME_DETAIL_KEY] = runtime
    before = deepcopy(rows)
    plan = plan_rows(rows, targets=_targets(), published=lambda *_: False, now=NOW)
    assert not plan["differences"] and rows == before
    change = plan["changes"]["video_parse_task"]["parent"]
    assert not {"prepare_retry_count", "scheduler_slot"} & change.keys()
    migrated_runtime = change["detail"][RUNTIME_DETAIL_KEY]
    assert migrated_runtime[PREPARE_FAILURES_KEY] == (detail_count if detail_count is not None else legacy_count)
    assert CAPACITY_ACQUIRED_KEY not in migrated_runtime
    assert migrated_runtime["accepted"] is True
    for name, changes in plan["changes"].items():
        for row in rows[name]:
            row.update(changes[row["id"]])
    repeat = plan_rows(rows, targets={}, published=lambda *_: False, now=NOW)
    assert not repeat["differences"] and not any(repeat["changes"].values())


def test_already_migrated_parent_moves_legacy_budget_without_resetting_children_or_audit():
    rows = _rows()
    first = plan_rows(rows, targets=_targets(), published=lambda *_: False, now=NOW)
    for name, changes in first["changes"].items():
        for row in rows[name]:
            row.update(changes[row["id"]])
    parent = rows["video_parse_task"][0]
    parent.update(prepare_retry_count=3, scheduler_slot=2)
    parent["detail"][RUNTIME_DETAIL_KEY].pop(PREPARE_FAILURES_KEY)
    parent["detail"][RUNTIME_DETAIL_KEY][CAPACITY_ACQUIRED_KEY] = True
    before = deepcopy(rows)
    plan = plan_rows(rows, targets={}, published=lambda *_: False, now=NOW)
    assert not plan["differences"] and rows == before
    assert not plan["changes"]["chunk_parse_task"] and not plan["changes"]["interval_parse_task"]
    change = plan["changes"]["video_parse_task"]["parent"]
    assert change.keys() == {"detail"}
    assert change["detail"][MARKER] == parent["detail"][MARKER]
    assert change["detail"][RUNTIME_DETAIL_KEY][PREPARE_FAILURES_KEY] == 3
    assert CAPACITY_ACQUIRED_KEY not in change["detail"][RUNTIME_DETAIL_KEY]
    parent.update(change)
    repeat = plan_rows(rows, targets={}, published=lambda *_: False, now=NOW)
    assert not repeat["differences"] and not any(repeat["changes"].values())


def test_already_migrated_generation_moves_to_config_without_restarting_children():
    rows = _rows()
    first = plan_rows(rows, targets=_targets(), published=lambda *_: True, now=NOW)
    for name, changes in first["changes"].items():
        for row in rows[name]:
            row.update(changes[row["id"]])
    parent = rows["video_parse_task"][0]
    parent["target_index_generation"] = parent["parse_config"]["indexing"].pop("index_generation")
    for child in rows["chunk_parse_task"]:
        child.update(indexed_generation=7, execution_deadline=None, start_time=NOW - timedelta(minutes=5))
    rows["interval_parse_task"][0].update(execution_deadline=None, start_time=NOW - timedelta(minutes=6))
    before = deepcopy(rows)
    observed = []
    plan = plan_rows(rows, targets={}, published=lambda expected, *_: observed.append(expected) or True, now=NOW)
    assert not plan["differences"] and rows == before
    assert not plan["changes"]["chunk_parse_task"] and not plan["changes"]["interval_parse_task"]
    change = plan["changes"]["video_parse_task"]["parent"]
    assert change.keys() == {"parse_config"}
    assert change["parse_config"]["indexing"]["index_generation"] == 7
    assert all(expected["index_generation"] == 7 for expected in observed)
    parent.update(change)
    repeat = plan_rows(rows, targets={}, published=lambda *_: True, now=NOW)
    assert not repeat["differences"] and not any(repeat["changes"].values())


@pytest.mark.parametrize("defect", ["legacy_target", "manifest", "published_generation", "expired_execution"])
def test_migrated_generation_conflicts_or_unreclaimed_execution_block_retirement(defect):
    rows = _rows()
    first = plan_rows(rows, targets=_targets(), published=lambda *_: True, now=NOW)
    for name, changes in first["changes"].items():
        for row in rows[name]:
            row.update(changes[row["id"]])
    targets = {}
    if defect == "legacy_target":
        rows["video_parse_task"][0]["target_index_generation"] = 8
    elif defect == "manifest":
        targets = _targets()
        targets["parent"]["generation"] = 8
    elif defect == "published_generation":
        rows["chunk_parse_task"][0]["indexed_generation"] = 8
    else:
        rows["chunk_parse_task"][0].update(status="RUNNING", lease_owner="expired",
            lease_expire_time=NOW - timedelta(seconds=1), execution_deadline=NOW - timedelta(seconds=1))
    before = deepcopy(rows)
    plan = plan_rows(rows, targets=targets, published=lambda *_: True, now=NOW)
    assert rows == before and plan["differences"]
    assert not any(plan["changes"].values())


@pytest.mark.parametrize("legacy_count,detail_count", [(3, 1), (-1, None), (None, True)])
def test_parent_prepare_budget_conflict_is_reported_without_guessing(legacy_count, detail_count):
    rows = _rows()
    rows["video_parse_task"][0].update(prepare_retry_count=legacy_count)
    rows["video_parse_task"][0]["detail"][RUNTIME_DETAIL_KEY] = {PREPARE_FAILURES_KEY: detail_count}
    before = deepcopy(rows)
    plan = plan_rows(rows, targets=_targets(), published=lambda *_: False, now=NOW)
    assert rows == before
    assert any("Prepare failure budget" in difference for difference in plan["differences"])



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
    url = os.getenv("SEEKINO_MIGRATION_TEST_DATABASE_URL")
    if not url:
        pytest.skip("SEEKINO_MIGRATION_TEST_DATABASE_URL is not configured")
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


def _seed_legacy_upload(engine, ids, *, completed):
    upload_id = str(uuid4())
    config = _upload_config()
    with engine.connect() as connection:
        connection.exec_driver_sql("ALTER TABLE upload_session ADD COLUMN video_parse_config JSON NULL")
        connection.commit()
    with engine.begin() as connection:
        uploads = Table("upload_session", MetaData(), autoload_with=connection)
        connection.execute(uploads.insert().values(
            **{**_upload_row(config), "id": upload_id, "tenant_id": "test", "series_id": ids["series"],
               "episode_order": 1, "upload_sequence": 1, "filename": "historical.mp4", "media_type": "video/mp4",
               "size_bytes": 4, "segment_size_bytes": 4, "segment_count": 1,
               "parse_config": _server_profile().model_dump(mode="json"),
               "status": "COMPLETED" if completed else "UPLOADING",
               "video_id": ids["video"] if completed else None,
               "initial_task_id": ids["parent"] if completed else None,
               "create_time": 1, "update_time": 2},
        ))
        segments = Table("upload_segment", MetaData(), autoload_with=connection)
        connection.execute(segments.insert().values(session_id=upload_id, segment_number=0,
            start_byte=0, end_byte_exclusive=4, size_bytes=4, sha256="e" * 64, artifact_uri="fixture://segment"))
        before = dict(connection.execute(select(uploads).where(uploads.c.id == upload_id)).mappings().one())
        before_segments = [dict(row) for row in connection.execute(select(segments)).mappings()]
    expected_config = deepcopy(config)
    if completed:
        expected_config["indexing"]["index_generation"] = 7
    return upload_id, before, before_segments, expected_config


@pytest.mark.parametrize("completed", [False, True], ids=["pending-upload", "completed-upload"])
def test_real_mysql_upload_snapshot_retirement_preserves_segments_and_replay_identity(migration_engine, completed):
    targets, ids = _seed_legacy(migration_engine)
    upload_id, before, before_segments, expected = _seed_legacy_upload(migration_engine, ids, completed=completed)
    with pytest.raises(SchemaUpgradeRequiredError):
        create_schema(migration_engine)
    dry = migrate(migration_engine, targets=targets, published=lambda *_: False, now=NOW)
    assert not dry["applied"] and not dry["differences"]
    assert dry == migrate(migration_engine, targets=targets, published=lambda *_: False, now=NOW)
    assert "ALTER TABLE `upload_session` DROP COLUMN `video_parse_config`" in dry["schemaFinalization"]
    with migration_engine.connect() as connection:
        uploads = Table("upload_session", MetaData(), autoload_with=connection)
        assert dict(connection.execute(select(uploads).where(uploads.c.id == upload_id)).mappings().one()) == before
    applied = migrate(migration_engine, targets=targets, published=lambda *_: False, now=NOW,
                      apply=True, writers_stopped=True)
    assert applied["applied"] and applied["preservedContentDigest"] == dry["preservedContentDigest"]
    create_schema(migration_engine)
    with migration_engine.connect() as connection:
        uploads = Table("upload_session", MetaData(), autoload_with=connection)
        assert "video_parse_config" not in uploads.c
        after = dict(connection.execute(select(uploads).where(uploads.c.id == upload_id)).mappings().one())
        assert after["parse_config"] == expected
        assert {key: value for key, value in after.items() if key != "parse_config"} == {
            key: value for key, value in before.items() if key not in {"parse_config", "video_parse_config"}}
        segments = Table("upload_segment", MetaData(), autoload_with=connection)
        assert [dict(row) for row in connection.execute(select(segments)).mappings()] == before_segments
    repeat = migrate(migration_engine, published=lambda *_: False, now=NOW, apply=True, writers_stopped=True)
    assert repeat["applied"] and not repeat["differences"] and not any(repeat["updates"].values())
    assert not repeat["schemaPreparation"] and not repeat["schemaFinalization"]


def test_real_mysql_historical_upload_cutover_keeps_already_migrated_tasks_unchanged(migration_engine):
    from scripts.migrate_video_parse_chunk_tasks import _inventory
    targets, ids = _seed_legacy(migration_engine)
    first = migrate(migration_engine, targets=targets, published=lambda *_: False, now=NOW,
                    apply=True, writers_stopped=True)
    assert first["applied"]
    upload_id, _before, _segments, _expected = _seed_legacy_upload(migration_engine, ids, completed=True)
    historical = _historical_upload_config()
    with migration_engine.begin() as connection:
        uploads = Table("upload_session", MetaData(), autoload_with=connection)
        connection.execute(uploads.update().where(uploads.c.id == upload_id).values(video_parse_config=historical))
        _, before = _inventory(connection)
    task_config = before["video_parse_task"][0]["parse_config"]
    expected = deepcopy(historical)
    expected["execution"].pop("max_concurrent_model_calls")
    for name, value in task_config["execution"].items():
        expected["execution"].setdefault(name, value)
    expected["indexing"] = deepcopy(task_config["indexing"])

    dry = migrate(migration_engine, published=lambda *_: False, now=NOW)

    assert not dry["differences"]
    assert dry["updates"] == {"video_parse_task": 0, "chunk_parse_task": 0,
                              "interval_parse_task": 0, "upload_session": 1}
    assert not dry["schemaPreparation"]
    assert dry["schemaFinalization"] == ["ALTER TABLE `upload_session` DROP COLUMN `video_parse_config`"]
    applied = migrate(migration_engine, published=lambda *_: False, now=NOW,
                      apply=True, writers_stopped=True)
    assert applied["applied"] and applied["preservedContentDigest"] == dry["preservedContentDigest"]
    with migration_engine.connect() as connection:
        _, after = _inventory(connection)
    assert {name: rows for name, rows in after.items() if name != "upload_session"} == {
        name: rows for name, rows in before.items() if name != "upload_session"}
    assert after["upload_session"] == [{
        **{key: value for key, value in before["upload_session"][0].items() if key != "video_parse_config"},
        "parse_config": expected,
    }]
    repeat = migrate(migration_engine, published=lambda *_: False, now=NOW)
    assert not repeat["differences"] and not any(repeat["updates"].values())
    assert not repeat["schemaPreparation"] and not repeat["schemaFinalization"]


@pytest.mark.parametrize("interruption", ["before_upload_column_drop", "after_upload_column_drop"])
def test_real_mysql_upload_migration_resumes_on_either_side_of_retirement(migration_engine, monkeypatch, interruption):
    import scripts.migrate_video_parse_chunk_tasks as module
    targets, ids = _seed_legacy(migration_engine)
    upload_id, before, _segments, expected = _seed_legacy_upload(migration_engine, ids, completed=True)
    original_schema = module._schema
    stopped = False
    def interrupted(connection, *, finalize, apply):
        nonlocal stopped
        should_stop = finalize and apply and not stopped
        if should_stop and interruption == "before_upload_column_drop":
            stopped = True
            raise RuntimeError("simulated upload migration interruption")
        result = original_schema(connection, finalize=finalize, apply=apply)
        if should_stop:
            stopped = True
            raise RuntimeError("simulated upload migration interruption")
        return result
    monkeypatch.setattr(module, "_schema", interrupted)
    with pytest.raises(RuntimeError, match="simulated upload migration interruption"):
        migrate(migration_engine, targets=targets, published=lambda *_: False, now=NOW,
                apply=True, writers_stopped=True)
    repeated = migrate(migration_engine, targets=targets, published=lambda *_: False, now=NOW,
                       apply=True, writers_stopped=True)
    assert repeated["applied"] and not repeated["differences"]
    with migration_engine.connect() as connection:
        uploads = Table("upload_session", MetaData(), autoload_with=connection)
        after = dict(connection.execute(select(uploads).where(uploads.c.id == upload_id)).mappings().one())
        assert "video_parse_config" not in uploads.c and after["parse_config"] == expected
        assert after["version"] == before["version"] and after["request_fingerprint"] == before["request_fingerprint"]


def test_real_mysql_active_upload_completion_blocks_all_migration_writes(migration_engine):
    targets, ids = _seed_legacy(migration_engine)
    upload_id, _before, _segments, _config = _seed_legacy_upload(migration_engine, ids, completed=False)
    with migration_engine.begin() as connection:
        connection.execute(text("UPDATE upload_session SET status='COMPLETING', completion_token=:token WHERE id=:id"),
                           {"token": str(uuid4()), "id": upload_id})
        uploads = Table("upload_session", MetaData(), autoload_with=connection)
        before = dict(connection.execute(select(uploads).where(uploads.c.id == upload_id)).mappings().one())
    plan = migrate(migration_engine, targets=targets, published=lambda *_: False, now=NOW,
                   apply=True, writers_stopped=True)
    assert not plan["applied"] and any("Upload completion" in item for item in plan["differences"])
    assert "phase" not in {item["name"] for item in inspect(migration_engine).get_columns("chunk_parse_task")}
    with migration_engine.connect() as connection:
        assert dict(connection.execute(select(uploads).where(uploads.c.id == upload_id)).mappings().one()) == before


def test_real_mysql_dry_run_apply_repeat_and_results_preserved(migration_engine):
    targets, ids = _seed_legacy(migration_engine)
    before = inspect(migration_engine).get_columns("chunk_parse_task")
    dry = migrate(migration_engine, targets=targets, published=lambda expected, *_: expected["video_chunk_id"] == ids["chunk-0"], now=NOW)
    assert not dry["applied"] and not dry["differences"]
    repeat_dry = migrate(migration_engine, targets=targets, published=lambda expected, *_: expected["video_chunk_id"] == ids["chunk-0"], now=NOW)
    assert repeat_dry == dry
    assert not any(column in sql for columns in RETIRED_COLUMNS.values() for column in columns
                   for sql in dry["schemaPreparation"] + dry["schemaFinalization"])
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


def test_real_mysql_retired_columns_are_planned_then_removed_after_snapshot_and_budget_copy(migration_engine):
    targets, ids = _seed_legacy(migration_engine)
    first = migrate(migration_engine, targets=targets, published=lambda *_: False,
                    now=NOW, apply=True, writers_stopped=True)
    assert first["applied"]
    with migration_engine.connect() as connection:
        connection.exec_driver_sql("ALTER TABLE video_parse_task ADD COLUMN scheduler_slot INT NULL, "
            "ADD COLUMN prepare_retry_count INT NOT NULL DEFAULT 0, "
            "ADD COLUMN target_index_generation INT NOT NULL DEFAULT 7, "
            "ADD CONSTRAINT uq_video_parse_task_scheduler_slot UNIQUE (scheduler_slot), "
            "ADD CONSTRAINT ck_video_parse_task_scheduler_slot CHECK (scheduler_slot IS NULL OR scheduler_slot > 0), "
            "ADD CONSTRAINT ck_video_parse_task_prepare_retry_count CHECK (prepare_retry_count >= 0), "
            "ADD CONSTRAINT ck_video_parse_task_index_generation CHECK (target_index_generation > 0)")
        connection.exec_driver_sql("ALTER TABLE chunk_parse_task ADD COLUMN indexed_generation INT NULL, "
            "ADD COLUMN execution_deadline TIMESTAMP NULL, "
            "ADD CONSTRAINT ck_chunk_parse_task_legacy_generation CHECK (indexed_generation IS NULL OR indexed_generation > 0)")
        connection.exec_driver_sql("ALTER TABLE interval_parse_task ADD COLUMN execution_deadline TIMESTAMP NULL")
        connection.exec_driver_sql("ALTER TABLE upload_session ADD COLUMN video_parse_config JSON NULL")
        connection.execute(text("UPDATE video_parse_task SET scheduler_slot=2, prepare_retry_count=3, "
            "parse_config=JSON_REMOVE(parse_config, '$.indexing.index_generation'), "
            "detail=JSON_SET(JSON_REMOVE(detail, :budget_path), :capacity_path, CAST('true' AS JSON)) WHERE id=:task_id"),
            {"budget_path": f"$.{RUNTIME_DETAIL_KEY}.{PREPARE_FAILURES_KEY}",
             "capacity_path": f"$.{RUNTIME_DETAIL_KEY}.{CAPACITY_ACQUIRED_KEY}", "task_id": ids["parent"]})
        connection.commit()
    dry = migrate(migration_engine, published=lambda *_: False, now=NOW)
    assert not dry["applied"] and not dry["differences"]
    assert dry == migrate(migration_engine, published=lambda *_: False, now=NOW)
    assert dry["updates"] == {"video_parse_task": 1, "chunk_parse_task": 0, "interval_parse_task": 0, "upload_session": 0}
    for table, columns in RETIRED_COLUMNS.items():
        for column in columns:
            assert f"ALTER TABLE `{table}` DROP COLUMN `{column}`" in dry["schemaFinalization"]
            assert column in {item["name"] for item in inspect(migration_engine).get_columns(table)}
    applied = migrate(migration_engine, published=lambda *_: False, now=NOW, apply=True, writers_stopped=True)
    assert applied["applied"] and applied["preservedContentDigest"] == dry["preservedContentDigest"]
    create_schema(migration_engine)
    with migration_engine.connect() as connection:
        assert connection.scalar(text("SELECT JSON_EXTRACT(detail, :path) FROM video_parse_task WHERE id=:task_id"),
            {"path": f"$.{RUNTIME_DETAIL_KEY}.{PREPARE_FAILURES_KEY}", "task_id": ids["parent"]}) == "3"
        assert connection.scalar(text("SELECT JSON_EXTRACT(parse_config, '$.indexing.index_generation') "
            "FROM video_parse_task WHERE id=:task_id"), {"task_id": ids["parent"]}) == "7"
    repeat = migrate(migration_engine, published=lambda *_: False, now=NOW)
    assert not repeat["differences"] and not any(repeat["updates"].values())
    assert repeat["schemaPreparation"] == [] and repeat["schemaFinalization"] == []


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
