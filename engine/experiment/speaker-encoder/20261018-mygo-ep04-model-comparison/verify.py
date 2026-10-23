"""Independently audit frozen audio, vectors and comparison arithmetic; no model calls."""
from __future__ import annotations

import ast
import csv
from datetime import UTC, datetime
import hashlib
import json
import math
from pathlib import Path
import wave


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
ERRORS = []


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def load(path):
    return json.loads(path.read_text())


def close(actual, expected):
    difference = abs(actual - expected)
    ERRORS.append(difference)
    assert difference <= 1e-10, (actual, expected, difference)


def cosine(left, right):
    assert len(left) == len(right)
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)


def neural_definitions(path):
    class ReviewedDifferences(ast.NodeTransformer):
        def visit_Expr(self, node):
            return None if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str) else node

        def visit_AugAssign(self, node):
            # The only execution difference found between the two official copies:
            # positional addition uses x += x_conv versus x = x + x_conv.
            if (isinstance(node.target, ast.Name) and node.target.id == "x" and isinstance(node.op, ast.Add)
                    and isinstance(node.value, ast.Name) and node.value.id == "x_conv"):
                return ast.Assign(targets=[node.target], value=ast.BinOp(
                    left=ast.Name(id="x", ctx=ast.Load()), op=ast.Add(), right=node.value))
            return node

    tree = ReviewedDifferences().visit(ast.parse(path.read_text()))
    return {node.name: ast.dump(node, include_attributes=False) for node in tree.body
            if isinstance(node, (ast.ClassDef, ast.FunctionDef))}


def recompute(target, pool, vectors, excluded=()):
    selected = []
    for ref in pool:
        if ref["cohort"] != target["cohort"] or ref["id"] == target["id"] or ref["id"] in excluded:
            continue
        if ref["pcm_sha256"] == target["pcm_sha256"]:
            continue
        if ref["start_ms"] < target["end_ms"] and target["start_ms"] < ref["end_ms"]:
            continue
        selected.append(ref)
    assert len({ref["pcm_sha256"] for ref in selected}) == len(selected)
    groups = {}
    for ref in selected:
        groups.setdefault(ref["speaker"], []).append(ref)
    rows = {}
    for speaker, refs in groups.items():
        scores = {ref["id"]: cosine(vectors[target["id"]], vectors[ref["id"]]) for ref in refs}
        center = [sum(vectors[ref["id"]][column] for ref in refs) / len(refs)
                  for column in range(len(vectors[target["id"]]))]
        rows[speaker] = {"count": len(refs), "mean_pairwise_cosine": sum(scores.values()) / len(scores),
                         "normalized_centroid_cosine": cosine(vectors[target["id"]], center),
                         "mean_reference_vector_norm": math.sqrt(sum(value * value for value in center)),
                         "scores": scores}
    return rows


def check_comparison(actual, target, pool, vectors, excluded=()):
    assert actual["target_id"] == target["id"] and actual["cohort"] == target["cohort"]
    assert actual["registered_speaker"] == target["speaker"]
    assert actual["known_actual_speaker"] == target["known_actual_speaker"]
    expected = recompute(target, pool, vectors, excluded)
    assert {row["speaker"] for row in actual["candidates"]} == set(expected)
    for row in actual["candidates"]:
        calculated = expected[row["speaker"]]
        assert row["count"] == calculated["count"]
        assert [item["reference_id"] for item in row["individual_scores"]] == list(calculated["scores"])
        for item in row["individual_scores"]:
            close(item["cosine"], calculated["scores"][item["reference_id"]])
        for metric in ("mean_pairwise_cosine", "normalized_centroid_cosine", "mean_reference_vector_norm"):
            close(row[metric], calculated[metric])
    for metric, rank in actual["rankings"].items():
        order = sorted(expected, key=lambda speaker: (-expected[speaker][metric], speaker))
        assert rank["ordered_speakers"] == order and rank["top1"] == order[0]
        close(rank["top1_score"], expected[order[0]][metric])
        close(rank["top1_top2_margin"], expected[order[0]][metric] - expected[order[1]][metric])
        known_rank = order.index(target["known_actual_speaker"]) + 1 if target["known_actual_speaker"] in order else None
        assert rank["known_actual_rank"] == known_rank


