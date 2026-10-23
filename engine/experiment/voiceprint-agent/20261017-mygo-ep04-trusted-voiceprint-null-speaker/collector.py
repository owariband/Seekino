"""SELECT-only collector for the explicitly authorized isolated EP04 reparse."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import time
from uuid import UUID

from sqlalchemy import create_engine, event, text

ROOT = Path(__file__).resolve().parents[4]
VIDEO = "0019c16c-a7b7-5f6c-8275-5019869c1207"
SERIES = "bdbf28ae-5404-58e4-8c98-699433499ffc"
SOURCE_SHA = "50e981e49a3d74e8c0aca7571ffae8265b88f6388f1d34849876b61ad8bf5814"
RUNTIME = "_worker_runtime_v2"


def now():
    return datetime.now(UTC).isoformat()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def rows(connection, sql, task_id):
    result = [dict(row) for row in connection.execute(text(sql), {"task": task_id}).mappings()]
    for row in result:
        for key in ("detail", "parse_config", "content", "usage", "metadata", "tokens", "price_snapshot", "last_error", "chunk_task_ids"):
            if isinstance(row.get(key), str):
                row[key] = json.loads(row[key])
    return result


def collect(connection, task_id):
    task = rows(connection, "SELECT p.*,v.series_id,v.episode_order,v.hash AS source_sha256 FROM video_parse_task p JOIN video v ON v.id=p.video_id WHERE p.id=:task", task_id)
    if len(task) != 1:
        raise ValueError("The exact requested task does not exist")
    task = task[0]
    if (task["video_id"], task["series_id"], task["episode_order"], task["source_sha256"]) != (VIDEO, SERIES, 4, SOURCE_SHA) or task["is_deleted"] or task["version"] <= 3:
        raise ValueError("Task is outside the authorized isolated EP04 reparse")
    chunks = rows(connection, """SELECT t.id AS chunk_task_id,t.chunk_id,t.phase,t.status,t.retry_count,
        t.lease_owner,t.lease_version,t.start_time,t.finish_time,t.last_error,t.indexed_content_version,
        v.sequence_no,v.start_ms,v.end_ms,c.id AS content_id,c.version,c.content,
        JSON_REMOVE(c.detail,'$._index_embeddings_v2') AS detail,c.create_time AS content_create_time,c.update_time AS content_update_time
        FROM chunk_parse_task t JOIN video_chunk v ON v.id=t.chunk_id
        LEFT JOIN chunk_parse_content c ON c.chunk_task_id=t.id WHERE t.video_task_id=:task ORDER BY v.sequence_no""", task_id)
    intervals = rows(connection, """SELECT i.*,c.content,c.detail,c.create_time AS content_create_time,c.update_time AS content_update_time
        FROM interval_parse_task i LEFT JOIN interval_parse_content c ON c.interval_task_id=i.id
        WHERE i.video_task_id=:task ORDER BY i.start_sequence_no""", task_id)
    return task, chunks, intervals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", help="Defaults to the local reparse-response.json task")
    parser.add_argument("--max-seconds", type=float, default=5400)
    parser.add_argument("--poll-seconds", type=float, default=0.5)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    bound_task = json.loads((Path(__file__).resolve().parent / "reparse-response.json").read_text())["body"]["videoParseTaskId"]
    if args.task_id is not None and args.task_id != bound_task:
        parser.error("task-id must match this experiment's reparse response")
    args.task_id = bound_task
    UUID(args.task_id)
    if args.max_seconds <= 0 or args.poll_seconds < 0.2:
        parser.error("max-seconds must be positive and poll-seconds must be at least 0.2")
    url = next(line.split("=", 1)[1].strip().strip("\"'") for line in (ROOT / ".env.local").read_text().splitlines() if line.startswith("ANIVAG_DATABASE_URL="))
    engine = create_engine(url, connect_args={"connect_timeout": 5, "read_timeout": 15, "write_timeout": 5}, pool_pre_ping=True, hide_parameters=True)

    @event.listens_for(engine, "connect")
    def utc(connection, _record):
        with connection.cursor() as cursor:
            cursor.execute("SET time_zone = '+00:00'")

    @event.listens_for(engine, "before_cursor_execute")
    def readonly(_connection, _cursor, statement, _parameters, _context, _many):
        if not statement.lstrip().upper().startswith("SELECT"):
            raise ValueError("Collector permits SELECT statements only")

    output = args.output_dir.resolve() if args.output_dir is not None else Path(__file__).resolve().parent / args.task_id
    previous, violations, target_paths, progress = {}, [], [], {}
    started, deadline = now(), time.monotonic() + args.max_seconds
    terminal_at, ready_at, reason, final, last_print = None, None, "max_seconds", None, 0.0
    next_usage, next_logs = 0.0, 0.0
    logs, conflicts = set(), set()

    def snapshot(kind, key, payload):
        fingerprint = digest(payload)
        if previous.get((kind, key)) == fingerprint:
            return None
        previous[kind, key] = fingerprint
        path = output / kind / f"{key}-{fingerprint[:16]}.json"
        if not path.exists():
            write(path, {"captured_at": now(), "task_id": args.task_id, "payload_sha256": fingerprint, **payload})
        return str(path.relative_to(output))

    def capture_logs(intervals):
        identities = {item["id"] for item in intervals}
        for path in (ROOT / "outputs/server/logs/development").glob("*.log*"):
            if not path.is_file():
                continue
            for line in path.read_text(errors="replace").splitlines():
                if f"task={args.task_id} " in line and "event=voiceprint_" in line:
                    logs.add(line)
                if ("event=refine_commit_conflict " in line or "Refine snapshot changed; refreshing interval " in line) and (
                    f"task={args.task_id} " in line or any(identity in line for identity in identities)
                ):
                    conflicts.add(line)
        output.mkdir(parents=True, exist_ok=True)
        (output / "voiceprint-events.log").write_text("\n".join(sorted(logs)) + "\n")
        (output / "refine-conflicts.log").write_text("\n".join(sorted(conflicts)) + "\n")

    try:
        while time.monotonic() < deadline:
            with engine.connect() as connection:
                task, chunks, intervals = collect(connection, args.task_id)
            final = task, chunks, intervals
            snapshot("tasks", task["status"], task)
            runtime = task["detail"].get(RUNTIME, {})
            voice = task["detail"].get("voiceprint", {"ready": False, "voiceprintList": []})
            phase, prefix = runtime.get("voiceprint_preparation", {}), runtime.get("prefix", {})
            current = time.monotonic()
            if current >= next_usage:
                with engine.connect() as connection:
                    usage = rows(connection, "SELECT * FROM model_usage WHERE task_type='video_parse' AND run_id=:task ORDER BY start_time,id", args.task_id)
                    calls = rows(connection, "SELECT * FROM parse_model_call WHERE video_task_id=:task ORDER BY start_time,id", args.task_id)
                snapshot("model-usage", "usage", {"rows": usage})
                snapshot("model-calls", "calls", {"rows": calls})
                write(output / "latest-model-usage.json", usage)
                write(output / "latest-model-calls.json", calls)
                next_usage = current + 10
            if current >= next_logs:
                capture_logs(intervals)
                next_logs = current + 10
            if previous.get(("latest", "voice")) != digest(voice):
                write(output / "voiceprint.json", {"captured_at": now(), "task_id": args.task_id, **voice})
                snapshot("voiceprints", "ready" if voice.get("ready") else "preparing", voice)
                previous["latest", "voice"] = digest(voice)
            for chunk in chunks:
                if chunk["content"] is None:
                    continue
                path = snapshot("chunks", f"seq{chunk['sequence_no']:03d}-v{chunk['version']}", chunk)
                if path and chunk["sequence_no"] == 0:
                    target = [item for item in chunk["content"].get("timeline", []) if item.get("event_type") == "DIALOGUE" and item["start_seconds"] < 55000 and item["end_seconds"] > 35000]
                    asr = chunk.get("detail", {}).get("evidence", {}).get("asr", {}).get("transcript", [])
                    target_asr = [item for item in asr if item["start_seconds"] < 55000 and item["end_seconds"] > 35000]
                    target_paths.append({"captured_at": now(), "snapshot": path, "version": chunk["version"], "events": target,
                                         "asr": target_asr})
                    write(output / "target-history.json", target_paths)
            for interval in intervals:
                snapshot("intervals", f"seq{interval['start_sequence_no']:03d}-{interval['status']}", interval)
            source_key = encoded([phase.get("lease_version"), phase.get("source_chunk_ids")])
            if phase.get("source_chunk_ids") and previous.get(("source", "execution")) != source_key:
                selected = [chunk for chunk in chunks if chunk["chunk_id"] in phase["source_chunk_ids"]]
                snapshot("sources", f"lease{phase.get('lease_version')}", {"phase": phase, "chunks": selected})
                previous["source", "execution"] = source_key
            if voice.get("ready") and ready_at is None:
                ready_at = now()
            premature = [item["id"] for item in intervals if not voice.get("ready") and (item["lease_version"] > 0 or item["content"] is not None)]
            if premature:
                violations.append({"captured_at": now(), "interval_ids": premature})
                write(output / "ready-gate-violations.json", violations)
            published = sum(item["phase"] == 3 and item["status"] == "COMPLETE" and item["version"] == item["indexed_content_version"] for item in chunks)
            exhausted = prefix.get("retry_count", 0) > task["parse_config"]["execution"]["max_retry_count"]
            prefix_done = not task["parse_config"].get("compact_enabled") or prefix.get("next_chunk_sequence_no", 0) >= task["chunk_count"]
            drained = not (prefix.get("lease_owner") or runtime.get("model_calls", {}).get("active") or any(item["lease_owner"] for item in chunks + intervals))
            progress = {"at": now(), "status": task["status"], "understanding": sum(item["content"] is not None for item in chunks), "ready": voice.get("ready"), "references": sum(len(item["references"]) for item in voice.get("voiceprintList", [])), "speaker_list_size": len(phase.get("speaker_list", [])), "voiceprint_active": phase.get("active", False), "voiceprint_failures": phase.get("failures", 0), "interval_status": dict(Counter(item["status"] for item in intervals)), "refine_committed": sum(item["content"] is not None for item in intervals), "published_current": published, "prefix_cursor": prefix.get("next_chunk_sequence_no", 0), "prefix_done": prefix_done, "prefix_exhausted": exhausted, "drained": drained, "premature_claims": premature}
            output.mkdir(parents=True, exist_ok=True)
            with (output / "progress.jsonl").open("a") as stream:
                stream.write(encoded(progress) + "\n")
            signature = digest({key: value for key, value in progress.items() if key != "at"})
            if signature != previous.get(("progress", "printed")) or time.monotonic() - last_print >= 60:
                print(encoded(progress), flush=True)
                previous["progress", "printed"], last_print = signature, time.monotonic()
            if task["status"] in {"COMPLETE", "PARSING_FAILED", "INDEXING_FAILED"}:
                terminal_at = terminal_at or time.monotonic()
                if drained and (task["status"] == "PARSING_FAILED" or (prefix_done or exhausted) and (published == task["chunk_count"] or task["status"] == "INDEXING_FAILED")):
                    reason = "failed_and_drained" if task["status"] != "COMPLETE" else "prefix_budget_exhausted" if exhausted else "complete_and_prefix_done"
                    break
            if terminal_at and time.monotonic() - terminal_at >= 900:
                reason = "terminal_observation_timeout"
                break
            time.sleep(min(args.poll_seconds, max(0, deadline - time.monotonic())))
    except KeyboardInterrupt:
        reason = "interrupted"
    except Exception as error:
        reason = "collector_error:" + type(error).__name__
        print(reason, flush=True)  # Do not expose connection details or DB parameters.
    finally:
        if final is not None:
            task, chunks, intervals = final
            write(output / "final-task.json", task)
            write(output / "final-chunks.json", chunks)
            write(output / "final-intervals.json", intervals)
            try:
                with engine.connect() as connection:
                    write(output / "final-model-usage.json", rows(connection, "SELECT * FROM model_usage WHERE task_type='video_parse' AND run_id=:task ORDER BY start_time,id", args.task_id))
                    write(output / "final-model-calls.json", rows(connection, "SELECT * FROM parse_model_call WHERE video_task_id=:task ORDER BY start_time,id", args.task_id))
            except Exception as error:
                reason += ";usage_export_error:" + type(error).__name__
            capture_logs(intervals)
            write(output / "summary.json", {"task_id": args.task_id, "video_id": VIDEO, "series_id": SERIES, "started_at": started, "finished_at": now(), "exit_reason": reason, "first_observed_ready_at": ready_at, "ready_gate_violations": violations, "last_progress": progress, "limitations": "SELECT polling may miss intermediate revisions/claims. First observed ready is not its transaction timestamp; source captures are observed DB state. Main COMPLETE alone does not imply Prefix completion."})
        engine.dispose()
    if reason.startswith("collector_error"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
