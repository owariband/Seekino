"""Independently audit official-path results without importing any neural model."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib.metadata
from itertools import combinations
import json
import math
import os
from pathlib import Path
import subprocess
import wave


HERE = Path(__file__).resolve().parent
EXPERIMENT = HERE.parent
ROOT = HERE.parents[4]
COMMIT = "7ab62aaf2606d83da6c71ee74e7d16e0979edbc3"
CRITERIA = {"normalized_vector_max_abs": 1e-5, "vector_cosine_min": 1 - 1e-6,
            "within_cohort_pair_score_max_abs": 1e-5}
DIFFERENCES = []


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read(path):
    return json.loads(path.read_text())


def close(left, right):
    difference = abs(left - right)
    DIFFERENCES.append(difference)
    assert difference <= 1e-12, (left, right, difference)


def norm(vector):
    return math.sqrt(sum(value * value for value in vector))


def cosine(left, right):
    assert len(left) == len(right)
    return sum(a * b for a, b in zip(left, right)) / (norm(left) * norm(right))


def eligible(target, ref):
    return (target["cohort"] == ref["cohort"] and ref["target_role"] == "reference"
            and target["id"] != ref["id"] and target["pcm_sha256"] != ref["pcm_sha256"]
            and (ref["end_ms"] <= target["start_ms"] or target["end_ms"] <= ref["start_ms"]))


def check_probe(probe, target, samples, vectors):
    assert (probe["registered_speaker"], probe["known_actual_speaker"], probe["cohort"]) == (
        target["speaker"], target["known_actual_speaker"], target["cohort"])
    groups = {}
    for ref in samples:
        if eligible(target, ref):
            groups.setdefault(ref["speaker"], []).append(ref)
    assert set(groups) == {row["speaker"] for row in probe["candidates"]}
    scores = {}
    for row in probe["candidates"]:
        refs = groups[row["speaker"]]
        assert row["count"] == len(refs)
        assert len({ref["pcm_sha256"] for ref in refs}) == len(refs)
        individual = {ref["id"]: cosine(vectors[target["id"]], vectors[ref["id"]]) for ref in refs}
        assert set(individual) == {item["reference_id"] for item in row["individual_scores"]}
        for item in row["individual_scores"]:
            close(item["cosine"], individual[item["reference_id"]])
        center = [sum(vectors[ref["id"]][i] for ref in refs) / len(refs) for i in range(256)]
        mean = sum(individual.values()) / len(individual)
        centroid = cosine(vectors[target["id"]], center)
        close(row["mean_pairwise_cosine"], mean)
        close(row["normalized_centroid_cosine"], centroid)
        close(row["mean_reference_vector_norm"], norm(center))
        scores[row["speaker"]] = {"mean_pairwise_cosine": mean, "normalized_centroid_cosine": centroid}
    for metric, rank in probe["rankings"].items():
        ordered = sorted(scores, key=lambda speaker: (-scores[speaker][metric], speaker))
        assert ordered == rank["ordered_speakers"] and rank["top1"] == ordered[0]
        assert rank["known_actual_rank"] == ordered.index(target["known_actual_speaker"]) + 1
        close(rank["top1_score"], scores[ordered[0]][metric])
        close(rank["top1_top2_margin"], scores[ordered[0]][metric] - scores[ordered[1]][metric])


def main():
    comparison_path = HERE / "comparison.json"
    report = read(comparison_path)
    comparison_hash = sha(comparison_path)
    assert report["acceptance_criteria"] == CRITERIA
    run = HERE / report["run"]
    assert run.resolve().is_relative_to(HERE.resolve())
    status, provenance = read(run / "status.json"), read(run / "provenance.json")
    assert status == {"status": "complete", "sample_count": 47}
    assert provenance == report["official_provenance"] and provenance["sample_count"] == 47
    manifest_path = EXPERIMENT / "manifest.json"
    manifest = read(manifest_path)
    assert sha(manifest_path) == report["manifest_sha256"] == provenance["manifest_sha256"]
    samples = manifest["samples"]
    assert len(samples) == 47 and len({sample["id"] for sample in samples}) == 47
    known = {sample["id"]: sample for sample in samples if sample["known_actual_speaker"]}
    assert {key: value["known_actual_speaker"] for key, value in known.items()} == {
        "v11_02": "千早爱音", "v11_27": "长崎爽世", "balanced_null_target": "长崎爽世"}
    for sample in samples:
        path = EXPERIMENT / sample["audio"]
        assert sha(path) == sample["wav_sha256"]
        with wave.open(str(path), "rb") as audio:
            assert (audio.getnchannels(), audio.getframerate(), audio.getsampwidth()) == (1, 16000, 2)
            assert hashlib.sha256(audio.readframes(audio.getnframes())).hexdigest() == sample["pcm_sha256"]

    old_path, new_path = EXPERIMENT / "wavlm_large/embeddings.json", run / "embeddings.json"
    assert sha(old_path) == report["old_embedding_sha256"]
    assert sha(new_path) == report["official_embedding_sha256"]
    initial_results = read(EXPERIMENT / "results.json")
    assert sha(old_path) == initial_results["models"]["wavlm_large"]["embedding_file_sha256"]
    old_records, new_records = read(old_path), read(new_path)
    assert len(old_records) == len(new_records) == 47
    records = {"previous_wrapper": old_records, "official_pinned": new_records}
    vectors, by_id = {}, {sample["id"]: sample for sample in samples}
    for label, rows in records.items():
        assert {row["id"] for row in rows} == set(by_id)
        vectors[label] = {row["id"]: row["vector"] for row in rows}
        for row in rows:
            assert row["pcm_sha256"] == by_id[row["id"]]["pcm_sha256"] and len(row["vector"]) == 256
            assert all(math.isfinite(value) for value in row["vector"]) and abs(norm(row["vector"]) - 1) < 1e-6
            assert math.isfinite(row["raw_norm"]) and row["raw_norm"] > 0
    sample_checks = {row["id"]: row for row in report["per_sample_differences"]}
    assert set(sample_checks) == set(by_id)
    max_abs, min_cos = 0, 1
    for sample_id in by_id:
        left, right = vectors["previous_wrapper"][sample_id], vectors["official_pinned"][sample_id]
        element_diff = max(abs(a - b) for a, b in zip(left, right))
        score = cosine(left, right)
        close(sample_checks[sample_id]["normalized_max_abs"], element_diff)
        close(sample_checks[sample_id]["vector_cosine"], score)
        close(sample_checks[sample_id]["normalized_l2"], norm([a - b for a, b in zip(left, right)]))
        max_abs, min_cos = max(max_abs, element_diff), min(min_cos, score)
    pair_checks = {(row["left"], row["right"]): row for row in report["pair_differences"]}
    assert len(pair_checks) == len(report["pair_differences"]) == 585
    max_pair, pair_count, aucs = 0, 0, {}
    for cohort in ("v11", "balanced"):
        cohort_samples = [sample for sample in samples if sample["cohort"] == cohort]
        for left, right in combinations(cohort_samples, 2):
            assert left["pcm_sha256"] != right["pcm_sha256"]
            assert left["end_ms"] <= right["start_ms"] or right["end_ms"] <= left["start_ms"]
            row = pair_checks[(left["id"], right["id"])]
            assert row["cohort"] == cohort
            before = cosine(vectors["previous_wrapper"][left["id"]], vectors["previous_wrapper"][right["id"]])
            after = cosine(vectors["official_pinned"][left["id"]], vectors["official_pinned"][right["id"]])
            close(row["old_cosine"], before)
            close(row["official_cosine"], after)
            close(row["absolute_difference"], abs(before - after))
            max_pair, pair_count = max(max_pair, abs(before - after)), pair_count + 1
        refs = [sample for sample in cohort_samples if sample["target_role"] == "reference"
                and sample["id"] not in {"v11_02", "v11_27"}]
        for label, data in vectors.items():
            same, other = [], []
            for left, right in combinations(refs, 2):
                (same if left["speaker"] == right["speaker"] else other).append(cosine(data[left["id"]], data[right["id"]]))
            expected_counts = (29, 65, 341) if cohort == "v11" else (15, 30, 75)
            assert (len(refs), len(same), len(other)) == expected_counts
            auc = sum(1 if a > b else 0.5 if a == b else 0 for a in same for b in other) / (len(same) * len(other))
            recorded = next(row for row in report["weak_label_auc"][label] if row["cohort"] == cohort)
            assert (recorded["references"], recorded["same_label_pairs"], recorded["different_label_pairs"]) == expected_counts
            close(recorded["auc"], auc)
            aucs.setdefault(label, {})[cohort] = auc
    assert pair_count == 585
    for label, probes in report["known_targets"].items():
        assert len(probes) == 3 and {probe["target_id"] for probe in probes} == set(known)
        for probe in probes:
            check_probe(probe, known[probe["target_id"]], samples, vectors[label])
    close(report["summary"]["maximum_normalized_element_difference"], max_abs)
    close(report["summary"]["minimum_vector_cosine"], min_cos)
    close(report["summary"]["maximum_pair_score_difference"], max_pair)
    equivalent = (max_abs <= CRITERIA["normalized_vector_max_abs"] and min_cos >= CRITERIA["vector_cosine_min"]
                  and max_pair <= CRITERIA["within_cohort_pair_score_max_abs"])
    assert report["approximately_equivalent"] is equivalent

    cache = ROOT / "outputs/local-models/wavlm-speaker-sv"
    pinned = cache / "official-pinned/s3prl"
    environment_path = cache / "official-pinned/environment.json"
    runtime = read(environment_path)
    assert sha(environment_path) == report["environment_manifest_sha256"]
    assert runtime["python_executable"] == provenance["python_executable"]
    assert runtime["python_version"] == provenance["python"]
    assert runtime["python_machine"] == provenance["machine"]
    freeze = runtime["full_freeze"]
    freeze_path = environment_path.parent / freeze["path"]
    assert sha(freeze_path) == freeze["sha256"]
    assert len(freeze_path.read_text().splitlines()) == freeze["line_count"]
    assert provenance["s3prl_commit"] == COMMIT
    assert runtime["sources"]["s3prl"]["commit"] == COMMIT
    environment = {**os.environ, "GIT_NO_LAZY_FETCH": "1"}
    for filename, digest in provenance["source_sha256"].items():
        path = ROOT / filename
        assert sha(path) == digest
        repo = pinned if path.is_relative_to(pinned) else cache / "UniSpeech"
        commit = COMMIT if repo == pinned else provenance["unispeech_commit"]
        committed = subprocess.check_output(["git", "-C", str(repo), "show", f"{commit}:{path.relative_to(repo)}"], env=environment)
        assert hashlib.sha256(committed).hexdigest() == digest
    assert len(provenance["actual_feature_modules"]) == 3
    for filename in provenance["actual_feature_modules"].values():
        assert (ROOT / filename).resolve().is_relative_to(pinned.resolve())
    assert sha(HERE / "runner.py") == provenance["runner_sha256"]
    assert sha(HERE / "hubconf.py") == provenance["hub_router_sha256"]
    for filename, digest in provenance["checkpoint_sha256"].items():
        assert sha(ROOT / filename) == digest
    state = provenance["state_dict"]
    assert state["strict"] is True and state["keys"] == 710 and state["missing"] == []
    assert state["unexpected_after_exclusion"] == []
    assert state["excluded_training_only"] == {"loss_calculator.projection.weight": [5994, 256]}
    assert provenance["device"] == "cpu" and provenance["dtype"] == "float32" and provenance["threads"] == 4
    assert provenance["python"].startswith("3.9.")
    versions = provenance["versions"]
    assert versions["torch"].split("+")[0] == "1.9.0"
    expected_versions = {"scipy": "1.7.1", "fire": "0.4.0", "sklearn": "0.0", "s3prl": "0.3.1",
                         "torchaudio": "0.9.0", "sentencepiece": "0.1.96"}
    assert runtime["original_six_pins"] == expected_versions
    env_root = Path(provenance["python_executable"]).parent.parent
    sites = [str(path) for path in (env_root / "lib").glob("python3.9/site-packages")]
    installed = {dist.metadata["Name"].lower(): dist.version for dist in importlib.metadata.distributions(path=sites)}
    for name, version in expected_versions.items():
        assert versions.get(name, installed.get(name)).split("+")[0] == version
    for name, version in versions.items():
        assert runtime["all_installed_distributions"][name] == version
    assert sha(comparison_path) == comparison_hash and sha(old_path) == report["old_embedding_sha256"]
    verification = {"verified_at": datetime.now(timezone.utc).isoformat(), "status": "comparison_reproduced",
                    "comparison_sha256": comparison_hash, "manifest_sha256": sha(manifest_path),
                    "environment_manifest_sha256": sha(environment_path), "freeze_sha256": sha(freeze_path),
                    "old_embedding_sha256": sha(old_path), "official_embedding_sha256": sha(new_path),
                    "verify_script_sha256": sha(Path(__file__)), "approximately_equivalent": equivalent,
                    "acceptance_criteria": CRITERIA, "pcm_matches": 47, "vectors_per_path": 47,
                    "within_cohort_pairs": 585, "known_probe_conditions_per_path": 3,
                    "maximum_normalized_element_difference": max_abs, "minimum_vector_cosine": min_cos,
                    "maximum_pair_score_difference": max_pair, "weak_label_auc": aucs,
                    "arithmetic_checks": len(DIFFERENCES), "maximum_recalculation_difference": max(DIFFERENCES),
                    "official_six_versions": expected_versions, "s3prl_commit": COMMIT,
                    "runtime_machine": provenance["machine"], "runtime_python": provenance["python"],
                    "limits": ["Only two distinct utterances are user-confirmed; registry AUC is weak-label agreement.",
                               "The same mirrored checkpoint bytes are used on both paths; Microsoft original equivalence remains unverified.",
                               "Official-version execution uses x86_64 CPU under Rosetta; timing is not directly comparable with the earlier native arm64 run. The installed runtime is not a complete historical training environment.",
                               "Verification reads saved outputs, checkpoint hashes and source files; it performs no neural inference."]}
    (HERE / "verification.json").write_text(json.dumps(verification, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: verification[key] for key in ("status", "approximately_equivalent",
          "maximum_normalized_element_difference", "minimum_vector_cosine", "maximum_pair_score_difference", "arithmetic_checks")}))


if __name__ == "__main__":
    main()
