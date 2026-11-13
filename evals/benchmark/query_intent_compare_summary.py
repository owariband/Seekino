"""Deterministic local summary for the three-path query-intent comparison.
Reads the frozen protocol and one result per step. Response text stays in the
local metrics JSON; raw requests/history, configs and credentials are not read.

Usage:
    python -m evals.benchmark.query_intent_compare_summary RAW_DIRECTORY --out OUT
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
from statistics import median
ROOT = Path(__file__).resolve().parents[2]
STEPS, ARMS = 27, ("deepseek_decider", "jev_rewriter", "supervisor_direct")
ROUNDS = {1: 18, 2: 9}
TOKENS = ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens", "reasoning_tokens")
RETRIEVAL = ("retrieve", "search_video_events", "query_video")
DEFAULT_CASES = ROOT / "evals/benchmark/intent_routing/three_path_20261113_cases.json"
HOOK = re.compile(r"event=(query_hook_(?:route|decide|rewrite))\b(.*)$")
def need(condition, message):
    if not condition:
        raise ValueError(message)
def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))
def read_jsonl(path):
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
def stat(values):
    values = [float(v) for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if not values:
        return {"samples": 0, "mean": None, "median": None}
    return {"samples": len(values), "mean": round(sum(values) / len(values), 6), "median": round(median(values), 6)}
def field(text, name):
    match = re.search(rf"\b{name}=(.+?)(?=\s+\w+=|$)", text)
    return match.group(1).strip() if match else None
def hook_log(path):
    rows = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = HOOK.search(line)
        if not match:
            continue
        rest, duration, error = match.group(2), field(match.group(2), "duration_ms"), field(match.group(2), "error")
        rows.append({"event": match.group(1), "status": field(rest, "status"), "action": field(rest, "action"),
                     "duration_ms": float(duration) if duration else None,
                     "error": None if error in (None, "None", "null") else error})
    return rows
ROLE = {"query_hook_route": "route", "query_hook_decision": "decision", "retrieval_slot": "rewrite", "text_embedding": "embeddings"}
def role(row):
    return ROLE.get(row.get("operation")) or ("supervisor" if (row.get("context") or {}).get("stage") == "agent_chat" else "other")
def usage_new():
    value = {"calls": 0, "completed": 0, "failed": 0, "missing_usage": 0, "provider_duration_seconds": [0.0, 0, 0]}
    value.update({name: [0, 0, 0] for name in TOKENS})
    return value
def usage_add(target, row):
    target["calls"] += 1
    target["completed" if row.get("status") == "completed" else "failed"] += 1
    usage = row.get("usage") if isinstance(row.get("usage"), dict) else {}
    target["missing_usage"] += not isinstance(row.get("usage"), dict)
    for name in TOKENS:
        value = usage.get(name)
        if isinstance(value, int) and not isinstance(value, bool):
            target[name][0] += value
            target[name][1] += 1
        else:
            target[name][2] += 1
    started, ended = row.get("started_at"), row.get("ended_at")
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (started, ended)) and ended >= started:
        target["provider_duration_seconds"][0] += (ended - started) / 1000.0
        target["provider_duration_seconds"][1] += 1
    else:
        target["provider_duration_seconds"][2] += 1
def usage_summary(rows):
    def finish(value):
        out = {name: {"sum": round(value[name][0], 6) if value[name][1] else None,
                      "known": value[name][1], "missing": value[name][2],
                      "partial": value[name][2] > 0}
               for name in TOKENS + ("provider_duration_seconds",)}
        out.update({name: value[name] for name in ("calls", "completed", "failed", "missing_usage")})
        return out
    total, groups = usage_new(), {name: defaultdict(usage_new) for name in
                                  ("by_operation", "by_model", "by_stage", "by_role")}
    for row in rows:
        usage_add(total, row)
        keys = {"by_operation": row.get("operation") or "<missing>", "by_model": row.get("model") or "<missing>",
                "by_stage": (row.get("context") or {}).get("stage") or "<missing>", "by_role": role(row)}
        for group, key in keys.items():
            usage_add(groups[group][key], row)
    result = finish(total)
    for group, values in groups.items():
        result[group] = {key: finish(value) for key, value in sorted(values.items())}
    return result
def load_inputs(raw):
    protocol = read_json(raw / "protocol.json")
    candidates = ([Path(protocol["cases_path"])] if protocol.get("cases_path") else []) + [DEFAULT_CASES]
    path = next((item for item in candidates if item.is_file()), None)
    need(path is not None, "frozen cases file not found")
    raw_cases = path.read_bytes()
    need(not protocol.get("cases_sha256") or hashlib.sha256(raw_cases).hexdigest() == protocol["cases_sha256"],
         "cases file does not match protocol.cases_sha256")
    document = json.loads(raw_cases)
    case_rows = [case for case in document.get("cases") or [] if isinstance(case, dict)]
    cases = {case.get("id"): case for case in case_rows}
    need(cases and len(cases) == len(case_rows) and all(
        isinstance(case.get("id"), str) and isinstance(case.get("expected"), dict) for case in case_rows),
        "every case needs a unique id and expected object")
    repeats = document.get("repeat_case_ids", [])
    need(protocol.get("repeat_case_ids") == repeats and all(x in cases for x in repeats),
         "repeat_case_ids differ from cases/protocol")
    schedule = protocol.get("schedule")
    need(isinstance(schedule, list) and len(schedule) == STEPS, f"protocol must contain exactly {STEPS} scheduled steps")
    need(isinstance(protocol.get("arms"), list) and set(protocol["arms"]) == set(ARMS), f"protocol arms must be {list(ARMS)}")
    indices, rounds, identities = set(), Counter(), set()
    for step in schedule:
        index = step.get("index") if isinstance(step, dict) else None
        identity = (step.get("round"), step.get("case_id"), step.get("arm")) if isinstance(step, dict) else None
        need(isinstance(index, int) and not isinstance(index, bool) and index not in indices,
             f"schedule step needs a unique integer index: {index}")
        need(step.get("arm") in ARMS and step.get("case_id") in cases, f"step {index}: unknown arm or case")
        need(identity not in identities, f"duplicate scheduled identity: {identity}")
        indices.add(index)
        identities.add(identity)
        rounds[step.get("round")] += 1
    need(indices == set(range(STEPS)) and dict(rounds) == ROUNDS and all(
        sum(step["arm"] == arm for step in schedule) == 9 for arm in ARMS),
        f"schedule must cover indices 0..{STEPS - 1}, rounds {ROUNDS} and 9 steps per arm")
    found = {}
    for result_path in sorted((raw / "runs").glob("*/result.json")):
        result = read_json(result_path)
        index = result.get("index")
        need(isinstance(index, int) and not isinstance(index, bool) and index not in found,
             f"{result_path}: missing or duplicate result index {index}")
        found[index] = (result_path.parent, result)
    missing, extra = sorted(set(range(STEPS)) - set(found)), sorted(set(found) - set(range(STEPS)))
    need(not missing, f"incomplete protocol: missing result indices {missing}")
    need(not extra, f"unexpected result indices not in protocol: {extra}")
    for index, step in enumerate(schedule):
        need(not any(found[index][1].get(name) != step.get(name) for name in ("round", "case_id", "arm")),
             f"result {index}: metadata differs from protocol schedule")
    seen_calls = set()
    for index in sorted(found):
        directory = found[index][0]
        rows = read_jsonl(directory / "usage.jsonl")
        for row in rows:
            call_id = row.get("call_id")
            if isinstance(call_id, str) and call_id:
                need(call_id not in seen_calls, f"duplicate usage call_id {call_id}")
                seen_calls.add(call_id)
        found[index] = (directory, found[index][1], rows, hook_log(directory / "runtime.log"))
    return protocol, cases, repeats, document.get("fixture"), schedule, found
def final_content(directory, result):
    path = directory / "final.json"
    assistant = read_json(path).get("assistant_message") if path.is_file() else None
    content = assistant.get("content") if isinstance(assistant, dict) else None
    observed = (result.get("observed") or {}).get("content")
    content = content if isinstance(content, str) else (observed if isinstance(observed, str) else None)
    return content, content == observed if isinstance(content, str) else None
def hook_details(result):
    calls = (result.get("hook") or {}).get("calls") or []
    elapsed = [float(call["elapsed_seconds"]) for call in calls if isinstance(call, dict)
               and isinstance(call.get("elapsed_seconds"), (int, float))
               and not isinstance(call.get("elapsed_seconds"), bool)]
    return {"configured": (result.get("hook") or {}).get("configured"), "calls": len(calls),
            "elapsed_seconds": [round(x, 6) for x in elapsed],
            "total_elapsed_seconds": round(sum(elapsed), 6) if elapsed else None,
            "fail_open_suspected": (result.get("hook") or {}).get("fail_open_suspected")}
def arm_summary(turns, usage_rows):
    statuses = Counter(turn["status"] for turn in turns)
    ftt = [turn["ftt_seconds"] for turn in turns]
    tt = [turn["tt_seconds"] for turn in turns if turn["tt_success"]]
    hook = [turn["hook"]["total_elapsed_seconds"] for turn in turns if turn["hook"]["total_elapsed_seconds"] is not None]
    runtime = Counter(json.dumps(turn.pop("_runtime_model"), sort_keys=True, ensure_ascii=False) for turn in turns)
    models = [json.loads(item) for item in sorted(runtime)]
    policies = Counter(json.dumps(model.get("context_policy"), sort_keys=True) for model in models if "context_policy" in model)
    ftt_stat, tt_stat = stat(ftt), stat(tt)
    return {"turns": len(turns), "status_counts": dict(sorted(statuses.items())),
            "failures": len(turns) - statuses.get("COMPLETED", 0), "timeouts": sum(1 for turn in turns if turn["timeout"]),
            "ftt_seconds": {**ftt_stat, "missing": len(turns) - ftt_stat["samples"]},
            "successful_tt_seconds": {**tt_stat, "successful_only": True, "missing": len(turns) - tt_stat["samples"]},
            "hook_elapsed_seconds": {**stat(hook), "missing_turns": len(turns) - len(hook)},
            "runtime_model": {"models": models, "consistent": len(runtime) == 1 and bool(models[0]),
                              "context_policy_consistent": len(policies) == 1 and len(runtime) == 1},
            "usage": usage_summary(usage_rows)}
def summarize(raw):
    protocol, cases, repeats, fixture, schedule, results = load_inputs(raw)
    turns, all_usage, output_matches = [], [], []
    for step in schedule:
        directory, result, usage_rows, logs = results[step["index"]]
        observed = result.get("observed") or {}
        status, timeout = observed.get("status") or "UNKNOWN", bool(result.get("timeout") or observed.get("timeout"))
        ftt, tt = result.get("ftt_seconds"), result.get("tt_seconds")
        ftt = float(ftt) if isinstance(ftt, (int, float)) and not isinstance(ftt, bool) else None
        tt = float(tt) if isinstance(tt, (int, float)) and not isinstance(tt, bool) else None
        content, matches = final_content(directory, result)
        if matches is not None:
            output_matches.append(matches)
        tools = Counter((tool.get("name") or "<unknown>") if isinstance(tool, dict) else "<unknown>"
                        for tool in result.get("tool_calls") or [])
        blocks = observed.get("render_blocks") or []
        turns.append({"index": step["index"], "round": step["round"], "arm": step["arm"], "case_id": step["case_id"],
                      "case_position": step.get("case_position"), "status": status, "timeout": timeout,
                      "ftt_seconds": ftt, "tt_seconds": tt, "tt_success": tt is not None and status == "COMPLETED" and not timeout,
                      "first_text_event_type": (result.get("first_text_event") or {}).get("type"),
                      "text_stream_before_model_completed": result.get("text_stream_before_model_completed"),
                      "usage": usage_summary(usage_rows), "hook": hook_details(result), "hook_log": logs,
                      "tool_counts": dict(sorted(tools.items())),
                      "retrieval_tool_counts": {name: tools.get(name, 0) for name in RETRIEVAL},
                      "video_render_blocks": sum(1 for block in blocks if isinstance(block, dict) and block.get("type") == "video"),
                      "output_chars": len(content) if content is not None else None, "final_answer": content,
                      "case_expected": cases[step["case_id"]]["expected"], "semantic_quality_review": "required",
                      "_runtime_model": result.get("runtime_model") if isinstance(result.get("runtime_model"), dict) else {}})
        all_usage.extend(usage_rows)
    arms = {}
    for arm in ARMS:
        arm_turns = [turn for turn in turns if turn["arm"] == arm]
        rows = [row for step in schedule if step["arm"] == arm for row in results[step["index"]][2]]
        arms[arm] = arm_summary(arm_turns, rows)
    usage, statuses = usage_summary(all_usage), Counter(turn["status"] for turn in turns)
    model_sets = {json.dumps(x["runtime_model"]["models"], sort_keys=True) for x in arms.values()}
    checks = {"schedule_complete": True, "result_ids_unique": True, "usage_call_ids_unique": True,
              "final_matches_observed": all(output_matches),
              "role_buckets_complete": sum(x["calls"] for x in usage["by_role"].values()) == usage["calls"],
              "arm_usage_matches_total": sum(x["usage"]["calls"] for x in arms.values()) == usage["calls"],
              "runtime_model_consistent_across_arms": len(model_sets) == 1 and all(x["runtime_model"]["consistent"] for x in arms.values()),
              "runtime_context_policy_consistent_across_arms": len(model_sets) == 1 and all(x["runtime_model"]["context_policy_consistent"] for x in arms.values())}
    return {"schema_version": 1, "semantic_quality_review": "required",
            "policy": {"response_data": "local_only", "factual_quality": "not_inferred", "quality_flags_source": "case_expected",
                       "reasoning_tokens": "subset_of_output_tokens"},
            "experiment": {"protocol_started_at": protocol.get("started_at"), "git_head": protocol.get("git_head"),
                           "cases_sha256": protocol.get("cases_sha256"), "fixture": fixture, "arms": list(ARMS),
                           "case_ids": sorted(cases), "repeat_case_ids": repeats, "schedule_steps": len(schedule),
                           "rounds": dict(sorted(Counter(step["round"] for step in schedule).items()))},
            "checks": checks, "arms": arms, "usage": usage,
            "totals": {"turns": len(turns), "status_counts": dict(sorted(statuses.items())),
                       "failures": len(turns) - statuses.get("COMPLETED", 0),
                       "timeouts": sum(1 for turn in turns if turn["timeout"]),
                       "output_chars": sum(turn["output_chars"] or 0 for turn in turns),
                       "video_render_blocks": sum(turn["video_render_blocks"] for turn in turns),
                       "tool_counts": dict(sorted(sum((Counter(turn["tool_counts"]) for turn in turns), Counter()).items()))},
            "turns": [{key: value for key, value in turn.items() if not key.startswith("_")} for turn in turns]}
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw_directory", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        metrics = summarize(args.raw_directory.resolve())
    except (OSError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"out": str(args.out), "turns": len(metrics["turns"]), "semantic_quality_review": "required"}))
    return 0
if __name__ == "__main__":
    raise SystemExit(main())
