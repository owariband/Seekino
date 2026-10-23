"""Audit a completed 200-frame pilot and render consistency metrics and local review UI.

This script reads experiment artifacts only. It never requests model inference,
assigns relevance judgments, or changes the pilot, gallery, or production index.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import fields
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import html
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
from typing import get_args
from urllib.parse import quote

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[3]
sys.path.insert(0, str(ROOT))

from gateway.usage import TokenUsage
from server.config.usage import load_model_pricing
from server.service.model_pricing import estimate_model_cost
from schema_models import CompositionLabel, PROMPT_VERSION, SCHEMA_VERSION, ShotTag, count_agreement, count_bucket

RUNS = ("primary_a", "primary_b", "cheap")
FIELDS = ("camera.shot_size", "camera.vertical_angle", "composition.primary",
          "subjects.main_subject_cell", "subjects.main_subject_scale", "subjects.person_count")
PCP_CLASSES = ["rule_of_thirds", "vertical", "horizontal", "diagonal", "curved",
               "triangle", "center", "symmetric", "pattern"]
COMPOSITION_CLASSES = list(get_args(CompositionLabel))
PCP_CANONICAL = {name: "curve" if name == "curved" else name for name in PCP_CLASSES}
PILOT_SIZE = 200
FULL_FRAME_COUNT = 8147


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path):
    return json.loads(path.read_text())


def read_rows(path):
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if len({row["frame_id"] for row in rows}) != len(rows):
        raise ValueError(f"Duplicate frame IDs in {path}")
    return rows


def save_json(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def value_at(tags, field):
    group, name = field.split(".")
    value = tags[group][name]
    return count_bucket(value) if field == "subjects.person_count" else value


def agreement(pairs, denominator):
    """Unweighted Cohen's kappa, including unknown as a literal valid category."""
    n = len(pairs)
    if not n:
        return dict(valid_pairs=0, total_frames=denominator, coverage=0, exact_agreement=None,
                    kappa=None, undefined_reason="no valid pairs", left_counts={}, right_counts={})
    left, right = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    observed = sum(a == b for a, b in pairs) / n
    expected = sum(left[label] * right[label] for label in left.keys() | right.keys()) / (n * n)
    undefined = math.isclose(expected, 1.0, abs_tol=1e-15)
    return dict(valid_pairs=n, total_frames=denominator, coverage=n / denominator,
                exact_agreement=observed, expected_agreement=expected,
                kappa=None if undefined else (observed - expected) / (1 - expected),
                undefined_reason="degenerate marginals: expected agreement is 1" if undefined else None,
                left_counts=dict(left), right_counts=dict(right))


def audited_run(rows):
    valid, errors = {}, []
    for row in rows:
        attempts = row.get("attempts", [])
        if row["status"] != "api_error" and (not attempts or attempts[-1]["status"] != row["status"]):
            errors.append(dict(frame_id=row["frame_id"], error="Final status lacks matching attempt metadata"))
        if row["status"] == "valid":
            try:
                valid[row["frame_id"]] = ShotTag.model_validate(row["tags"], strict=True).model_dump(mode="json")
            except Exception as error:
                errors.append(dict(frame_id=row["frame_id"], error=str(error)))
        elif row.get("tags") is not None:
            errors.append(dict(frame_id=row["frame_id"], error="Nonvalid record contains tags"))
    first_valid = sum(row["frame_id"] in valid and bool(row.get("attempts")) and
                      row["attempts"][0]["status"] == "valid" for row in rows)
    return valid, dict(total_frames=PILOT_SIZE, recorded_rows=len(rows),
                       status_counts=dict(Counter(row["status"] for row in rows)),
                       first_response_valid=first_valid, first_response_valid_rate=first_valid / PILOT_SIZE,
                       final_valid=len(valid), final_valid_rate=len(valid) / PILOT_SIZE,
                       retried_frames=sum(len(row.get("attempts", [])) > 1 for row in rows),
                       runner_error_frames=sum(bool(row.get("runner_error")) for row in rows),
                       audit_errors=errors)


def compare(left, right):
    shared = sorted(left.keys() & right.keys())
    return {field: agreement([(value_at(left[fid], field), value_at(right[fid], field)) for fid in shared],
                            PILOT_SIZE) for field in FIELDS}


