"""Read-only final checks for EP04 with trusted, static prepared voice references."""
from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import statistics

from control import OUT, ROOT, VIDEO, SHA, database, facts, get, rows, services, write

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
    all_references = []
    for person in references:
        valid_counts[person["speaker"]] = 0
        for ref in person["references"]:
            assert len(ref["vector"]) == 192 and all(math.isfinite(x) for x in ref["vector"])
            assert math.isclose(math.hypot(*ref["vector"]), 1.0, abs_tol=1e-6)
            # Prepared references are static results. Timeline edits are deliberately irrelevant.
            assert ref["chunk_id"] in by_chunk
            assert ref["source_version"] > 0 and ref["start_ms"] < ref["end_ms"]
            assert ref["duration_ms"] == ref["end_ms"] - ref["start_ms"]
            valid_counts[person["speaker"]] += 1
            all_references.append({"speaker": person["speaker"], **ref})
    assert all(left["end_ms"] <= right["start_ms"] or right["end_ms"] <= left["start_ms"]
               for index, left in enumerate(all_references) for right in all_references[index + 1:])
    summary = read("summary.json")
    assert not summary["ready_gate_violations"]
    prepared = sorted((json.loads(path.read_text()) for path in (DATA / "voiceprints").glob("ready-*.json")),
                      key=lambda item: item["captured_at"])
    assert prepared and prepared[0]["voiceprintList"] == references
    original_before = json.loads((OUT / "baseline.json").read_text())["original_series"]
    original_current = json.loads(json.dumps(facts()["original_series"], default=str))
    original_unchanged = original_before == original_current
    write("original-preservation-check.json", {"captured_at": datetime.now(UTC).isoformat(), "unchanged": original_unchanged,
                                               "counts": {key: len(value) for key, value in original_current.items()}})
    assert original_unchanged, "Original series changed during isolated reparse"
    # This known-target acceptance range only evaluates persisted output; it never modifies parsing.
    target_overlap = [event for event in chunks[0]["content"]["timeline"] if event["event_type"] == "DIALOGUE"
                      and event["start_seconds"] < 49000 and event["end_seconds"] > 47000]
    matched_target = [event for event in target_overlap
                      if any("ちゃんも悪くない" in variant["text"] for variant in event["text_variants"])]
    target = matched_target or target_overlap
    speaker_correct = len(matched_target) == 1 and matched_target[0]["speaker"] == "长崎爽世"
    isolated_boundary = (len(matched_target) == 1
                         and 46000 <= matched_target[0]["start_seconds"] < matched_target[0]["end_seconds"] <= 49000)
    target_correct = speaker_correct and isolated_boundary
    initial_chunks = {}
    for path in (DATA / "chunks").glob("*.json"):
        item = json.loads(path.read_text())
        previous = initial_chunks.get(item["chunk_id"])
        if previous is None or item["captured_at"] < previous["captured_at"]:
            initial_chunks[item["chunk_id"]] = item

    def attribution_counts(values):
        dialogues = [event for chunk in values for event in chunk["content"]["timeline"] if event["event_type"] == "DIALOGUE"]
        asr = [item for chunk in values for item in chunk.get("detail", {}).get("evidence", {}).get("asr", {}).get("transcript", [])]
        return {"chunks": len(values), "dialogue_count": len(dialogues), "dialogue_speaker_null": sum(item.get("speaker") is None for item in dialogues),
                "asr_segments": len(asr), "asr_speaker_id_null": sum(item.get("speaker_id") is None for item in asr)}
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
    es_target = [field for doc in docs for field in doc["lexical_fields"] if field["field"] == "dialogue"
                 and any(field["start_seconds"] == event["start_seconds"] and field["end_seconds"] == event["end_seconds"]
                         for event in target)]
    indexed_target_speaker_correct = bool(es_target) and {field["speaker"] for field in es_target} == {"长崎爽世"}
    indexed_target_correct = target_correct and indexed_target_speaker_correct
    write("es-documents.json", docs)
    source_manifest = json.loads((OUT / "source-sha256.json").read_text())
    assert all(hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == value for name, value in source_manifest["files"].items())
    health = get(f"http://127.0.0.1:{module.port('ANIVAG_HTTP_PORT')}/healthz")
    assert health["status"] == "ok" and health["components"]["parseConsumer"]["status"] == "running"
    result = {
        "captured_at": datetime.now(UTC).isoformat(), "task_id": TASK, "status": task["status"], "prefix_enabled": False,
        "understanding_count": len(chunks), "refine_count": len(intervals), "indexed_count": len(docs),
        "registered_reference_counts": {person["speaker"]: len(person["references"]) for person in references},
        "final_valid_reference_counts": valid_counts, "reference_contract": "Prepared source labels and ranges are trusted static results; no timeline fingerprint revalidation.",
        "references_unchanged_after_ready": True, "ready_gate_violations": [], "original_series_unchanged": original_unchanged,
        "target": target, "indexed_target": es_target,
        "target_match_found": bool(matched_target), "target_speaker_correct": speaker_correct,
        "target_isolated_boundary": isolated_boundary, "target_overlap_diagnostics": target_overlap,
        "indexed_target_speaker_correct": indexed_target_speaker_correct,
        "target_correct": target_correct, "indexed_target_correct": indexed_target_correct,
        "speaker_attribution": {"first_observed_understanding": attribution_counts(list(initial_chunks.values())),
                                "final": attribution_counts(chunks),
                                "target_history": read("target-history.json"),
                                "limitation": "First observed snapshots may miss intermediate changes. Null counts measure abstention, not identification accuracy."},
        "source_sha256": digest.hexdigest(), "source_path": str(source_path), "source_code_unchanged_during_run": True,
        "model_usage_statuses": dict(Counter(row["metadata"].get("status") for row in usage)),
        "model_operations": dict(Counter(row["metadata"].get("operation") for row in usage)),
        "speaker_calls": {"count": len(speaker), "by_stage": dict(Counter(row["metadata"].get("context", {}).get("stage") for row in speaker)),
                          "duration_ms": {"min": min(durations) if durations else None,
                                          "median": statistics.median(durations) if durations else None,
                                          "p95": durations[math.ceil(len(durations) * 0.95) - 1] if durations else None,
                                          "max": max(durations) if durations else None},
                          "limitation": "Includes initialization, lock waiting and CPU inference; not a pure inference benchmark."},
        "temporary_workspace": str(workspace), "temporary_workspace_exists": workspace.exists(), "remaining_files": remaining,
        "active_leases": 0, "active_model_reservations": 0, "capacity_acquired": False, "health": health,
        "limitations": "One known target; not a global accuracy estimate. Initial reference labels are not all manually verified. Exact discarded Canvas drafts and full Tool receipts are not persisted by the existing diagnostics. Target correctness is reported separately from pipeline completion. No mutable timeline fingerprint is used to invalidate prepared references.",
    }
    write("final-verification.json", result)
    print(json.dumps({key: result[key] for key in ["status", "understanding_count", "refine_count", "indexed_count", "final_valid_reference_counts", "target_correct", "indexed_target_correct", "active_leases", "remaining_files"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
