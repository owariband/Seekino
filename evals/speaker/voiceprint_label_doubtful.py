"""Audit B0 label doubt using frozen vectors and existing text results only.

Example (no database, media extraction, model calls or downloads):
    python -m evals.speaker.voiceprint_label_doubtful --artifact <scoring.json> \
        --text-results <actual-test-summary.json> --out <verification-directory>
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
import time

import numpy as np

from engine.parsev2.voiceprint_matching import classify_voiceprint_result, score_baseline, score_voiceprint_batch
from server.constants.parse import (
    VOICEPRINT_B0_LABEL_MIN_DURATION_MS,
    VOICEPRINT_B0_LABEL_MIN_MARGIN,
    VOICEPRINT_B0_LABEL_MIN_RANK,
    VOICEPRINT_B0_LABEL_MIN_REF_COUNT,
    VOICEPRINT_B0_LABEL_MIN_REF_HIGHER_FRACTION,
    VOICEPRINT_B0_LABEL_TAIL_MIN_REFS,
    VOICEPRINT_B0_METHOD_VERSION,
)


def _read(path):
    content = path.read_bytes()
    return json.loads(content), hashlib.sha256(content).hexdigest()


def _text_by_id(results):
    rows = {}
    for outcome in results.get("line_outcomes", []):
        final = outcome.get("final")
        if isinstance(final, list):
            final = final[0] if len(final) == 1 else None
        if not isinstance(final, dict):
            continue
        variants = final.get("text_variants") or []
        original = [row["text"] for row in variants if row.get("role") == "ORIGINAL"]
        rows[outcome["id"]] = {
            "text": "\n".join(original) if original else final.get("text"),
            "historical_final_speaker": final.get("speaker"),
            "historical_final_speaker_source": final.get("speaker_source"),
        }
    return rows


def _distribution(values):
    return dict(zip(("min", "p10", "median", "max"),
                    map(float, np.quantile(values, [0, .1, .5, 1])))) if values else None


def _diagnostic_row(source, score, texts, band_parameters):
    candidates = score["candidates"]
    return {
        "id": source["id"], "chunk_task_id": source["chunk_task_id"],
        "start_ms": source["start_ms"], "end_ms": source["end_ms"],
        "duration_ms": score["duration_ms"], "understanding_speaker": source.get("label"),
        "top1": candidates[0]["speaker"] if candidates else None,
        "top1_score": candidates[0]["similarity"] if candidates else None,
        "top2": candidates[1]["speaker"] if len(candidates) > 1 else None,
        "top2_score": candidates[1]["similarity"] if len(candidates) > 1 else None,
        "top3": candidates[2]["speaker"] if len(candidates) > 2 else None,
        "top3_score": candidates[2]["similarity"] if len(candidates) > 2 else None,
        "gap": score["gap"],
        **{key: score[key] for key in ("label_score", "label_rank", "label_count", "label_margin",
                                       "label_ref_count", "label_ref_consistency", "label_ref_higher_count",
                                       "label_ref_higher_fraction", "label_doubtful", "label_doubtful_basis")},
        "original_band": source.get("band"),
        **classify_voiceprint_result(score, current_label=source.get("label"),
                                    song=source["song"], overlap=source["overlap"], **band_parameters),
        "candidates": [{key: value for key, value in candidate.items() if key != "reference_ids"}
                       for candidate in candidates],
        **texts.get(source["id"], {"text": None}),
    }


def main():
    started = time.perf_counter()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--text-results", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--min-ref-higher-fraction", type=float, default=VOICEPRINT_B0_LABEL_MIN_REF_HIGHER_FRACTION,
                        help="Explicit offline calibration override; default comes from server constants")
    parser.add_argument("--previous-report", type=Path, help="Optional older report.json for flag-set comparison")
    parser.add_argument("--tail-min-refs", type=int, default=VOICEPRINT_B0_LABEL_TAIL_MIN_REFS,
                        help="Reference count at which the tail test replaces the common-gate fallback")
    args = parser.parse_args()
    payload, artifact_sha = _read(args.artifact)
    text_results, text_sha = _read(args.text_results)
    if text_results.get("task", {}).get("id") != payload["video_parse_task_id"]:
        raise ValueError("Text results must belong to the exact scoring artifact task")
    texts = _text_by_id(text_results)
    samples = {row["id"]: row for row in payload["samples"]}
    # Reconstitute all targets, including unembeddable short lines. The source
    # pool is immutable; pass-two exclusions are IDs, never edited labels.
    targets = [{**line, **({"vector": samples[line["id"]]["vector"]} if line["id"] in samples else {})}
               for line in payload["lines"]]
    parameters = {key: payload["parameters"][key]
                  for key in ("dimensions", "min_duration_ms", "trim_fraction")}
    parameters.update(
        label_min_rank=VOICEPRINT_B0_LABEL_MIN_RANK,
        label_min_margin=VOICEPRINT_B0_LABEL_MIN_MARGIN,
        label_min_duration_ms=VOICEPRINT_B0_LABEL_MIN_DURATION_MS,
        label_min_ref_count=VOICEPRINT_B0_LABEL_MIN_REF_COUNT,
        label_min_ref_higher_fraction=args.min_ref_higher_fraction,
        label_tail_min_refs=args.tail_min_refs,
    )
    batch = score_voiceprint_batch(
        targets, model=payload["model"]["name"], encoder_revision=payload["model"]["encoder_revision"],
        **parameters,
    )
    excluded = set(batch["hygiene"]["excluded_reference_ids"])
    band_parameters = {key: payload["parameters"][key] for key in (
        "accept_gap", "accept_duration_ms", "accept_min_cues", "conflict_gap", "no_voice_duration_ms",
    )}
    pass1_rows = [_diagnostic_row(source, score, texts, band_parameters)
                  for source, score in zip(targets, batch["pass1_scores"], strict=True)]
    rows = [_diagnostic_row(source, score, texts, band_parameters)
            for source, score in zip(targets, batch["scores"], strict=True)]
    scoring_lines = []
    for source, score, row, first in zip(targets, batch["scores"], rows, pass1_rows, strict=True):
        scoring_lines.append({**{key: value for key, value in source.items() if key != "vector"},
                              **score, **{key: row[key] for key in ("band", "conflict", "review_reason")},
                              "method_version": VOICEPRINT_B0_METHOD_VERSION})
        row.update({"pass1_" + key: first[key] for key in (
            "label_doubtful", "label_doubtful_basis", "label_ref_count",
            "label_ref_higher_count", "label_ref_higher_fraction",
        )})
    # Use actual pass-one/final results for per-own-label distributions and the
    # baseline check; no third per-line scoring pass is needed.
    named_samples = [row for row in payload["samples"] if row.get("label")]
    before, after = defaultdict(list), defaultdict(list)
    baseline_matches = 0
    for source, first, final in zip(targets, batch["pass1_scores"], batch["scores"], strict=True):
        for destination, result in ((before, first), (after, final)):
            if result["label_ref_consistency"] is not None:
                destination[source["label"]].append(result["label_ref_consistency"])
        if "vector" in source:
            baseline_matches += (
                [(row["speaker"], row["similarity"], row["reference_ids"]) for row in first["candidates"]]
                == [(row["speaker"], row["similarity"], row["reference_ids"]) for row in source["candidates"]]
            )
    probe = {**payload["samples"][0], "id": "full-pool-diagnostic", "pcm_sha256": "no-source-pcm",
             "cue_ids": [], "start_ms": -2, "end_ms": -1} if payload["samples"] else None
    full_before = score_baseline(probe, named_samples, trim_fraction=parameters["trim_fraction"])["candidates"] if probe else []
    full_after = score_baseline(probe, named_samples, trim_fraction=parameters["trim_fraction"],
                                excluded_reference_ids=excluded)["candidates"] if probe else []
    after_by_name = {row["speaker"]: row for row in full_after}
    distribution = [{
        "speaker": candidate["speaker"],
        "original_pool_lines": sum(row.get("label") == candidate["speaker"] for row in targets),
        "embedded_pool_lines": sum(row.get("label") == candidate["speaker"] for row in payload["samples"]),
        "full_pool_kept": candidate["reference_count"], "full_pool_consistency": candidate["reference_consistency"],
        "pass1_target_excluded": _distribution(before[candidate["speaker"]]),
        "pass2_target_excluded": _distribution(after[candidate["speaker"]]),
        "pass2_full_pool_kept": after_by_name.get(candidate["speaker"], {}).get("reference_count"),
        "pass2_full_pool_consistency": after_by_name.get(candidate["speaker"], {}).get("reference_consistency"),
    } for candidate in sorted(full_before, key=lambda row: row["speaker"])]
    known = []
    for center, truth in ((47900, "长崎爽世"), (294500, "千早爱音")):
        matching = [row for row in rows if row["start_ms"] <= center < row["end_ms"]]
        known.append({"center_ms": center, "truth": truth, "match_count": len(matching), "lines": matching,
                      "pass1_lines": [row for row in pass1_rows if row["start_ms"] <= center < row["end_ms"]],
                      "pass2_flagged": len(matching) == 1 and matching[0]["label_doubtful"],
                      "top1_matches_truth": len(matching) == 1 and matching[0]["top1"] == truth})
    flagged = [row for row in rows if row["label_doubtful"]]
    previous_comparison = None
    old_ids = None
    if args.previous_report:
        previous, previous_sha = _read(args.previous_report)
        if previous.get("source_task_id") != payload["video_parse_task_id"]:
            raise ValueError("Previous report must belong to the same source task")
        old_ids = {row["id"] for row in previous["flagged_lines"]}
        new_ids = {row["id"] for row in flagged}
        previous_comparison = {
            "path": str(args.previous_report.resolve()), "sha256": previous_sha,
            "method_version": previous["method_version"], "old_flag_count": len(old_ids),
            "current_flag_count": len(new_ids), "newly_flagged_ids": sorted(new_ids - old_ids),
            "cleared_flag_ids": sorted(old_ids - new_ids),
            "newly_flagged_by_label": dict(Counter(row["understanding_speaker"] for row in rows
                                                   if row["id"] in new_ids - old_ids)),
            "cleared_by_label": dict(Counter(row["understanding_speaker"] for row in rows
                                            if row["id"] in old_ids - new_ids)),
        }
    changed_rows = []
    for row, first in zip(rows, pass1_rows, strict=True):
        pass_changed = (row["label_doubtful"], row["label_doubtful_basis"]) != (
            first["label_doubtful"], first["label_doubtful_basis"])
        previous_changed = old_ids is not None and row["label_doubtful"] != (row["id"] in old_ids)
        if pass_changed or previous_changed:
            changed_rows.append({**row,
                "change_from_pass1": ("new_flag" if row["label_doubtful"] and not first["label_doubtful"] else
                                      "cleared_flag" if first["label_doubtful"] and not row["label_doubtful"] else
                                      "basis_changed") if pass_changed else None,
                "previous_label_doubtful": row["id"] in old_ids if old_ids is not None else None,
                "change_from_previous": ("new_flag" if row["label_doubtful"] else "cleared_flag")
                                        if previous_changed else None,
            })
    def grouped_flags(items):
        groups = defaultdict(Counter)
        for row in items:
            if row["label_doubtful"]:
                groups[row["understanding_speaker"]][row["label_doubtful_basis"]] += 1
        return {label: dict(counts) for label, counts in groups.items()}
    checks = {
        "pass1_top3_reproduces_frozen_artifact": baseline_matches == len(samples),
        "both_known_lines_unique": all(row["match_count"] == 1 for row in known),
        "both_known_lines_flagged_in_pass2": all(row["pass2_flagged"] for row in known),
        "both_known_top1_match_confirmed_identity": all(row["top1_matches_truth"] for row in known),
        "all_final_flags_have_existing_text": all(row.get("text") for row in flagged),
        "all_target_ids_have_existing_text": all(row.get("text") for row in rows),
        "pass1_excluded_ids_absent_from_final_references": all(
            not excluded.intersection(candidate["reference_ids"])
            for score in batch["scores"] for candidate in score["candidates"]),
        "both_passes_export_all_targets": len(pass1_rows) == len(rows) == len(targets),
        "flag_bases_match_frozen_population_boundary": all(
            row["label_doubtful_basis"] == (
                "rank_margin_only_few_refs" if row["label_ref_count"] < parameters["label_tail_min_refs"] else "tail")
            if row["label_doubtful"] else row["label_doubtful_basis"] is None
            for row in [*pass1_rows, *rows]),
    }
    report = {
        "method_version": VOICEPRINT_B0_METHOD_VERSION, "source_task_id": payload["video_parse_task_id"],
        "artifact": {"path": str(args.artifact.resolve()), "sha256": artifact_sha,
                     "method_version": payload["method_version"], "model": payload["model"]},
        "parameters": parameters, "frozen_band_parameters": band_parameters,
        "target_lines": len(targets), "embedded_targets": len(samples), "checks": checks,
        "execution": {"cwd": os.getcwd(),
                      "invocation": shlex.join([sys.executable, "-m", "evals.speaker.voiceprint_label_doubtful", *sys.argv[1:]]),
                      "elapsed_wall_ms": (time.perf_counter() - started) * 1000,
                      "elapsed_scope": "CLI main through cached-vector calculations; excludes interpreter/module-import startup and final report serialization",
                      "model_calls": 0, "decoder_calls": 0, "database_calls": 0, "api_calls": 0},
        "tail_definition": "After existing target/cue/overlap/PCM/revision exclusions and20% trim, retain the target score against the full kept centroid. Each kept reference benchmark uses normalize(sum(kept unit vectors)-that reference), with no extra trim. higher_count counts STRICT LOO-reference cosine>target own-label cosine; higher_fraction=higher_count/kept_reference_count. Ties do not count. Any degenerate/nonfinite LOO centroid makes BOTH calibration fields null; the denominator is never shrunk. At n>=label_tail_min_refs, require the frozen tail fraction; at common minimum<=n<label_tail_min_refs, use rank_margin_only_few_refs without requiring tail success. Fractions remain diagnostic and are not statistical confidence or correctness probability.",
        "previous_report_comparison": previous_comparison,
        "flagged_by_label": dict(Counter(row["understanding_speaker"] for row in flagged)),
        "flagged_by_basis": dict(Counter(row["label_doubtful_basis"] for row in flagged)),
        "flagged_by_label_and_basis": grouped_flags(rows),
        "pass1_flagged_by_label_and_basis": grouped_flags(pass1_rows),
        "pass1_label_diagnostics": pass1_rows,
        "changed_lines": changed_rows,
        "label_diagnostics": rows, "hygiene": batch["hygiene"], "flagged_lines": flagged,
        "prototype_distribution": distribution, "known_cases": known,
        "unchanged_pass1_top3": baseline_matches,
        "band_comparison": {
            "original": dict(Counter(str(row["original_band"]) for row in rows)),
            "final": dict(Counter(str(row["band"]) for row in rows)),
            "changed_count": sum(row["original_band"] != row["band"] for row in rows),
            "transitions": dict(Counter(f"{row['original_band']}->{row['band']}" for row in rows
                                        if row["original_band"] != row["band"])),
        },
        "text_provenance": {"path": str(args.text_results.resolve()), "sha256": text_sha,
                            "matched_line_ids": len(set(texts) & {row["id"] for row in rows}),
                            "meaning": "Existing completed-result wording joined by immutable line ID; not claimed to be original pre-Refine wording or new ground truth."},
        "limitations": [
            "Label agreement and prototype coherence are consistency diagnostics, not speaker identity accuracy or calibrated probability.",
            "The two historical user-confirmed identities are targeted checks, not a representative accuracy sample.",
            "Exactly two per-line scoring passes determine flags; their actual complete diagnostics are exported. Full-pool prototype probes do not feed a third per-line pass or change labels. Per-character consistency distributions use scored own-label targets, rather than every character for every target.",
            "Pass-one excluded IDs remain excluded even when their flags clear in pass two; original sample names continue to govern duplicate-PCM abstention.",
            "References are scored against the OTHER kept references, while the target uses the full kept centroid. The benchmark uses n-1 references and the target n; this is a diagnostic comparison, not an exchangeability or accuracy guarantee.",
            "The population is the SAME kept references after trimming, not all eligible references before trimming. Calibration may be calculated for3–7 references, but its result does not control the deliberate rank/margin fallback. With8–19 references,95% requires every reference to score strictly higher.",
            "No ASR, media extraction, model/API/database access, Refine run or production write is performed.",
        ],
    }
    args.out.mkdir(parents=True, exist_ok=True)
    scoring_artifact = {**payload, "method_version": VOICEPRINT_B0_METHOD_VERSION,
                        "parameters": {**band_parameters, **parameters}, "lines": scoring_lines,
                        "hygiene": batch["hygiene"],
                        "offline_recalculation": {"source_artifact_sha256": artifact_sha,
                                                  "source_method_version": payload["method_version"]}}
    scoring_bytes = json.dumps(scoring_artifact, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False).encode()
    (args.out / "scoring-artifact.json").write_bytes(scoring_bytes)
    report["scoring_artifact"] = {
        "path": str((args.out / "scoring-artifact.json").resolve()),
        "sha256": hashlib.sha256(scoring_bytes).hexdigest(), "bytes": len(scoring_bytes),
        "method_version": VOICEPRINT_B0_METHOD_VERSION, "line_count": len(scoring_lines),
        "sample_count": len(scoring_artifact["samples"]),
    }
    (args.out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    fields = ["id", "chunk_task_id", "start_ms", "end_ms", "duration_ms", "text", "understanding_speaker",
              "top1", "top1_score", "top2", "top2_score", "top3", "top3_score", "gap",
              "label_score", "label_rank", "label_count", "label_margin",
              "label_ref_count", "label_ref_consistency", "label_ref_higher_count", "label_ref_higher_fraction",
              "pass1_label_doubtful", "pass1_label_doubtful_basis", "pass1_label_ref_count",
              "pass1_label_ref_higher_count", "pass1_label_ref_higher_fraction", "label_doubtful", "label_doubtful_basis",
              "band", "conflict", "review_reason", "original_band", "historical_final_speaker", "historical_final_speaker_source"]
    with (args.out / "label-doubtful.tsv").open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(flagged)
    with (args.out / "flag-changes.tsv").open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=[*fields, "change_from_pass1", "previous_label_doubtful",
                                                    "change_from_previous"], delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(changed_rows)
    markdown = ["# B0 label-doubtful cached-vector verification", "",
                f"Source task `{payload['video_parse_task_id']}`; {len(targets)} lines, {len(samples)} cached vectors.", "",
                f"Pass 1 flags **{batch['hygiene']['pass1_flag_count']}**; pass 2 flags **{len(flagged)}**; changed flags **{batch['hygiene']['changed_flag_count']}**. Pass 1 excludes {len(excluded)} reference IDs. No relabeling or convergence loop is performed.", "",
                f"Common rule: rank ≥{parameters['label_min_rank']}, top1-minus-label ≥{parameters['label_min_margin']}, duration ≥{parameters['label_min_duration_ms']} ms, kept PCM reference count ≥{parameters['label_min_ref_count']}. At n≥{parameters['label_tail_min_refs']}, strict LOO-higher fraction must be ≥{parameters['label_min_ref_higher_fraction']}; below{parameters['label_tail_min_refs']}, the common rule alone flags with `rank_margin_only_few_refs`.", "",
                "No global mean-cosine floor, song, overlap or independent-cue condition is added to this label flag. Reference consistency remains diagnostic. Existing band/default semantics are not changed by this offline audit.", "",
                report["tail_definition"], "",
                f"Final flags by original label: `{json.dumps(report['flagged_by_label'], ensure_ascii=False)}`. Previous-report comparison: `{json.dumps(previous_comparison, ensure_ascii=False)}`.", "",
                f"Final flags by basis: `{json.dumps(report['flagged_by_basis'])}`. Pass1 label/basis groups: `{json.dumps(report['pass1_flagged_by_label_and_basis'], ensure_ascii=False)}`; final groups: `{json.dumps(report['flagged_by_label_and_basis'], ensure_ascii=False)}`.", "",
                "Final bands/conflicts/review reasons are recomputed from the final pass-2 scores with the unchanged classifier and the artifact's frozen band parameters. The TSV retains original_band for comparison and exports all top-three names and cosine scores.", "",
                f"Original pass-1 top-three ranks, scores and reference IDs reproduced exactly for **{baseline_matches}/{len(samples)}** embeddable lines.", "",
                f"Final-band comparison after the intended pool cleanup: **{report['band_comparison']['changed_count']}** lines changed band. Original `{json.dumps(report['band_comparison']['original'])}`; final `{json.dumps(report['band_comparison']['final'])}`; transitions `{json.dumps(report['band_comparison']['transitions'])}`. The classifier and its frozen thresholds are unchanged.", "",
                "| Known center | Actual range (ms) | Original label | Pass-2 top1 | Label rank/count | Label margin | Strict higher / kept refs | Pass-2 flag / basis |",
                "|---:|---|---|---|---|---:|---|---|"]
    for case in known:
        for row in case["lines"]:
            markdown.append(f"| {case['center_ms']/1000:g} s | {row['start_ms']}–{row['end_ms']} | {row['understanding_speaker']} | {row['top1']} | {row['label_rank']}/{row['label_count']} | {row['label_margin']:.6f} | {row['label_ref_higher_count']} / {row['label_ref_count']} | {row['label_doubtful']} / {row['label_doubtful_basis']} |")
    markdown += ["", "| Character | Source / embedded / full kept | Full mean cosine | Own-target-excluded min / p10 / median / max (pass 1) |", "|---|---|---:|---|"]
    for row in distribution:
        values = row["pass1_target_excluded"]
        summary = " / ".join(f"{values[key]:.6f}" for key in ("min", "p10", "median", "max")) if values else "none"
        markdown.append(f"| {row['speaker']} | {row['original_pool_lines']} / {row['embedded_pool_lines']} / {row['full_pool_kept']} | {row['full_pool_consistency']:.6f} | {summary} |")
    markdown += ["", f"All **{len(flagged)}** final flags, with text, tail diagnostics and decision basis, are exported to `label-doubtful.tsv`. `flag-changes.tsv` contains **{len(changed_rows)}** rows whose flag/basis changed between passes or whose final flag differs from the previous report. Full actual pass1/final diagnostics for every target are in `report.json`. `scoring-artifact.json` contains all{len(targets)} final lines, all{len(samples)} original embeddings, complete reference IDs, frozen method/parameters and hygiene; it has not been published to a task.", "",
                 f"Source counts include all{len(targets)} lines. {len(targets)-len(samples)} lines have no compatible embedding: " + "; ".join(
                     f"{row['speaker']} has{row['original_pool_lines']} source/{row['embedded_pool_lines']} embedded lines"
                     for row in distribution if row['original_pool_lines'] != row['embedded_pool_lines'])
                 + ". These unscored targets do not enter centroids.", "",
                 f"Verification checks: `{json.dumps(checks, ensure_ascii=False)}`.", "",
                 "Wording comes from the existing completed-result text snapshot, joined by line ID. It may include historical Refine edits. It is not freshly transcribed or original pre-Refine evidence.", "",
                 *["- " + value for value in report["limitations"]], ""]
    markdown += [f"Invocation: `{report['execution']['invocation']}` from `{report['execution']['cwd']}`. Calculation wall time: **{report['execution']['elapsed_wall_ms']:.1f} ms** ({report['execution']['elapsed_scope']}). Model/decoder/database/API calls: **0/0/0/0**.", ""]
    (args.out / "report.md").write_text("\n".join(markdown))
    print(json.dumps({"out": str(args.out), "hygiene": batch["hygiene"],
                      "known_cases_pass2_flagged": [row["pass2_flagged"] for row in known],
                      "baseline_top3_matches": baseline_matches, "text_lines": report["text_provenance"]["matched_line_ids"],
                      "checks": checks, "scoring_artifact": report["scoring_artifact"]}, ensure_ascii=False))
    if not all(checks.values()):
        raise SystemExit("Cached-vector verification failed; inspect report.json checks")


if __name__ == "__main__":
    main()