def decide_gate(schema, self_metrics, cross_metrics):
    checks = {}
    for run in ("primary_a", "primary_b"):
        checks[f"{run}_schema"] = schema[run]["final_valid_rate"] >= .98
        checks[f"{run}_artifact_integrity"] = not schema[run].get("audit_errors", [])
    for field in ("camera.vertical_angle", "camera.shot_size"):
        kappa = self_metrics[field]["kappa"]
        checks[f"self_{field}"] = kappa is not None and kappa >= .6
    primary_pass = all(checks.values())
    cheap_checks = {"cheap_schema": schema["cheap"]["final_valid_rate"] >= .98,
                    "cheap_artifact_integrity": not schema["cheap"].get("audit_errors", [])}
    for field in ("camera.vertical_angle", "camera.shot_size"):
        cross, own = cross_metrics[field]["kappa"], self_metrics[field]["kappa"]
        cheap_checks[f"cross_{field}_within_0.1"] = cross is not None and own is not None and cross + 1e-12 >= own - .1
    selected = ("cheap" if all(cheap_checks.values()) else "primary") if primary_pass else None
    return dict(passed=primary_pass, primary_pass=primary_pass, primary_checks=checks, cheap_checks=cheap_checks,
                cheap_eligible=primary_pass and all(cheap_checks.values()),
                selected_model_kind=selected, selected_run="cheap" if selected == "cheap" else "primary_a" if selected else None,
                selected_model="doubao-seed-2-0-lite-260428" if selected == "cheap" else "gpt-5.6-sol" if selected else None,
                decision="proceed" if primary_pass else "stop_before_full_tagging",
                failed_checks=[name for name, passed in checks.items() if not passed],
                rule="Both primary runs must be >=98% finally schema-valid over all 200 frames; primary self kappa must be >=0.6 for angle and size. Cheap is eligible only if >=98% valid and each primary_a-versus-cheap kappa is at least the corresponding primary self kappa minus 0.1. Higher cross kappa is allowed. Undefined kappa fails its check.",
                next_action="Root reviews the pilot gate before any full tagging." if primary_pass else
                "Stop. Review disagreements and propose a prompt revision; no automatic retry, full tagging, or relevance labeling.")


def feature_comparison(valid, features):
    matrix = [[0 for _ in PCP_CLASSES] for _ in COMPOSITION_CLASSES]
    count_pairs, directions = [], Counter()
    same, represented = 0, 0
    for fid, tag in valid.items():
        feature = features[fid]
        native = feature["pcp"]["top_class"]
        primary = tag["composition"]["primary"]
        matrix[COMPOSITION_CLASSES.index(primary)][PCP_CLASSES.index(native)] += 1
        same += primary == PCP_CANONICAL[native]
        represented += primary in PCP_CANONICAL.values()
        count = tag["subjects"]["person_count"]
        detector = feature["detector"]["person_count"]
        count_pairs.append((count_bucket(count), count_bucket(detector)))
        directions[count_agreement(count, detector)] += 1
    n = len(valid)
    return dict(pcp_confusion=dict(rows=COMPOSITION_CLASSES, columns=PCP_CLASSES, counts=matrix,
                                  valid_pairs=n, total_frames=PILOT_SIZE, canonical_map=PCP_CANONICAL,
                                  canonical_top_class_matches=same,
                                  canonical_top_class_match_rate=same / n if n else None,
                                  vlm_classes_represented_in_pcp=represented),
                detector_count=dict(bucket_agreement=agreement(count_pairs, PILOT_SIZE),
                                    exact_count_directions=dict(directions),
                                    semantics="Literal VLM many means >6; reporting buckets map both exact 6 and many to 6+. Detector count is retained boxes, not ground truth."))


