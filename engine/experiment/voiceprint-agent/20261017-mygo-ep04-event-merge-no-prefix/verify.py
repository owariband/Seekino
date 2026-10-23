"""Read-only final checks for the completed, explicitly bound EP04 event-merge run."""
from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import statistics

from control import OUT, ROOT, VIDEO, SHA, database, get, rows, services, write

TASK = json.loads((OUT / "reparse-response.json").read_text())["body"]["videoParseTaskId"]
DATA = OUT / TASK


def read(name):
    return json.loads((DATA / name).read_text())


def main():
    module = services()
    settings = module.configure()
    task, chunks, intervals = read("final-task.json"), read("final-chunks.json"), read("final-intervals.json")
    assert task["id"] == TASK and task["video_id"] == VIDEO and task["status"] == "COMPLETE"
    assert task["parse_config"]["compact_enabled"] is False
    assert task["parse_config"]["prefix_context"] is None and task["parse_config"]["models"]["prefix_context"] is None
    assert len(chunks) == 24 and all(c["phase"] == 3 and c["status"] == "COMPLETE" and c["version"] == c["indexed_content_version"] for c in chunks)
    assert len(intervals) == 22 and all(i["status"] == "COMPLETE" and i["retry_count"] == 0 for i in intervals)
    runtime = task["detail"]["_worker_runtime_v2"]
    assert not task["lease_owner"] and not runtime.get("capacity_acquired")
    assert not runtime.get("model_calls", {}).get("active")
    assert all(not row["lease_owner"] for row in chunks + intervals)
    assert runtime["prefix"]["lease_version"] == 0 and runtime["prefix"]["next_chunk_sequence_no"] == 0
    usage = read("final-model-usage.json")
    assert not any(str(row["metadata"].get("context", {}).get("stage", "")).startswith("prefix") for row in usage)
    speaker = [row for row in usage if row["metadata"].get("operation") == "speaker_embedding"]
    durations = sorted(row["metadata"]["ended_at"] - datetime.fromisoformat(row["start_time"]).replace(tzinfo=UTC).timestamp() * 1000 for row in speaker)
    references = task["detail"]["voiceprint"]["voiceprintList"]
    assert task["detail"]["voiceprint"]["ready"] is True
    assert all(0 < len(person["references"]) <= 5 for person in references)
    by_chunk = {chunk["chunk_id"]: chunk for chunk in chunks}
    valid_counts = {}
    for person in references:
        valid_counts[person["speaker"]] = 0
        for ref in person["references"]:
            assert len(ref["vector"]) == 192 and all(math.isfinite(x) for x in ref["vector"])
            assert math.isclose(math.hypot(*ref["vector"]), 1.0, abs_tol=1e-6)
            chunk = by_chunk[ref["chunk_id"]]
            for event in chunk["content"]["timeline"]:
                identity = {key: event.get(key) for key in ["event_type", "speaker", "start_seconds", "end_seconds", "text_variants"]}
                digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                if digest == ref["event_fingerprint"] and event["speaker"] == person["speaker"] and chunk["version"] >= ref["source_version"]:
                    valid_counts[person["speaker"]] += 1
                    break
    target = [event for event in chunks[0]["content"]["timeline"] if event["event_type"] == "DIALOGUE" and event["start_seconds"] < 49000 and event["end_seconds"] > 47000]
    target_correct = len(target) == 1 and target[0]["speaker"] == "长崎爽世"
    from server.storage.artifact import LocalArtifactStore
    store = LocalArtifactStore(os.environ["ANIVAG_ARTIFACT_ROOT"], environment=settings.environment, create_base_directory=False)
    engine = database()
    try:
        with engine.connect() as connection:
            source_uri = rows(connection, "SELECT source_uri FROM video WHERE id=:video", video=VIDEO)[0]["source_uri"]
    finally:
        engine.dispose()
    source_path = store.resolve(source_uri)
    digest = hashlib.sha256()
    with source_path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    assert digest.hexdigest() == SHA
    workspace = store.base_directory / "work/video-parse-v2" / TASK
    remaining = [str(path.relative_to(workspace)) for path in workspace.rglob("*") if path.is_file()] if workspace.exists() else []
    assert not remaining, remaining
    from server.caller.elasticsearch import open_elasticsearch_client
    with open_elasticsearch_client(settings.elasticsearch, request_timeout_seconds=10) as client:
        response = client.search(index=settings.elasticsearch.index_name, query={"term": {"parse_task_id": TASK}}, size=100,
                                 source_excludes=["semantic_vector", "vision_vector", "sparse_vector", "ordered_frames_manifest"], track_total_hits=True)
    docs = [hit["_source"] for hit in response["hits"]["hits"]]
    assert response["hits"]["total"]["value"] == len(docs) == 24
    assert all(doc["video_id"] == VIDEO and doc["content_version"] == by_chunk[doc["video_chunk_id"]]["version"] for doc in docs)
    es_target = [field for doc in docs for field in doc["lexical_fields"] if field["field"] == "dialogue" and field["start_seconds"] < 49000 and field["end_seconds"] > 47000]
    indexed_target_correct = bool(es_target) and {field["speaker"] for field in es_target} == {"长崎爽世"}
    write("es-documents.json", docs)
    source_manifest = json.loads((OUT / "source-sha256.json").read_text())
    assert all(hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == value for name, value in source_manifest["files"].items())
    health = get(f"http://127.0.0.1:{module.port('ANIVAG_HTTP_PORT')}/healthz")
    assert health["status"] == "ok" and health["components"]["parseConsumer"]["status"] == "running"
    result = {
        "captured_at": datetime.now(UTC).isoformat(), "task_id": TASK, "status": task["status"], "prefix_enabled": False,
        "understanding_count": len(chunks), "refine_count": len(intervals), "indexed_count": len(docs),
        "registered_reference_counts": {person["speaker"]: len(person["references"]) for person in references},
        "final_valid_reference_counts": valid_counts, "target": target, "indexed_target": es_target,
        "target_correct": target_correct, "indexed_target_correct": indexed_target_correct,
        "source_sha256": digest.hexdigest(), "source_path": str(source_path), "source_code_unchanged_during_run": True,
        "model_usage_statuses": dict(Counter(row["metadata"].get("status") for row in usage)),
        "model_operations": dict(Counter(row["metadata"].get("operation") for row in usage)),
        "speaker_calls": {"count": len(speaker), "by_stage": dict(Counter(row["metadata"].get("context", {}).get("stage") for row in speaker)),
                          "duration_ms": {"min": min(durations), "median": statistics.median(durations), "p95": durations[math.ceil(len(durations) * 0.95) - 1], "max": max(durations)},
                          "limitation": "Includes initialization, lock waiting and CPU inference; not a pure inference benchmark."},
        "temporary_workspace": str(workspace), "temporary_workspace_exists": workspace.exists(), "remaining_files": remaining,
        "active_leases": 0, "active_model_reservations": 0, "capacity_acquired": False, "health": health,
        "limitations": "One known target; not a global accuracy estimate. Initial reference labels are not all manually verified. Exact discarded Canvas drafts and full Tool receipts are not persisted by the existing diagnostics. Target correctness is reported separately from pipeline completion.",
    }
    write("final-verification.json", result)
    print(json.dumps({key: result[key] for key in ["status", "understanding_count", "refine_count", "indexed_count", "final_valid_reference_counts", "target_correct", "indexed_target_correct", "active_leases", "remaining_files"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
