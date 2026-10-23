"""Build a seeded, blinded top-10 M0–M5 union without creating relevance labels.

Method ownership, ranks, scores and collapse evidence go only to the separate
method_components.json audit. pool.json contains image identities and aliases.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import random

import numpy as np

from rank import ROOT, METHODS, load_embeddings, midpoint, read_jsonl, relative_image, require_gate, save_json, sha


class ShotPairSimilarity:
    """v1 pool collapse: same episode, midpoint separation <=5s, best cosine >=.97."""

    def __init__(self, rows, embedding_ids, matrix, window_ms=5000, threshold=0.97):
        self.shots = defaultdict(list)
        for row in rows:
            self.shots[row["shot_id"]].append(row)
        self.embedding_ids = embedding_ids
        self.matrix = matrix
        self.window_ms = window_ms
        self.threshold = threshold
        self.cache = {}

    def compare(self, left, right):
        key = tuple(sorted((left, right)))
        if key in self.cache:
            return self.cache[key]
        a, b = self.shots[key[0]], self.shots[key[1]]
        delta = abs(midpoint(a[0]) - midpoint(b[0]))
        evidence = {"shot_ids": list(key), "midpoint_distance_ms": delta, "same_episode": a[0]["episode"] == b[0]["episode"],
                    "best_cosine": None, "witness_frame_ids": None, "qualifies": False}
        if evidence["same_episode"] and delta <= self.window_ms:
            ai = [self.embedding_ids[r["frame_id"]] for r in a]
            bi = [self.embedding_ids[r["frame_id"]] for r in b]
            similarities = self.matrix[ai] @ self.matrix[bi].T
            ix, iy = np.unravel_index(np.argmax(similarities), similarities.shape)
            evidence["best_cosine"] = float(similarities[ix, iy])
            evidence["witness_frame_ids"] = [a[ix]["frame_id"], b[iy]["frame_id"]]
            evidence["qualifies"] = evidence["best_cosine"] >= self.threshold
        self.cache[key] = evidence
        return evidence


def support_order(shot_id, occurrences):
    rows = occurrences[shot_id]
    return (-len(rows), min(r["rank"] for r in rows), sum(r["rank"] for r in rows) / len(rows), shot_id)


def choose_frame(occurrences):
    frames = defaultdict(list)
    for row in occurrences:
        frames[row["frame_id"]].append(row)
    fid = min(frames, key=lambda value: (-len(frames[value]), sum(r["rank"] for r in frames[value]) / len(frames[value]), value))
    return frames[fid][0]


def complete_link_clusters(ordered_shots, compare):
    """Greedy deterministic assignment; every final cluster passes all pair checks."""
    groups = []
    for shot_id in ordered_shots:
        for group in groups:
            if all(compare(shot_id, member)["qualifies"] for member in group):
                group.append(shot_id)
                break
        else:
            groups.append([shot_id])
    return groups


def run(queries_path, seed):
    require_gate()
    manifest = json.loads((ROOT / "input_manifest.json").read_text())
    gallery = Path(manifest["gallery"])
    ranking_run = json.loads((ROOT / "rankings/run.json").read_text())
    if ranking_run["status"] != "complete" or ranking_run["source_hashes"]["queries"] != sha(queries_path):
        raise ValueError("Completed rankings must match this query set")
    if ranking_run["source_hashes"]["keyframe_tags"] != sha(ROOT / "keyframe_tags.jsonl"):
        raise ValueError("Tag data changed since ranking")
    scope = ranking_run["scope"]
    for field in ["manifest", "frames"]:
        if sha(ROOT / scope[field + "_path"]) != scope[field + "_sha256"]:
            raise ValueError("Declared annotation selection changed since ranking")
    if sha(gallery / "gallery.jsonl") != manifest["gallery_sha256"]:
        raise ValueError("Gallery changed from the experiment snapshot")
    for key, name in [("embeddings", "embeddings.npy"), ("embedding_rows", "embedding_rows.jsonl")]:
        if sha(gallery / name) != ranking_run["source_hashes"][key]:
            raise ValueError("Gallery embeddings changed since ranking")
    emb_rows, embedding_ids, matrix = load_embeddings(gallery)
    gallery_rows = read_jsonl(gallery / "gallery.jsonl")
    by_frame = {row["frame_id"]: row for row in gallery_rows}
    if set(by_frame) != set(embedding_ids):
        raise ValueError("Embedding/gallery frame coverage mismatch")
    for row in gallery_rows:
        if row["sha256"] != emb_rows[embedding_ids[row["frame_id"]]]["sha256"]:
            raise ValueError("Gallery/embedding SHA mismatch")
    pair_similarity = ShotPairSimilarity(gallery_rows, embedding_ids, matrix)
    queries = json.loads(queries_path.read_text())["queries"]
    if len(queries) != ranking_run["query_count"]:
        raise ValueError("Ranking/query count differs")
    public, audits = [], []
    ranking_hashes = {}
    for query in queries:
        qid = query["query_shot_id"]
        occurrences = defaultdict(list)
        for method in METHODS:
            path = ROOT / "rankings" / method / f"{qid}.json"
            ranking_hashes[str(path.relative_to(ROOT))] = sha(path)
            ranking = json.loads(path.read_text())
            if ranking["method"] != method or ranking["query_shot_id"] != qid:
                raise ValueError("Ranking identity differs from its file path")
            if len({row["shot_id"] for row in ranking["rows"]}) != len(ranking["rows"]):
                raise ValueError("Ranking contains duplicate shots")
            for position, row in enumerate(ranking["rows"], 1):
                if row["rank"] != position:
                    raise ValueError("Ranking must have contiguous one-based ranks")
            for row in ranking["rows"][:10]:
                occurrences[row["shot_id"]].append(dict(row, method=method))
        ordered = sorted(occurrences, key=lambda sid: support_order(sid, occurrences))
        groups = complete_link_clusters(ordered, pair_similarity.compare)
        candidates, candidate_audits = [], []
        for group in groups:
            canonical = min(group, key=lambda sid: support_order(sid, occurrences))
            evidence = choose_frame(occurrences[canonical])
            frame = by_frame[evidence["frame_id"]]
            if frame["shot_id"] != canonical:
                raise ValueError("Candidate frame/shot mismatch")
            candidate = {"candidate_shot_id": canonical, "frame_id": evidence["frame_id"],
                         "image_path": relative_image(frame, gallery), "collapsed_shot_ids": sorted(group)}
            candidates.append(candidate)
            accepted_pairs = [pair_similarity.compare(left, right) for i, left in enumerate(group) for right in group[i + 1:]]
            if not all(pair["qualifies"] for pair in accepted_pairs):
                raise AssertionError("Pool cluster is not complete-link")
            candidate_audits.append({"candidate_shot_id": canonical, "display_frame_id": evidence["frame_id"],
                                     "collapsed_shot_ids": sorted(group), "pair_evidence": accepted_pairs,
                                     "method_occurrences": [row for sid in sorted(group) for row in occurrences[sid]]})
        shuffle_seed = hashlib.sha256(f"{seed}:{qid}".encode()).hexdigest()
        random.Random(shuffle_seed).shuffle(candidates)
        query_frame = by_frame[query.get("frame_id", query.get("representative_frame_id"))]
        if query_frame["shot_id"] != qid:
            raise ValueError("Query frame/shot mismatch")
        public.append({"query_shot_id": qid, "frame_id": query_frame["frame_id"],
                       "image_path": relative_image(query_frame, gallery), "candidates": candidates})
        audits.append({"query_shot_id": qid, "union_shots_before_collapse": len(ordered),
                       "candidates_after_collapse": len(candidates), "shuffle_seed_sha256": shuffle_seed,
                       "candidates": candidate_audits})
        print(json.dumps({"query_shot_id": qid, "union_shots": len(ordered), "pooled_candidates": len(candidates)}), flush=True)
    # Validate every displayed local path before producing the review contract.
    for query in public:
        for row in [query, *query["candidates"]]:
            if not (ROOT / row["image_path"]).is_file():
                raise FileNotFoundError(row["image_path"])
    metadata = {
        "created_at": datetime.now(timezone.utc).isoformat(), "schema_version": "composition-pool-v1",
        "methods": list(METHODS), "top_k_per_method": 10, "random_seed": seed,
        "scope": scope,
        "collapse": {"version": "v1", "same_shot": "always unified before near-duplicate collapse",
                     "near_duplicate": "same episode AND all-pairs shot-midpoint distance <=5000ms AND every shot pair has at least one saved-keyframe Doubao cosine >=0.97",
                     "embedding_frames": "all saved gallery keyframes, including frames not selected for full tagging",
                     "clustering": "complete-link greedy insertion ordered by method support, best rank, mean rank and shot_id; no transitive chains",
                     "canonical_shot": "most top-10 method occurrences, then best rank, mean rank, lexical shot_id",
                     "display_frame": "within canonical shot: most top-10 method votes, then best mean rank, lexical frame_id",
                     "evidence_limitation": "One display frame and one user judgment represent each collapsed candidate; method-specific alternative keyframes remain in this audit."},
        "source_hashes": {"queries": sha(queries_path), "gallery": manifest["gallery_sha256"],
                          "ranking_run": sha(ROOT / "rankings/run.json"), "rankings": ranking_hashes,
                          "runner": sha(__file__)},
        "judgments": "No relevance labels generated. Method identities/ranks/scores are excluded from pool.json and judge.html.",
        "query_count": len(public), "candidate_pairs": sum(len(q["candidates"]) for q in public), "queries": audits,
    }
    save_json(ROOT / "pool.json", {"schema_version": "composition-pool-v1", "queries": public})
    metadata["pool_sha256"] = sha(ROOT / "pool.json")
    save_json(ROOT / "method_components.json", metadata)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queries", type=Path, default=ROOT / "queries.json")
    parser.add_argument("--seed", type=int, default=20261019)
    args = parser.parse_args()
    run(args.queries.resolve(), args.seed)