def usage_report(rows, summary, pricing, *, project=True):
    token_fields = [field.name for field in fields(TokenUsage)]
    sums, token_coverage, calls, attempts = Counter(), Counter(), [], []
    total_cny, total_usd = Decimal(0), Decimal(0)
    priced_calls, usd_calls, missing_usage = 0, 0, 0
    seen_calls = set()
    for row in rows:
        for attempt in row.get("attempts", []):
            attempts.append({**attempt, "frame_id": row["frame_id"]})
            for raw in attempt.get("usage", []):
                call_id = raw["call_id"]
                if call_id in seen_calls:
                    raise ValueError(f"Repeated usage call_id {call_id}; refusing to double-count")
                seen_calls.add(call_id)
                usage = raw.get("usage")
                amount, snapshot, assumption = None, None, None
                if usage is None:
                    missing_usage += 1
                else:
                    for name in token_fields:
                        if usage.get(name) is not None:
                            sums[name] += usage[name]
                            token_coverage[name] += 1
                    payload = {name: usage.get(name) for name in token_fields}
                    # Every request is a prompt plus one JPEG, never audio. Keep raw metadata unchanged.
                    if payload.get("audio_input_tokens") is None:
                        payload["audio_input_tokens"] = 0
                        payload["cached_audio_input_tokens"] = 0
                        assumption = "audio tokens are 0 because this experiment sends text and one JPEG only"
                    normalized = TokenUsage(**payload)
                    snapshot = pricing.snapshot(provider=raw.get("provider"), model=raw.get("model"),
                                                category="parse", input_tokens=normalized.input_tokens,
                                                started_at=raw["started_at"])
                    amount = estimate_model_cost(pricing, provider=raw.get("provider"), model=raw.get("model"),
                                                 category="parse", operation=raw.get("operation"),
                                                 started_at=raw["started_at"], usage=normalized)
                sdk_costs = [model["cost_usd"] for model in raw.get("sdk_model_usage", []) if model.get("cost_usd") is not None]
                if amount is not None:
                    total_cny += amount
                    priced_calls += 1
                if sdk_costs:
                    total_usd += sum((Decimal(str(cost)) for cost in sdk_costs), Decimal(0))
                    usd_calls += 1
                calls.append(dict(frame_id=row["frame_id"], attempt=attempt.get("attempt"), raw_metadata=raw,
                                  estimated_cost_cny=str(amount) if amount is not None else None,
                                  configured_price_snapshot=snapshot, pricing_assumption=assumption,
                                  sdk_reported_cost_usd=str(sum((Decimal(str(v)) for v in sdk_costs), Decimal(0))) if sdk_costs else None))
    latencies = [a["latency_seconds"] for a in attempts if a.get("latency_seconds") is not None]
    factor = FULL_FRAME_COUNT / len(rows)
    all_frames_have_calls = all(row.get("attempts") and all(a.get("usage") for a in row["attempts"]) for row in rows)
    complete_cny = len(calls) > 0 and priced_calls == len(calls) and all_frames_have_calls
    complete_usd = len(calls) > 0 and usd_calls == len(calls) and all_frames_have_calls
    result = dict(frames=len(rows), attempts=len(attempts), calls=len(calls), calls_without_token_usage=missing_usage,
                attempts_without_usage=sum(not a.get("usage") for a in attempts),
                recorded_token_sums={name: sums[name] if token_coverage[name] else None for name in token_fields},
                token_field_call_coverage=dict(token_coverage),
                configured_cost_cny=dict(known_subtotal=str(total_cny) if priced_calls else None,
                                         priced_calls=priced_calls, complete=complete_cny,
                                         total=str(total_cny) if complete_cny else None),
                sdk_reported_cost_usd=dict(known_subtotal=str(total_usd) if usd_calls else None,
                                          priced_calls=usd_calls, complete=complete_usd,
                                          total=str(total_usd) if complete_usd else None),
                timing=dict(attempt_latency_sum_seconds=sum(latencies),
                            attempt_latency_median_seconds=statistics.median(latencies) if latencies else None,
                            observed_run_wall_seconds=summary.get("wall_seconds"), workers=summary.get("workers")),
                projection=dict(frames=FULL_FRAME_COUNT, factor=factor,
                                basis="Linear extrapolation of all recorded attempts including retries over 200 pilot frames; representative-frame sampling may not predict long-shot extras or OP/ED. Token sums with missing usage are lower bounds. No added full-run pilot repetitions are assumed.",
                                configured_total_cny=str(total_cny * Decimal(FULL_FRAME_COUNT) / Decimal(PILOT_SIZE)) if complete_cny else None,
                                sdk_reported_total_usd=str(total_usd * Decimal(FULL_FRAME_COUNT) / Decimal(PILOT_SIZE)) if complete_usd else None,
                                recorded_tokens={name: sums[name] * factor if token_coverage[name] else None for name in token_fields},
                                serial_attempt_seconds=sum(latencies) * factor,
                                ideal_wall_seconds_at_10_workers=sum(latencies) * factor / 10,
                                ten_worker_caveat="Idealized sum of recorded attempt latencies divided by 10; not a measured ten-worker run and excludes scheduling, throttling, tail, and startup overhead.",
                                wall_seconds_at_observed_workers=summary["wall_seconds"] * factor if summary.get("wall_seconds") else None),
                run_summary=summary, attempts_raw_metadata=attempts, call_records=calls,
                cost_notes="CNY and USD are separate, never converted or added. Codex is unpriced in the repository calculator; missing cost is null, not zero. Reasoning and cached token fields are subsets, not additional total tokens. Raw provider metadata is retained.")
    if not project:
        result["projection"] = None
    return result


def total_usage(parts):
    """Keep known spend separate from unavailable all-provider totals."""
    result = dict(frame_observations=sum(p["frames"] for p in parts), attempts=sum(p["attempts"] for p in parts),
                  calls=sum(p["calls"] for p in parts), recorded_token_sums={})
    for name in (field.name for field in fields(TokenUsage)):
        known = [p["recorded_token_sums"][name] for p in parts if p["recorded_token_sums"][name] is not None]
        result["recorded_token_sums"][name] = sum(known) if known else None
    for key in ("configured_cost_cny", "sdk_reported_cost_usd"):
        amounts = [Decimal(p[key]["known_subtotal"]) for p in parts if p[key]["known_subtotal"] is not None]
        complete = bool(parts) and all(p[key]["complete"] for p in parts)
        result[key] = dict(known_subtotal=str(sum(amounts, Decimal(0))) if amounts else None,
                           total=str(sum(amounts, Decimal(0))) if complete else None, complete=complete,
                           priced_calls=sum(p[key]["priced_calls"] for p in parts))
    return result


def percent(value):
    return "undefined" if value is None else f"{value:.1%}"


def number(value):
    return "undefined" if value is None else f"{value:.3f}"


