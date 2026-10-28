"""In-place Parse V2 task migration; dry-run by default, never writes Elasticsearch.

Use ``python -m scripts.migrate_video_parse_chunk_tasks --help``. Historical
index targets must come from an audited per-task manifest, not live defaults::

  {"task-id": {"generation": 1, "sparse_enabled": false,
    "contract_fingerprint": "<64 hex digits>", "evidence": "deployment/config backup"}}

The same manifest may include an upload ID without an initial task, with the
same index target plus all six stage execution fields under ``execution`` and
``source_config_digest`` matching the original V2 upload snapshot. Its evidence
must identify the audited historical configuration; live defaults are not evidence.

Stop old Parse and Upload writers and back up MySQL before --apply --writers-stopped. Run the
same command again to verify idempotence, then start only the new workers.
DDL is resumable (MySQL implicitly commits ALTER); row conversion and its audit
marker commit together. Existing IDs, content bytes, and membership never change.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
from typing import Callable, Mapping

from sqlalchemy import CheckConstraint, MetaData, Table, inspect, select, text
from sqlalchemy.schema import CreateColumn, CreateIndex

from agent.video_parse_v2.input import ParseConfig
from agent.video_parse_v2.scheduler import chunk_index_ready, summarize_video_task
from engine.common.config import VideoParseRuntimeConfig
from engine.parsev2.schemas import VideoParseResult
from engine.parsev2.planning import interval_size, plan_chunks, plan_intervals
from engine.parsev2.validation import ValidatedIntervalRefinement
from server.config.parse_profile import ParseConfig as ServerParseConfig
from server.database import _normalize_task_check, _schema_structure_differences, create_database_engine, DatabaseEnvironment
from server.constants.parse import CAPACITY_ACQUIRED_KEY, MAX_INDEX_GENERATION, PREPARE_FAILURES_KEY, RUNTIME_DETAIL_KEY
from server.model.job import ChunkTask, ChunkTaskPhase, IntervalParseTask, VideoParseTask
from server.model.upload import UploadSession
from server.utils.retry import task_retry_delay

MARKER = "_chunk_task_migration_20261014"
MODELS = (VideoParseTask.__table__, ChunkTask.__table__, IntervalParseTask.__table__, UploadSession.__table__)
ADDED = {
    "video_parse_task": set(),
    "chunk_parse_task": {"phase", "indexed_content_version"},
    "interval_parse_task": set(),
    "upload_session": set(),
}
RETIRED_COLUMNS = {
    "video_parse_task": {"scheduler_slot", "prepare_retry_count", "target_index_generation"},
    "chunk_parse_task": {"indexed_generation", "execution_deadline"},
    "interval_parse_task": {"execution_deadline"},
    "upload_session": {"video_parse_config"},
}
TABLES = ("video_parse_task", "chunk_parse_task", "chunk_parse_content",
          "interval_parse_task", "interval_parse_content", "video_chunk", "video", "series",
          "upload_session", "upload_segment")
STAGE_EXECUTION_FIELDS = (
    "parse_concurrency", "refine_concurrency", "index_concurrency",
    "parse_execution_timeout_seconds", "refine_execution_timeout_seconds",
    "index_execution_timeout_seconds",
)


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
        "upload_session": [{key: value for key, value in row.items()
                            if key not in {"parse_config", "video_parse_config"}}
                           for row in rows["upload_session"]],
        "upload_segment": rows["upload_segment"],
    }


def _utc(value):
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


def _released(row: dict) -> dict:
    return {"lease_owner": None, "lease_expire_time": None,
            "lease_version": row["lease_version"] + int(row.get("lease_owner") is not None)}


def _snapshot(task: dict, target: dict | None) -> tuple[dict, int, dict]:
    config = deepcopy(task["parse_config"])
    old_execution = deepcopy(config.get("execution", {}))
    defaults = VideoParseRuntimeConfig()
    execution = config.setdefault("execution", {})
    for name in STAGE_EXECUTION_FIELDS:
        execution.setdefault(name, getattr(defaults, name))
    execution.pop("max_concurrent_model_calls", None)
    indexing = config.get("indexing")
    if not indexing:
        if not target or not str(target.get("evidence", "")).strip():
            raise ValueError("historical index contract requires an audited task-target manifest")
        indexing = {key: target[key] for key in ("sparse_enabled", "contract_fingerprint")}
    if not isinstance(indexing, dict):
        raise ValueError("invalid frozen indexing configuration")
    generations = [value for value in (
        indexing.get("index_generation"), task.get("target_index_generation"),
        target.get("generation") if target else None,
    ) if value is not None]
    if not generations:
        raise ValueError("historical index generation requires an audited task-target manifest")
    if any(type(value) is not int or not 0 < value <= MAX_INDEX_GENERATION for value in generations):
        raise ValueError("invalid target generation")
    if len(set(generations)) != 1:
        raise ValueError("index generation differs between frozen config, retired column, or manifest")
    generation = generations[0]
    if (indexing.get("index_generation") is None and task.get("target_index_generation") is None
            and not str(target.get("evidence", "")).strip()):
        raise ValueError("historical index generation requires an audited task-target manifest")
    config["indexing"] = {**indexing, "index_generation": generation}
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
    task_configs = {}
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
        task_configs[task_id] = (task, config)
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
        detail = deepcopy(task.get("detail") or {})
        runtime = deepcopy(detail.get(RUNTIME_DETAIL_KEY, {}))
        if not isinstance(runtime, dict):
            local.append("invalid parent runtime detail")
            runtime = {}
        prepare_failures = runtime.get(PREPARE_FAILURES_KEY)
        legacy_prepare_failures = task.get("prepare_retry_count")
        for count in (prepare_failures, legacy_prepare_failures):
            if count is not None and (type(count) is not int or count < 0):
                local.append("invalid Prepare failure budget")
        if (prepare_failures is not None and legacy_prepare_failures is not None
                and prepare_failures != legacy_prepare_failures):
            local.append("Prepare failure budget differs between detail and retired column")
        if prepare_failures is None:
            prepare_failures = legacy_prepare_failures if legacy_prepare_failures is not None else 0
        runtime[PREPARE_FAILURES_KEY] = prepare_failures
        runtime.pop(CAPACITY_ACQUIRED_KEY, None)
        detail[RUNTIME_DETAIL_KEY] = runtime
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
            elif already_migrated and row is not task and row.get("lease_owner"):
                local.append(f"migrated execution lease must be reclaimed before schema retirement: {row['id']}")
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
                    stored = refinements[interval["id"]]["content"]
                    # The persisted model_dump includes null defaults for unchanged fields.
                    # Read them as omissions without changing the stored audit content.
                    if isinstance(stored, Mapping) and isinstance(stored.get("updates"), list):
                        stored = {**stored, "updates": [
                            {key: value for key, value in update.items()
                             if key not in {"persons", "timeline"} or value is not None}
                            if isinstance(update, Mapping) else update
                            for update in stored["updates"]
                        ]}
                    result = ValidatedIntervalRefinement.model_validate(stored)
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
                allowed = {(ChunkTaskPhase.PARSE, state) for state in ("QUEUED", "RUNNING", "FAILED")} | {
                    (ChunkTaskPhase.REFINE, "WAITING")} | {
                    (ChunkTaskPhase.INDEX, state) for state in ("QUEUED", "RUNNING", "COMPLETE", "FAILED")}
                if phase_status not in allowed:
                    local.append(f"invalid migrated phase/status: {child_id}")
                if child.get("phase") == ChunkTaskPhase.PARSE and child_id in valid_content:
                    local.append(f"migrated Parse would repeat committed understanding: {child_id}")
                if child.get("phase") in (ChunkTaskPhase.REFINE, ChunkTaskPhase.INDEX) and child_id not in valid_content:
                    local.append(f"migrated later phase has no understanding: {child_id}")
                if child.get("phase") == ChunkTaskPhase.INDEX and not chunk_index_ready(child_id in valid_content, coverage[child_id]):
                    local.append(f"migrated Index lacks complete Refine coverage: {child_id}")
                if phase_status == (ChunkTaskPhase.INDEX, "COMPLETE") and child_id in valid_content:
                    content = contents[child_id]
                    if (child.get("indexed_content_version") != content["version"]
                            or "indexed_generation" in child and child["indexed_generation"] != generation):
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
            update = {**_released(child), "indexed_content_version": None}
            if child_id not in valid_content:
                update.update(phase=ChunkTaskPhase.PARSE, status="FAILED" if child["status"] == "FAILED" else "QUEUED")
                if child.get("lease_owner") and update["status"] != "FAILED":
                    _recover(update, child, config, now, "FAILED")
            elif not chunk_index_ready(True, coverage[child_id]):
                if not coverage[child_id]:
                    local.append(f"understood Chunk has no planned covering Interval: {child_id}")
                update.update(phase=ChunkTaskPhase.REFINE, status="WAITING")
            else:
                sources = index_attempts[child_id]
                count = max((source["retry_count"] + int(source.get("lease_owner") is not None)
                             for source in sources), default=0)
                update.update(phase=ChunkTaskPhase.INDEX, status="QUEUED", retry_count=count)
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
                        update.update(status="COMPLETE", indexed_content_version=content["version"])
                    elif child_id in failed_index:
                        update.update(status="FAILED")
            converted_children.append({**child, **update})
            changes["chunk_parse_task"][child_id] = update
        # Already converted tasks retain their budgets and original audit marker.
        # Only retire the previous parent capacity/budget storage if necessary.
        if already_migrated:
            if task["status"] == "COMPLETE" and (
                not topology_complete or runtime.get("failure_status")
                or any(child.get("phase") != ChunkTaskPhase.INDEX or child["status"] != "COMPLETE" for child in children)
            ):
                local.append("migrated parent COMPLETE differs from persisted child/failure state")
            for child in children:
                changes["chunk_parse_task"].pop(child["id"], None)
                if child.get("phase") not in tuple(ChunkTaskPhase):
                    local.append(f"migration marker without phase: {child['id']}")
            for interval in intervals:
                changes["interval_parse_task"].pop(interval["id"], None)
            parent_update = {}
            if detail != (task.get("detail") or {}):
                parent_update["detail"] = detail
            if config != task["parse_config"]:
                parent_update["parse_config"] = config
            if parent_update:
                changes["video_parse_task"][task_id] = parent_update
            differences.extend(f"{task_id}: {item}" for item in local)
            audit.append({"task_id": task_id, "already_migrated": True,
                          PREPARE_FAILURES_KEY: prepare_failures})
            continue
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
                           or any(c["phase"] == ChunkTaskPhase.PARSE and c["status"] == "FAILED" for c in converted_children)
                           or any(i["status"] == "PARSING_FAILED" for i in converted_intervals),
            indexing_failed=task["status"] == "INDEXING_FAILED" or runtime.get("failure_status") == "INDEXING_FAILED"
                            or any(c["phase"] == ChunkTaskPhase.INDEX and c["status"] == "FAILED" for c in converted_children),
        )
        if not topology_complete and not status.endswith("FAILED"):
            status = "PENDING" if not children and task["status"] == "PENDING" else "PREPARING"
        if status.endswith("FAILED"):
            detail[RUNTIME_DETAIL_KEY]["failure_status"] = status
        changes["video_parse_task"][task_id] = {
            **_released(task), "heartbeat_time": None,
            "parse_config": config, "status": status, "detail": detail,
            "finish_time": task.get("finish_time") or now if status in {"COMPLETE", "PARSING_FAILED", "INDEXING_FAILED"} else None,
        }
        differences.extend(f"{task_id}: {item}" for item in local)
        audit.append({"task_id": task_id, "from": task["status"], "to": status,
                      PREPARE_FAILURES_KEY: prepare_failures,
                      "chunk_states": dict(Counter(f"{c['phase']}/{c['status']}" for c in converted_children))})
    upload_changes, upload_differences, uploads = _plan_uploads(rows["upload_session"], task_configs, targets)
    changes["upload_session"] = upload_changes
    differences.extend(upload_differences)
    return {"changes": changes, "differences": differences, "tasks": audit, "uploads": uploads}


def _upload_snapshot(config: dict, initial_config: dict | None, target: dict | None) -> dict:
    config = deepcopy(config)
    execution = config.get("execution")
    if not isinstance(execution, dict):
        raise ValueError("invalid Upload execution configuration")
    frozen = initial_config
    if frozen is None and ("indexing" not in config or any(name not in execution for name in STAGE_EXECUTION_FIELDS)):
        if (not target or not str(target.get("evidence", "")).strip()
                or target.get("source_config_digest") != _json_hash(config)):
            raise ValueError("historical Upload configuration requires an audited snapshot manifest")
        generation = target["generation"]
        if type(generation) is not int or not 0 < generation <= MAX_INDEX_GENERATION:
            raise ValueError("invalid Upload target generation")
        frozen = {"execution": target["execution"], "indexing": {
            "index_generation": generation,
            **{key: target[key] for key in ("sparse_enabled", "contract_fingerprint")},
        }}
    if frozen is not None:
        for name in STAGE_EXECUTION_FIELDS:
            execution.setdefault(name, frozen["execution"][name])
        if "indexing" not in config:
            if initial_config is not None and (
                config["models"]["embedding"] != initial_config["models"]["embedding"]
                or config["visual_input"]["max_height"] != initial_config["visual_input"]["max_height"]
            ):
                raise ValueError("Upload index inputs differ from its initial task")
            config["indexing"] = deepcopy(frozen["indexing"])
    execution.pop("max_concurrent_model_calls", None)
    ParseConfig.model_validate(config)
    return config


def _plan_uploads(uploads: list[dict], task_configs: dict, targets: Mapping[str, dict]) -> tuple[dict, list[str], list[dict]]:
    changes, differences, audit = {}, [], []
    for upload in uploads:
        try:
            if upload["status"] == "COMPLETING" and upload.get("completion_token"):
                raise ValueError("Upload completion must be cancelled/drained before migration")
            initial_config = None
            initial_task_id = upload.get("initial_task_id")
            if initial_task_id is not None:
                if initial_task_id not in task_configs:
                    raise ValueError("Upload initial task has no verified configuration")
                task, initial_config = task_configs[initial_task_id]
                if upload.get("video_id") != task["video_id"]:
                    raise ValueError("Upload initial task belongs to another Video")
            legacy = upload.get("video_parse_config")
            stored = upload.get("parse_config")
            source = "video_parse_config" if legacy is not None else "parse_config"
            config = deepcopy(legacy if legacy is not None else stored)
            if not isinstance(config, dict):
                raise ValueError("missing effective Upload parse configuration")
            if legacy is not None or "models" in config:
                config = _upload_snapshot(config, initial_config, targets.get(upload["id"]))
            else:
                config = ServerParseConfig.model_validate(config).to_video_parse_v2().model_dump(mode="json")
                source = "legacy_server_profile"
            if legacy is not None and isinstance(stored, dict) and "models" in stored:
                stored_v2 = _upload_snapshot(stored, initial_config, targets.get(upload["id"]))
                old_generation = config["indexing"].get("index_generation")
                stored_generation = stored_v2["indexing"].get("index_generation")
                if old_generation is not None and stored_generation is not None and old_generation != stored_generation:
                    raise ValueError("Upload V2 configurations have conflicting generations")
                old_comparison, stored_comparison = deepcopy(config), deepcopy(stored_v2)
                old_comparison["indexing"].pop("index_generation", None)
                stored_comparison["indexing"].pop("index_generation", None)
                if old_comparison != stored_comparison:
                    raise ValueError("Upload has conflicting V2 configurations")
                if stored_generation is not None:
                    config["indexing"]["index_generation"] = stored_generation
            generation = config["indexing"].get("index_generation")
            if generation is not None and generation > MAX_INDEX_GENERATION:
                raise ValueError("Upload generation exceeds the index version contract")
            if initial_config is not None:
                generation = initial_config["indexing"]["index_generation"]
                if config["indexing"].get("index_generation") not in (None, generation):
                    raise ValueError("Upload generation differs from its initial task")
                config["indexing"]["index_generation"] = generation
            if config != stored:
                changes[upload["id"]] = {"parse_config": config}
            audit.append({"upload_session_id": upload["id"], "source": source,
                          "config_digest": _json_hash(config)})
        except (ValueError, KeyError, TypeError) as exc:
            differences.append(f"upload {upload['id']}: {exc}")
    return changes, differences, audit


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
        update["next_execute_time"] = now + task_retry_delay(
            execution["retry_initial_delay_seconds"], execution["retry_max_delay_seconds"], count,
        )


def _schema(connection, *, finalize: bool, apply: bool) -> list[str]:
    """Alter the known delta, retiring obsolete columns after their data is copied."""
    statements = []
    inspector = inspect(connection)
    for table in MODELS:
        name = table.name
        columns = {c["name"]: c for c in inspector.get_columns(name)}
        missing = set(table.c.keys()) - columns.keys()
        extra = columns.keys() - set(table.c.keys())
        retired = extra & RETIRED_COLUMNS[name]
        if missing - ADDED[name] or extra - retired:
            raise ValueError(f"{name}: unsupported schema differences {sorted((missing - ADDED[name]) | (extra - retired))}")
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
            expected_checks = {constraint.name for constraint in table.constraints
                               if isinstance(constraint, CheckConstraint)}
            for constraint in inspector.get_check_constraints(name):
                if (constraint["name"] not in expected_checks
                        and any(column in constraint["sqltext"] for column in retired)):
                    statements.append(f"ALTER TABLE `{name}` DROP CHECK `{constraint['name']}`")
            for index in inspector.get_indexes(name):
                if set(index["column_names"]) & retired:
                    statements.append(f"ALTER TABLE `{name}` DROP INDEX `{index['name']}`")
            for column_name in sorted(retired):
                statements.append(f"ALTER TABLE `{name}` DROP COLUMN `{column_name}`")
            for column_name in sorted(ADDED[name]):
                column = table.c[column_name]
                if not column.nullable and (column_name not in columns or columns[column_name]["nullable"]):
                    statements.append(f"ALTER TABLE `{name}` MODIFY COLUMN {CreateColumn(column).compile(dialect=connection.dialect)}")
            actual_checks = {c["name"]: c["sqltext"] for c in inspector.get_check_constraints(name)}
            for constraint in sorted(table.constraints, key=lambda c: str(c.name)):
                if isinstance(constraint, CheckConstraint) and (
                    constraint.name not in actual_checks
                    or _normalize_task_check(actual_checks[constraint.name]) != _normalize_task_check(str(constraint.sqltext))
                ):
                    statements.append(f"ALTER TABLE `{name}` ADD CONSTRAINT `{constraint.name}` CHECK ({constraint.sqltext})")
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
        raise ValueError("apply requires --writers-stopped after old Parse and Upload writers have drained")
    now = now or datetime.now(UTC)
    with engine.connect() as connection:
        locked = False
        try:
            if apply:
                locked = connection.scalar(text("SELECT GET_LOCK('seekino_chunk_task_migration', 0)")) == 1
                if not locked:
                    raise RuntimeError("another task migration is running")
                connection.commit()
            tables, rows = _inventory(connection)
            plan = plan_rows(rows, targets=targets or {}, published=published, now=now)
            ddl = _schema(connection, finalize=False, apply=False)
            preserved = _json_hash(_preserved_facts(rows))
            report = {"apply": apply, "differences": plan["differences"], "tasks": plan["tasks"],
                      "uploads": plan["uploads"],
                      "counts": {name: len(value) for name, value in rows.items()},
                      "updates": {name: len(value) for name, value in plan["changes"].items()},
                      "schemaPreparation": ddl,
                      "schemaFinalization": _schema(connection, finalize=True, apply=False),
                      "preservedContentDigest": preserved}
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
                for name in ("interval_parse_task", "chunk_parse_task", "video_parse_task", "upload_session"):
                    for row_id, values in plan["changes"][name].items():
                        connection.execute(tables[name].update().where(tables[name].c.id == row_id).values(**values))
            report["schemaFinalization"] = _schema(connection, finalize=True, apply=True)
            schema_differences = _schema_structure_differences(inspect(connection), set(TABLES), MODELS)
            if schema_differences:
                raise RuntimeError(f"final task schema differs: {sorted(schema_differences)}")
            _, after = _inventory(connection)
            if preserved != _json_hash(_preserved_facts(after)):
                raise RuntimeError("preserved content verification failed")
            expected_uploads = {item["upload_session_id"]: item["config_digest"] for item in plan["uploads"]}
            actual_uploads = {row["id"]: _json_hash(row["parse_config"]) for row in after["upload_session"]}
            if actual_uploads != expected_uploads:
                raise RuntimeError("Upload configuration verification failed")
            report["applied"] = True
            return report
        finally:
            if locked:
                connection.execute(text("SELECT RELEASE_LOCK('seekino_chunk_task_migration')"))
                connection.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.getenv("SEEKINO_DATABASE_URL"), required=not os.getenv("SEEKINO_DATABASE_URL"))
    parser.add_argument("--task-targets", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--writers-stopped", action="store_true")
    parser.add_argument("--environment", choices=[v.value for v in DatabaseEnvironment], required=True)
    parser.add_argument("--elasticsearch-url", default=os.getenv("SEEKINO_ELASTICSEARCH_URL"))
    parser.add_argument("--elasticsearch-index", default=os.getenv("SEEKINO_ELASTICSEARCH_INDEX"))
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
                                         api_key=os.getenv("SEEKINO_ELASTICSEARCH_API_KEY") or None)
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
