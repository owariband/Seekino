"""Prepare one frozen offline VoiceprintAgent candidate set; no model or database calls."""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import wave

sys.dont_write_bytecode = True
import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
FROZEN = HERE.parent / "20261018-mygo-ep04-wavlm-kmeans"
OLD = ROOT / "engine/experiment/speaker-encoder/20261018-mygo-ep04-model-comparison"


def read(path):
    return json.loads(path.read_text())


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def freeze(path, value):
    if path.exists():
        if read(path) != value:
            raise RuntimeError(f"Refusing to replace frozen output: {path}; use a new experiment directory")
        return
    with path.open("x") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def main():
    manifest = read(FROZEN / "manifest.json")
    snapshot_path = ROOT / manifest["inputs"]["asr_snapshot"]["path"]
    paths = [FROZEN / name for name in ("protocol.json", "manifest.json", "embeddings.json", "embedding-provenance.json", "clustering.json", "cluster.py")]
    paths += [snapshot_path, OLD / "manifest.json", OLD / "wavlm_large/embeddings.json"]
    input_hashes = {str(path.relative_to(ROOT)): sha(path) for path in paths}
    protocol_path = HERE / "protocol.json"
    if not protocol_path.exists():
        freeze(protocol_path, {
            "version": 1, "experiment": HERE.name, "inputs": input_hashes,
            "clustering": {"k": 8, "seed": 1000, "reuse_frozen_model": True},
            "distance": "1 - cosine on unit embeddings", "radius": 0.40, "minimum_independent_neighbors": 2,
            "medoid": "minimum cue-balanced weighted distance sum within Q; each cue has total weight 1",
            "selection": {"maximum_per_group": 3, "minimum_added_distance": 0.05, "prefer_distinct_chunks": True,
                          "source_ties": "start_ms then sample_id", "budget_is_not_a_speaker_cap": True},
            "eligibility": {"reuse_frozen_fit_mask": True, "expected_fit_count": 226,
                            "exclude_whole_holdout_cues_pcm_and_overlap": True},
            "scoring": {"primary": "mean_pairwise_cosine", "secondary": "normalized_centroid_cosine",
                        "all_registered_speakers": "retain every registered speaker; never remove competitors",
                        "original_six_speaker_pool": ["丰川祥子", "千早爱音", "椎名立希", "要乐奈", "长崎爽世", "高松灯"],
                        "missing_speakers": "report explicitly in the original six-speaker comparison",
                        "old_reference_fallback": "separate mixed-bank control only; preserve missing speakers' old references",
                        "excluded_old_reference_ids": ["v11_02", "v11_27"], "feedback_target_scores_to_agent": False},
            "agent_budget": {"maximum_runs": 2, "maximum_tool_calls": 64, "maximum_model_calls": 65, "timeout_seconds": 900},
            "limits": ["K8 and the target results have already been explored; this is not a blind test.",
                       "Pre-inference budget correction: Codex reserves one model slot per tool callback; 65 supports the unchanged 64-tool cap plus initial generation (was 20).",
                       "Neighborhood support and diversity do not certify speaker identity.",
                       "One discarded harness run duplicated weak identities in members; one corrected run is allowed before target scoring, with unchanged candidates and geometry.",
                       "Zero-Q groups and scarce speakers remain visible; no threshold relaxation to fill a quota."]})
    protocol = read(protocol_path)
    assert protocol["inputs"] == input_hashes
    frozen_protocol, clustering, provenance = (read(FROZEN / name) for name in ("protocol.json", "clustering.json", "embedding-provenance.json"))
    assert sha(snapshot_path) == frozen_protocol["source_sha256"]
    for filename, digest in clustering["inputs"].items():
        assert sha(FROZEN / filename) == digest
    model = next(row for row in clustering["k_scan"] if row["k"] == protocol["clustering"]["k"])
    assert model["final_seed"] == protocol["clustering"]["seed"]
    spec = importlib.util.spec_from_file_location("frozen_voice_clustering", FROZEN / "cluster.py")
    core = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(core)
    samples, rows = manifest["samples"], read(FROZEN / "embeddings.json")
    assert [row["id"] for row in rows] == [sample["id"] for sample in samples]
    values = np.array([row["vector"] for row in rows], dtype=np.float64)
    assert values.shape == (439, 256) and np.isfinite(values).all()
    values /= np.linalg.norm(values, axis=1)[:, None]
    similarities = np.clip(np.einsum("id,jd->ij", values, values, optimize=False), -1, 1)
    distances = 1 - similarities
    fit = [index for index, sample in enumerate(samples) if sample["fit_eligible"]]
    assert len(fit) == protocol["eligibility"]["expected_fit_count"]
    assert [samples[index]["id"] for index in fit] == clustering["fit_sample_ids"]
    held_pcm = {sample["pcm_sha256"] for sample in samples if sample["role"] == "holdout"}
    for index in fit:
        sample = samples[index]
        assert sample["role"] == "asr" and not sample["exclusion_reasons"] and sample["pcm_sha256"] not in held_pcm
        assert not any(core.overlap((sample["cue_start_ms"], sample["cue_end_ms"]), interval) for interval in frozen_protocol["held_out_intervals_ms"])
        assert not any(core.overlap(core.interval(sample), core.interval(query)) for query in samples if query["role"] == "holdout")
        assert rows[index]["pcm_sha256"] == sample["pcm_sha256"]
    predicted = np.sum((values[:, None, :] - np.array(model["centers"])[None, :, :]) ** 2, axis=2).argmin(axis=1)
    np.testing.assert_array_equal(predicted[fit], model["labels"])
    encoder = {"name": provenance["model"], "encoder_revision": "sv-sha256:" + provenance["checkpoint_sha256"] + ";compat-sha256:" + provenance["compatibility_previous_runner_sha256"]}
    selection, groups, candidates = [], [], []
    tie = lambda index: (samples[index]["start_ms"], samples[index]["id"])
    source_ref = lambda index: "k8:" + samples[index]["id"]
    for label in range(model["k"]):
        group_id = f"K{model['k']}:C{label + 1:02d}"
        members = [index for index in fit if predicted[index] == label]
        neighbors = {index: [other for other in core.nearest_independent(index, members, samples, similarities, len(members))
                             if distances[index, other] <= protocol["radius"]] for index in members}
        q = [index for index in members if len(neighbors[index]) >= protocol["minimum_independent_neighbors"]]
        pending = [index for index in members if index not in q]
        counts = Counter(samples[index]["cue_id"] for index in q)
        costs = {index: sum(distances[index, other] / counts[samples[other]["cue_id"]] for other in q) for index in q}
        chosen, rounds, reason = [], [], "empty_Q"
        if q:
            medoid = min(q, key=lambda index: (costs[index], *tie(index)))
            chosen.append(medoid)
            rounds.append({"round": 1, "role": "medoid", "selected_sample_id": samples[medoid]["id"],
                           "weighted_costs": {samples[index]["id"]: float(costs[index]) for index in q}})
            reason = "display_budget_reached"
            while len(chosen) < protocol["selection"]["maximum_per_group"]:
                available = [index for index in q if all(samples[index]["cue_id"] != samples[other]["cue_id"]
                             and samples[index]["pcm_sha256"] != samples[other]["pcm_sha256"]
                             and not core.overlap(core.interval(samples[index]), core.interval(samples[other])) for other in chosen)]
                additions = {index: float(min(distances[index, other] for other in chosen)) for index in available}
                novel = [index for index in available if additions[index] >= protocol["selection"]["minimum_added_distance"]]
                if not novel:
                    reason = "no_independent_sources" if not available else "minimum_added_distance_not_met"
                    rounds.append({"round": len(chosen) + 1, "role": "stop", "reason": reason,
                                   "minimum_distances": {samples[index]["id"]: score for index, score in additions.items()}})
                    break
                chunks = {samples[index]["chunk_sequence"] for index in chosen}
                distinct = [index for index in novel if samples[index]["chunk_sequence"] not in chunks]
                eligible = distinct if distinct and protocol["selection"]["prefer_distinct_chunks"] else novel
                selected = min(eligible, key=lambda index: (-additions[index], *tie(index)))
                rounds.append({"round": len(chosen) + 1, "role": "diversity", "selected_sample_id": samples[selected]["id"],
                               "minimum_distances": {samples[index]["id"]: score for index, score in additions.items()},
                               "eligible_sample_ids": [samples[index]["id"] for index in eligible], "selected_distance": additions[selected]})
                chosen.append(selected)
        summary = {"group_id": group_id, "fit_count": len(members), "q_count": len(q), "pending_count": len(pending),
                   "selected_source_refs": [source_ref(index) for index in chosen], "status": "selected" if chosen else "empty_Q"}
        groups.append(summary)
        selection.append({**summary, "q_sample_ids": [samples[index]["id"] for index in q],
                          "pending": [{"sample_id": samples[index]["id"], "reason": "insufficient_independent_neighbors", "neighbor_ids": [samples[other]["id"] for other in neighbors[index]]} for index in pending],
                          "neighbors": {samples[index]["id"]: [samples[other]["id"] for other in neighbors[index]] for index in members},
                          "cue_window_counts_in_Q": dict(counts), "rounds": rounds, "stop_reason": reason})
        for position, index in enumerate(chosen):
            sample, row = samples[index], rows[index]
            audio_path = (FROZEN / sample["audio"]).resolve()
            assert audio_path.is_relative_to(FROZEN.resolve()) and sha(audio_path) == sample["wav_sha256"]
            with wave.open(str(audio_path), "rb") as audio:
                assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (16000, 1, 2)
                assert hashlib.sha256(audio.readframes(audio.getnframes())).hexdigest() == row["pcm_sha256"]
            chosen_round = next(item for item in rounds if item.get("selected_sample_id") == sample["id"])
            candidates.append({"source_ref": source_ref(index), "sample_id": sample["id"], "group_id": group_id,
                               "start_ms": sample["start_ms"], "end_ms": sample["end_ms"], "chunk_ref": sample["chunk_id"], "sequence_no": sample["chunk_sequence"],
                               "text": sample["text"], "weak_speaker": sample["weak_speakers"][0] if len(sample["weak_speakers"]) == 1 else None,
                               "weak_speakers": sample["weak_speakers"], "audio_path": str(audio_path), "pcm_sha256": row["pcm_sha256"], "embedding": row["vector"], "model": encoder,
                               "provenance": {"cue_id": sample["cue_id"], "content_version": sample["content_version"], "wav_sha256": sample["wav_sha256"], "embedding_input_sha256": sha(FROZEN / "embeddings.json"), "raw_embedding_norm": row["raw_norm"]},
                               "selection": {"role": chosen_round["role"], "round": position + 1, "weighted_distance_sum": float(costs[index]) if position == 0 else None,
                                             "min_distance_to_selected": chosen_round.get("selected_distance"), "neighbor_count": len(neighbors[index]), "neighbor_ids": [samples[other]["id"] for other in neighbors[index]]}})
    names = defaultdict(dict)
    for chunk in read(snapshot_path)["chunks"]:
        for event in chunk["content"]["timeline"]:
            if event["event_type"] == "DIALOGUE" and event.get("speaker"):
                names[event["speaker"]][chunk["chunk_id"]] = chunk["sequence_no"]
    speakers = [{"speaker": name, "chunk_ids": sorted(chunks, key=chunks.get), "sequence_nos": sorted(chunks.values())} for name, chunks in sorted(names.items())]
    all_ids = [candidate["sample_id"] for candidate in candidates]
    assert len(set(all_ids)) == len(all_ids)
    assert len({candidate["pcm_sha256"] for candidate in candidates}) == len(candidates)
    assert all(not core.overlap((a["start_ms"], a["end_ms"]), (b["start_ms"], b["end_ms"])) for i, a in enumerate(candidates) for b in candidates[i + 1:])
    common = {"protocol": {"path": str(protocol_path), "sha256": sha(protocol_path)}, "inputs": {**input_hashes, str(Path(__file__).relative_to(ROOT)): sha(Path(__file__))}}
    freeze(HERE / "selection.json", {**common, "groups": selection, "fit_count": len(fit), "candidate_count": len(candidates), "model_calls": 0})
    freeze(HERE / "candidates.json", {**common, "source_video_path": str((ROOT / manifest["video"]["path"]).resolve()), "video": manifest["video"], "model": encoder,
                                     "background": {"context": {"experiment": "offline anonymous candidates", "identity_labels": "weak evidence only"}},
                                     "speakers": speakers, "groups": groups, "candidates": candidates})
    print(json.dumps({"groups": len(groups), "candidates": len(candidates), "q_count": sum(group["q_count"] for group in groups), "pending_count": sum(group["pending_count"] for group in groups),
                      "empty_Q": [group["group_id"] for group in groups if group["status"] == "empty_Q"], "speaker_count": len(speakers), "strict_majority_required": len(speakers) // 2 + 1, "model_calls": 0}))


if __name__ == "__main__":
    main()