def markdown_report(report):
    gate = report["gate"]
    lines = ["# Shot-tag pilot report", "", f"Generated: {report['created_at']}", "",
             f"Pilot gate: **{gate['decision']}**. Selected model kind: **{gate['selected_model_kind'] or 'none'}**.", "",
             "These are consistency measurements, not accuracy or retrieval relevance. No relevance judgments were generated.", "",
             "## Coverage and schema validity", "", "Rates use all 200 pilot frames, including API and runner failures. A retry does not overwrite the first-response result.", "",
             "| Run | First response | Final valid | Retried | API errors |", "|---|---:|---:|---:|---:|"]
    for run in RUNS:
        s = report["schema_validity"][run]
        lines.append(f"| {run} | {s['first_response_valid']}/200 ({percent(s['first_response_valid_rate'])}) | {s['final_valid']}/200 ({percent(s['final_valid_rate'])}) | {s['retried_frames']} | {s['status_counts'].get('api_error', 0)} |")
    lines += ["", "## Agreement", "", "Unweighted Cohen's kappa uses valid pairs only; invalid records are excluded with coverage shown. Valid `unknown` labels remain ordinary categories. Person count uses 0, 1, 2, 3–5, and 6+ (`many`) reporting buckets.", ""]
    for comparison, metrics in report["agreement"].items():
        lines += [f"### {comparison}", "", "| Field | Valid pairs | Exact agreement | Cohen's kappa |", "|---|---:|---:|---:|"]
        for field, result in metrics.items():
            lines.append(f"| {field} | {result['valid_pairs']}/200 | {percent(result['exact_agreement'])} | {number(result['kappa'])} |")
        lines.append("")
    lines += ["## Gate", "", gate["rule"], "", gate["next_action"], "", "```json", json.dumps(gate, indent=2), "```", "",
              "## PCP and detector comparison", "", "PCP has nine native classes. Its `curved` label maps explicitly to VLM `curve` for canonical comparisons; all other native labels retain their names. The 13×9 matrices retain native PCP column names. Four VLM classes have no PCP equivalent. Neither source is treated as ground truth.", ""]
    for run, result in report["features"].items():
        matrix = result["pcp_confusion"]
        lines += [f"### {run}", "", "| VLM \\ PCP | " + " | ".join(matrix["columns"]) + " |",
                  "|---|" + "---:|" * len(matrix["columns"])]
        lines += ["| " + label + " | " + " | ".join(map(str, values)) + " |" for label, values in zip(matrix["rows"], matrix["counts"])]
        detector = result["detector_count"]
        metric = detector["bucket_agreement"]
        lines += ["", f"Detector count bucket agreement: {percent(metric['exact_agreement'])}; kappa {number(metric['kappa'])}; coverage {metric['valid_pairs']}/200. Exact-count direction counts: `{json.dumps(detector['exact_count_directions'])}`.", "", detector["semantics"], ""]
    lines += ["## Usage, latency, and configured cost", "", "| Run | Attempts / calls | Input / output tokens | Known CNY subtotal | Full-run CNY estimate | Observed wall seconds |", "|---|---:|---:|---:|---:|---:|"]
    for run, usage in report["usage"].items():
        tokens = usage["recorded_token_sums"]
        lines.append(f"| {run} | {usage['attempts']} / {usage['calls']} | {tokens['input_tokens']} / {tokens['output_tokens']} | {usage['configured_cost_cny']['known_subtotal'] or 'unavailable'} | {usage['projection']['configured_total_cny'] or 'unavailable'} | {usage['timing']['observed_run_wall_seconds']} |")
    lines += ["", "| Run | Workers | Median attempt seconds | Full-run serial attempt hours | Full-run wall hours at observed workers | Idealized full-run hours at 10 workers |", "|---|---:|---:|---:|---:|---:|"]
    for run, usage in report["usage"].items():
        timing, projection = usage["timing"], usage["projection"]
        wall = projection["wall_seconds_at_observed_workers"]
        lines.append(f"| {run} | {timing['workers']} | {number(timing['attempt_latency_median_seconds'])} | {number(projection['serial_attempt_seconds'] / 3600)} | {number(wall / 3600 if wall is not None else None)} | {number(projection['ideal_wall_seconds_at_10_workers'] / 3600)} |")
    lines += ["", "The ten-worker estimate divides summed request latency by ten. It is not measured ten-worker throughput and excludes scheduling, throttling, long-tail, and startup overhead."]
    feature_time = report["feature_run_metadata"]
    lines += ["", f"Offline feature pilot wall time: {feature_time['elapsed_seconds']:.1f} seconds on CPU. Median PCP inference: {feature_time['median_pcp_ms']:.3f} ms; detector pipeline: {feature_time['median_detector_ms']:.3f} ms. A linear 8,147-frame projection at this observed throughput is {feature_time['elapsed_seconds'] * FULL_FRAME_COUNT / PILOT_SIZE / 3600:.2f} hours. It does not assume an acceleration backend or subtract future cache hits."]
    lines += ["", "Smoke runs are excluded from all pilot schema and agreement denominators. Their experimental usage is itemized separately:", "",
              "| Smoke run | Frames | Input / output tokens | Known CNY subtotal | Known SDK USD subtotal |", "|---|---:|---:|---:|---:|"]
    for run, usage in report["smoke_usage"].items():
        tokens = usage["recorded_token_sums"]
        lines.append(f"| {run} | {usage['frames']} | {tokens['input_tokens']} / {tokens['output_tokens']} | {usage['configured_cost_cny']['known_subtotal'] or 'unavailable'} | {usage['sdk_reported_cost_usd']['known_subtotal'] or 'unavailable'} |")
    totals = report["experimental_usage_totals"]
    lines += ["", f"Pilot plus separately itemized smoke usage: {totals['attempts']} attempts / {totals['calls']} calls. Known configured CNY subtotal: {totals['configured_cost_cny']['known_subtotal'] or 'unavailable'}; complete all-call configured cost: {totals['configured_cost_cny']['total'] or 'unavailable'}. Full token and USD coverage is recorded in report.json."]
    lines += ["", "The full-run projection covers 8,147 frames: 5,898 representatives plus 2,249 additional frames from shots longer than four seconds. Each run is projected independently by 8,147/200, including its observed retries; this is a planning estimate, not a billing quote. Pilot selection excludes OP/ED and low-information frames, so the full distribution differs.", "",
              "Configured rates come from `config/model-pricing.yaml`; report.json includes its hash, exact per-call rate snapshots, raw usage, timing, sampling settings, and unpriced-call coverage. Text plus JPEG requests contain zero audio tokens; this explicit pricing assumption leaves raw metadata unchanged. Codex has no configured monetary rate. Missing prices remain unavailable; CNY and any SDK-reported USD remain separate. Cached and reasoning tokens are subsets and are not double-counted.", "",
              "## Artifacts", "", "- `../review.html`: all 200 source frames, detector overlays, PCP probabilities, and all three VLM outputs.", "- `report.json`: metrics, decisions, source hashes, raw call metadata, and cost projections.", "- `../validation.json`: pilot artifact integrity and the separate pilot-gate result.", "", "No full tagging, retrieval relevance assessment, or production write is performed by this report.", ""]
    return "\n".join(lines)


