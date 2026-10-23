"""Independent final artifact audit; never performs inference, judging, or scoring.

Run only after exploratory assembly, query/ranking/pool generation, README finalization,
and all independent audit receipts exist. Missing evidence cannot pass validation.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess
from urllib.parse import unquote, urlsplit

from schema_models import PROMPT_VERSION, SCHEMA_VERSION, ShotTag, count_agreement

OUT = Path(__file__).resolve().parent
EXPECTED_FRAMES, EXPECTED_SHOTS, EXPECTED_UNIQUE = 8147, 5898, 8092
ANCHORS = ("mygo-ep01-s00152-f2", "mygo-ep07-s00056-f1", "mygo-ep10-s00336-f2", "mygo-ep10-s00337-f2")
METHODS = ("M0", "M1", "M2", "M3", "M4", "M5")
RECEIPTS = {
    "vlm": "full/audit-final.json",
    "features": "full/features-validation.json",
    "rankings": "rankings/validation.json",
}
DELIVERABLES = [
    "README.md", "input_manifest.json", "prompt/shot-tag-v1.md", "prompt/shot-tag-v1.schema.json",
    "prompt/schema-source.md", "schema_models.py", "prepare_pilot.py", "tag_vlm.py", "features.py",
    "full_gallery.py", "select_queries.py", "rank.py", "build_pool.py", "build_judge.py", "score.py",
    "pilot_report.py", "validate_delivery.py", "audit_full.py", "validate_features.py", "validate_rankings.py", "pilot/frames.jsonl", "pilot/sampling.json",
    "pilot/report.json", "pilot/report.md", "pilot/validation.json", "pilot/features.jsonl", "review.html",
    "pilot/primary_a.jsonl", "pilot/primary_b.jsonl", "pilot/cheap.jsonl", "pilot/full.jsonl",
    "pilot/primary_a-summary.json", "pilot/primary_b-summary.json", "pilot/cheap-summary.json", "pilot/full-summary.json",
    "pilot/primary_smoke.jsonl", "pilot/cheap_smoke.jsonl", "pilot/primary_smoke-summary.json", "pilot/cheap_smoke-summary.json",
    "full/frames.jsonl", "full/missing.jsonl", "full/manifest.json", "full/features.jsonl",
    "exploration/manifest.json", "exploration/frames.jsonl", "scope_change.json", "full/interruption.json",
    "features/runs/full_features.jsonl.json", "keyframe_tags.jsonl", "shot_tags.jsonl",
    "distributions.json", "distributions.html", "queries.json", "queries.html", "rankings/run.json",
    "rankings/methods.json", "rankings/sanity_reference_ranks.json", "method_components.json", "pool.json", "judge.html",
    *RECEIPTS.values(),
]


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result
    def constant(value):
        raise ValueError(f"Non-finite JSON constant: {value}")
    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def read_json(path):
    return strict_json(Path(path).read_text())


def read_rows(path):
    return [strict_json(line) for line in Path(path).read_text().splitlines() if line.strip()]


def keyed(rows, field):
    result = {row[field]: row for row in rows}
    if len(result) != len(rows):
        raise ValueError(f"Duplicate {field}")
    return result


def require(condition, message):
    if not condition:
        raise ValueError(message)


def leaf_values(value, path=""):
    """Compare enum lists as sets; preserve the order of annotated person records."""
    if isinstance(value, dict):
        return {key: item for name, child in value.items()
                for key, item in leaf_values(child, f"{path}.{name}" if path else name).items()}
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        value = sorted(value)
    return {path: json.dumps(value, sort_keys=True, ensure_ascii=False)}


class Audit:
    def __init__(self):
        self.checks, self.files, self.context = [], {}, {}

    def sha(self, path):
        path = Path(path).resolve()
        if path not in self.files:
            before = path.stat()
            hasher = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    hasher.update(chunk)
            after = path.stat()
            require((before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns), f"File changed during hashing: {path}")
            self.files[path] = dict(sha256=hasher.hexdigest(), bytes=after.st_size, mtime_ns=after.st_mtime_ns)
        return self.files[path]["sha256"]

    def local(self, relative):
        path = (OUT / relative).resolve()
        require(path.is_relative_to(OUT), f"Provenance path escapes experiment: {relative}")
        return path

    def stage(self, name, function):
        try:
            details = function()
            self.checks.append(dict(name=name, status="passed", details=details))
        except Exception as error:
            self.checks.append(dict(name=name, status="failed", error=f"{type(error).__name__}: {error}"))

    def pilot_archive(self):
        archive = read_json(OUT / "pilot/validation.json")
        require(archive["stage"] == "pilot" and archive["status"] == "passed" and archive["pilot_gate"]["passed"], "Pilot archive is not a passing pilot result")
        for group in ("source_hashes", "output_hashes"):
            for relative, expected in archive[group].items():
                require(self.sha(self.local(relative)) == expected, f"Archived pilot {group} mismatch: {relative}")
        return dict(path="pilot/validation.json", sha256=self.sha(OUT / "pilot/validation.json"))

    def gallery_and_tags(self):
        inputs = read_json(OUT / "input_manifest.json")
        original_manifest = read_json(OUT / "full/manifest.json")
        manifest = read_json(OUT / "exploration/manifest.json")
        scope_change = read_json(OUT / "scope_change.json")
        require(manifest["scope"] == scope_change["scope"] == "exploratory_completed_subset", "Authorized exploratory scope is missing")
        require(manifest["selection_bias"] and scope_change["relevance_labels_created"] == 0, "Subset limitation/authorization is missing")
        for relative, expected_hash in manifest["source_hashes"].items():
            require(self.sha(self.local(relative)) == expected_hash, f"Exploratory selection source hash differs: {relative}")
        pilot = read_json(OUT / "pilot/report.json")
        require(pilot["gate"]["passed"] and manifest["selected_pilot_run"] == original_manifest["selected_pilot_run"] == pilot["gate"]["selected_run"], "Selected model differs from passing pilot gate")
        gallery = Path(inputs["gallery"])
        require(self.sha(gallery / "gallery.jsonl") == inputs["gallery_sha256"] == manifest["gallery_sha256"] == original_manifest["gallery_sha256"], "Source gallery hash changed")
        require(read_json(gallery / "validation.json")["status"] == "passed", "Source gallery validation is not passed")
        source_rows = read_rows(gallery / "gallery.jsonl")
        all_source = keyed(source_rows, "frame_id")
        original_expected = {row["frame_id"]: row for row in source_rows if row["is_representative"] or row["end_ms"] - row["start_ms"] > 4000}
        original_frames = keyed(read_rows(OUT / "full/frames.jsonl"), "frame_id")
        frames = keyed(read_rows(OUT / "exploration/frames.jsonl"), "frame_id")
        tags = keyed(read_rows(OUT / "keyframe_tags.jsonl"), "frame_id")
        features = keyed(read_rows(OUT / "full/features.jsonl"), "frame_id")
        require(set(original_expected) == set(original_frames) == set(features), "Original gallery/full manifest/feature frame sets differ")
        require(len(original_expected) == original_manifest["keyframes"] == manifest["original_target_keyframes"] == EXPECTED_FRAMES, "Original keyframe target differs")
        require(original_manifest["shots"] == manifest["original_target_shots"] == EXPECTED_SHOTS, "Original shot target differs")
        require(len({row["sha256"] for row in original_frames.values()}) == original_manifest["unique_sha256"] == manifest["original_unique_sha256"] == EXPECTED_UNIQUE, "Original unique image target differs")
        completed_records = [*read_rows(OUT / f"pilot/{manifest['selected_pilot_run']}.jsonl"), *read_rows(OUT / "pilot/full.jsonl")]
        completed_by_sha = keyed(completed_records, "sha256")
        valid_sha = {sha for sha, row in completed_by_sha.items() if row["status"] == "valid"}
        eligible_shots = {row["shot_id"] for row in original_frames.values() if row["is_representative"] and row["sha256"] in valid_sha}
        expected = {fid: row for fid, row in original_expected.items() if row["shot_id"] in eligible_shots and row["sha256"] in valid_sha}
        require(set(expected) == set(frames) == set(tags), "Exploratory selection differs from all completed frames whose representative is complete")
        require(len(expected) == manifest["keyframes"], "Exploratory keyframe count differs")
        require(len(completed_records) == manifest["completed_cache_records"] and len(valid_sha) == manifest["completed_valid_sha256"], "Completed cache count differs")
        representatives = Counter(row["shot_id"] for row in tags.values() if row["is_representative"])
        require(len(representatives) == manifest["shots"] and set(representatives.values()) == {1}, "Exactly one representative per selected shot is required")
        require(len({row["sha256"] for row in tags.values()}) == manifest["unique_sha256"], "Selected unique image count differs")
        require({str(ep): count for ep, count in Counter(row["episode"] for row in tags.values()).items()} == manifest["keyframes_by_episode"], "Selected keyframe episode counts differ")
        require({str(ep): count for ep, count in Counter(row["episode"] for row in tags.values() if row["is_representative"]).items()} == manifest["episode_counts"], "Selected shot episode counts differ")
        source_identity_keys = ("shot_id", "episode", "start_ms", "end_ms", "frame_timestamp_ms", "frame_path", "sha256", "is_representative", "reuse_group_id", "event_ids", "is_op_ed", "low_information", "internal_motion")
        for fid, source in original_expected.items():
            require(all(source[key] == original_frames[fid][key] == features[fid][key] for key in source_identity_keys), f"{fid}: original feature/source identity mismatch")
            require(features[fid]["image_sha256"] == source["sha256"], f"{fid}: original feature image digest mismatch")
        valid, statuses, contracts = {}, Counter(), {}
        for fid, source in expected.items():
            frame, tag, feature = frames[fid], tags[fid], features[fid]
            for key in source_identity_keys:
                require(source[key] == frame[key] == tag[key] == feature[key], f"{fid}: source identity mismatch for {key}")
            image = (gallery / source["frame_path"]).resolve()
            require(Path(frame["image_path"]).resolve() == image == Path(tag["image_path"]).resolve(), f"{fid}: image path mismatch")
            require(self.sha(image) == source["sha256"] == feature["image_sha256"], f"{fid}: JPEG SHA mismatch")
            require(tag["schema_version"] == SCHEMA_VERSION and tag["prompt_version"] == PROMPT_VERSION and tag["vlm_model"] == manifest["model"], f"{fid}: model/schema/prompt mismatch")
            statuses[tag["status"]] += 1
            require(tag["status"] == "valid", f"{fid}: exploratory selection must contain only completed valid tags")
            if tag["status"] == "valid":
                valid[fid] = ShotTag.model_validate({group: tag[group] for group in ShotTag.model_fields}, strict=True).model_dump(mode="json")
                require(tag["detector"]["count_agreement"] == count_agreement(tag["subjects"]["person_count"], feature["detector"]["person_count"]), f"{fid}: count agreement mismatch")
            else:
                require(tag["status"] in {"invalid", "api_error"} and not any(group in tag for group in ShotTag.model_fields), f"{fid}: invalid status/tag contract")
            require(tag["pcp"] == feature["pcp"], f"{fid}: PCP feature changed during assembly")
            require({key: value for key, value in tag["detector"].items() if key != "count_agreement"} == feature["detector"], f"{fid}: detector feature changed during assembly")
            contract_path = self.local(feature["contract_path"])
            contract_id = feature["feature_contract_id"]
            if contract_id not in contracts:
                contract = read_json(contract_path)
                require(hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()).hexdigest() == contract_id, "Feature contract digest mismatch")
                self.sha(contract_path)
                contracts[contract_id] = contract
            cache_path = self.local(feature["cache_path"])
            require(self.sha(cache_path) == feature["cache_sha256"], f"{fid}: feature cache digest mismatch")
            cached = read_json(cache_path)
            require(all(feature[key] == value for key, value in cached.items()), f"{fid}: feature cache contents mismatch")
            for group in ("pcp", "detector"):
                provenance = tag["provenance"][group]
                require(provenance["run_id"] == contract_id and provenance["contract_path"] == feature["contract_path"] and provenance["cache_path"] == feature["cache_path"], f"{fid}: feature provenance mismatch")
                require(provenance["computed_at"] == feature["computed_at"], f"{fid}: feature provenance timestamp differs")
            require(tag["provenance"]["pcp"]["source"] == "pcp:" + contracts[contract_id]["pcp"]["weights_sha256"], f"{fid}: PCP model provenance differs")
            require(tag["provenance"]["detector"]["source"] == "detector:grounding-dino-tiny@" + contracts[contract_id]["detector"]["checkpoint"]["revision"], f"{fid}: detector model provenance differs")
            require(tag["provenance"]["detector"]["device"] == feature["detector"]["device"] == contracts[contract_id]["runtime"]["device"], f"{fid}: detector device provenance differs")
        self.context.update(gallery=gallery, all_source=all_source, expected=expected, frames=frames, tags=tags,
                            original_frames=original_frames, original_manifest=original_manifest, completed_by_sha=completed_by_sha,
                            features=features, valid=valid, manifest=manifest, statuses=dict(statuses))
        return dict(scope=manifest["scope"], original_full_tagging_complete=False, keyframes=len(tags), shots=len(representatives), unique_jpeg_sha256=manifest["unique_sha256"],
                    original_target_keyframes=EXPECTED_FRAMES, original_target_shots=EXPECTED_SHOTS, limitation=manifest["selection_bias"],
                    strict_valid_keyframes=len(valid), status_counts=dict(statuses), feature_contracts=list(contracts))

    def vlm_provenance(self):
        manifest, tags = self.context["manifest"], self.context["tags"]
        interruption = read_json(OUT / "full/interruption.json")
        selected = manifest["selected_pilot_run"]
        source_runs = {run: keyed(read_rows(OUT / f"pilot/{run}.jsonl"), "frame_id") for run in (selected, "full")}
        require(len(source_runs[selected]) == 200 and len(source_runs["full"]) == manifest["completed_new_calls"] == interruption["completed_full_rows"], "Completed VLM call coverage differs from interrupted manifest")
        by_sha = {}
        raw_directories = set()
        for run, records in source_runs.items():
            summary = read_json(OUT / f"pilot/{run}-summary.json")
            expected_status = "running" if run == "full" else "complete"
            require(summary["status"] == expected_status and summary["completed"] == len(records), f"{run}: retained run summary differs from completed evidence")
            require(summary["counts"] == dict(Counter(row["status"] for row in records.values())), f"{run}: summary counts mismatch")
            for record in records.values():
                require(record["sha256"] not in by_sha, "Pilot and full calls duplicate an image SHA")
                by_sha[record["sha256"]] = record
                require(not record.get("runner_error"), "Runner error has no complete inference provenance")
                require(record["prompt_sha256"] == self.sha(OUT / "prompt/shot-tag-v1.md"), "VLM prompt digest mismatch")
                for attempt in record["attempts"]:
                    directory = self.local(attempt["raw_path"])
                    raw_directories.add(directory)
                    require(read_json(directory / "call.json") == attempt, "Raw call metadata differs from VLM row")
                    request = read_json(directory / "request.json")
                    require(request["image_sha256"] == record["sha256"] and request["prompt_sha256"] == record["prompt_sha256"], "Raw request source mismatch")
                    required = ["request.json", "call.json"]
                    if attempt["status"] in {"valid", "invalid"}:
                        required += ["response.txt", "response.json", "events.jsonl", "effective-thread-options.json", "effective-turn-input.json"]
                    if attempt["status"] == "valid":
                        required.append("parsed.json")
                        original = ShotTag.model_validate(strict_json((directory / "response.txt").read_text()), strict=True).model_dump(mode="json")
                        require(original == read_json(directory / "parsed.json") == record["tags"], "Original/parsed/VLM tags differ")
                    elif attempt["status"] == "invalid":
                        required.append("validation-errors.json")
                    for name in required:
                        self.sha(directory / name)
                    for file in directory.iterdir():
                        if file.is_file():
                            self.sha(file)
        require({row["sha256"] for row in tags.values()} <= set(by_sha), "VLM cache does not cover every selected image SHA")
        require(len(by_sha) == manifest["completed_cache_records"], "Completed VLM cache coverage differs")
        pending = interruption["interrupted_attempts"]
        require(len(pending) == interruption["interrupted_raw_attempts"], "Interrupted raw attempt count differs")
        for attempt in pending:
            directory = self.local(attempt["raw_path"])
            require(directory not in raw_directories and attempt["sha256"] not in by_sha, "Interrupted request was consumed as a complete result")
            require(not any((directory / name).exists() for name in ("call.json", "response.txt", "parsed.json")), "Interrupted raw attempt now contains an unexpected completed result")
            for name, expected_hash in attempt["retained_file_sha256"].items():
                require(self.sha(directory / name) == expected_hash, "Interrupted raw artifact changed")
        for fid, tag in tags.items():
            record = by_sha[tag["sha256"]]
            require(tag["vlm_cache_frame_id"] == record["frame_id"] and tag["vlm_cache_run_id"] == record["run_id"] and tag["status"] == record["status"], f"{fid}: VLM cache identity mismatch")
            require(tag["raw_response_paths"] == [attempt["raw_path"] for attempt in record["attempts"]], f"{fid}: raw response paths mismatch")
            if tag["status"] == "valid":
                require({group: tag[group] for group in ShotTag.model_fields} == record["tags"], f"{fid}: assembled VLM tags changed")
            for group in ShotTag.model_fields:
                require(tag["provenance"][group] == record["provenance"][group], f"{fid}: VLM provenance changed")
        return dict(unique_image_results=len(by_sha), selected_unique_images=manifest["unique_sha256"], raw_attempt_directories=len(raw_directories),
                    interrupted_unconsumed_attempts=len(pending), original_run_complete=False,
                    untouched_response_equality_checked=True)

    def aggregates_and_anchors(self):
        tags, valid = self.context["tags"], self.context["valid"]
        shots = keyed(read_rows(OUT / "shot_tags.jsonl"), "shot_id")
        by_shot, gallery_shots, requested_shots = defaultdict(list), defaultdict(list), defaultdict(list)
        for row in tags.values():
            by_shot[row["shot_id"]].append(row)
        for row in self.context["all_source"].values():
            gallery_shots[row["shot_id"]].append(row["frame_id"])
        for row in self.context["original_frames"].values():
            requested_shots[row["shot_id"]].append(row["frame_id"])
        require(set(shots) == set(by_shot) and len(shots) == self.context["manifest"]["shots"], "Shot aggregate coverage differs")
        variation_counts, varied, incomplete = Counter(), 0, 0
        for sid, frames in by_shot.items():
            result = shots[sid]
            representative, = [frame for frame in frames if frame["is_representative"]]
            require(result["representative_frame_id"] == representative["frame_id"] and result["tags"] == representative, f"{sid}: aggregate representative differs")
            ids = [frame["frame_id"] for frame in frames]
            require(result["keyframe_ids"] == ids, f"{sid}: aggregate keyframe order differs")
            comparisons = [leaf_values(valid[fid]) for fid in ids if fid in valid]
            variation = sorted(key for key in comparisons[0] if len({row[key] for row in comparisons}) > 1) if comparisons else []
            observation = "insufficient_keyframes" if len(comparisons) < 2 else "observed_difference" if variation else "no_observed_difference"
            require(result["varies_within_shot"] == variation and result["variation_compared_valid_keyframes"] == len(comparisons), f"{sid}: variation fields differ")
            missing_requested = [fid for fid in requested_shots[sid] if fid not in ids]
            require(result["untagged_requested_keyframe_ids"] == missing_requested, f"{sid}: uncompleted originally requested frames differ")
            require(result["variation_incomplete"] == (len(comparisons) < len(frames) or bool(missing_requested)) and result["variation_observation"] == observation, f"{sid}: variation coverage differs")
            require(result["untagged_gallery_keyframe_ids"] == [fid for fid in gallery_shots[sid] if fid not in ids], f"{sid}: skipped gallery extras differ")
            variation_counts[observation] += 1
            varied += bool(variation)
            incomplete += result["variation_incomplete"]
        distributions = read_json(OUT / "distributions.json")
        require(distributions["keyframes"] == len(tags) and distributions["shots"] == len(shots), "Distribution counts differ")
        require(distributions["status_counts"] == self.context["statuses"] and distributions["variation_observation_counts"] == dict(variation_counts), "Distribution status/variation counts differ")
        require(distributions["shots_with_variation"] == varied and distributions["share_shots_with_variation"] == varied / len(shots), "Distribution variation share differs")
        require(distributions["valid_rate"] == len(valid) / len(tags), "Distribution schema-valid rate differs")
        require(distributions["variation_incomplete_shots"] == incomplete and distributions["scope"] == self.context["manifest"]["scope"], "Distribution scope/incomplete variation count differs")
        for fid in ANCHORS:
            require(fid in tags and tags[fid]["is_representative"], f"Missing required anchor: {fid}")
        require(tags[ANCHORS[0]]["start_ms"] <= 542300 and tags[ANCHORS[0]]["end_ms"] >= 544300, "Episode 1 reference interval is outside its anchor shot")
        require(tags[ANCHORS[1]]["start_ms"] <= 248600 < tags[ANCHORS[1]]["end_ms"], "Episode 7 candidate instant is outside its anchor shot")
        ep10 = {row["shot_id"] for row in tags.values() if row["episode"] == 10 and row["start_ms"] < 935600 and 930300 < row["end_ms"]}
        require(ep10 == {"mygo-ep10-s00336", "mygo-ep10-s00337"}, "Episode 10 requested interval no longer maps to both required shots")
        return dict(shots=len(shots), variation_observation_counts=dict(variation_counts), variation_incomplete_shots=incomplete, anchors=list(ANCHORS), ep10_interval_shots=sorted(ep10))

    def all_raw_files(self):
        # Primary A plus full were checked against assembled tags above. Audit
        # the remaining independent pilot/smoke responses and retain all files.
        extra_runs = ("primary_b", "cheap", "primary_smoke", "cheap_smoke")
        checked_attempts = 0
        for run in extra_runs:
            records = read_rows(OUT / f"pilot/{run}.jsonl")
            summary = read_json(OUT / f"pilot/{run}-summary.json")
            require(summary["status"] == "complete" and summary["completed"] == len(records), f"{run}: incomplete auxiliary run")
            for row in records:
                for attempt in row["attempts"]:
                    directory = self.local(attempt["raw_path"])
                    require(read_json(directory / "call.json") == attempt, f"{run}: auxiliary raw metadata differs")
                    request = read_json(directory / "request.json")
                    require(request["image_sha256"] == row["sha256"] and self.sha(Path(request["image_path"])) == row["sha256"], f"{run}: auxiliary source image differs")
                    if attempt["status"] == "valid":
                        original = ShotTag.model_validate(strict_json((directory / "response.txt").read_text()), strict=True).model_dump(mode="json")
                        require(original == row["tags"] == read_json(directory / "parsed.json"), f"{run}: auxiliary original/parsed/tags differ")
                    for name in ("request.json", "call.json"):
                        self.sha(directory / name)
                    checked_attempts += 1
        count, total_bytes = 0, 0
        for path in (OUT / "raw").rglob("*"):
            if path.is_file():
                require(path.resolve().is_relative_to(OUT), "Raw provenance symlink leaves experiment")
                self.sha(path)
                total_bytes += self.files[path.resolve()]["bytes"]
                count += 1
        require(count > 0, "Raw provenance directory is empty")
        return dict(raw_files_hashed=count, raw_bytes=total_bytes, auxiliary_attempts_checked=checked_attempts)

    def queries_rankings_pool(self):
        payload, pool = read_json(OUT / "queries.json"), read_json(OUT / "pool.json")
        queries, alternates = payload["queries"], payload["alternates"]
        require(len(queries) == 25 and len(alternates) == 10, "Expected 25 queries and 10 alternates")
        require(len({row["query_shot_id"] for row in queries + alternates}) == 35, "Queries and alternates overlap or duplicate shots")
        require("mygo-ep01-s00152" in {row["query_shot_id"] for row in queries}, "Mandatory Episode 1 query missing")
        for row in queries + alternates:
            source = self.context["tags"][row["frame_id"]]
            require(source["shot_id"] == row["query_shot_id"] and source["status"] == "valid" and source["frame"]["kind"] == "story", "Query/alternate is not a valid story frame")
            require(not source["is_op_ed"] and not source["low_information"], "Excluded OP/ED or low-information query")
            require(row["reason"] and row["coverage_features"] is not None, "Missing query selection reason/coverage")
            require(row["tag_observations"] == {group: source[group] for group in ShotTag.model_fields}, "Query tag observations differ from source tags")
            require((OUT / row["image_path"]).resolve() == Path(source["image_path"]).resolve() and row["sha256"] == source["sha256"], "Query image provenance differs")
            if not source["is_representative"]:
                require(row.get("representative_override") or any(item.get("frame_id") == source["frame_id"] for item in payload.get("representative_overrides", [])), "Undocumented nonrepresentative query override")
        qids = {row["query_shot_id"] for row in queries}
        pool_by_id = keyed(pool["queries"], "query_shot_id")
        require(set(pool_by_id) == qids, "Pool and query IDs differ")
        metadata = read_json(OUT / "method_components.json")
        for relative, expected in payload["source_hashes"].items():
            require(self.sha(self.local(relative)) == expected, f"Query selection source hash differs: {relative}")
        require(metadata["pool_sha256"] == self.sha(OUT / "pool.json") and metadata["source_hashes"]["queries"] == self.sha(OUT / "queries.json"), "Pool provenance hash mismatch")
        require(metadata["methods"] == list(METHODS), "Required M0–M5 methods differ")
        ranking_run = read_json(OUT / "rankings/run.json")
        require(ranking_run["status"] == "complete" and ranking_run["query_count"] == 25 and ranking_run["input_keyframes"] == self.context["manifest"]["keyframes"], "Ranking run is incomplete for exploratory scope")
        for artifact in (ranking_run, metadata):
            scope = artifact["scope"]
            require(scope["kind"] == "exploratory_completed_subset" and scope["annotated_keyframes"] == self.context["manifest"]["keyframes"] and scope["annotated_shots"] == self.context["manifest"]["shots"], "Ranking/pool scope differs from selection")
            require(scope["manifest_sha256"] == self.sha(OUT / "exploration/manifest.json") and scope["frames_sha256"] == self.sha(OUT / "exploration/frames.jsonl"), "Ranking/pool scope hashes differ")
        for key, path in {"keyframe_tags": OUT / "keyframe_tags.jsonl", "queries": OUT / "queries.json",
                          "gallery": self.context["gallery"] / "gallery.jsonl", "embeddings": self.context["gallery"] / "embeddings.npy",
                          "embedding_rows": self.context["gallery"] / "embedding_rows.jsonl", "runner": OUT / "rank.py", "methods": OUT / "rankings/methods.json",
                          "selection_manifest": OUT / "exploration/manifest.json", "selection_frames": OUT / "exploration/frames.jsonl"}.items():
            require(ranking_run["source_hashes"][key] == self.sha(path), f"Ranking source hash mismatch: {key}")
        pairs = 0
        for qid, query in pool_by_id.items():
            require(query["candidates"], f"Empty judging pool for {qid}")
            aliases = set()
            keyed(query["candidates"], "candidate_shot_id")
            for candidate in query["candidates"]:
                require(not ({"labels", "category", "layout", "method", "rank", "score"} & candidate.keys()), "Pool contains labels or unblinded method fields")
                cluster = candidate["collapsed_shot_ids"]
                require(candidate["candidate_shot_id"] in cluster and not aliases.intersection(cluster), "Pool aliases overlap or omit canonical candidate")
                aliases.update(cluster)
                source = self.context["tags"][candidate["frame_id"]]
                require(source["shot_id"] == candidate["candidate_shot_id"], "Pool candidate frame belongs to another shot")
                require((OUT / candidate["image_path"]).is_file(), "Pool image is missing")
            pairs += len(query["candidates"])
            for method in METHODS:
                path = OUT / "rankings" / method / f"{qid}.json"
                key = str(path.relative_to(OUT))
                require(self.sha(path) == metadata["source_hashes"]["rankings"][key], "Ranking changed since pool construction")
                ranking = read_json(path)
                require(ranking["method"] == method and ranking["query_shot_id"] == qid, "Ranking identity differs")
                require(len({row["shot_id"] for row in ranking["rows"]}) == len(ranking["rows"]), "Ranking contains repeated shot")
                require([row["rank"] for row in ranking["rows"]] == list(range(1, len(ranking["rows"]) + 1)), "Ranking positions are not contiguous")
                require({row["shot_id"] for row in ranking["rows"][:10]} <= aliases, "Original top10 candidate is absent from pooled aliases")
                self.sha(self.local(ranking["component_audit"]))
        self.context.update(queries=payload, pool=pool)
        return dict(queries=25, alternates=10, methods=list(METHODS), ranking_files=25 * len(METHODS), judging_pairs=pairs)

    def receipts(self):
        receipts = {name: read_json(OUT / path) for name, path in RECEIPTS.items()}
        vlm, feature, ranking = receipts["vlm"], receipts["features"], receipts["rankings"]
        manifest = self.context["manifest"]
        interruption = read_json(OUT / "full/interruption.json")
        require(vlm["status"] == interruption["status"] == "interrupted_by_user" and vlm["integrity_status"] == interruption["integrity_status"] == "passed" and not vlm["artifact_audit_errors"] and not vlm["runner_error_frames"], "Interrupted VLM integrity audit is incomplete or contains errors")
        require(vlm["audited_rows"] == vlm["completed"] == vlm["completed_full_rows"] == manifest["completed_new_calls"] and vlm["selected_pilot_audited_rows"] == 200 and vlm["audited_unique_sha256"] == manifest["completed_cache_records"], "Completed VLM audit coverage differs")
        require(vlm["original_source_keyframes"] == EXPECTED_FRAMES and vlm["original_source_shots"] == EXPECTED_SHOTS and vlm["original_target_frames"] == self.context["original_manifest"]["missing_unique_sha256"], "VLM original target differs")
        covered = sum(row["sha256"] in self.context["completed_by_sha"] for row in self.context["original_frames"].values())
        require(vlm["covered_source_keyframes"] == vlm["valid_source_keyframes"] == covered, "VLM completed source coverage differs")
        require(vlm["no_new_calls_or_replay"] and interruption["control"]["no_new_calls_after_stop"] and interruption["control"]["no_retries_or_replay"], "Stopped-run control receipt differs")
        require(vlm["interrupted_raw_attempts"] == interruption["interrupted_raw_attempts"] and set(vlm["interrupted_raw_paths"]) == {row["raw_path"] for row in interruption["interrupted_attempts"]}, "Interrupted attempt receipts differ")
        for relative, expected in vlm["source_hashes"].items():
            require(self.sha(self.local(relative)) == expected, f"VLM audit source hash differs: {relative}")
        require(feature["status"] == "passed" and feature["input_rows"] == feature["audited_rows"] == EXPECTED_FRAMES and feature["unique_images"] == EXPECTED_UNIQUE, "Feature audit is incomplete or lacks full coverage")
        require(feature["input_sha256"] == self.sha(OUT / "full/frames.jsonl") and feature["features_sha256"] == self.sha(OUT / "full/features.jsonl"), "Feature audit source hashes differ")
        feature_run = read_json(OUT / "features/runs/full_features.jsonl.json")
        require(feature_run["status"] == "complete" and feature_run["input_rows"] == feature_run["completed_rows"] == EXPECTED_FRAMES, "Feature runner receipt is incomplete")
        require(feature_run["input_sha256"] == feature["input_sha256"] and feature_run["output_sha256"] == feature["features_sha256"], "Feature runner/auditor hashes differ")
        require(ranking["status"] == "passed", "Ranking/pool audit is not passed")
        require(ranking["scope"]["kind"] == manifest["scope"] and ranking["scope"]["annotated_keyframes"] == manifest["keyframes"] and ranking["scope"]["annotated_shots"] == manifest["shots"], "Ranking audit scope differs")
        require(ranking["query_count"] == 25 and ranking["candidate_pairs"] == sum(len(query["candidates"]) for query in self.context["pool"]["queries"]), "Ranking/pool audit coverage differs")
        require(ranking.get("hashes"), "Ranking/pool audit lacks source hashes")
        for relative, expected in ranking["hashes"].items():
            require(self.sha(self.local(relative)) == expected, f"Ranking/pool audit source hash differs: {relative}")
        return {name: dict(path=RECEIPTS[name], sha256=self.sha(OUT / RECEIPTS[name]), status=value["status"]) for name, value in receipts.items()}

    def html_and_no_judgments(self):
        parser_results = {}
        for name in ("review.html", "distributions.html", "queries.html", "judge.html"):
            parser = StaticHTML()
            parser.feed((OUT / name).read_text())
            require(not parser.external_urls, f"{name}: external resources/links are not offline")
            for path in parser.local_urls:
                resolved = (OUT / unquote(urlsplit(path).path)).resolve()
                require(resolved.is_file(), f"{name}: missing local reference {path}")
                if resolved != OUT / "validation.json":
                    self.sha(resolved)
            for script in parser.scripts:
                subprocess.run(["node", "--check"], input=script, text=True, capture_output=True, check=True)
            for data in parser.json_data:
                for relative in image_paths(data):
                    require((OUT / unquote(relative)).is_file(), f"{name}: missing dynamic image reference {relative}")
            if name == "review.html":
                require(len(parser.json_data) == 1 and len(parser.json_data[0]["cards"]) == 200, "Pilot review must contain 200 cards")
            if name == "judge.html":
                expected = {"queries": [{"query_shot_id": query["query_shot_id"], "image_path": query["image_path"],
                                          "candidates": [{"candidate_shot_id": candidate["candidate_shot_id"], "image_path": candidate["image_path"]} for candidate in query["candidates"]]}
                                         for query in self.context["pool"]["queries"]]}
                require(parser.json_data == [expected], "Judge page data differs from blinded pool or contains extra fields")
            parser_results[name] = dict(inline_scripts_checked=len(parser.scripts), local_references=len(parser.local_urls))
        forbidden = sorted(str(path.relative_to(OUT)) for pattern in ("judgments*.jsonl", "scores*.json") for path in OUT.rglob(pattern))
        require(not forbidden, f"Judgment/score artifacts unexpectedly exist: {forbidden}")
        return dict(html=parser_results, judgment_or_score_files=[], scorer_executed_by_validator=False,
                    browser_render_verified=False, browser_limitation="Local-file preview is prohibited by browser URL policy; only static data/path/JavaScript syntax checks are performed.")


class StaticHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.local_urls, self.external_urls, self.scripts, self.json_data = [], [], [], []
        self.script_type, self.buffer = None, []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        for name in ("src", "href"):
            value = attrs.get(name)
            if value and not value.startswith("#"):
                if urlsplit(value).scheme or value.startswith("//"):
                    self.external_urls.append(value)
                else:
                    self.local_urls.append(value)
        if tag == "script":
            self.script_type, self.buffer = attrs.get("type", "text/javascript"), []

    def handle_data(self, data):
        if self.script_type is not None:
            self.buffer.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.script_type is not None:
            text = "".join(self.buffer)
            if self.script_type == "application/json":
                self.json_data.append(strict_json(text))
            elif text.strip():
                self.scripts.append(text)
            self.script_type, self.buffer = None, []


def image_paths(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"image", "image_path"} and isinstance(child, str):
                yield child
            else:
                yield from image_paths(child)
    elif isinstance(value, list):
        for child in value:
            yield from image_paths(child)


def preserve_pilot():
    archive, current = OUT / "pilot/validation.json", OUT / "validation.json"
    if archive.exists():
        require(read_json(archive).get("stage") == "pilot", "Existing pilot validation archive has another stage")
        if current.exists() and read_json(current).get("stage") == "pilot":
            require(archive.read_bytes() == current.read_bytes(), "Pilot validation archives differ; refusing overwrite")
    else:
        require(current.exists() and read_json(current).get("stage") == "pilot", "Original stage-pilot validation is unavailable")
        shutil.copyfile(current, archive)
    require(read_json(archive)["status"] == "passed", "Archived pilot artifact validation did not pass")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    # Archive first, before replacing the top-level validation, even on failure.
    preserve_pilot()
    audit = Audit()
    missing = [name for name in DELIVERABLES if not (OUT / name).is_file()]
    if not missing:
        audit.stage("preserved_pilot_archive", audit.pilot_archive)
        audit.stage("gallery_selection_strict_tags_and_feature_provenance", audit.gallery_and_tags)
        if "tags" in audit.context:
            audit.stage("independent_raw_vlm_and_cache_provenance", audit.vlm_provenance)
            audit.stage("all_experiment_raw_file_inventory", audit.all_raw_files)
            audit.stage("shot_aggregates_variation_and_required_anchors", audit.aggregates_and_anchors)
            audit.stage("query_alternate_ranking_and_pool_coverage", audit.queries_rankings_pool)
        audit.stage("independent_audit_receipts", audit.receipts)
        if "pool" in audit.context:
            audit.stage("offline_html_and_absence_of_generated_judgments", audit.html_and_no_judgments)
    for name in DELIVERABLES:
        if (OUT / name).is_file():
            try:
                audit.sha(OUT / name)
            except Exception as error:
                audit.checks.append(dict(name="deliverable_hash", status="failed", path=name, error=str(error)))
    changed = []
    for path, entry in audit.files.items():
        try:
            stat = path.stat()
            if (stat.st_size, stat.st_mtime_ns) != (entry["bytes"], entry["mtime_ns"]):
                changed.append(str(path))
        except OSError:
            changed.append(str(path))
    if changed:
        audit.checks.append(dict(name="stable_snapshot", status="failed", changed_files=changed))
    else:
        audit.checks.append(dict(name="stable_snapshot", status="passed"))
    verification = OUT / "verification"
    verification.mkdir(exist_ok=True)
    inventory = verification / "delivery-file-hashes.jsonl"
    inventory_rows = [dict(path=str(path.relative_to(OUT)), **entry) for path, entry in sorted(audit.files.items()) if path.is_relative_to(OUT)]
    inventory.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in inventory_rows))
    failed = any(check["status"] == "failed" for check in audit.checks)
    status = "failed" if failed else "incomplete" if missing else "passed"
    report = dict(stage="delivery", status=status, created_at=datetime.now(timezone.utc).isoformat(),
                  ready_for_user_judging=status == "passed", missing_artifacts=missing, checks=audit.checks,
                  pilot_validation=dict(path="pilot/validation.json", sha256=audit.sha(OUT / "pilot/validation.json")),
                  deliverable_hashes={name: audit.files[(OUT / name).resolve()]["sha256"] for name in DELIVERABLES if (OUT / name).resolve() in audit.files},
                  file_inventory=dict(path=str(inventory.relative_to(OUT)), sha256=hashlib.sha256(inventory.read_bytes()).hexdigest(), files=len(inventory_rows)),
                  judgments_created=0, scoring_executed=False,
                  scope="Authorized completed-data exploratory subset: artifact integrity, schema, selected source coverage, provenance, and independent audit receipts; no accuracy or relevance assessment.",
                  selection_scope={key: audit.context.get("manifest", {}).get(key) for key in ("scope", "keyframes", "shots", "unique_sha256", "original_target_keyframes", "original_target_shots", "selection_bias")},
                  original_full_tagging_complete=False)
    temporary = OUT / "validation.json.tmp"
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    temporary.replace(OUT / "validation.json")
    print(json.dumps(dict(status=status, missing_artifacts=missing,
                          failed_checks=[check for check in audit.checks if check["status"] == "failed"],
                          validation=str(OUT / "validation.json")), ensure_ascii=False))
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
