"""Gate-protected full-gallery manifests and aggregation; no VLM calls here."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path

from schema_models import SCHEMA_VERSION, PROMPT_VERSION, ShotTag, count_agreement

ROOT = Path(__file__).resolve().parent


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def write_jsonl(path, rows):
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))


def require_gate():
    gate = json.loads((ROOT / "pilot/report.json").read_text())["gate"]
    if not gate.get("passed"):
        raise RuntimeError("Phase 2 requires a completed, passing pilot gate")
    return gate


def prepare(selected_run):
    gate = require_gate()
    if selected_run != gate["selected_run"]:
        raise ValueError("Selected full-run model must match the pilot's model-selection gate")
    gallery = Path(json.loads((ROOT / "input_manifest.json").read_text())["gallery"])
    rows = [dict(row, image_path=str(gallery / row["frame_path"]))
            for row in read_jsonl(gallery / "gallery.jsonl")
            if row["is_representative"] or row["end_ms"] - row["start_ms"] > 4000]
    source = read_jsonl(ROOT / "pilot" / f"{selected_run}.jsonl")
    cache = {row["sha256"]: row for row in source if row["status"] in ("valid", "invalid")}
    # Cache terminal invalid responses too: two invalid attempts must not become
    # an endless repair loop when assembling or resuming the same prompt version.
    pending = {}
    for row in rows:
        if row["sha256"] not in cache:
            pending.setdefault(row["sha256"], row)
    target = ROOT / "full"
    target.mkdir(exist_ok=True)
    write_jsonl(target / "frames.jsonl", rows)
    write_jsonl(target / "missing.jsonl", list(pending.values()))
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(), "selected_pilot_run": selected_run,
        "model": source[0]["model"], "prompt_version": PROMPT_VERSION,
        "keyframes": len(rows), "shots": len({row["shot_id"] for row in rows}),
        "unique_sha256": len({row["sha256"] for row in rows}), "pilot_cache_sha256": len(cache),
        "missing_unique_sha256": len(pending),
        "selection": "All representative frames, plus every saved keyframe of shots strictly longer than 4000ms; no additional frames from shots <=4000ms.",
        "cache_key": ["image_sha256", "prompt_version", "prompt_sha256", "model", "reasoning_and_sampling_settings"],
        "gallery_sha256": hashlib.sha256((gallery / "gallery.jsonl").read_bytes()).hexdigest(),
    }
    write_json(target / "manifest.json", manifest)
    print(json.dumps(manifest, indent=2))


def prepare_exploration():
    """Freeze completed data after the user's explicit scope reduction; no calls."""
    require_gate()
    original = json.loads((ROOT / "full/manifest.json").read_text())
    rows = read_jsonl(ROOT / "full/frames.jsonl")
    cache_rows = [row for run in (original["selected_pilot_run"], "full")
                  for row in read_jsonl(ROOT / "pilot" / f"{run}.jsonl")]
    completed = {row["sha256"] for row in cache_rows if row["status"] == "valid"}
    representative_shots = {row["shot_id"] for row in rows
                             if row["is_representative"] and row["sha256"] in completed}
    selected = [row for row in rows if row["sha256"] in completed and row["shot_id"] in representative_shots]
    target = ROOT / "exploration"
    target.mkdir(exist_ok=True)
    write_jsonl(target / "frames.jsonl", selected)
    excluded = [dict(frame_id=row["frame_id"], shot_id=row["shot_id"], sha256=row["sha256"],
                     reason="no_completed_valid_tag" if row["sha256"] not in completed else "representative_not_completed")
                for row in rows if row not in selected]
    write_jsonl(target / "excluded_frames.jsonl", excluded)
    stamp = datetime.now(timezone.utc).isoformat()
    change = {
        "recorded_at": stamp,
        "user_instruction": "现在就停止新增标注，用已完成数据做探索，并注明集数偏差",
        "interpretation": "Stop new VLM annotations and do not retry interrupted calls. Continue ranking and blind-pool preparation on the completed annotated subset, explicitly reporting episode bias.",
        "original_target_keyframes": original["keyframes"], "original_target_shots": original["shots"],
        "runner_exit_code": 143, "interrupted_run_receipt": "full/interruption.json",
        "scope": "exploratory_completed_subset", "relevance_labels_created": 0,
    }
    write_json(ROOT / "scope_change.json", change)
    manifest = dict(original,
        created_at=stamp, scope="exploratory_completed_subset", source_frames="exploration/frames.jsonl",
        keyframes=len(selected), shots=len(representative_shots), unique_sha256=len({row["sha256"] for row in selected}),
        original_target_keyframes=original["keyframes"], original_target_shots=original["shots"],
        original_unique_sha256=original["unique_sha256"], original_missing_unique_sha256=original["missing_unique_sha256"],
        completed_new_calls=sum(row["run_id"] == "full" for row in cache_rows),
        completed_cache_records=len(cache_rows), completed_valid_sha256=len(completed),
        selection="All originally selected frames with completed valid primary tags, restricted to shots whose representative tag is complete. Existing completed long-shot extras are retained; no further calls or interrupted-call retries.",
        selection_bias="Full dispatch followed gallery episode order. Early episodes dominate; later episodes primarily contribute stratified pilot anchors. This is an exploratory subset, not a full-gallery or balanced evaluation.",
        episode_counts=dict(Counter(row["episode"] for row in selected if row["is_representative"])),
        keyframes_by_episode=dict(Counter(row["episode"] for row in selected)),
        interrupted_run_receipt="full/interruption.json", scope_change="scope_change.json",
        source_hashes={str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                       for path in (ROOT / "full/manifest.json", ROOT / "full/frames.jsonl",
                                    ROOT / "pilot/primary_a.jsonl", ROOT / "pilot/full.jsonl",
                                    ROOT / "scope_change.json", target / "frames.jsonl")})
    manifest.pop("missing_unique_sha256", None)
    write_json(target / "manifest.json", manifest)
    print(json.dumps(manifest, indent=2))


