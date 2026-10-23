"""Exhaustive M0–M5 composition rankings. Reads tags; never writes judgments.

All fusions operate on coherent keyframe evidence, then retain the best frame
per shot. Run only after a passing pilot gate and full tag assembly.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re

import numpy as np
import scipy
from scipy.optimize import linear_sum_assignment

from schema_models import count_bucket

ROOT = Path(__file__).resolve().parent
METHODS = {
    "M0": ["doubao"], "M1": ["doubao"], "M2": ["doubao", "tags"],
    "M3": ["doubao", "tags", "pcp"], "M4": ["doubao", "tags", "layout"],
    "M5": ["tags", "pcp", "layout"],
}
ANGLE = ["overhead", "high", "eye_level", "low", "worm"]
SIZE = ["extreme_close_up", "close_up", "medium_close_up", "medium", "medium_long", "full", "long", "extreme_long"]
SCALE = ["tiny", "small", "medium", "large", "dominant"]
COUNTS = ["0", "1", "2", "3–5", "many"]
PCP_ORDER = ["rule_of_thirds", "vertical", "horizontal", "diagonal", "curved", "triangle", "center", "symmetric", "pattern"]
CELLS = {name: divmod(i, 3) for i, name in enumerate(["TL", "TC", "TR", "ML", "MC", "MR", "BL", "BC", "BR"])}


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save_json(path, value):
    path = Path(path)
    if not path.resolve().is_relative_to(ROOT):
        raise ValueError("Outputs must stay in this experiment")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def require_gate():
    gate = json.loads((ROOT / "pilot/report.json").read_text())["gate"]
    if not gate.get("passed"):
        raise RuntimeError("Ranking requires a completed, passing pilot gate")
    return gate


def relative_image(row, gallery):
    image = Path(row.get("image_path", gallery / row["frame_path"]))
    if not image.is_absolute():
        image = ROOT / image
    return os.path.relpath(image, ROOT)


def midpoint(row):
    return (row["start_ms"] + row["end_ms"]) / 2


def exclusion_reasons(query, row, method):
    reasons = []
    if row["status"] != "valid":
        reasons.append("status:" + row["status"])
    elif row["frame"]["kind"] != "story":
        reasons.append("frame.kind:" + row["frame"]["kind"])
    if row["is_op_ed"]:
        reasons.append("is_op_ed")
    if row["shot_id"] == query["shot_id"]:
        reasons.append("same_query_shot")
    if row["reuse_group_id"] == query["reuse_group_id"]:
        reasons.append("same_reuse_group")
    if method != "M0" and row["episode"] == query["episode"] and abs(midpoint(row) - midpoint(query)) <= 60000:
        reasons.append("same_episode_midpoint_within_60000ms")
    return reasons


def ordinal(a, b, order):
    if a not in order or b not in order:
        return None
    return 1.0 - abs(order.index(a) - order.index(b)) / (len(order) - 1)


def jaccard(a, b):
    a, b = set(a), set(b)
    return len(a & b) / len(a | b) if a | b else None


def tag_similarity(a, b):
    ac, bc = a["camera"], b["camera"]
    ap, bp = a["composition"], b["composition"]
    ass, bs = a["subjects"], b["subjects"]
    size_a, size_b = ac.get("shot_size"), bc.get("shot_size")
    if "unknown" in (size_a, size_b) or None in (size_a, size_b):
        size = None
    elif "insert_detail" in (size_a, size_b):
        size = float(size_a == size_b)
    else:
        size = ordinal(size_a, size_b, SIZE)
    count_a, count_b = ass.get("person_count"), bs.get("person_count")
    count = None
    if count_a not in (None, "unknown") and count_b not in (None, "unknown"):
        delta = abs(COUNTS.index(count_bucket(count_a)) - COUNTS.index(count_bucket(count_b)))
        count = 1.0 if delta == 0 else 0.5 if delta == 1 else 0.0
    cell_a, cell_b = ass.get("main_subject_cell"), bs.get("main_subject_cell")
    cell = None
    if cell_a in CELLS and cell_b in CELLS:
        cell = 1 - sum(abs(x - y) for x, y in zip(CELLS[cell_a], CELLS[cell_b])) / 4
    comps_a = {ap.get("primary"), *ap.get("secondary", [])} - {None, "unknown"}
    comps_b = {bp.get("primary"), *bp.get("secondary", [])} - {None, "unknown"}
    fields = {
        "vertical_angle": ordinal(ac.get("vertical_angle"), bc.get("vertical_angle"), ANGLE),
        "shot_size": size,
        "composition_set": jaccard(comps_a, comps_b) if comps_a and comps_b else None,
        "person_count": count,
        "main_subject_cell": cell,
        "main_subject_scale": ordinal(ass.get("main_subject_scale"), bs.get("main_subject_scale"), SCALE),
        "negative_space_sides": jaccard(ap.get("negative_space", {}).get("sides", []), bp.get("negative_space", {}).get("sides", [])),
    }
    valid = [value for value in fields.values() if value is not None]
    return (sum(valid) / len(valid) if valid else None), fields


def box_geometry(boxes):
    boxes = np.asarray(boxes, dtype=np.float64).reshape((-1, 5))
    if not np.isfinite(boxes).all() or (boxes[:, :4] < 0).any() or (boxes[:, :4] > 1).any():
        raise ValueError("Detector boxes must be finite, normalized xyxy+score")
    spans = boxes[:, 2:4] - boxes[:, :2]
    if (spans <= 0).any():
        raise ValueError("Detector boxes must have positive area")
    return (boxes[:, :2] + boxes[:, 2:4]) / 2, spans.prod(axis=1)


def layout_distance(a, b, unmatched_penalty=2.0):
    ca, aa = a
    cb, ab = b
    n, m = len(aa), len(ab)
    if not n and not m:
        return None
    if not n or not m:
        return float(unmatched_penalty)
    costs = np.linalg.norm(ca[:, None, :] - cb[None, :, :], axis=2) / math.sqrt(2)
    costs += np.abs(np.log(aa[:, None] / ab[None, :]))
    ri, ci = linear_sum_assignment(costs)
    return float((costs[ri, ci].sum() + unmatched_penalty * abs(n - m)) / max(n, m))


def competition_ranks(values, descending=True):
    """Equal finite scores share the first rank; missing values get no rank."""
    order = sorted((i for i, value in enumerate(values) if value is not None),
                   key=lambda i: -values[i] if descending else values[i])
    result = [None] * len(values)
    prior, prior_rank = None, None
    for position, i in enumerate(order, 1):
        value = values[i]
        if not math.isfinite(value):
            raise ValueError("Non-finite component score")
        rank = prior_rank if value == prior else position
        result[i] = rank
        prior, prior_rank = value, rank
    return result


def rrf_score(ranks, components, k=60):
    return sum(1.0 / (k + ranks[name]) for name in components if ranks[name] is not None)


def load_embeddings(gallery):
    rows = read_jsonl(gallery / "embedding_rows.jsonl")
    matrix = np.load(gallery / "embeddings.npy").astype(np.float32)
    if matrix.ndim != 2 or matrix.shape[0] != len(rows) or not np.isfinite(matrix).all():
        raise ValueError("Invalid gallery embedding matrix")
    if [row["row"] for row in rows] != list(range(len(rows))):
        raise ValueError("Embedding row map is not contiguous")
    ids = {row["frame_id"]: i for i, row in enumerate(rows)}
    if len(ids) != len(rows):
        raise ValueError("Duplicate embedding frame IDs")
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    if (norms <= 0).any():
        raise ValueError("Zero embedding vector")
    return rows, ids, matrix / norms


def method_parameters(unmatched_penalty):
    return {
        "methods": METHODS, "rrf_k": 60, "rrf_weights": "equal, sum of 1/(60+rank); no renormalization",
        "component_ties": "competition ranks: equal raw scores share the first rank; final frame/shot ties use lexical IDs",
        "aggregation": "rank each component over eligible keyframes, fuse per keyframe, retain highest-score frame per shot",
        "multiple_frame_limitation": "Long shots have more opportunities to achieve a high score because they supply more keyframes.",
        "candidate_eligibility": "valid VLM status, frame.kind=story, not is_op_ed, different query shot and reuse_group_id",
        "M1_to_M5_extra_exclusion": "same episode and absolute difference between shot interval midpoints <=60000ms",
        "M0_M1_score": "Doubao image cosine; no RRF",
        "M5_doubao": "not used in ranking score or eligibility; gallery reuse IDs and later pool collapse still use prior Doubao evidence",
        "tag_similarity": {
            "aggregation": "mean of seven available fields with equal weight; unknown/missing omitted from denominator",
            "vertical_angle": {"order": ANGLE, "similarity": "1-|index_a-index_b|/4"},
            "shot_size": {"order": SIZE, "similarity": "1-|index_a-index_b|/7; insert_detail is nonordinal: exact match 1, otherwise 0"},
            "composition": "Jaccard of primary plus secondary; none_clear is an explicit category",
            "person_count": {"order": COUNTS, "similarity": "exact bucket 1, adjacent 0.5, otherwise 0; 6 is bucketed as many"},
            "main_subject_cell": "1-Manhattan distance on the 3x3 grid/4",
            "main_subject_scale": {"order": SCALE, "similarity": "1-|index_a-index_b|/4"},
            "negative_space_sides": "Jaccard; both empty is missing evidence and omitted",
            "unused": "clarity, subject names, location_hint and content tags do not weight or change similarity",
        },
        "pcp": "cosine over the same ordered nine class probabilities",
        "pcp_class_order": PCP_ORDER,
        "layout": {
            "pair_cost": "Euclidean distance between normalized box centers/sqrt(2) + absolute natural log area ratio (unscaled)",
            "assignment": "Hungarian minimum-cost matching of min(n,m) pairs",
            "unmatched_penalty": unmatched_penalty,
            "distance": "(sum matched costs + unmatched_penalty*abs(n-m))/max(n,m); lower is better",
            "empty": "both zero boxes -> unavailable; only one empty -> unmatched_penalty",
            "confidence": "box confidence is not used after thresholding/NMS",
        },
        "missing_components": "Unavailable component has null rank and contributes no RRF term. Missing detector evidence can therefore lower fused scores.",
        "relevance_judgments": "none; these are model-derived ranking features only",
    }


def selection_scope(tags, gallery_rows):
    """Honor the explicit completed-data subset when its manifest is present."""
    full_rows = read_jsonl(ROOT / "full/frames.jsonl")
    full_by_frame = {r["frame_id"]: r for r in full_rows}
    expected_full = {r["frame_id"] for r in gallery_rows if r["is_representative"] or r["end_ms"] - r["start_ms"] > 4000}
    if set(full_by_frame) != expected_full or len(full_by_frame) != len(full_rows):
        raise ValueError("Original full manifest must cover every representative and all saved frames of shots longer than 4s")
    exploration = (ROOT / "exploration/manifest.json").is_file()
    directory = "exploration" if exploration else "full"
    manifest_path = ROOT / directory / "manifest.json"
    frames_path = ROOT / directory / "frames.jsonl"
    selected = read_jsonl(frames_path)
    selected_ids = {r["frame_id"] for r in selected}
    if len(selected_ids) != len(selected) or selected_ids != {r["frame_id"] for r in tags}:
        raise ValueError("Assembled tags must exactly cover the declared selection without duplicate frames")
    if not selected_ids <= set(full_by_frame):
        raise ValueError("Selected frames must belong to the original full tagging manifest")
    for row in selected:
        original = full_by_frame[row["frame_id"]]
        for field in ["shot_id", "episode", "start_ms", "end_ms", "frame_timestamp_ms", "sha256", "is_representative"]:
            if row[field] != original[field]:
                raise ValueError(f"Selection changed source identity: {row['frame_id']} {field}")
    selected_shots = {r["shot_id"] for r in selected}
    if selected_shots != {r["shot_id"] for r in selected if r["is_representative"]}:
        raise ValueError("Every selected shot must include its completed representative")
    gallery_shots = {r["shot_id"] for r in gallery_rows}
    return {
        "kind": "exploratory_completed_subset" if exploration else "full_gallery",
        "manifest_path": str(manifest_path.relative_to(ROOT)), "frames_path": str(frames_path.relative_to(ROOT)),
        "manifest_sha256": sha(manifest_path), "frames_sha256": sha(frames_path),
        "annotated_keyframes": len(selected), "annotated_shots": len(selected_shots),
        "original_full_tagging_frames": len(full_rows), "gallery_keyframes": len(gallery_rows), "gallery_shots": len(gallery_shots),
        "untagged_gallery_shots": len(gallery_shots - selected_shots),
        "original_tagging_frames_outside_selection": len(set(full_by_frame) - selected_ids),
        "candidate_universe": "All methods start from exactly this annotated selection; valid/story/non-OPED/reuse exclusions apply to all, and M1–M5 additionally apply the same temporal exclusion.",
        "limitation": "Exploratory completed-data subset with episode bias; untagged gallery shots are not ranked and no full-gallery quality claim is supported." if exploration else "All requested gallery frames are annotated; invalid/non-story/OPED frames remain ineligible.",
    }


def run(queries_path, unmatched_penalty):
    gate = require_gate()
    tags_path = ROOT / "keyframe_tags.jsonl"
    tags = read_jsonl(tags_path)
    by_frame = {row["frame_id"]: row for row in tags}
    if len(by_frame) != len(tags):
        raise ValueError("Duplicate tagged frame IDs")
    manifest = json.loads((ROOT / "input_manifest.json").read_text())
    gallery = Path(manifest["gallery"])
    if sha(gallery / "gallery.jsonl") != manifest["gallery_sha256"]:
        raise ValueError("Gallery changed from the experiment snapshot")
    gallery_rows = read_jsonl(gallery / "gallery.jsonl")
    scope = selection_scope(tags, gallery_rows)
    emb_rows, emb_ids, matrix = load_embeddings(gallery)
    for row in tags:
        if row["sha256"] != emb_rows[emb_ids[row["frame_id"]]]["sha256"]:
            raise ValueError("Tag/embedding image SHA mismatch")
    queries = json.loads(queries_path.read_text())["queries"]
    if not queries or len({q["query_shot_id"] for q in queries}) != len(queries):
        raise ValueError("Queries must be nonempty with unique shots")
    base = sorted([row for row in tags if row["status"] == "valid" and row["frame"]["kind"] == "story" and not row["is_op_ed"]], key=lambda row: row["frame_id"])
    probabilities, geometries = {}, {}
    for row in base:
        pcp = row["pcp"]
        order = pcp["class_order"]
        if order != PCP_ORDER:
            raise ValueError("PCP class order differs from the native nine-label checkpoint order")
        vector = np.asarray(pcp["probs"], dtype=np.float64)
        norm = np.linalg.norm(vector)
        if vector.shape != (9,) or not np.isfinite(vector).all() or (vector < 0).any() or not np.isclose(vector.sum(), 1, atol=1e-5) or norm <= 0:
            raise ValueError("Invalid PCP probability vector")
        probabilities[row["frame_id"]] = vector / norm
        geometries[row["frame_id"]] = box_geometry(row["detector"]["person_boxes"])
    output = ROOT / "rankings"
    parameters = method_parameters(unmatched_penalty)
    parameters["scope"] = scope
    save_json(output / "methods.json", parameters)
    source_hashes = {"keyframe_tags": sha(tags_path), "queries": sha(queries_path),
                     "gallery": manifest["gallery_sha256"], "embeddings": sha(gallery / "embeddings.npy"),
                     "embedding_rows": sha(gallery / "embedding_rows.jsonl"), "runner": sha(__file__),
                     "methods": sha(output / "methods.json"),
                     "selection_manifest": scope["manifest_sha256"], "selection_frames": scope["frames_sha256"]}
    current_ids = {q["query_shot_id"] for q in queries}
    for method in METHODS:
        stale = [p.name for p in (output / method).glob("*.json") if p.stem not in current_ids]
        if stale:
            raise ValueError(f"Stale rankings from a different query set in {method}: {stale}")
    save_json(output / "run.json", {"status": "running", "started_at": datetime.now(timezone.utc).isoformat(),
                                   "query_count": len(queries), "source_hashes": source_hashes, "scope": scope})
    summaries, sanity = [], []
    for query in queries:
        qid = query["query_shot_id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", qid):
            raise ValueError("Unsafe query identifier")
        qframe = query.get("frame_id", query.get("representative_frame_id"))
        q = by_frame[qframe]
        if q["shot_id"] != qid or qframe not in probabilities:
            raise ValueError("Query must identify an eligible tagged story frame")
        if not q.get("reuse_group_id"):
            raise ValueError("Query has no reuse group")
        candidates = [r for r in base if r["shot_id"] != qid and r["reuse_group_id"] != q["reuse_group_id"]]
        near = [r["episode"] == q["episode"] and abs(midpoint(r) - midpoint(q)) <= 60000 for r in candidates]
        frame_ids = [row["frame_id"] for row in candidates]
        cosines = matrix[[emb_ids[fid] for fid in frame_ids]] @ matrix[emb_ids[qframe]]
        components, field_scores = [], []
        for row, cos in zip(candidates, cosines):
            fid = row["frame_id"]
            tag_score, fields = tag_similarity(q, row)
            distance = layout_distance(geometries[qframe], geometries[fid], unmatched_penalty)
            components.append({"doubao": float(cos), "tags": tag_score,
                               "pcp": float(probabilities[qframe] @ probabilities[fid]), "layout": distance})
            field_scores.append(fields)
        m0_ranks = competition_ranks([row["doubao"] for row in components])
        ranks = {name: competition_ranks([None if blocked else values[name] for values, blocked in zip(components, near)], descending=name != "layout") for name in ("doubao", "tags", "pcp", "layout")}
        component_path = output / "components" / f"{qid}.jsonl"
        component_path.parent.mkdir(parents=True, exist_ok=True)
        with component_path.with_suffix(".jsonl.tmp").open("w") as handle:
            for i, row in enumerate(candidates):
                audit = {"query_shot_id": qid, "query_frame_id": qframe, "shot_id": row["shot_id"], "frame_id": row["frame_id"],
                         "eligible_M0": True, "eligible_M1_to_M5": not near[i], "component_scores": components[i],
                         "tag_field_scores": field_scores[i], "tag_valid_fields": sum(v is not None for v in field_scores[i].values()),
                         "M0_doubao_rank": m0_ranks[i], "M1_to_M5_component_ranks": {name: values[i] for name, values in ranks.items()}}
                handle.write(json.dumps(audit, ensure_ascii=False, allow_nan=False) + "\n")
        component_path.with_suffix(".jsonl.tmp").replace(component_path)
        counts = {}
        for method, used in METHODS.items():
            best = {}
            for i, row in enumerate(candidates):
                if method != "M0" and near[i]:
                    continue
                rr = {name: values[i] for name, values in ranks.items()}
                if method == "M0":
                    rr = {"doubao": m0_ranks[i]}
                score = components[i]["doubao"] if method in ("M0", "M1") else rrf_score(rr, used)
                record = {"shot_id": row["shot_id"], "frame_id": row["frame_id"], "episode": row["episode"],
                          "frame_timestamp_ms": row["frame_timestamp_ms"], "image_path": relative_image(row, gallery),
                          "score": score, "component_ranks": {name: rr[name] for name in used},
                          "component_scores": {name: components[i][name] for name in used},
                          "available_components": [name for name in used if rr[name] is not None]}
                prior = best.get(row["shot_id"])
                if prior is None or (-score, row["frame_id"]) < (-prior["score"], prior["frame_id"]):
                    best[row["shot_id"]] = record
            ranked = sorted(best.values(), key=lambda row: (-row["score"], row["shot_id"], row["frame_id"]))
            for position, row in enumerate(ranked, 1):
                row["rank"] = position
            save_json(output / method / f"{qid}.json", {"method": method, "query_shot_id": qid, "query_frame_id": qframe,
                       "candidate_keyframes": len(candidates) if method == "M0" else len(candidates) - sum(near),
                       "candidate_shots": len(ranked), "score_type": "cosine" if method in ("M0", "M1") else "RRF",
                       "component_audit": os.path.relpath(component_path, ROOT), "rows": ranked})
            counts[method] = len(ranked)
            if qid == "mygo-ep01-s00152":
                for episode, start_ms, end_ms, label in [(7, 248600, 248601, "Ep7 04:08.600"), (10, 930300, 935600, "Ep10 15:30.3–15:35.6")]:
                    target_shots = {r["shot_id"] for r in tags if r["episode"] == episode and r["start_ms"] < end_ms and start_ms < r["end_ms"]}
                    for sid in sorted(target_shots):
                        result = next((r for r in ranked if r["shot_id"] == sid), None)
                        excluded_frames = {r["frame_id"]: exclusion_reasons(q, r, method) for r in tags if r["shot_id"] == sid}
                        sanity.append({"method": method, "query_shot_id": qid, "target": label, "candidate_shot_id": sid,
                                       "rank": result["rank"] if result else None, "frame_id": result["frame_id"] if result else None,
                                       "exclusion_reasons": sorted({reason for reasons in excluded_frames.values() for reason in reasons}) if result is None else [],
                                       "frame_exclusions": excluded_frames if result is None else {},
                                       "note": "Rank observation only; no relevance judgment"})
        summaries.append({"query_shot_id": qid, "M0_keyframes": len(candidates), "M1_to_M5_keyframes": len(candidates) - sum(near), "ranked_shots": counts})
        print(json.dumps(summaries[-1]), flush=True)
    save_json(output / "sanity_reference_ranks.json", sanity)
    save_json(output / "run.json", {"status": "complete", "created_at": datetime.now(timezone.utc).isoformat(),
              "query_count": len(queries), "input_keyframes": len(tags), "valid_story_nonoped_keyframes": len(base),
              "runtime": {"python": platform.python_version(), "numpy": np.__version__, "scipy": scipy.__version__},
              "excluded_status_counts": dict(Counter(r["status"] for r in tags if r["status"] != "valid")),
              "gate_selected_run": gate["selected_run"], "source_hashes": source_hashes, "scope": scope, "queries": summaries})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queries", type=Path, default=ROOT / "queries.json")
    parser.add_argument("--unmatched-penalty", type=float, choices=[1.0, 2.0], default=2.0)
    args = parser.parse_args()
    run(args.queries.resolve(), args.unmatched_penalty)
