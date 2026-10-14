"""In-place Parse V2 task migration; dry-run by default, never writes Elasticsearch.

Use ``python -m scripts.migrate_video_parse_chunk_tasks --help``. Historical
index targets must come from an audited per-task manifest, not live defaults::

  {"task-id": {"generation": 1, "sparse_enabled": false,
    "contract_fingerprint": "<64 hex digits>", "evidence": "deployment/config backup"}}

Stop old writers and back up MySQL before --apply --writers-stopped. Run the
same command again to verify idempotence, then start only the new workers.
DDL is resumable (MySQL implicitly commits ALTER); row conversion and its audit
marker commit together. Existing IDs, content bytes, and membership never change.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
from typing import Callable, Mapping

from sqlalchemy import CheckConstraint, MetaData, Table, UniqueConstraint, inspect, select, text
from sqlalchemy.schema import CreateColumn, CreateIndex

from agent.video_parse_v2.input import ParseConfig
from agent.video_parse_v2.scheduler import chunk_index_ready, summarize_video_task
from engine.common.config import VideoParseRuntimeConfig
from engine.parsev2.schemas import VideoParseResult
from engine.parsev2.planning import interval_size, plan_chunks, plan_intervals
from engine.parsev2.validation import ValidatedIntervalRefinement
from server.database import _normalize_task_check, _schema_structure_differences, create_database_engine, DatabaseEnvironment
from server.model.job import ChunkTask, IntervalParseTask, VideoParseTask

MARKER = "_chunk_task_migration_20261014"
RUNTIME = "_worker_runtime_v2"
MODELS = (VideoParseTask.__table__, ChunkTask.__table__, IntervalParseTask.__table__)
ADDED = {
    "video_parse_task": {"scheduler_slot", "target_index_generation", "prepare_retry_count"},
    "chunk_parse_task": {"phase", "indexed_content_version", "indexed_generation", "execution_deadline"},
    "interval_parse_task": {"execution_deadline"},
}
TABLES = ("video_parse_task", "chunk_parse_task", "chunk_parse_content",
          "interval_parse_task", "interval_parse_content", "video_chunk", "video", "series")


def _json_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     default=str).encode()).hexdigest()


def _preserved_facts(rows):
    return {
        **{name: rows[name] for name in ("chunk_parse_content", "interval_parse_content", "video_chunk")},
        "chunk_identities": [{key: row[key] for key in ("id", "video_task_id", "chunk_id")}
                             for row in rows["chunk_parse_task"]],
        "interval_members": [{key: row[key] for key in ("id", "video_task_id", "chunk_task_id", "chunk_task_ids", "start_sequence_no", "chunk_count")}
                             for row in rows["interval_parse_task"]],
    }


def _utc(value):
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


def _released(row: dict, *, deadline: bool = True) -> dict:
    values = {"lease_owner": None, "lease_expire_time": None,
              "lease_version": row["lease_version"] + int(row.get("lease_owner") is not None)}
    if deadline:
        values["execution_deadline"] = None
    return values


def _snapshot(task: dict, target: dict | None) -> tuple[dict, int, dict]:
    config = deepcopy(task["parse_config"])
    old_execution = deepcopy(config.get("execution", {}))
    defaults = VideoParseRuntimeConfig()
    execution = config.setdefault("execution", {})
    for name in ("parse_concurrency", "refine_concurrency", "index_concurrency",
                 "parse_execution_timeout_seconds", "refine_execution_timeout_seconds",
                 "index_execution_timeout_seconds"):
        execution.setdefault(name, getattr(defaults, name))
    execution.pop("max_concurrent_model_calls", None)
    generation = task.get("target_index_generation")
    if not generation or not config.get("indexing"):
        if not target or not str(target.get("evidence", "")).strip():
            raise ValueError("historical index contract requires an audited task-target manifest")
        generation = target["generation"]
        config["indexing"] = {key: target[key] for key in ("sparse_enabled", "contract_fingerprint")}
    if isinstance(generation, bool) or not isinstance(generation, int) or not 0 < generation < (1 << 31):
        raise ValueError("invalid target generation")
    # Validation fills no model/visual/budget defaults; missing history is a difference.
    config = ParseConfig.model_validate(config).model_dump(mode="json")
    return config, generation, {"execution_before": old_execution,
                                "target_evidence": target.get("evidence") if target else "existing snapshot"}


def _inventory(connection) -> tuple[dict[str, Table], dict[str, list[dict]]]:
    metadata = MetaData()
    tables = {name: Table(name, metadata, autoload_with=connection) for name in TABLES}
    rows = {name: [dict(row) for row in connection.execute(select(table).order_by(*table.primary_key.columns)).mappings()]
            for name, table in tables.items()}
    return tables, rows


def plan_rows(rows: dict[str, list[dict]], *, targets: Mapping[str, dict],
              published: Callable[[dict, bool, bool], bool] | None,
              now: datetime) -> dict:
    """Pure conversion plan except the supplied read-only ES verifier."""
    changes = {name: {} for name in ADDED}
    differences: list[str] = []
    audit = []
    contents = {row["chunk_task_id"]: row for row in rows["chunk_parse_content"]}
    refinements = {row["interval_task_id"]: row for row in rows["interval_parse_content"]}
    physical = {row["id"]: row for row in rows["video_chunk"]}
    videos = {row["id"]: row for row in rows["video"]}
    series = {row["id"]: row for row in rows["series"]}
    for task in rows["video_parse_task"]:
        task_id = task["id"]
        children = [row for row in rows["chunk_parse_task"] if row["video_task_id"] == task_id]
        intervals = [row for row in rows["interval_parse_task"] if row["video_task_id"] == task_id]
        local: list[str] = []
        already_migrated = bool((task.get("detail") or {}).get(MARKER))
        try:
            config, generation, provenance = _snapshot(task, targets.get(task_id))
        except (ValueError, KeyError, TypeError) as exc:
            differences.append(f"{task_id}: snapshot: {exc}")
            continue
        video = videos.get(task["video_id"])
        if video is None:
            differences.append(f"{task_id}: missing Video")
            continue
        expected_chunks = plan_chunks(duration_seconds=video["duration_ms"], chunk_duration=config["chunk_duration"])
        expected_windows = plan_intervals(chunk_count=len(expected_chunks), size=interval_size(
            interval_duration=config["interval_duration"], chunk_duration=config["chunk_duration"],
        ))
        expected_specs = {(window.start_sequence_no, window.chunk_count) for window in expected_windows}
        actual_specs = {(interval["start_sequence_no"], interval["chunk_count"]) for interval in intervals}
        if task["chunk_count"] != len(expected_chunks):
            local.append("parent Chunk count differs from fixed media topology")
        if len(actual_specs) != len(intervals) or actual_specs - expected_specs:
            local.append("unexpected or duplicate Refine windows")
        actual_sequences = []
        for child in children:
            chunk = physical.get(child["chunk_id"])
            if chunk is None:
                local.append(f"missing physical Chunk: {child['id']}")
                continue
            sequence = chunk["sequence_no"]
            actual_sequences.append(sequence)
            if (chunk["video_id"] != task["video_id"] or not 0 <= sequence < len(expected_chunks)
                    or (chunk["start_ms"], chunk["end_ms"]) != (
                        expected_chunks[sequence].start_seconds, expected_chunks[sequence].end_seconds)):
                local.append(f"Chunk differs from fixed media topology: {child['id']}")
        if len(set(actual_sequences)) != len(actual_sequences):
            local.append("duplicate physical Chunk sequence")
        topology_complete = (sorted(actual_sequences) == list(range(len(expected_chunks)))
                             and actual_specs == expected_specs)
        runtime = (task.get("detail") or {}).get(RUNTIME, {})
        prefix = runtime.get("prefix", {})
        if prefix.get("lease_owner") and (
            not isinstance(prefix.get("lease_expire_time_ms"), int)
            or prefix["lease_expire_time_ms"] > int(now.timestamp() * 1000)
        ):
            local.append("live Prefix execution must be cancelled/drained before migration")
        for row in [task, *children, *intervals]:
            if row.get("lease_owner") and (not row.get("lease_expire_time")
                                           or _utc(row["lease_expire_time"]) > now):
                local.append(f"live lease: {row['id']}")
        valid_content = set()
        for child in children:
            content = contents.get(child["id"])
            if content and content.get("content") is not None:
                try:
                    VideoParseResult.model_validate(content["content"])
                    if content["version"] <= 0:
                        raise ValueError("nonpositive version")
                    valid_content.add(child["id"])
                except (ValueError, TypeError):
                    local.append(f"invalid understanding: {child['id']}")
            elif child["status"] == "COMPLETE":
                local.append(f"completed Chunk has no understanding: {child['id']}")
        members = {child["id"] for child in children}
        coverage = {child_id: [] for child_id in members}
        # Missing planned windows still block their members even before Prepare
        # recreates those rows. Existing overlapping windows are insufficient.
        for child in children:
            chunk = physical.get(child["chunk_id"])
            if chunk is not None:
                coverage[child["id"]].extend(False for window in expected_windows
                    if (window.start_sequence_no, window.chunk_count) not in actual_specs
                    and window.contains(chunk["sequence_no"]))
        failed_index = set()
        index_attempts = {child_id: [] for child_id in members}
        converted_intervals = []
        for interval in intervals:
            ids = interval["chunk_task_ids"]
            if (not isinstance(ids, list) or len(ids) != interval["chunk_count"]
                    or len(set(ids)) != len(ids) or not set(ids) <= members
                    or not ids or interval["chunk_task_id"] != ids[0]):
                local.append(f"invalid Interval membership: {interval['id']}")
                continue
            seq = [physical.get(next(c["chunk_id"] for c in children if c["id"] == item), {}).get("sequence_no", -1)
                   for item in ids]
            if seq != list(range(interval["start_sequence_no"], interval["start_sequence_no"] + len(ids))):
                local.append(f"noncontiguous Interval: {interval['id']}")
            committed = False
            if interval["id"] in refinements:
                try:
                    result = ValidatedIntervalRefinement.model_validate(refinements[interval["id"]]["content"])
                    targets_in_result = [item.sequence_no for item in result.updates]
                    if len(set(targets_in_result)) != len(targets_in_result) or not set(targets_in_result) <= set(seq):
                        raise ValueError("refinement targets outside members")
                    committed = True
                except (ValueError, TypeError):
                    local.append(f"invalid Refine result: {interval['id']}")
            if interval["status"] in {"INDEXING", "INDEXING_FAILED", "COMPLETE"} and not committed:
                local.append(f"completed Refine has no result: {interval['id']}")
            if already_migrated and interval["status"] not in {"WAITING", "REFINING", "COMPLETE", "PARSING_FAILED"}:
                local.append(f"invalid migrated Interval status: {interval['id']}")
            for child_id in ids:
                coverage[child_id].append(committed and (not already_migrated or interval["status"] == "COMPLETE"))
            if interval["status"] in {"INDEXING", "INDEXING_FAILED"}:
                for child_id in ids:
                    index_attempts[child_id].append(interval)
            if interval["status"] == "INDEXING_FAILED":
                failed_index.update(ids)
            if interval["status"] == "PARSING_FAILED" and committed:
                local.append(f"explicit Refine failure conflicts with committed result: {interval['id']}")
            update = {**_released(interval), "ready_mask": sum(1 << i for i, item in enumerate(ids)
                                                               if item in valid_content)}
            if committed:
                update["status"] = "COMPLETE"
            elif interval["status"] == "PARSING_FAILED":
                update["status"] = "PARSING_FAILED"
            else:
                update["status"] = "WAITING"
                if interval.get("lease_owner"):
                    _recover(update, interval, config, now, "PARSING_FAILED")
            converted_intervals.append({**interval, **update})
            changes["interval_parse_task"][interval["id"]] = update
        converted_children = []
        for child in children:
            child_id = child["id"]
            if already_migrated:
                phase_status = (child.get("phase"), child["status"])
                allowed = {(1, state) for state in ("QUEUED", "RUNNING", "FAILED")} | {(2, "WAITING")} | {
                    (3, state) for state in ("QUEUED", "RUNNING", "COMPLETE", "FAILED")}
                if phase_status not in allowed:
                    local.append(f"invalid migrated phase/status: {child_id}")
                if child.get("phase") == 1 and child_id in valid_content:
                    local.append(f"migrated Parse would repeat committed understanding: {child_id}")
                if child.get("phase") in (2, 3) and child_id not in valid_content:
                    local.append(f"migrated later phase has no understanding: {child_id}")
                if child.get("phase") == 3 and not chunk_index_ready(child_id in valid_content, coverage[child_id]):
                    local.append(f"migrated Index lacks complete Refine coverage: {child_id}")
                if phase_status == (3, "COMPLETE") and child_id in valid_content:
                    content = contents[child_id]
                    if (child.get("indexed_content_version") != content["version"]
                            or child.get("indexed_generation") != generation):
                        local.append(f"migrated publication fields differ from target: {child_id}")
                    elif published is None:
                        local.append(f"ES publication verification required: {child_id}")
                    else:
                        try:
                            if not _published(published, task, child, content, physical, videos, series, generation, config):
                                local.append(f"migrated COMPLETE is not query-visible: {child_id}")
                        except Exception as exc:
                            local.append(f"ES verification failed for {child_id}: {type(exc).__name__}")
                converted_children.append(child)
                continue
            if child["status"] == "FAILED" and child_id in valid_content:
                local.append(f"legacy Parse failure conflicts with committed understanding: {child_id}")
            update = {**_released(child), "indexed_content_version": None, "indexed_generation": None}
            if child_id not in valid_content:
                update.update(phase=1, status="FAILED" if child["status"] == "FAILED" else "QUEUED")
                if child.get("lease_owner") and update["status"] != "FAILED":
                    _recover(update, child, config, now, "FAILED")
            elif not chunk_index_ready(True, coverage[child_id]):
                if not coverage[child_id]:
                    local.append(f"understood Chunk has no planned covering Interval: {child_id}")
                update.update(phase=2, status="WAITING")
            else:
                sources = index_attempts[child_id]
                count = max((source["retry_count"] + int(source.get("lease_owner") is not None)
                             for source in sources), default=0)
                update.update(phase=3, status="QUEUED", retry_count=count)
                if sources:
                    update["next_execute_time"] = max(_utc(source["next_execute_time"]) for source in sources)
                    if any(source.get("lease_owner") for source in sources):
                        recovered = {}
                        _recover(recovered, {"retry_count": count - 1}, config, now, "FAILED")
                        if "next_execute_time" in recovered:
                            recovered["next_execute_time"] = max(update["next_execute_time"], recovered["next_execute_time"])
                        update.update(recovered)
                    if count > config["execution"]["max_retry_count"]:
                        update["status"] = "FAILED"
                if published is None:
                    local.append(f"ES publication verification required: {child_id}")
                else:
                    content = contents[child_id]
                    try:
                        ready = _published(published, task, child, content, physical, videos, series, generation, config)
                    except Exception as exc:
                        local.append(f"ES verification failed for {child_id}: {type(exc).__name__}")
                        ready = False
                    if ready:
                        update.update(status="COMPLETE", indexed_content_version=content["version"],
                                      indexed_generation=generation)
                    elif child_id in failed_index:
                        update.update(status="FAILED")
            converted_children.append({**child, **update})
            changes["chunk_parse_task"][child_id] = update
        # Already converted tasks are verification-only. Never reinitialize their budgets.
        if already_migrated:
            if task["status"] == "COMPLETE" and (
                not topology_complete or runtime.get("failure_status")
                or any(child.get("phase") != 3 or child["status"] != "COMPLETE" for child in children)
            ):
                local.append("migrated parent COMPLETE differs from persisted child/failure state")
            for child in children:
                changes["chunk_parse_task"].pop(child["id"], None)
                if child.get("phase") not in (1, 2, 3):
                    local.append(f"migration marker without phase: {child['id']}")
            for interval in intervals:
                changes["interval_parse_task"].pop(interval["id"], None)
            differences.extend(f"{task_id}: {item}" for item in local)
            audit.append({"task_id": task_id, "already_migrated": True})
            continue
        detail = deepcopy(task.get("detail") or {})
        detail[MARKER] = {"at": now.isoformat(), **provenance,
                          "parent_status": task["status"], "attempt_count": task["attempt_count"],
                          "chunks": {c["id"]: {k: c.get(k) for k in ("status", "retry_count", "lease_version")}
                                     for c in children},
                          "intervals": {i["id"]: {k: i.get(k) for k in ("status", "retry_count", "lease_version")}
                                        for i in intervals}}
        status = summarize_video_task(
            topology_complete=topology_complete,
            understanding_complete=len(valid_content) == len(children),
            refinement_complete=all(i["status"] == "COMPLETE" for i in converted_intervals),
            indexing_complete=all(c["status"] == "COMPLETE" for c in converted_children),
            parsing_failed=task["status"] == "PARSING_FAILED" or runtime.get("failure_status") == "PARSING_FAILED"
                           or any(c["phase"] == 1 and c["status"] == "FAILED" for c in converted_children)
                           or any(i["status"] == "PARSING_FAILED" for i in converted_intervals),
            indexing_failed=task["status"] == "INDEXING_FAILED" or runtime.get("failure_status") == "INDEXING_FAILED"
                            or any(c["phase"] == 3 and c["status"] == "FAILED" for c in converted_children),
        )
        if not topology_complete and not status.endswith("FAILED"):
            status = "PENDING" if not children and task["status"] == "PENDING" else "PREPARING"
        if status.endswith("FAILED"):
            detail.setdefault(RUNTIME, {})["failure_status"] = status
        changes["video_parse_task"][task_id] = {
            **_released(task, deadline=False), "heartbeat_time": None, "scheduler_slot": None,
            "target_index_generation": generation, "prepare_retry_count": task.get("prepare_retry_count") or 0,
            "parse_config": config, "status": status, "detail": detail,
            "finish_time": task.get("finish_time") or now if status in {"COMPLETE", "PARSING_FAILED", "INDEXING_FAILED"} else None,
        }
        differences.extend(f"{task_id}: {item}" for item in local)
        audit.append({"task_id": task_id, "from": task["status"], "to": status,
                      "chunk_states": dict(Counter(f"{c['phase']}/{c['status']}" for c in converted_children))})
    return {"changes": changes, "differences": differences, "tasks": audit}


def _published(published, task, child, content, physical, videos, series, generation, config):
    chunk = physical[child["chunk_id"]]
    video = videos[task["video_id"]]
    expected = {"tenant_id": series[video["series_id"]]["tenant_id"],
                "series_id": video["series_id"], "video_id": task["video_id"],
                "parse_task_id": task["id"], "video_chunk_id": child["chunk_id"],
                "content_version": content["version"], "index_generation": generation,
                "chunk_start_seconds": chunk["start_ms"], "chunk_end_seconds": chunk["end_ms"]}
    semantic = any(event["start_seconds"] < chunk["end_ms"] and event["end_seconds"] > chunk["start_ms"]
                   for event in content["content"]["timeline"])
    return published(expected, semantic, config["indexing"]["sparse_enabled"])


def _recover(update, row, config, now, failed_status):
    execution = config["execution"]
    count = row["retry_count"] + 1
    update["retry_count"] = count
    if count > execution["max_retry_count"]:
        update["status"] = failed_status
        update["finish_time"] = now
    else:
        delay = min(execution["retry_max_delay_seconds"],
                    execution["retry_initial_delay_seconds"] * 2 ** min(count - 1, 30))
        update["next_execute_time"] = now + timedelta(seconds=delay)


def _schema(connection, *, finalize: bool, apply: bool) -> list[str]:
    """Only alter the known task delta. Never create/drop data or disable FKs."""
    statements = []
    inspector = inspect(connection)
    for table in MODELS:
        name = table.name
        columns = {c["name"]: c for c in inspector.get_columns(name)}
        missing = set(table.c.keys()) - columns.keys()
        extra = columns.keys() - set(table.c.keys())
        if missing - ADDED[name] or extra:
            raise ValueError(f"{name}: unsupported schema differences {sorted((missing - ADDED[name]) | extra)}")
        if not finalize:
            for column_name in sorted(missing):
                column = table.c[column_name]
                sql_type = column.type.compile(dialect=connection.dialect)
                statements.append(f"ALTER TABLE `{name}` ADD COLUMN `{column_name}` {sql_type} NULL")
            actual_checks = {c["name"]: c["sqltext"] for c in inspector.get_check_constraints(name)}
            for constraint in table.constraints:
                if isinstance(constraint, CheckConstraint) and constraint.name in actual_checks:
                    if _normalize_task_check(actual_checks[constraint.name]) != _normalize_task_check(str(constraint.sqltext)):
                        statements.append(f"ALTER TABLE `{name}` DROP CHECK `{constraint.name}`")
        else:
            for column_name in sorted(ADDED[name]):
                column = table.c[column_name]
                if not column.nullable and columns[column_name]["nullable"]:
                    statements.append(f"ALTER TABLE `{name}` MODIFY COLUMN {CreateColumn(column).compile(dialect=connection.dialect)}")
            actual_checks = {c["name"] for c in inspector.get_check_constraints(name)}
            for constraint in sorted(table.constraints, key=lambda c: str(c.name)):
                if isinstance(constraint, CheckConstraint) and constraint.name not in actual_checks:
                    statements.append(f"ALTER TABLE `{name}` ADD CONSTRAINT `{constraint.name}` CHECK ({constraint.sqltext})")
                if isinstance(constraint, UniqueConstraint) and tuple(constraint.columns.keys()) == ("scheduler_slot",):
                    uniques = {tuple(c["column_names"]) for c in inspector.get_unique_constraints(name)}
                    if ("scheduler_slot",) not in uniques:
                        statements.append(f"ALTER TABLE `{name}` ADD CONSTRAINT `{constraint.name}` UNIQUE (scheduler_slot)")
            indexes = {i["name"]: tuple(i["column_names"]) for i in inspector.get_indexes(name)}
            for index in sorted(table.indexes, key=lambda i: i.name):
                columns_list = tuple(index.columns.keys())
                if indexes.get(index.name) == columns_list:
                    continue
                if index.name in indexes:
                    # One ALTER preserves indexes required by existing foreign keys.
                    columns_sql = ", ".join(f"`{col}`" for col in columns_list)
                    statements.append(f"ALTER TABLE `{name}` DROP INDEX `{index.name}`, ADD INDEX `{index.name}` ({columns_sql})")
                else:
                    statements.append(str(CreateIndex(index).compile(dialect=connection.dialect)))
    if apply:
        for statement in statements:
            connection.exec_driver_sql(statement)
            connection.commit()
    return statements


def migrate(engine, *, targets: Mapping[str, dict] | None = None,
            published: Callable[[dict, bool, bool], bool] | None = None,
            apply: bool = False, writers_stopped: bool = False, now: datetime | None = None) -> dict:
    if engine.dialect.name != "mysql":
        raise ValueError("migration requires real MySQL; SQLite cannot validate this transition")
    if apply and not writers_stopped:
        raise ValueError("apply requires --writers-stopped after old workers have drained")
    now = now or datetime.now(UTC)
    with engine.connect() as connection:
        locked = False
        try:
            if apply:
                locked = connection.scalar(text("SELECT GET_LOCK('anivag_chunk_task_migration', 0)")) == 1
                if not locked:
                    raise RuntimeError("another task migration is running")
                connection.commit()
            tables, rows = _inventory(connection)
            plan = plan_rows(rows, targets=targets or {}, published=published, now=now)
            ddl = _schema(connection, finalize=False, apply=False)
            preserved = _json_hash(_preserved_facts(rows))
            report = {"apply": apply, "differences": plan["differences"], "tasks": plan["tasks"],
                      "counts": {name: len(value) for name, value in rows.items()},
                      "updates": {name: len(value) for name, value in plan["changes"].items()},
                      "schemaPreparation": ddl, "preservedContentDigest": preserved}
            if not apply or plan["differences"]:
                report["applied"] = False
                return report
            connection.rollback()
            _schema(connection, finalize=False, apply=True)
            tables, current = _inventory(connection)
            if _json_hash(current) != _json_hash(rows):
                # Newly added nullable columns are the only allowed inventory delta.
                for name in ADDED:
                    for row in current[name]:
                        for column in ADDED[name]:
                            if rows[name] and column not in rows[name][0]:
                                row.pop(column, None)
                if _json_hash(current) != _json_hash(rows):
                    raise RuntimeError("rows changed after planning; keep writers stopped and rerun")
            connection.rollback()
            with connection.begin():
                for name in ("interval_parse_task", "chunk_parse_task", "video_parse_task"):
                    for row_id, values in plan["changes"][name].items():
                        connection.execute(tables[name].update().where(tables[name].c.id == row_id).values(**values))
            report["schemaFinalization"] = _schema(connection, finalize=True, apply=True)
            schema_differences = _schema_structure_differences(inspect(connection), set(TABLES), MODELS)
            if schema_differences:
                raise RuntimeError(f"final task schema differs: {sorted(schema_differences)}")
            _, after = _inventory(connection)
            if preserved != _json_hash(_preserved_facts(after)):
                raise RuntimeError("preserved content verification failed")
            report["applied"] = True
            return report
        finally:
            if locked:
                connection.execute(text("SELECT RELEASE_LOCK('anivag_chunk_task_migration')"))
                connection.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.getenv("ANIVAG_DATABASE_URL"), required=not os.getenv("ANIVAG_DATABASE_URL"))
    parser.add_argument("--task-targets", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--writers-stopped", action="store_true")
    parser.add_argument("--environment", choices=[v.value for v in DatabaseEnvironment], required=True)
    parser.add_argument("--elasticsearch-url", default=os.getenv("ANIVAG_ELASTICSEARCH_URL"))
    parser.add_argument("--elasticsearch-index", default=os.getenv("ANIVAG_ELASTICSEARCH_INDEX"))
    parser.add_argument("--vector-dimensions", type=int, default=None)
    args = parser.parse_args()
    targets = json.loads(args.task_targets.read_text()) if args.task_targets else {}
    engine = create_database_engine(args.database_url, environment=DatabaseEnvironment(args.environment))
    try:
        if args.elasticsearch_url and args.elasticsearch_index:
            from server.caller.elasticsearch import open_elasticsearch_client
            from server.config.elasticsearch import ElasticsearchConfig, resolve_vector_dimensions
            from server.storage.elasticsearch import ElasticsearchVideoChunkStore
            config = ElasticsearchConfig(args.elasticsearch_url, args.elasticsearch_index,
                                         api_key=os.getenv("ANIVAG_ELASTICSEARCH_API_KEY") or None)
            config.validate_environment(DatabaseEnvironment(args.environment))
            dimensions, _ = resolve_vector_dimensions(text_dimensions=args.vector_dimensions)
            with open_elasticsearch_client(config) as client:
                store = ElasticsearchVideoChunkStore(client, index_name=args.elasticsearch_index,
                    environment=DatabaseEnvironment(args.environment), semantic_dimensions=dimensions,
                    vision_dimensions=dimensions)
                def verify(expected, semantic, sparse):
                    state = store.inspect([(expected["parse_task_id"], expected["video_chunk_id"])])
                    actual = next(iter(state.values()))
                    if actual.present and (actual.index_generation or 0, actual.content_version or 0) > (expected["index_generation"], expected["content_version"]):
                        raise ValueError("ES contains a newer target; resolve historical generation")
                    return store.is_current(expected, semantic_required=semantic, sparse_required=sparse)
                report = migrate(engine, targets=targets, published=verify, apply=args.apply,
                                 writers_stopped=args.writers_stopped)
        else:
            report = migrate(engine, targets=targets, apply=args.apply, writers_stopped=args.writers_stopped)
        print(json.dumps(report, ensure_ascii=False, default=str, indent=2))
        if report["differences"]:
            raise SystemExit(2)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