def flatten(value, prefix=""):
    result = {}
    if isinstance(value, dict):
        for key, child in value.items():
            result.update(flatten(child, f"{prefix}.{key}" if prefix else key))
    else:
        # Enum lists represent sets; people are ordered largest first.
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            value = sorted(value)
        result[prefix] = json.dumps(value, sort_keys=True)
    return result


def enum_values(tags):
    """Counts array enum membership per frame; person-array enums per person."""
    for field, encoded in flatten(tags).items():
        if field in ("content.location_hint", "subjects.persons"):
            continue
        value = json.loads(encoded)
        if isinstance(value, list):
            for member in value or ["<empty>"]:
                yield field, str(member)
        else:
            yield field, str(value).lower() if isinstance(value, bool) else str(value)
    for person in tags["subjects"]["persons"]:
        for field, value in person.items():
            yield "subjects.persons[]." + field, value


def assemble():
    require_gate()
    scope_directory = "exploration" if (ROOT / "exploration/manifest.json").exists() else "full"
    manifest = json.loads((ROOT / scope_directory / "manifest.json").read_text())
    rows = read_jsonl(ROOT / scope_directory / "frames.jsonl")
    run_names = [manifest["selected_pilot_run"], "full"]
    cache = {}
    for run in run_names:
        path = ROOT / "pilot" / f"{run}.jsonl"
        for row in read_jsonl(path) if path.exists() else []:
            if row["model"] != manifest["model"] or row.get("prompt_version") != PROMPT_VERSION:
                raise ValueError("Full-run result model/prompt differs from the selected pilot")
            cache[row["sha256"]] = row
    feature_rows = read_jsonl(ROOT / "full/features.jsonl")
    features = {row["frame_id"]: row for row in feature_rows}
    assert len(features) == len(feature_rows), "Duplicate full feature frame IDs"
    assert {row["frame_id"] for row in rows} <= set(features), "Feature coverage must include every selected frame"
    if scope_directory == "full":
        assert set(features) == {row["frame_id"] for row in rows}, "Feature coverage must match requested full frames"
    records = []
    distributions = defaultdict(Counter)
    enum_histograms = defaultdict(Counter)
    representative_histograms = defaultdict(Counter)
    feature_contracts = {}
    for row in rows:
        result = cache[row["sha256"]]
        feature = features[row["frame_id"]]
        assert feature["image_sha256"] == row["sha256"], "Feature/image hash mismatch"
        contract_id = feature["feature_contract_id"]
        if contract_id not in feature_contracts:
            feature_contracts[contract_id] = json.loads((ROOT / feature["contract_path"]).read_text())
        contract = feature_contracts[contract_id]
        record = dict(row, schema_version=SCHEMA_VERSION, status=result["status"],
                      prompt_version=PROMPT_VERSION, vlm_model=result["model"],
                      vlm_cache_frame_id=result["frame_id"], vlm_cache_run_id=result["run_id"],
                      raw_response_paths=[attempt["raw_path"] for attempt in result["attempts"]],
                      provenance=result.get("provenance", {}).copy(), pcp=feature["pcp"], detector=feature["detector"])
        record["provenance"]["pcp"] = {
            "source": "pcp:" + contract["pcp"]["weights_sha256"],
            "run_id": contract_id, "contract_path": feature["contract_path"],
            "cache_path": feature["cache_path"], "computed_at": feature["computed_at"],
        }
        record["provenance"]["detector"] = {
            "source": "detector:grounding-dino-tiny@" + contract["detector"]["checkpoint"]["revision"],
            "run_id": contract_id, "contract_path": feature["contract_path"],
            "cache_path": feature["cache_path"], "computed_at": feature["computed_at"],
            "device": feature["detector"]["device"],
        }
        if result["status"] == "valid":
            tags = ShotTag.model_validate(result["tags"], strict=True).model_dump(mode="json")
            record.update(tags)
            record["detector"] = dict(record["detector"], count_agreement=count_agreement(tags["subjects"]["person_count"], feature["detector"]["person_count"]))
            for field, value in flatten(tags).items():
                # All categorical, Boolean and count values are reported. Avoid
                # treating free-text hints and per-person structures as enums.
                if field not in ("content.location_hint", "subjects.persons"):
                    distributions[field][value] += 1
            for field, value in enum_values(tags):
                enum_histograms[field][value] += 1
                if row["is_representative"]:
                    representative_histograms[field][value] += 1
        records.append(record)
    write_jsonl(ROOT / "keyframe_tags.jsonl", records)
    by_shot = defaultdict(list)
    for row in records:
        by_shot[row["shot_id"]].append(row)
    gallery_directory = Path(json.loads((ROOT / "input_manifest.json").read_text())["gallery"])
    all_gallery_frames = defaultdict(list)
    for row in read_jsonl(gallery_directory / "gallery.jsonl"):
        all_gallery_frames[row["shot_id"]].append(row["frame_id"])
    originally_requested_frames = defaultdict(list)
    for row in read_jsonl(ROOT / "full/frames.jsonl"):
        originally_requested_frames[row["shot_id"]].append(row["frame_id"])
    shot_records = []
    for shot, frames in by_shot.items():
        representative, = [row for row in frames if row["is_representative"]]
        valid = [row for row in frames if row["status"] == "valid"]
        flattened = [flatten({key: row[key] for key in ShotTag.model_fields}) for row in valid]
        fields = sorted(flattened[0]) if flattened else []
        varies = [field for field in fields if len({row[field] for row in flattened}) > 1]
        selected_ids = {row["frame_id"] for row in frames}
        missing_requested = [fid for fid in originally_requested_frames[shot] if fid not in selected_ids]
        shot_records.append({"shot_id": shot, "representative_frame_id": representative["frame_id"],
                             "keyframe_ids": [row["frame_id"] for row in frames], "tags": representative,
                             "varies_within_shot": varies, "variation_compared_valid_keyframes": len(valid),
                             "variation_incomplete": len(valid) < len(frames) or bool(missing_requested),
                             "untagged_requested_keyframe_ids": missing_requested,
                             "untagged_gallery_keyframe_ids": [fid for fid in all_gallery_frames[shot] if fid not in selected_ids],
                             "variation_observation": "insufficient_keyframes" if len(valid) < 2 else "observed_difference" if varies else "no_observed_difference"})
    write_jsonl(ROOT / "shot_tags.jsonl", shot_records)
    valid_count = sum(row["status"] == "valid" for row in records)
    summary = {
        "scope": manifest.get("scope", "full_gallery"), "scope_manifest": f"{scope_directory}/manifest.json",
        "selection_bias": manifest.get("selection_bias"),
        "status_counts": dict(Counter(row["status"] for row in records)),
        "valid_rate": valid_count / len(records), "keyframes": len(records), "shots": len(shot_records),
        "enum_histograms": dict(enum_histograms),
        "enum_combination_histograms": dict(distributions),
        "representative_enum_histograms": dict(representative_histograms),
        "valid_representatives": sum(row["is_representative"] and row["status"] == "valid" for row in records),
        "unknown_rates": {field: counts.get('"unknown"', 0) / valid_count if valid_count else None
                          for field, counts in distributions.items()},
        "count_agreement": dict(Counter(row["detector"]["count_agreement"] for row in records if row["status"] == "valid")),
        "shots_with_variation": sum(bool(row["varies_within_shot"]) for row in shot_records),
        "share_shots_with_variation": sum(bool(row["varies_within_shot"]) for row in shot_records) / len(shot_records),
        "variation_incomplete_shots": sum(row["variation_incomplete"] for row in shot_records),
        "variation_observation_counts": dict(Counter(row["variation_observation"] for row in shot_records)),
        "shots_with_untagged_gallery_extras": sum(bool(row["untagged_gallery_keyframe_ids"]) for row in shot_records),
        "notes": ["VLM distributions and cross-frame disagreement are model observations, not accuracy or relevance judgments.",
                  "Enum histograms count list membership per frame (empty lists have an <empty> bucket), except subjects.persons[] enums which count individual annotated people. Combination histograms retain complete lists.",
                  "Representative histograms count one frame per shot. All-keyframe histograms also include long-shot extras. Valid/unknown denominators exclude invalid/API-error records."]}
    summary["notes"].append("An empty varies_within_shot list is not proof of stability: variation_observation marks shots with fewer than two valid tagged frames. untagged_gallery_keyframe_ids records every unused saved extra; untagged_requested_keyframe_ids distinguishes originally requested long-shot frames left uncompleted after the user's stop. Missing requested frames make variation_incomplete true.")
    write_json(ROOT / "distributions.json", summary)
    tables = []
    for field, counts in enum_histograms.items():
        axis_max = max(valid_count, max(counts.values(), default=0))
        body = "".join(f"<tr><td>{html.escape(value)}</td><td>{n}</td><td><meter min='0' max='{axis_max}' value='{n}'></meter></td></tr>" for value, n in counts.most_common())
        tables.append(f"<section><h2>{html.escape(field)}</h2><table>{body}</table></section>")
    (ROOT / "distributions.html").write_text("<!doctype html><meta charset='utf-8'><title>Tag distributions</title><style>body{font:16px system-ui;max-width:1100px;margin:30px auto}section{break-inside:avoid;border:1px solid #ccc;padding:15px;margin:15px 0}td{padding:4px 12px}meter{width:300px}</style><h1>VLM tag distributions</h1><p>Model observations, not accuracy or relevance judgments. Counts cover all valid tagged keyframes, including long-shot extras. List fields count membership per frame; subjects.persons[] fields count individual annotated people. Representative-only and combination histograms are in distributions.json.</p>" + "".join(tables))
    print(json.dumps({key: value for key, value in summary.items() if "histograms" not in key and key != "unknown_rates"}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "prepare-exploration", "assemble"])
    parser.add_argument("--selected-run", choices=["primary_a", "cheap"])
    args = parser.parse_args()
    if args.action == "prepare":
        if not args.selected_run:
            parser.error("--selected-run is required for prepare")
        prepare(args.selected_run)
    elif args.action == "prepare-exploration":
        prepare_exploration()
    else:
        assemble()
