"""Offline latency evidence for EP04; reads exports/logs, never contacts services."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import UTC, datetime
import json
import math
from pathlib import Path
import re
import statistics

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
BASELINE = HERE.parent / "20261017-mygo-ep04-multiref-no-prefix" / "dd55671d-a84b-4628-bedb-4007a4f4aa9c"
REQUIRED = ("final-task.json", "final-chunks.json", "final-intervals.json",
            "final-model-usage.json", "voiceprint-events.log")


def timestamp(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) / 1000
    parsed = datetime.fromisoformat(value)
    return (parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed).timestamp()


def union_seconds(ranges):
    total, right = 0.0, None
    for start, end in sorted(ranges):
        total += max(0, end - max(start, right if right is not None else start))
        right = max(end, right if right is not None else end)
    return round(total, 6)


def call_stats(calls):
    ranges = [(call["start"], call["end"]) for call in calls if call["end"] is not None]
    durations = sorted(end - start for start, end in ranges)
    active = peak = 0
    for _time, change in sorted([(start, 1) for start, end in ranges if start < end]
                                + [(end, -1) for start, end in ranges if start < end]):
        active += change
        peak = max(peak, active)
    return {
        "calls": len(calls), "timed_calls": len(ranges),
        "statuses": dict(Counter(call["status"] for call in calls)),
        "cumulative_seconds": round(sum(durations), 6),
        "activity_union_seconds": union_seconds(ranges),
        "peak_overlapping_calls": peak,
        "activity_envelope_seconds": round(max(end for _, end in ranges) - min(start for start, _ in ranges), 6) if ranges else None,
        "median_seconds": round(statistics.median(durations), 6) if durations else None,
        "p95_seconds": round(durations[math.ceil(len(durations) * .95) - 1], 6) if durations else None,
        "max_seconds": round(max(durations), 6) if durations else None,
    }


def comparison_stats(path, task_id, generations):
    rows = []
    by_generation, by_interval, by_target = defaultdict(list), defaultdict(list), defaultdict(list)
    unmatched = ambiguous = 0
    for line in path.read_text().splitlines():
        if "event=voiceprint_compared " not in line:
            continue
        fields = dict(re.findall(r"(task|interval|lease|start_ms|end_ms|pcm)=([^\s]+)", line))
        if fields.get("task") != task_id:
            continue
        target = (int(fields["start_ms"]), int(fields["end_ms"]))
        fields["timestamp"] = timestamp(line.split()[0])
        rows.append(fields)
        by_interval[fields["interval"]].append(target)
        by_target[target].append(fields["pcm"])
        # Text logs round down to seconds; intersect that whole second with SDK spans.
        matches = [call for call in generations if call["interval"] == fields["interval"]
                   and call["end"] is not None and call["start"] < fields["timestamp"] + 1
                   and call["end"] >= fields["timestamp"]]
        if len(matches) == 1:
            by_generation[matches[0]["id"]].append(target)
        else:
            unmatched += not matches
            ambiguous += len(matches) > 1
    interval_unique = sum(len(set(values)) for values in by_interval.values())
    generation_unique = sum(len(set(values)) for values in by_generation.values())
    complete_assignment = not unmatched and not ambiguous
    return {
        "compared_calls": len(rows), "unique_targets": len(by_target),
        "unique_pcm": len({row["pcm"] for row in rows}),
        "duplicate_target_calls": len(rows) - len(by_target),
        "duplicate_target_fraction": (len(rows) - len(by_target)) / len(rows) if rows else None,
        "generation_assignment": {"matched": sum(map(len, by_generation.values())), "unmatched": unmatched, "ambiguous": ambiguous},
        "same_generation_repeats": len(rows) - generation_unique if complete_assignment else None,
        "across_generations_same_interval_repeats": generation_unique - interval_unique if complete_assignment else None,
        "across_interval_repeats": interval_unique - len(by_target),
        "targets": [{"start_ms": start, "end_ms": end, "calls": len(pcms), "pcm_sha256": sorted(set(pcms))}
                    for (start, end), pcms in sorted(by_target.items())],
    }


def refresh_stats(api_log, intervals, start, end):
    paths = sorted(path for path in api_log.parent.glob(api_log.name + "*") if path.is_file())
    if not paths:
        return {"status": "unavailable", "reason": "API log and rotated siblings are missing",
                "path": str(api_log), "source_paths": []}
    counts, stamps = Counter(), []
    seen = set()
    identities = {row["id"] for row in intervals}
    for path in paths:
        with path.open() as stream:
            for line in stream:
                if line in seen:
                    continue
                seen.add(line)
                try:
                    at = timestamp(line.split()[0])
                except (ValueError, IndexError):
                    continue
                stamps.append(at)
                match = re.search(r"Refine snapshot changed; refreshing interval ([a-f0-9-]+)", line)
                if match and match[1] in identities and start - 1 <= at <= end + 1:
                    counts[match[1]] += 1
    return {
        "status": "observed", "path": str(api_log), "source_paths": [str(path) for path in paths],
        "interval_ids_filter_size": len(identities),
        "observed_count": sum(counts.values()), "by_interval": dict(sorted(counts.items())),
        "log_time_bounds_cover_task": bool(stamps and min(stamps) <= start and max(stamps) + 1 >= end),
        "limitation": "Counts matching interval IDs and task time range across discovered log siblings, deduplicating identical raw lines; missing log spans are not inferred.",
    }


def analyze(directory, api_log):
    missing = [str(directory / name) for name in REQUIRED if not (directory / name).is_file()]
    if missing:
        return {"status": "not_ready", "directory": str(directory), "missing_files": missing}
    task, chunks, intervals, raw_usage = [json.loads((directory / name).read_text()) for name in REQUIRED[:4]]
    start, end = timestamp(task.get("start_time")), timestamp(task.get("finish_time"))
    if start is None or end is None or end < start:
        return {"status": "not_ready", "task_id": task.get("id"), "task_status": task.get("status"),
                "reason": "No final increasing task start/finish timestamps; exports may still be in progress."}
    if task["id"] != directory.name:
        raise ValueError("Export task identity differs from its directory")
    calls = []
    for row in raw_usage:
        metadata = row["metadata"]
        context = metadata.get("context", {})
        if row.get("run_id") != task["id"]:
            raise ValueError("Model usage contains another task")
        first, last = timestamp(row["start_time"]), timestamp(metadata.get("ended_at"))
        if last is not None and last < first:
            raise ValueError("Model call timestamps are decreasing")
        calls.append({"id": row["id"], "start": first, "end": last,
                      "stage": context.get("stage"), "operation": metadata.get("operation"),
                      "interval": context.get("interval_parse_task_id"), "status": metadata.get("status")})
    phases = sorted({call["stage"] for call in calls if call["stage"] is not None})
    generations = [call for call in calls if call["stage"] == "refine" and call["operation"] != "speaker_embedding"]
    per_interval = []
    for interval in sorted(intervals, key=lambda row: row["start_sequence_no"]):
        selected = sorted((call for call in generations if call["interval"] == interval["id"]), key=lambda call: call["start"])
        per_interval.append({"interval_id": interval["id"], "start_sequence_no": interval["start_sequence_no"],
                             "status": interval["status"], "committed": interval.get("content") is not None,
                             "generation": call_stats(selected),
                             "last_generation_seconds": round(selected[-1]["end"] - selected[-1]["start"], 6)
                             if selected and selected[-1]["end"] is not None else None})
    interval_spans = [(timestamp(row["start_time"]), timestamp(row["finish_time"])) for row in intervals
                      if row.get("start_time") and row.get("finish_time")]
    return {
        "status": "ready", "directory": str(directory), "task_id": task["id"], "video_id": task["video_id"],
        "parse_version": task["version"], "task_status": task["status"], "wall_seconds": round(end - start, 6),
        "wall_start_utc": datetime.fromtimestamp(start, UTC).isoformat(), "wall_end_utc": datetime.fromtimestamp(end, UTC).isoformat(),
        "chunks": len(chunks), "understanding_committed": sum(row.get("content") is not None for row in chunks),
        "indexed_current": sum(row.get("content") is not None and row.get("version") == row.get("indexed_content_version") for row in chunks),
        "intervals": len(intervals), "refine_commits": sum(row.get("content") is not None for row in intervals),
        "parse_config": task["parse_config"],
        "phase_model_activity": {phase: call_stats([call for call in calls if call["stage"] == phase]) for phase in phases},
        "all_model_activity": call_stats(calls), "refine_interval_execution_union_seconds": union_seconds(interval_spans),
        "voice_generation": call_stats([call for call in calls if call["stage"] == "voiceprint" and call["operation"] != "speaker_embedding"]),
        "speaker_embedding": {phase: call_stats([call for call in calls if call["operation"] == "speaker_embedding" and call["stage"] == phase])
                              for phase in ("voiceprint", "refine")},
        "refine_generation": call_stats(generations), "refine_by_interval": per_interval,
        "refine_snapshot_refresh_log": refresh_stats(api_log, intervals, start, end),
        "voice_comparisons": comparison_stats(directory / "voiceprint-events.log", task["id"], generations),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current-task-dir", type=Path, help="Defaults to the task in this directory's reparse-response.json")
    parser.add_argument("--baseline-task-dir", type=Path, default=BASELINE)
    parser.add_argument("--api-log", type=Path, default=ROOT / "outputs/server/logs/development/api.log")
    args = parser.parse_args()
    results = {}
    current_directory = args.current_task_dir
    if current_directory is None:
        try:
            task_id = json.loads((HERE / "reparse-response.json").read_text())["body"]["videoParseTaskId"]
            if not isinstance(task_id, str) or not task_id or Path(task_id).name != task_id:
                raise ValueError("Invalid task ID in reparse-response.json")
            current_directory = HERE / task_id
        except (ValueError, KeyError, TypeError, OSError) as error:
            results["current"] = {"status": "not_ready", "reason": f"Task response missing or invalid: {error}"}
    for name, directory in (("baseline", args.baseline_task_dir), ("current", current_directory)):
        if name in results:
            continue
        try:
            results[name] = analyze(directory.resolve(), args.api_log.resolve())
        except (ValueError, KeyError, TypeError, OSError) as error:
            results[name] = {"status": "not_ready", "directory": str(directory), "reason": f"Invalid/incomplete export: {error}"}
    baseline, current = results["baseline"], results["current"]
    ready = all(result["status"] == "ready" for result in results.values())
    comparable = ready and baseline["task_status"] == current["task_status"] == "COMPLETE" and baseline["video_id"] == current["video_id"]
    report = {
        "status": "ready" if ready else "not_ready", "generated_at": datetime.now(UTC).isoformat(), **results,
        "comparison": {"comparable_successful_runs": comparable,
                       "same_parse_config": baseline.get("parse_config") == current.get("parse_config") if ready else None,
                       "wall_seconds_delta": round(current["wall_seconds"] - baseline["wall_seconds"], 6) if comparable else None,
                       "baseline_over_current_wall_ratio": baseline["wall_seconds"] / current["wall_seconds"] if comparable and current["wall_seconds"] else None},
        "limitations": ["A failed final export can be analyzed but is never treated as a successful speedup.",
                        "Phase activity uses unions of recorded model-call spans, not full phase wall time; phases overlap and must not be added.",
                        "Cumulative model durations include parallel calls and nesting; CAM++ timing excludes media extraction and Tool round trips.",
                        "SDK generation spans do not reveal individual model turns. Repeated compare attribution uses second-resolution log timestamps.",
                        "Single executions and changing Agent-selected evidence are not a controlled quality or causal performance benchmark."],
    }
    output = HERE / "latency-comparison.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "output": str(output),
                      "runs": {name: {key: run.get(key) for key in ("status", "task_status", "wall_seconds", "refine_commits")}
                               for name, run in results.items()}, "comparison": report["comparison"]}, ensure_ascii=False))
    return 0 if ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