def render_html(frames, runs, features, report):
    cards = []
    for frame in frames:
        fid = frame["frame_id"]
        cards.append(dict(frame_id=fid, episode=frame["episode"], start_ms=frame["start_ms"], end_ms=frame["end_ms"],
                          timestamp_ms=frame["frame_timestamp_ms"], duration_bucket=frame["duration_bucket"],
                          internal_motion=frame["internal_motion"], mandatory_anchor=frame["mandatory_anchor"],
                          image=quote(os.path.relpath(frame["image_path"], OUT), safe="/"),
                          runs={run: dict(status=runs[run][fid]["status"], tags=runs[run][fid].get("tags"),
                                          attempts=len(runs[run][fid].get("attempts", [])),
                                          model=runs[run][fid].get("model")) for run in RUNS},
                          pcp=features[fid]["pcp"], detector=features[fid]["detector"]))
    payload = json.dumps(dict(cards=cards, gate=report["gate"], schema=report["schema_validity"],
                              agreement=report["agreement"], features=report["features"]), ensure_ascii=False, allow_nan=False).replace("<", "\\u003c")
    return HTML_TEMPLATE.replace("__DATA__", payload).replace("__CREATED__", html.escape(report["created_at"]))


HTML_TEMPLATE = '''<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Shot-tag pilot review</title><style>
:root{color-scheme:dark;background:#10141b;color:#e4e9f1;font:15px/1.5 system-ui}body{max-width:1500px;margin:auto;padding:24px}h1{margin-bottom:6px}a{color:#94c9ff}small,.muted{color:#abb7c7}header{margin-bottom:24px}.bar{position:sticky;top:0;z-index:2;background:#10141bf2;padding:12px 0;display:flex;gap:16px;flex-wrap:wrap;align-items:center}input,select{background:#222b38;color:inherit;border:1px solid #536074;border-radius:5px;padding:8px}input[type=search]{min-width:280px}article{border:1px solid #374154;border-radius:9px;margin:24px 0;padding:16px;background:#19212c}article.anchor{border-color:#e1b864}h2{font-size:18px;margin:0 0 5px}.visual{display:grid;grid-template-columns:minmax(400px,1.5fr) minmax(280px,1fr);gap:20px}.frame{width:100%;background:#000}.runs{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:15px}.run{min-width:0;background:#111923;padding:12px;border-radius:6px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.5 ui-monospace,monospace}summary{cursor:pointer}table{border-collapse:collapse;font-size:12px;width:100%;margin:10px 0}td,th{padding:5px;border:1px solid #374154;text-align:left}th{background:#253145}.scroll{overflow:auto}.box{stroke:#54f5ad;stroke-width:2;fill:none;vector-effect:non-scaling-stroke}.box-label{fill:#54f5ad;font:bold 18px monospace;paint-order:stroke;stroke:#000;stroke-width:3px}.hide-boxes .box,.hide-boxes .box-label{display:none}.pill{background:#2c3a4c;padding:2px 7px;border-radius:5px;font-size:12px}.error{color:#ffb0ae}.good{color:#a4ecc3}svg text{fill:#dce6f6}@media(max-width:850px){.visual,.runs{grid-template-columns:1fr}body{padding:12px}}
</style><body><header><h1>Shot-tag pilot review</h1><p>200 stratified source frames · three independent tag runs · offline PCP and person boxes</p><p class="muted">Consistency review only. Model agreement is not accuracy. No relevance judgments or relevance controls are present. Images resolve from the unchanged local gallery. Generated __CREATED__.</p><p><a href="pilot/report.md">Report</a> · <a href="pilot/report.json">Metrics and raw usage</a> · <a href="validation.json">Pilot validation</a></p><div id="summary"></div><details><summary>Agreement and PCP confusion matrices</summary><div id="tables"></div></details></header>
<nav class="bar"><input id="search" type="search" placeholder="Filter frame ID, episode, or tag text"><select id="episode"><option value="">All episodes</option></select><label><input id="anchors" type="checkbox"> Anchors only</label><label><input id="boxes" type="checkbox" checked> Person boxes</label><span id="visible"></span></nav><main id="cards"></main>
<script id="data" type="application/json">__DATA__</script><script>
'use strict';
const data=JSON.parse(document.getElementById('data').textContent), runs=['primary_a','primary_b','cheap'];
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pct=x=>x==null?'undefined':(100*x).toFixed(1)+'%', num=x=>x==null?'undefined':x.toFixed(3);
const table=(heads,rows)=>'<div class="scroll"><table><thead><tr>'+heads.map(x=>'<th>'+esc(x)+'</th>').join('')+'</tr></thead><tbody>'+rows.map(r=>'<tr>'+r.map(x=>'<td>'+esc(x)+'</td>').join('')+'</tr>').join('')+'</tbody></table></div>';
document.getElementById('summary').innerHTML='<p><strong>Pilot gate: '+esc(data.gate.decision)+'</strong> · selected model kind: '+esc(data.gate.selected_model_kind??'none')+'</p>'+table(['Run','First response valid','Final valid'],runs.map(r=>[r,pct(data.schema[r].first_response_valid_rate),pct(data.schema[r].final_valid_rate)]))+'<p class="muted">'+esc(data.gate.rule)+'</p>';
document.getElementById('tables').innerHTML=Object.entries(data.agreement).map(([name,m])=>'<h3>'+esc(name)+'</h3>'+table(['Field','Pairs / 200','Exact agreement','Kappa'],Object.entries(m).map(([f,v])=>[f,v.valid_pairs,pct(v.exact_agreement),num(v.kappa)]))).join('')+'<p>PCP native <code>curved</code> explicitly maps to VLM <code>curve</code> only for canonical comparisons. Four VLM classes have no PCP counterpart. Each matrix is 13 × 9; columns preserve native PCP names.</p>'+runs.map(r=>{const m=data.features[r].pcp_confusion;return '<h3>'+esc(r)+' — VLM versus PCP</h3>'+table(['VLM / PCP',...m.columns],m.rows.map((label,i)=>[label,...m.counts[i]]));}).join('');
const fields=t=>t?['size: '+t.camera.shot_size,'angle: '+t.camera.vertical_angle,'composition: '+t.composition.primary,'cell / scale: '+t.subjects.main_subject_cell+' / '+t.subjects.main_subject_scale,'people: '+t.subjects.person_count,'kind: '+t.frame.kind].map(esc).join('<br>'):'No valid tags';
function draw(c){const d=c.detector, boxes=d.person_boxes.map((b,i)=>'<rect class="box" x="'+b[0]*1000+'" y="'+b[1]*562.5+'" width="'+(b[2]-b[0])*1000+'" height="'+(b[3]-b[1])*562.5+'"/><text class="box-label" x="'+(b[0]*1000+4)+'" y="'+(b[1]*562.5+20)+'">'+(i+1)+' '+b[4].toFixed(2)+'</text>').join('');
const bars=c.pcp.class_order.map((name,i)=>'<text x="0" y="'+(i*28+19)+'" font-size="13">'+esc(name)+'</text><rect x="130" y="'+(i*28+5)+'" width="'+c.pcp.probs[i]*210+'" height="18" fill="#65a5eb"/><text x="350" y="'+(i*28+19)+'" font-size="12">'+pct(c.pcp.probs[i])+'</text>').join('');
return '<article id="'+esc(c.frame_id)+'" class="'+(c.mandatory_anchor?'anchor':'')+'"><h2>'+esc(c.frame_id)+' '+(c.mandatory_anchor?'<span class="pill">mandatory anchor</span>':'')+'</h2><p class="muted">Episode '+c.episode+' · shot ['+c.start_ms+', '+c.end_ms+') ms · image '+c.timestamp_ms+' ms · '+esc(c.duration_bucket)+' · motion proxy '+c.internal_motion+'</p><div class="visual"><svg class="frame" viewBox="0 0 1000 562.5" role="img" aria-label="Source frame with normalized person detector boxes"><image href="'+esc(c.image)+'" width="1000" height="562.5" preserveAspectRatio="none"/>'+boxes+'</svg><div><h3>PCP probabilities</h3><svg viewBox="0 0 410 256" width="100%">'+bars+'</svg><p>Retained person boxes: <strong>'+d.person_count+'</strong> · union area '+num(d.person_box_union_area)+' · largest area '+num(d.largest_person_area)+'</p><details><summary>Detector evidence and thresholds</summary><pre>'+esc(JSON.stringify(d,null,2))+'</pre></details></div></div><div class="runs">'+runs.map(r=>{const t=c.runs[r];return '<section class="run"><h3>'+r+'</h3><p class="'+(t.status==='valid'?'good':'error')+'">'+esc(t.status)+' · '+t.attempts+' attempt(s)</p><small>'+esc(t.model)+'</small><p>'+fields(t.tags)+'</p><details><summary>All five tag groups</summary><pre>'+esc(JSON.stringify(t.tags,null,2))+'</pre></details></section>';}).join('')+'</div></article>';
}
document.getElementById('cards').innerHTML=data.cards.map(draw).join('');
document.getElementById('episode').innerHTML+=Array.from({length:13},(_,i)=>'<option value="'+(i+1)+'">Episode '+(i+1)+'</option>').join('');
const elements=data.cards.map(c=>({data:c,element:document.getElementById(c.frame_id),text:JSON.stringify(c).toLowerCase()}));
function filter(){const q=document.getElementById('search').value.toLowerCase(),ep=document.getElementById('episode').value,a=document.getElementById('anchors').checked;let n=0;for(const x of elements){const visible=(!q||x.text.includes(q))&&(!ep||x.data.episode===Number(ep))&&(!a||x.data.mandatory_anchor);x.element.hidden=!visible;n+=visible;}document.getElementById('visible').textContent=n+' / 200 frames';}
for(const id of ['search','episode','anchors'])document.getElementById(id).addEventListener('input',filter);
document.getElementById('boxes').addEventListener('change',e=>document.body.classList.toggle('hide-boxes',!e.target.checked));filter();
</script></body></html>'''


