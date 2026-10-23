"""Independent audit of frozen ASR audio, embeddings and anonymous clustering."""
from __future__ import annotations

import argparse
import ast
from collections import Counter
from datetime import UTC, datetime
import hashlib
import json
import math
from pathlib import Path
import re
from urllib.parse import unquote
import wave

import numpy as np


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
OLD = ROOT / "engine/experiment/speaker-encoder/20261018-mygo-ep04-model-comparison"


def read(path):
    return json.loads(path.read_text())


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def overlap(left, right):
    return left[0] < right[1] and right[0] < left[1]


def neural_block(path):
    body = next(node for node in ast.parse(path.read_text()).body
                if isinstance(node, ast.FunctionDef) and node.name == "main").body
    start = next(index for index, node in enumerate(body) if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == "initialized" for target in node.targets))
    end = next(index for index, node in enumerate(body) if ast.unparse(node) == "model.cpu().float().eval()")
    return [ast.dump(node, include_attributes=False) for node in body[start:end + 1]]


def verify_inputs():
    protocol, manifest = read(HERE / "protocol.json"), read(HERE / "manifest.json")
    preparation, provenance = read(HERE / "preparation.json"), read(HERE / "embedding-provenance.json")
    manifest_hash = sha(HERE / "manifest.json")
    assert manifest_hash == preparation["manifest_sha256"] == provenance["manifest_sha256"]
    assert sha(HERE / "protocol.json") == provenance["protocol_sha256"]
    for item in manifest["inputs"].values():
        assert sha(ROOT / item["path"]) == item["sha256"]
    assert sha(ROOT / protocol["source_snapshot"]) == protocol["source_sha256"]
    assert sha(HERE / "prepare.py") == preparation["prepare_script_sha256"]
    snapshot = read(ROOT / protocol["source_snapshot"])
    chunks = sorted(snapshot["chunks"], key=lambda chunk: chunk["sequence_no"])
    assert len(chunks) == 24 and [chunk["sequence_no"] for chunk in chunks] == list(range(24))
    cues = {}
    for chunk in chunks:
        media = chunk["detail"]["evidence"]["media"]
        assert media["video_id"] == manifest["video"]["id"]
        for index, cue in enumerate(chunk["detail"]["evidence"]["asr"]["transcript"]):
            start, end = cue["start_seconds"], cue["end_seconds"]
            assert type(start) is type(end) is int and media["start_ms"] <= start < end <= media["end_ms"]
            cues[f'asr_c{chunk["sequence_no"]:02d}_i{index:03d}'] = (start, end, chunk, cue)
    assert len(cues) == 378
    samples = manifest["samples"]
    assert len(samples) == 439 and len({sample["id"] for sample in samples}) == 439
    asr = [sample for sample in samples if sample["role"] == "asr"]
    holdouts = [sample for sample in samples if sample["role"] == "holdout"]
    assert len(asr) == 436 and {sample["id"] for sample in holdouts} == {"v11_02", "v11_27", "balanced_null_target"}
    assert {sample["cue_id"] for sample in asr} == set(cues)
    old_manifest = read(OLD / "manifest.json")
    assert manifest["video"] == old_manifest["video"]
    old_samples = {sample["id"]: sample for sample in old_manifest["samples"]}
    holdout_hashes = {sample["pcm_sha256"] for sample in holdouts}
    seen, reasons, actual_fit = {}, Counter(), []
    for sample in samples:
        audio_path = HERE / sample["audio"]
        assert sha(audio_path) == sample["wav_sha256"]
        with wave.open(str(audio_path), "rb") as audio:
            assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (16000, 1, 2)
            frames = audio.getnframes()
            pcm = audio.readframes(frames)
        assert len(pcm) == frames * 2 and frames == (sample["end_ms"] - sample["start_ms"]) * 16
        assert hashlib.sha256(pcm).hexdigest() == sample["pcm_sha256"]
        assert frames / 16 == sample["duration_ms"]
        values = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
        rms = float(np.sqrt(np.mean(values * values))) / 32768
        dbfs = max(-120.0, 20 * math.log10(rms)) if rms else -120.0
        assert abs(dbfs - sample["rms_dbfs"]) < 1e-10
        expected_reasons = []
        if sample["role"] == "asr":
            start, end, chunk, cue = cues[sample["cue_id"]]
            assert (sample["cue_start_ms"], sample["cue_end_ms"], sample["asr_speaker_id"], sample["text"]) == (
                start, end, cue["speaker_id"], cue["text"])
            assert (sample["chunk_id"], sample["chunk_sequence"], sample["content_version"]) == (
                chunk["chunk_id"], chunk["sequence_no"], chunk["version"])
            parts = math.ceil((end - start) / protocol["segments"]["maximum_ms"])
            part = sample["part_index"]
            assert sample["part_count"] == parts and 0 <= part < parts
            assert (sample["start_ms"], sample["end_ms"]) == (
                start + (end - start) * part // parts, start + (end - start) * (part + 1) // parts)
            if any(overlap((start, end), interval) for interval in protocol["held_out_intervals_ms"]):
                expected_reasons.append("holdout_related_cue")
            if any(other_id != sample["cue_id"] and overlap((start, end), values[:2]) for other_id, values in cues.items()):
                expected_reasons.append("cue_overlaps_other_asr")
            if sample["duration_ms"] < protocol["segments"]["minimum_fit_ms"]:
                expected_reasons.append("shorter_than_1500_ms")
            for name, interval in zip(("op_range", "ed_range"), protocol["known_music_intervals_ms"]):
                if overlap((sample["start_ms"], sample["end_ms"]), interval):
                    expected_reasons.append(name)
            # A copied holdout appended later must never leak into the fitting pool.
            if sample["pcm_sha256"] in holdout_hashes:
                assert sample["fit_eligible"] is False
        else:
            original = old_samples[sample["id"]]
            assert sample["wav_sha256"] == original["wav_sha256"] and sample["pcm_sha256"] == original["pcm_sha256"]
            assert sample["known_actual_speaker"] == original["known_actual_speaker"]
            expected_reasons.append("holdout")
        if dbfs < protocol["segments"]["silence_below_dbfs"]:
            expected_reasons.append("silence_rms_below_minus50_dbfs")
        if sample["pcm_sha256"] in seen:
            expected_reasons.append("duplicate_pcm")
            assert sample["duplicate_of_sample_id"] == seen[sample["pcm_sha256"]]
        seen.setdefault(sample["pcm_sha256"], sample["id"])
        assert sample["exclusion_reasons"] == expected_reasons
        assert sample["fit_eligible"] is (not expected_reasons)
        reasons.update(expected_reasons)
        if sample["fit_eligible"]:
            actual_fit.append(sample["id"])
    for cue_id, (start, end, _chunk, _cue) in cues.items():
        parts = sorted((sample for sample in asr if sample["cue_id"] == cue_id), key=lambda sample: sample["part_index"])
        assert parts[0]["start_ms"] == start and parts[-1]["end_ms"] == end
        assert [sample["part_index"] for sample in parts] == list(range(len(parts)))
        assert all(left["end_ms"] == right["start_ms"] for left, right in zip(parts, parts[1:]))
    assert len(actual_fit) == manifest["summary"]["fit_sample_count"] == 226
    assert dict(reasons) == manifest["summary"]["exclusion_reason_counts"]
    assert sum(end - start for start, end, _chunk, _cue in cues.values()) == sum(sample["duration_ms"] for sample in asr) == 683500
    assert preparation["decode_invocations"] == 1 and preparation["temporary_audio_cleaned"] is True

    status, rows = read(HERE / "embedding-status.json"), read(HERE / "embeddings.json")
    assert status == {"status": "complete", "sample_count": 439}
    assert len(rows) == provenance["sample_count"] == 439 and {row["id"] for row in rows} == {sample["id"] for sample in samples}
    by_id, vectors = {sample["id"]: sample for sample in samples}, {}
    for row in rows:
        assert row["dimension"] == len(row["vector"]) == 256
        assert all(math.isfinite(value) for value in row["vector"]) and abs(math.hypot(*row["vector"]) - 1) < 1e-6
        assert row["pcm_sha256"] == by_id[row["id"]]["pcm_sha256"] and row["duration_ms"] == by_id[row["id"]]["duration_ms"]
        vectors[row["id"]] = np.array(row["vector"], dtype=np.float64)
    old_provenance = read(OLD / "wavlm_large/provenance.json")
    assert provenance["state_dict"] == old_provenance["state_dict"]
    assert provenance["state_dict"]["strict"] is True and provenance["state_dict"]["keys"] == 710
    assert provenance["source_sha256"] == old_provenance["source_sha256"]
    assert provenance["backbone_configuration"] == old_provenance["backbone_configuration"]
    assert sha(HERE / "embed.py") == provenance["runner_sha256"]
    previous_runner = ROOT / provenance["compatibility_previous_runner"]
    assert sha(previous_runner) == provenance["compatibility_previous_runner_sha256"] == old_provenance["runner_sha256"]
    assert neural_block(HERE / "embed.py") == neural_block(previous_runner)
    model_home = ROOT / "outputs/local-models/wavlm-speaker-sv/UniSpeech"
    for filename, digest in provenance["source_sha256"].items():
        assert sha(model_home / filename) == digest
    for key in ("checkpoint", "backbone_config_checkpoint"):
        assert provenance[key + "_sha256"] == old_provenance[key + "_sha256"] == sha(ROOT / provenance[key])
    old_vectors = {row["id"]: np.array(row["vector"], dtype=np.float64) for row in read(OLD / "wavlm_large/embeddings.json")}
    parity = []
    for sample in holdouts:
        left, right = old_vectors[sample["id"]], vectors[sample["id"]]
        difference = float(np.max(np.abs(left - right)))
        cosine = float(left @ right / np.linalg.norm(left) / np.linalg.norm(right))
        assert difference <= 1e-5 and cosine >= 1 - 1e-6
        parity.append({"id": sample["id"], "normalized_max_abs": difference, "vector_cosine": cosine})
    checks = {"chunks": 24, "source_asr_cues": 378, "asr_subsegments": 436, "holdouts": 3,
              "fit_samples": 226, "pcm_and_wav_hash_matches": 439, "source_duration_ms": 683500,
              "asr_holdout_pcm_intersection": len({sample["pcm_sha256"] for sample in asr} & holdout_hashes),
              "holdout_vector_parity": parity, "neural_loading_and_wrapper_AST_equal": True,
              "exclusion_counts": dict(reasons)}
    return protocol, samples, vectors, checks


def verify_clustering(protocol, samples, vectors):
    from scipy.cluster.vq import kmeans2
    from scipy.optimize import linear_sum_assignment

    result = read(HERE / "clustering.json")
    for filename, digest in result["inputs"].items():
        assert sha(HERE / filename) == digest
    assert result["protocol"] == protocol and result["selection_uses_weak_labels_or_holdouts"] is False
    assert result["sample_count"] == len(samples) == 439
    indices = {sample["id"]: index for index, sample in enumerate(samples)}
    values = np.array([vectors[sample["id"]] for sample in samples])
    norms = np.sqrt(np.einsum("ij,ij->i", values, values))
    np.testing.assert_allclose(result["input_max_unit_norm_error"], np.max(np.abs(norms - 1)), atol=1e-14)
    values = values / norms[:, None]
    fit_indices = np.array([index for index, sample in enumerate(samples) if sample["fit_eligible"]])
    fitting = values[fit_indices]
    assert result["fit_sample_ids"] == [samples[index]["id"] for index in fit_indices]
    assert result["fit_count"] == len(fit_indices) == 226
    similarity = np.clip(np.einsum("ij,kj->ik", values, values), -1, 1)
    fit_cosine_distances = np.clip(1 - similarity[np.ix_(fit_indices, fit_indices)], 0, 2)
    np.fill_diagonal(fit_cosine_distances, 0)

    def distances(points, centers):
        residual = points[:, None, :] - centers[None, :, :]
        answer = np.einsum("ijk,ijk->ij", residual, residual)
        assert np.all(np.isfinite(answer)) and np.all(answer >= 0)
        return answer

    def silhouette(labels):
        scores = []
        groups = set(labels)
        for index, label in enumerate(labels):
            same = [other for other, other_label in enumerate(labels) if other != index and other_label == label]
            if not same:
                scores.append(0.)
                continue
            a = float(np.mean(fit_cosine_distances[index, same]))
            b = min(float(np.mean(fit_cosine_distances[index, labels == other])) for other in groups if other != label)
            scores.append((b - a) / max(a, b) if max(a, b) else 0.)
        return float(np.mean(scores))

    def adjusted_rand(left, right):
        choose2 = lambda size: size * (size - 1) / 2
        cells = Counter(zip(left, right))
        observed = sum(choose2(size) for size in cells.values())
        left_pairs = sum(choose2(size) for size in Counter(left).values())
        right_pairs = sum(choose2(size) for size in Counter(right).values())
        expected = left_pairs * right_pairs / choose2(len(left))
        denominator = (left_pairs + right_pairs) / 2 - expected
        return float((observed - expected) / denominator) if denominator else 1.

    scans = result["k_scan"]
    assert [scan["k"] for scan in scans] == protocol["k_values"]
    group_count, start_count, largest_distance_difference = 0, 0, 0.
    for scan in scans:
        k = scan["k"]
        starts = scan["starts"]
        assert [(item["group"], item["start"], item["seed"]) for item in starts] == [
            (group, start, 1000 * group + start) for group in protocol["seed_groups"]
            for start in range(protocol["starts_per_group"])]
        assert len(scan["groups"]) == 5
        start_count += len(starts)
        final_labels = np.array(scan["labels"])
        final_centers = np.array(scan["centers"])
        assert set(final_labels) == set(range(k)) and final_centers.shape == (k, 256)
        final_distance = distances(fitting, final_centers)
        np.testing.assert_array_equal(final_distance.argmin(axis=1), final_labels)
        np.testing.assert_allclose(final_distance[np.arange(len(fitting)), final_labels].sum(), scan["final_sse"], atol=1e-10)
        assert scan["cluster_sizes"] == np.bincount(final_labels, minlength=k).tolist()
        first_times = [min(samples[fit_indices[index]]["start_ms"] for index in np.flatnonzero(final_labels == label)) for label in range(k)]
        assert first_times == sorted(first_times)
        for group in scan["groups"]:
            group_count += 1
            successes = [item for item in starts if item["group"] == group["group"] and item["status"] == "success"]
            assert successes and min(successes, key=lambda item: item["sse"])["seed"] == group["seed"]
            labels, centers = np.array(group["labels"]), np.array(group["centers"])
            assert centers.shape == (k, 256) and set(labels) == set(range(k))
            distance = distances(fitting, centers)
            np.testing.assert_array_equal(distance.argmin(axis=1), labels)
            np.testing.assert_allclose(distance[np.arange(len(fitting)), labels].sum(), group["sse"], atol=1e-10)
            for label in range(k):
                np.testing.assert_allclose(centers[label], fitting[labels == label].mean(axis=0), atol=1e-10)
            np.testing.assert_allclose(silhouette(labels), group["cosine_silhouette"], atol=1e-12)
            np.testing.assert_allclose(adjusted_rand(labels, final_labels), group["ari_to_final"], atol=1e-12)
            counts = np.array([[np.count_nonzero((labels == a) & (final_labels == b)) for b in range(k)] for a in range(k)])
            left, right = linear_sum_assignment(-counts)
            alignment = np.empty(k, dtype=int)
            alignment[left] = right
            np.testing.assert_array_equal(alignment, group["label_alignment"])
        winner = min(scan["groups"], key=lambda item: item["sse"])
        assert scan["final_seed"] == winner["seed"]
        np.testing.assert_allclose(scan["final_sse"], winner["sse"], atol=1e-12)
        raw_labels, raw_centers = np.array(winner["labels"]), np.array(winner["centers"])
        order = sorted(range(k), key=lambda label: min((samples[fit_indices[index]]["start_ms"], index)
                       for index in np.flatnonzero(raw_labels == label)))
        np.testing.assert_allclose(final_centers, raw_centers[order], atol=1e-12)
        np.testing.assert_array_equal(final_labels, np.argsort(order)[raw_labels])
        np.testing.assert_allclose(scan["median_cosine_silhouette"], np.median([group["cosine_silhouette"] for group in scan["groups"]]), atol=1e-12)
    chosen = min(scans, key=lambda scan: (-scan["median_cosine_silhouette"], scan["k"]))
    assert (result["selected_k"], result["selected_seed"]) == (chosen["k"], chosen["final_seed"])
    # Re-execute one frozen seed only, independently of stored centers/labels.
    reproduced_centers, _ = kmeans2(fitting, chosen["k"], iter=protocol["lloyd_iterations"], minit="++", missing="raise",
                                     rng=np.random.default_rng(chosen["final_seed"]))
    stored_group = next(group for group in chosen["groups"] if group["seed"] == chosen["final_seed"])
    np.testing.assert_allclose(reproduced_centers, stored_group["centers"], atol=1e-12)
    np.testing.assert_array_equal(distances(fitting, reproduced_centers).argmin(axis=1), stored_group["labels"])

    centers = np.array(chosen["centers"])
    predicted_distances = distances(values, centers)
    predicted = predicted_distances.argmin(axis=1)
    margins = np.sort(predicted_distances, axis=1)[:, 1] - np.sort(predicted_distances, axis=1)[:, 0]
    normalized_centers = centers / np.sqrt(np.einsum("ij,ij->i", centers, centers))[:, None]
    center_cosines = np.einsum("ij,kj->ik", values, normalized_centers)
    stability = np.zeros(len(samples), dtype=int)
    for group in chosen["groups"]:
        group_predictions = distances(values, np.array(group["centers"])).argmin(axis=1)
        stability += np.array(group["label_alignment"])[group_predictions] == predicted

    def independent_support(index):
        sample = samples[index]
        candidates = [int(other) for other in fit_indices if predicted[other] == predicted[index]]
        selected, used_cues, used_pcm = [], {sample["cue_id"]}, {sample["pcm_sha256"]}
        for other in sorted(candidates, key=lambda value: (-similarity[index, value], samples[value]["start_ms"], samples[value]["id"])):
            candidate = samples[other]
            if candidate["cue_id"] in used_cues or candidate["pcm_sha256"] in used_pcm:
                continue
            interval = candidate["start_ms"], candidate["end_ms"]
            if overlap(interval, (sample["start_ms"], sample["end_ms"])) or any(overlap(interval, (samples[value]["start_ms"], samples[value]["end_ms"])) for value in selected):
                continue
            selected.append(other)
            used_cues.add(candidate["cue_id"])
            used_pcm.add(candidate["pcm_sha256"])
            if len(selected) == 10:
                break
        return selected

    supports, ranks = {}, {}
    assert [assignment["id"] for assignment in result["assignments"]] == [sample["id"] for sample in samples]
    for index, assignment in enumerate(result["assignments"]):
        sample = samples[index]
        for field in ("role", "cue_id", "chunk_sequence", "start_ms", "end_ms", "audio", "pcm_sha256", "text", "weak_speakers", "known_actual_speaker", "fit_eligible", "exclusion_reasons"):
            assert assignment[field] == sample.get(field)
        assert assignment["cluster"] == int(predicted[index]) and assignment["aligned_membership_count"] == int(stability[index])
        difference = float(np.max(np.abs(predicted_distances[index] - assignment["squared_center_distances"])))
        largest_distance_difference = max(largest_distance_difference, difference)
        np.testing.assert_allclose(predicted_distances[index], assignment["squared_center_distances"], atol=1e-12)
        np.testing.assert_allclose(center_cosines[index], assignment["normalized_center_cosines_diagnostic_only"], atol=1e-12)
        np.testing.assert_allclose(margins[index], assignment["euclidean_margin"], atol=1e-12)
        support = independent_support(index)
        assert assignment["support_ids"] == [samples[other]["id"] for other in support] and assignment["support_count"] == len(support)
        score = float(np.mean(similarity[index, support])) if support else None
        if score is None:
            assert assignment["same_cluster_support"] is None
        else:
            np.testing.assert_allclose(score, assignment["same_cluster_support"], atol=1e-12)
        supports[index] = score
        ranks[index] = (-stability[index], -score if score is not None else float("inf"), -margins[index], sample["start_ms"], sample["id"])

    def representatives(candidates, count):
        selected = []
        for distinct_chunk in (True, False):
            for index in sorted(candidates, key=ranks.get):
                sample = samples[index]
                if index in selected or any(sample["cue_id"] == samples[other]["cue_id"] or sample["pcm_sha256"] == samples[other]["pcm_sha256"]
                                          or overlap((sample["start_ms"], sample["end_ms"]), (samples[other]["start_ms"], samples[other]["end_ms"]))
                                          for other in selected):
                    continue
                if distinct_chunk and any(sample["chunk_sequence"] == samples[other]["chunk_sequence"] for other in selected):
                    continue
                selected.append(index)
                if len(selected) == count:
                    return selected
        return selected

    for cluster in result["clusters"]:
        members = [int(index) for index in fit_indices if predicted[index] == cluster["cluster"]]
        representative = representatives(members, protocol["representatives"]["count_per_cluster"])
        assert cluster["representative_ids"] == [samples[index]["id"] for index in representative]
        assert (cluster["fit_count"], cluster["fit_cue_count"], cluster["fit_chunk_count"]) == (
            len(members), len({samples[index]["cue_id"] for index in members}), len({samples[index]["chunk_sequence"] for index in members}))
        remaining = [index for index in members if index not in representative]
        boundary = min(remaining, key=lambda index: (margins[index], supports[index] if supports[index] is not None else -2, samples[index]["start_ms"])) if remaining else None
        assert cluster["boundary_example_id"] == (samples[boundary]["id"] if boundary is not None else None)
    for name, coverage in result["weak_speaker_coverage"].items():
        members = [index for index, sample in enumerate(samples) if sample["role"] == "asr" and name in (sample["weak_speakers"] or ["[无具名标签]"])]
        fitted = [index for index in members if samples[index]["fit_eligible"]]
        assert coverage["asr_windows"] == len(members) and coverage["asr_cues"] == len({samples[index]["cue_id"] for index in members})
        assert coverage["fit_windows"] == len(fitted) and coverage["chunks"] == sorted({samples[index]["chunk_sequence"] for index in members})
        assert coverage["cluster_asr_windows"] == {str(label): sum(predicted[index] == label for index in members) for label in range(chosen["k"]) if any(predicted[index] == label for index in members)}
        assert coverage["listening_hint_ids"] == [samples[index]["id"] for index in representatives(fitted or members, 2)]
    conditions = result["held_out_diagnostic_conditions"]
    expected_condition_ids = [sample["id"] for sample in samples if sample["role"] == "holdout" or "holdout_related_cue" in sample["exclusion_reasons"]]
    assert [condition["id"] for condition in conditions] == expected_condition_ids and len(conditions) == 5
    assert all(samples[indices[condition["id"]]]["fit_eligible"] is False for condition in conditions)
    primary_prediction = predicted.copy()
    derivative = read(HERE / "granularity.json")
    for filename, digest in derivative["inputs"].items():
        assert sha(HERE / filename) == digest
    assert derivative["models_refitted"] == 0 and derivative["primary_selected_k"] == chosen["k"]
    assert [view["k"] for view in derivative["views"]] == protocol["k_values"]
    query_indices = [indices[condition["id"]] for condition in conditions]
    evidence_count, representative_count, comparison_count, individual_cosine_count = 0, 0, 0, 0
    for model, view in zip(scans, derivative["views"]):
        assert view["seed"] == model["final_seed"] and view["median_cosine_silhouette"] == model["median_cosine_silhouette"]
        all_distances = distances(values, np.array(model["centers"]))
        predicted = all_distances.argmin(axis=1)
        np.testing.assert_array_equal(predicted[fit_indices], model["labels"])
        margins = np.sort(all_distances, axis=1)[:, 1] - np.sort(all_distances, axis=1)[:, 0]
        stability = np.zeros(len(samples), dtype=int)
        for group in model["groups"]:
            raw_prediction = distances(values, np.array(group["centers"])).argmin(axis=1)
            stability += np.array(group["label_alignment"])[raw_prediction] == predicted
        supports, ranks = {}, {}
        expected_evidence = {}
        for index in [*fit_indices, *query_indices]:
            near = independent_support(index)
            score = float(np.mean(similarity[index, near])) if near else None
            supports[index] = score
            ranks[index] = (-stability[index], -score if score is not None else float("inf"), -margins[index], samples[index]["start_ms"], samples[index]["id"])
            expected_evidence[index] = {"id": samples[index]["id"], "cluster": int(predicted[index]),
                                        "aligned_membership_count": int(stability[index]), "same_cluster_support": score,
                                        "support_count": len(near), "support_ids": [samples[other]["id"] for other in near],
                                        "euclidean_margin": float(margins[index])}

        def check_evidence(actual, index):
            nonlocal evidence_count
            evidence_count += 1
            for key, value in expected_evidence[index].items():
                if key in ("same_cluster_support", "euclidean_margin") and value is not None:
                    np.testing.assert_allclose(actual[key], value, atol=1e-12)
                else:
                    assert actual[key] == value

        assert len(view["clusters"]) == model["k"]
        for cluster in view["clusters"]:
            members = [int(index) for index in fit_indices if predicted[index] == cluster["cluster"]]
            assert cluster["member_ids"] == [samples[index]["id"] for index in members]
            assert (cluster["fit_count"], cluster["fit_cue_count"], cluster["fit_chunk_count"]) == (
                len(members), len({samples[index]["cue_id"] for index in members}), len({samples[index]["chunk_sequence"] for index in members}))
            assert cluster["weak_label_counts"] == dict(Counter(name for index in members for name in (samples[index]["weak_speakers"] or ["无具名标签"])))
            expected_representatives = representatives(members, 3)
            assert [row["id"] for row in cluster["representatives"]] == [samples[index]["id"] for index in expected_representatives]
            representative_count += len(expected_representatives)
            for row, index in zip(cluster["representatives"], expected_representatives):
                check_evidence(row, index)
            if model["k"] == chosen["k"]:
                assert [row["id"] for row in cluster["representatives"]] == result["clusters"][cluster["cluster"]]["representative_ids"]
            remaining = [index for index in members if index not in expected_representatives]
            boundary = min(remaining, key=lambda index: (margins[index], samples[index]["start_ms"])) if remaining else None
            if boundary is None:
                assert cluster["boundary_example"] is None
            else:
                check_evidence(cluster["boundary_example"], boundary)
        assert [row["id"] for row in view["queries"]] == expected_condition_ids
        for actual, condition, index in zip(view["queries"], conditions, query_indices):
            for key, value in condition.items():
                assert actual[key] == value
            check_evidence(actual, index)
            np.testing.assert_allclose(actual["squared_center_distances"], all_distances[index], atol=1e-12)
            assert [row["cluster"] for row in actual["representative_comparisons"]] == list(range(model["k"]))
            for comparison, cluster in zip(actual["representative_comparisons"], view["clusters"]):
                expected_reference_ids = [row["id"] for row in cluster["representatives"]]
                assert comparison["reference_count"] == len(expected_reference_ids) > 0
                assert [row["reference_id"] for row in comparison["individual_scores"]] == expected_reference_ids
                cosines = []
                for row in comparison["individual_scores"]:
                    reference = indices[row["reference_id"]]
                    assert reference != index and samples[reference]["fit_eligible"]
                    assert samples[reference]["pcm_sha256"] != samples[index]["pcm_sha256"]
                    assert not overlap((samples[reference]["start_ms"], samples[reference]["end_ms"]), (samples[index]["start_ms"], samples[index]["end_ms"]))
                    cosine = float(np.einsum("i,i->", values[index], values[reference]))
                    np.testing.assert_allclose(row["cosine"], cosine, atol=1e-12)
                    cosines.append(cosine)
                    individual_cosine_count += 1
                np.testing.assert_allclose(comparison["mean_pairwise_cosine"], sum(cosines) / len(cosines), atol=1e-12)
                comparison_count += 1
    html_audio_counts = {}
    for filename in ("report.html", "granularity.html"):
        page = (HERE / filename).read_text()
        tags = re.findall(r"<audio\b[^>]*>", page)
        assert tags and all("preload='none'" in tag for tag in tags)
        audio_paths = [unquote(re.search(r"src='([^']+)'", tag).group(1)) for tag in tags]
        assert all((HERE / path).is_file() for path in audio_paths)
        if filename == "report.html":
            assert {sample["audio"] for sample in samples} <= set(audio_paths)
        html_audio_counts[filename] = len(tags)
    return {"clustering_sha256": sha(HERE / "clustering.json"), "cluster_script_sha256": sha(HERE / "cluster.py"),
            "k_candidates": len(scans), "seed_group_winners_checked": group_count, "start_records_checked": start_count,
            "recorded_successful_starts": sum(item["status"] == "success" for scan in scans for item in scan["starts"]),
            "selected_k": chosen["k"], "selected_seed": chosen["final_seed"], "selected_seed_reexecuted": True,
            "assignments_checked": len(samples), "silhouette_ARI_Hungarian_center_SSE_recomputed": True,
            "support_ranking_representatives_and_boundaries_checked": True, "weak_metadata_only_checks": True,
            "independent_direct_distance_max_abs_difference": largest_distance_difference,
            "diagnostic_conditions": [{"id": condition["id"], "cluster": int(primary_prediction[indices[condition["id"]]]),
                                       "known_actual_speaker": condition["known_actual_speaker"]} for condition in conditions],
            "granularity": {"sha256": sha(HERE / "granularity.json"), "script_sha256": sha(HERE / "granularity.py"),
                            "views_checked": len(derivative["views"]), "representatives_checked": representative_count,
                            "displayed_evidence_records_checked": evidence_count, "queries_checked": len(conditions) * len(scans),
                            "query_cluster_representative_means_checked": comparison_count,
                            "individual_query_reference_cosines_checked": individual_cosine_count,
                            "selected_k_unchanged": True, "primary_representatives_identical": True, "models_refitted": 0},
            "offline_html_audio_elements_checked": html_audio_counts,
            "scope": "Recomputed all 55 stored group winners and 439 predictions; reexecuted selected seed, not all 550 starts."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args()
    protocol, samples, vectors, checks = verify_inputs()
    report = {"verified_at": datetime.now(UTC).isoformat(), "stage": "inputs_and_embeddings",
              "inputs_and_embeddings": checks, "manifest_sha256": sha(HERE / "manifest.json"),
              "protocol_sha256": sha(HERE / "protocol.json"), "embedding_sha256": sha(HERE / "embeddings.json"),
              "verify_script_sha256": sha(Path(__file__)), "model_inference_calls": 0,
              "limits": ["Voice cluster membership is not a character identity probability.",
                         "Labels from Understanding are unverified diagnostics, not ground truth.",
                         "ASR slices use one full-video PCM decode; copied holdouts preserve their original PCM exactly.",
                         "This fixed input has zero ASR/holdout PCM intersections; reusing preparation on other inputs requires excluding holdout PCM before selecting fitting sources.",
                         "No fresh model inference or independent perceptual identity/audio annotation was performed by this verifier."]}
    if not args.preflight:
        report["clustering"] = verify_clustering(protocol, samples, vectors)
        report["stage"] = "complete"
    (HERE / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"stage": report["stage"], **checks}, ensure_ascii=False))


if __name__ == "__main__":
    main()