def main():
    manifest_path, results_path = HERE / "manifest.json", HERE / "results.json"
    manifest, results = load(manifest_path), load(results_path)
    results_hash = sha(results_path)
    assert results["manifest_sha256"] == sha(manifest_path)
    samples = manifest["samples"]
    by_id = {sample["id"]: sample for sample in samples}
    assert len(samples) == len(by_id) == results["sample_count"] == 47
    assert [sample["id"] for sample in samples[:31]] == [f"v11_{index:02d}" for index in range(31)]
    for record in manifest["inputs"].values():
        assert sha(ROOT / record["path"]) == record["sha256"]
    v11 = load(ROOT / manifest["inputs"]["v11_snapshot"]["path"])
    original_refs = [(group["speaker"], ref) for group in v11["task"]["detail"]["voiceprint"]["voiceprintList"]
                     for ref in group["references"]]
    balanced = load(ROOT / manifest["inputs"]["balanced_manifest"]["path"])
    balanced_vectors = {item["id"]: item for item in load(ROOT / manifest["inputs"]["balanced_embeddings"]["path"])}
    for sample, (speaker, ref) in zip(samples[:31], original_refs, strict=True):
        assert (sample["speaker"], sample["start_ms"], sample["end_ms"], sample["pcm_sha256"]) == (
            speaker, ref["start_ms"], ref["end_ms"], ref["pcm_sha256"])
        assert sample["target_role"] == "reference"
    originals = [*balanced["references"], balanced["target"]]
    for sample, original in zip(samples[31:], originals, strict=True):
        assert sample["id"] == f'balanced_{original["id"]}'
        assert sample["speaker"] == original.get("speaker", original.get("speaker_in_timeline"))
        assert (sample["start_ms"], sample["end_ms"]) == (original["start_ms"], original["end_ms"])
    known = {sample["id"]: sample["known_actual_speaker"] for sample in samples if sample["known_actual_speaker"]}
    assert known == {"v11_02": "千早爱音", "v11_27": "长崎爽世", "balanced_null_target": "长崎爽世"}
    for sample in samples:
        audio_path = HERE / sample["audio"]
        assert sha(audio_path) == sample["wav_sha256"]
        with wave.open(str(audio_path), "rb") as audio:
            assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (16000, 1, 2)
            assert audio.getnframes() == sample["frames"]
            assert hashlib.sha256(audio.readframes(audio.getnframes())).hexdigest() == sample["pcm_sha256"]

    model_checks, pending = {}, []
    for name, report in results["models"].items():
        if report["status"]["status"] != "complete":
            pending.append({"model": name, "reported_status": report["status"]["status"]})
            continue
        path = HERE / name / "embeddings.json"
        assert sha(path) == report["embedding_file_sha256"]
        raw = load(path)
        records = raw["embeddings"] if isinstance(raw, dict) else raw
        assert len(records) == 47 and {row["id"] for row in records} == set(by_id)
        vectors = {row["id"]: row["vector"] for row in records}
        for row in records:
            assert row["pcm_sha256"] == by_id[row["id"]]["pcm_sha256"]
            assert len(row["vector"]) == report["dimension"]
            assert all(math.isfinite(value) for value in row["vector"])
            assert abs(math.hypot(*row["vector"]) - 1) < 1e-5
        if name == "campp_baseline":
            for sample, (_, ref) in zip(samples[:31], original_refs, strict=True):
                assert vectors[sample["id"]] == ref["vector"]
            for sample in samples[31:]:
                assert vectors[sample["id"]] == balanced_vectors[sample["source"]["original_id"]]["vector"]
        else:
            provenance = load(HERE / name / "provenance.json")
            assert provenance["manifest_sha256"] == sha(manifest_path)
            if name.startswith("redimnet2_"):
                record = provenance["checkpoint"]
                checkpoint = ROOT / "outputs/local-models/redimnet2" / record["name"]
                assert checkpoint.stat().st_size == record["size"] and sha(checkpoint) == record["sha256"]
                assert provenance["strict_state_dict"] is True
                download = load(ROOT / "outputs/local-models/redimnet2/download-provenance.json")
                assert provenance["source_commit"] == download["source_commit"]
                assert provenance["source_archive_sha256"] == download["source_archive_sha256"]
            elif name == "eres2netv2":
                checkpoint = ROOT / provenance["checkpoint"]
                assert sha(checkpoint) == provenance["checkpoint_sha256"] == provenance["official_file_record"]["Sha256"]
                assert checkpoint.stat().st_size == provenance["checkpoint_bytes"]
                for filename, digest in provenance["source_sha256"].items():
                    assert sha(ROOT / "outputs/local-models/campplus/3D-Speaker" / filename) == digest
            elif name == "wavlm_large":
                checkpoint = ROOT / provenance["checkpoint"]
                assert checkpoint.stat().st_size == provenance["checkpoint_bytes"] == 1301926579
                assert sha(checkpoint) == provenance["checkpoint_sha256"] == "51f07e3b94d9e0262a6a675ef5a087be3dd09e8c62e9d886827f44f82fe7f94b"
                backbone = ROOT / provenance["backbone_config_checkpoint"]
                assert backbone.stat().st_size == provenance["backbone_config_checkpoint_bytes"]
                assert sha(backbone) == provenance["backbone_config_checkpoint_sha256"]
                assert sha(HERE / name / "runner.py") == provenance["runner_sha256"]
                cfg = provenance["backbone_configuration"]
                assert (cfg["encoder_layers"], cfg["encoder_embed_dim"], cfg["normalize"], cfg["layer_norm_first"]) == (24, 1024, True, True)
                state = provenance["state_dict"]
                assert state["strict"] is True and state["keys"] == 710 and state["missing"] == []
                assert state["unexpected_after_training_key_exclusion"] == []
                assert state["excluded_training_only"] == {"loss_calculator.projection.weight": [5994, 256]}
                source_root = ROOT / "outputs/local-models/wavlm-speaker-sv"
                for filename, digest in provenance["source_sha256"].items():
                    assert sha(source_root / "UniSpeech" / filename) == digest
                assert sha(source_root / "s3prl/s3prl/upstream/wavlm/expert.py") == provenance["feature_wrapper_reference_sha256"]
                for filename in ("WavLM.py", "modules.py"):
                    assert neural_definitions(source_root / "UniSpeech/WavLM" / filename) == neural_definitions(
                        source_root / "s3prl/s3prl/upstream/wavlm" / filename)
                assert "not a Microsoft-hosted" in provenance["checkpoint_source"]
                assert "cannot be proven" in provenance["identity_limitation"]
        groups = [("known_targets_original_labels", ()),
                  ("sensitivity_excluding_two_known_wrong_reference_sources", {"v11_02", "v11_27"}),
                  ("v11_leave_one_out", ())]
        for field, excluded in groups:
            for actual in report[field]:
                target = by_id[actual["target_id"]]
                pool = [sample for sample in samples if sample["cohort"] == target["cohort"] and sample["target_role"] == "reference"]
                check_comparison(actual, target, pool, vectors, excluded)
        assert len(report["known_targets_original_labels"]) == 3
        assert len(report["sensitivity_excluding_two_known_wrong_reference_sources"]) == 2
        assert len(report["v11_leave_one_out"]) == 31
        for cohort in ("v11", "balanced"):
            cohort_samples = [sample for sample in samples if sample["cohort"] == cohort]
            with (HERE / "matrices" / f"{name}-{cohort}.csv").open() as stream:
                matrix = list(csv.reader(stream))
            assert matrix[0] == ["sample_id", *(sample["id"] for sample in cohort_samples)]
            assert len(matrix) == len(cohort_samples) + 1
            for row, left in zip(matrix[1:], cohort_samples, strict=True):
                assert row[0] == left["id"]
                for value, right in zip(row[1:], cohort_samples, strict=True):
                    if left["id"] == right["id"]:
                        assert value == ""
                    else:
                        close(float(value), cosine(vectors[left["id"]], vectors[right["id"]]))
        model_checks[name] = {"embedding_sha256": sha(path), "samples": 47, "comparison_conditions_checked": 36,
                              "known_targets": [{"target_id": probe["target_id"], "rankings": probe["rankings"]}
                                                for probe in report["known_targets_original_labels"]]}

    old_results = load(ROOT / "engine/experiment/campplus/20261017-balanced-multiref/run1/results.json")
    old_probe = next(probe for probe in results["models"]["campp_baseline"]["known_targets_original_labels"]
                     if probe["target_id"] == "balanced_null_target")
    metric_names = {"mean_pairwise_cosine": "mean_pairwise_cosine", "cosine_of_normalized_centroid": "normalized_centroid_cosine"}
    for summary in old_results["summaries"]:
        if summary["mode"] != "full" or summary["method"] not in metric_names:
            continue
        for row in old_probe["candidates"]:
            close(row[metric_names[summary["method"]]], summary["scores"][row["speaker"]])
    assert sha(results_path) == results_hash, "Comparison changed during verification; rerun the audit"
    verification = {"verified_at": datetime.now(UTC).isoformat(), "status": "passed_for_completed_models",
                    "manifest_sha256": sha(manifest_path), "results_sha256": results_hash,
                    "verification_script_sha256": sha(Path(__file__)), "pcm_and_wav_matches": 47,
                    "original_labels_preserved": True, "cohorts_isolated": True,
                    "campp_vectors_exact_to_frozen_sources": 47, "balanced_full_mean_and_centroid_reproduced": True,
                    "numeric_checks": len(ERRORS), "maximum_absolute_difference": max(ERRORS),
                    "model_checks": model_checks, "pending_models": pending,
                    "code_review": {"eres2netv2": "Official ERes2NetV2 constructor matches infer_sv.py; official FBank80/16k/mean/dither=0; runner loads strict=True.",
                                    "redimnet2": "Pinned official ReDimNet2Wrap uses checkpoint model_config, internal waveform frontend and strict=True; no architecture or label rewriting in runner.",
                                    "wavlm_large": "24 layer-input hooks plus final encoder output match s3prl order; normalize=True waveform layer_norm, learned feature_weight and official ECAPA InstanceNorm are preserved. Official neural class/function ASTs match after explicit positional-add in-place and docstring normalization. Strict 710-key embedding load excludes only the 5994x256 training classifier."},
                    "limits": ["No new model inference was performed by this audit.",
                               "Only three probe conditions from two known utterances have user-confirmed identities; no full-episode accuracy estimate.",
                               "Source and strict-loading review does not constitute independent rerunning of the neural forward pass.",
                               "WavLM uses a third-party mirror. Matching mirror SHA does not prove bit-identical equivalence to the inaccessible Microsoft Drive asset; full original s3prl forward parity was not rerun.",
                               "Pending models are not counted as complete; rerun verification after their results are published."]}
    (HERE / "verification.json").write_text(json.dumps(verification, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": verification["status"], "completed_models": list(model_checks), "pending": pending,
                      "numeric_checks": len(ERRORS), "maximum_absolute_difference": max(ERRORS)}))


if __name__ == "__main__":
    main()