def load_inputs():
    frames = read_rows(OUT / "pilot/frames.jsonl")
    if len(frames) != PILOT_SIZE:
        raise ValueError("Pilot must contain exactly 200 frames")
    expected = {frame["frame_id"]: frame for frame in frames}
    sampling = read_json(OUT / "pilot/sampling.json")
    if digest(OUT / "pilot/frames.jsonl") != sampling["frames_sha256"]:
        raise ValueError("Pilot manifest differs from sampling audit")
    runs, summaries = {}, {}
    for run in RUNS:
        summaries[run] = read_json(OUT / f"pilot/{run}-summary.json")
        if summaries[run].get("status") != "complete":
            raise ValueError(f"Run {run} is not complete; report generation deferred")
        rows = read_rows(OUT / f"pilot/{run}.jsonl")
        if {row["frame_id"] for row in rows} != expected.keys():
            raise ValueError(f"Run {run} does not cover exactly the pilot IDs")
        if summaries[run].get("completed") != PILOT_SIZE or summaries[run].get("counts") != dict(Counter(row["status"] for row in rows)):
            raise ValueError(f"Run {run} summary does not reconcile with its records")
        for row in rows:
            if row["sha256"] != expected[row["frame_id"]]["sha256"] or row["run_id"] != run:
                raise ValueError(f"Run/image identity mismatch in {run}")
            if row["status"] not in {"valid", "invalid", "api_error"}:
                raise ValueError("Unexpected terminal VLM status")
            if row["status"] != "api_error" and (row.get("schema_version") != SCHEMA_VERSION or row.get("prompt_version") != PROMPT_VERSION):
                raise ValueError("Mixed prompt/schema versions")
        runs[run] = {row["frame_id"]: row for row in rows}
    prompt_hash = digest(OUT / "prompt/shot-tag-v1.md")
    if any(summary.get("prompt_sha256") != prompt_hash for summary in summaries.values()):
        raise ValueError("Run prompt hash differs from pinned prompt")
    if any(row.get("prompt_sha256") != prompt_hash for run in runs.values() for row in run.values() if row.get("attempts")):
        raise ValueError("A response used a different prompt hash")
    feature_summary = read_json(OUT / "features/runs/pilot_features.jsonl.json")
    if feature_summary.get("status") != "complete":
        raise ValueError("Feature run is not complete; report generation deferred")
    if (feature_summary.get("input_sha256") != sampling["frames_sha256"] or
            feature_summary.get("output_sha256") != digest(OUT / "pilot/features.jsonl") or
            feature_summary.get("completed_rows") != PILOT_SIZE):
        raise ValueError("Feature run metadata does not reconcile with the manifests")
    feature_rows = read_rows(OUT / "pilot/features.jsonl")
    if {row["frame_id"] for row in feature_rows} != expected.keys():
        raise ValueError("Features do not cover exactly the pilot IDs")
    for row in feature_rows:
        if row["sha256"] != expected[row["frame_id"]]["sha256"]:
            raise ValueError("Feature image mismatch")
        if row["feature_contract_id"] != feature_summary["feature_contract_id"]:
            raise ValueError("Mixed feature inference contracts")
        pcp, detector = row["pcp"], row["detector"]
        if pcp["class_order"] != PCP_CLASSES or len(pcp["probs"]) != 9 or not all(math.isfinite(p) and 0 <= p <= 1 for p in pcp["probs"]) or not math.isclose(sum(pcp["probs"]), 1, abs_tol=1e-5):
            raise ValueError("Invalid PCP class order/probabilities")
        if pcp["top_class"] != PCP_CLASSES[max(range(9), key=lambda i: pcp["probs"][i])]:
            raise ValueError("PCP top class mismatch")
        if detector["person_count"] != len(detector["person_boxes"]):
            raise ValueError("Detector count differs from retained boxes")
        for box in detector["person_boxes"]:
            if len(box) != 5 or not all(math.isfinite(v) and 0 <= v <= 1 for v in box) or not (box[0] < box[2] and box[1] < box[3]):
                raise ValueError("Invalid normalized detector box")
    gallery = Path(sampling["gallery_directory"])
    if digest(gallery / "gallery.jsonl") != sampling["gallery_sha256"]:
        raise ValueError("Source gallery hash changed")
    gallery_rows = read_rows(gallery / "gallery.jsonl")
    if sum(row["is_representative"] or row["end_ms"] - row["start_ms"] > 4000 for row in gallery_rows) != FULL_FRAME_COUNT:
        raise ValueError("Full-run projection frame count changed")
    for frame in frames:
        if digest(Path(frame["image_path"])) != frame["sha256"]:
            raise ValueError(f"Image SHA mismatch for {frame['frame_id']}")
    return frames, runs, summaries, {row["frame_id"]: row for row in feature_rows}, sampling


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    frames, runs, summaries, features, sampling = load_inputs()
    valid, schema = {}, {}
    for run in RUNS:
        valid[run], schema[run] = audited_run(list(runs[run].values()))
    comparisons = dict(primary_a_vs_primary_b=compare(valid["primary_a"], valid["primary_b"]),
                       primary_a_vs_cheap=compare(valid["primary_a"], valid["cheap"]),
                       primary_b_vs_cheap=compare(valid["primary_b"], valid["cheap"]))
    pricing = load_model_pricing(ROOT / "config/model-pricing.yaml")
    source_paths = ["pilot/frames.jsonl", "pilot/sampling.json", "pilot/features.jsonl", "features/runs/pilot_features.jsonl.json", "schema_models.py", "pilot_report.py",
                    "prompt/shot-tag-v1.md", *[f"pilot/{run}.jsonl" for run in RUNS],
                    *[f"pilot/{run}-summary.json" for run in RUNS]]
    usage = {run: usage_report(list(runs[run].values()), summaries[run], pricing) for run in RUNS}
    smoke_usage = {}
    for path in sorted((OUT / "pilot").glob("*_smoke.jsonl")):
        summary_path = path.with_name(path.stem + "-summary.json")
        summary = read_json(summary_path)
        if summary.get("status") != "complete":
            raise ValueError(f"Smoke usage is incomplete: {path.name}")
        smoke_usage[path.stem] = usage_report(read_rows(path), summary, pricing, project=False)
        source_paths += [str(path.relative_to(OUT)), str(summary_path.relative_to(OUT))]
    report = dict(stage="pilot", created_at=datetime.now(timezone.utc).isoformat(),
                  schema_version=SCHEMA_VERSION, prompt_version=PROMPT_VERSION,
                  measurement_scope="schema validity and model consistency; no accuracy or relevance labels",
                  pilot_frames=PILOT_SIZE, full_projection_frames=FULL_FRAME_COUNT, sampling=sampling,
                  source_hashes={name: digest(OUT / name) for name in source_paths},
                  pricing_source=dict(path=str(ROOT / "config/model-pricing.yaml"),
                                      sha256=digest(ROOT / "config/model-pricing.yaml"), currency=pricing.currency),
                  schema_validity=schema, agreement=comparisons,
                  gate=decide_gate(schema, comparisons["primary_a_vs_primary_b"], comparisons["primary_a_vs_cheap"]),
                  features={run: feature_comparison(valid[run], features) for run in RUNS},
                  feature_run_metadata=read_json(OUT / "features/runs/pilot_features.jsonl.json"),
                  usage=usage, smoke_usage=smoke_usage, pilot_usage_totals=total_usage(list(usage.values())),
                  experimental_usage_totals=total_usage([*usage.values(), *smoke_usage.values()]))
    review_html = render_html(frames, runs, features, report)
    script = review_html.split("<script>", 1)[1].split("</script>", 1)[0]
    subprocess.run(["node", "--check"], input=script, text=True, capture_output=True, check=True)
    save_json(OUT / "pilot/report.json", report)
    (OUT / "pilot/report.md").write_text(markdown_report(report))
    (OUT / "review.html").write_text(review_html)
    validation = dict(stage="pilot", status="passed" if not any(s["audit_errors"] for s in schema.values()) else "failed",
                      created_at=report["created_at"], pilot_gate=report["gate"],
                      meaning="status checks artifact integrity; pilot_gate independently decides whether full tagging can proceed",
                      frames_verified=PILOT_SIZE, images_sha256_verified=PILOT_SIZE, feature_rows_verified=PILOT_SIZE,
                      source_hashes=report["source_hashes"],
                      output_hashes={name: digest(OUT / name) for name in ("pilot/report.json", "pilot/report.md", "review.html")},
                      schema_audit_errors={run: schema[run]["audit_errors"] for run in RUNS}, relevance_judgments_created=0)
    validation["review_html"] = dict(frames=PILOT_SIZE, tag_runs=list(RUNS), inline_svg_boxes=True, javascript_syntax_verified=True,
                                      browser_render_verified=False,
                                      limitation="Browser URL policy blocked local-file preview; generated HTML structure and script syntax were checked offline.")
    save_json(OUT / "validation.json", validation)
    print(json.dumps(dict(stage="pilot", artifact_validation=validation["status"], gate=report["gate"],
                          outputs=["pilot/report.json", "pilot/report.md", "review.html", "validation.json"])))


if __name__ == "__main__":
    main()
