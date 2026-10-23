"""Build offline PCP fusion comparisons for all boxes and person-only boxes."""
import hashlib
import json
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
FIRST = ROOT.parent / "20261017-first-pass"
RASTER = ROOT.parent / "20261017-layout-raster"
CLASS_LABELS = {
    "rule_of_thirds": "三分法", "vertical": "垂直", "horizontal": "水平",
    "diagonal": "对角线", "curved": "曲线", "triangle": "三角形",
    "center": "居中", "symmetric": "对称", "pattern": "重复图案",
}
SAMPLE_LABELS = dict(zip(
    ["aquarium", "room_high", "interior", "classroom_group", "door_pair", "window_close", "store_wide", "pink_center", "grey_left", "pink_tilt", "stage_wide", "stage_left", "phone_tilt", "bridge_diagonal", "school_layers", "rehearsal_left", "building_perspective"],
    ["水族馆", "俯视房间", "挑高客厅", "教室围桌", "门口两人", "窗边近景", "商店全景", "粉发正面近景", "灰发人物靠左", "粉发人物倾斜", "舞台全景", "舞台人物靠左", "倾斜手机", "斜向桥栏杆", "教学楼正面", "排练室人物靠左", "仰视建筑"],
))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def main():
    sources = [FIRST / "manifest.json", FIRST / "pcp/results.json", FIRST / "reference.json", RASTER / "results.json", ROOT / "person-boxes/results.json"]
    manifest, pcp, reference, raster, persons = [json.loads(p.read_text()) for p in sources]
    ids = [s["id"] for s in manifest["samples"]]
    boxes = raster["variants"]["boxes"]["records"]
    assert [r["id"] for r in pcp["records"]] == ids == [r["id"] for r in boxes]
    assert [r["id"] for r in persons["records"]] == ids
    assert persons["metadata"]["manifest_sha256"] == sha(sources[0])
    assert pcp["manifest_sha256"] == reference["manifest_sha256"] == sha(sources[0])
    assert pcp["class_order"] == list(CLASS_LABELS)
    probabilities = np.asarray([[r["class_probabilities"][c] for c in pcp["class_order"]] for r in pcp["records"]], dtype=float)
    assert np.isfinite(probabilities).all() and np.all(probabilities >= 0)
    assert np.allclose(probabilities.sum(axis=1), 1, atol=1e-5)
    box_vectors = np.asarray([r["embedding"] if r["valid"] else [0.0]*384 for r in boxes], dtype=float)
    assert box_vectors.shape == (len(ids), 384) and np.isfinite(box_vectors).all()
    valid_boxes = np.asarray([r["valid"] and np.linalg.norm(v) > 0 for r, v in zip(boxes, box_vectors)])
    p = probabilities / np.linalg.norm(probabilities, axis=1, keepdims=True)
    b_norms = np.linalg.norm(box_vectors, axis=1, keepdims=True)
    b = np.divide(box_vectors, b_norms, out=np.zeros_like(box_vectors), where=b_norms > 0)
    pcp_cos, box_cos = p @ p.T, b @ b.T
    person_vectors = np.asarray([r["embedding"] if r["valid"] else [0.0]*384 for r in persons["records"]], dtype=float)
    assert person_vectors.shape == box_vectors.shape and np.isfinite(person_vectors).all()
    valid_persons = np.asarray([r["valid"] for r in persons["records"]], dtype=bool)
    person_norms = np.linalg.norm(person_vectors, axis=1, keepdims=True)
    assert np.all(person_norms[valid_persons] > 0)
    person_unit = np.divide(person_vectors, person_norms, out=np.zeros_like(person_vectors), where=person_norms > 0)
    person_cos = person_unit @ person_unit.T
    samples = []
    for s, pr, br, rr, person in zip(manifest["samples"], pcp["records"], boxes, raster["samples"], persons["records"]):
        assert sha(FIRST / s["image_path"]) == s["sha256"]
        assert sha(RASTER / br["image_path"]) == br["image_sha256"]
        assert sha(ROOT / "person-boxes" / person["image_path"]) == person["image_sha256"]
        assert person["valid"] == (person["person_box_count"] > 0)
        samples.append({"id": s["id"], "label": SAMPLE_LABELS[s["id"]],
            "original_path": "../20261017-first-pass/"+s["image_path"],
            "box_image_path": "../20261017-layout-raster/"+br["image_path"],
            "pcp_probabilities": [pr["class_probabilities"][c] for c in pcp["class_order"]],
            "pcp_top_class": pr["top_class"], "box_count": rr["box_count"],
            "person_box_image_path": "person-boxes/"+person["image_path"],
            "person_box_count": person["person_box_count"], "person_box_valid": person["valid"],
            "box_valid": bool(valid_boxes[ids.index(s["id"])]),
            "layouts": reference["labels"][s["id"]]["layouts"]})
    data = {"metadata": {
        "default_box_weight": 0.2,
        "formula": "score = (1 - box_weight) * cosine(PCP_9class_query, PCP_9class_candidate) + box_weight * cosine(DINOv2_selected_box_CLS_query, DINOv2_selected_box_CLS_candidate); selected boxes = all boxes or person boxes",
        "default_note": "80% PCP /20% boxes is a starting setting reflecting category-first preference, not a trained or optimized weight.",
        "score_policy": "Raw cosine; no min-max, z-score, ranking fusion or hidden calibration. Weight coefficients do not imply equal ranking influence; scores are not probabilities.",
        "missing_policy": "If box weight is zero use PCP alone; otherwise a missing query/candidate box feature makes the pair unavailable. Exclude self. Rank by full-precision score; exactly equal candidate scores preserve manifest order. Only reference triplet margins within 1e-8 count as ties.",
        "scope": "17 existing anime images; existing PCP and all-box features, plus newly encoded person-only box rasters using the same cached frozen DINOv2 Small. No new detection, training, downloads or external API.",
        "persons": {"protocol": persons["metadata"], "valid_samples": int(valid_persons.sum()), "default_pool": "persons", "comparison": "Default A/B uses the same candidate pool with detected person boxes, weights and source images. Filtering benefit must not be attributed to person embeddings.", "missing_note": "No person detection is not proof of no person. pink_tilt is a confirmed missed close-up. No NMS; box count is not people count. Empty person rasters are never encoded."},
        "sources": {str(p.relative_to(ROOT.parent)): sha(p) for p in sources},
        "builder_sha256": sha(Path(__file__)),
    }, "class_order": pcp["class_order"], "class_labels": CLASS_LABELS, "samples": samples,
        "scores": {"pcp": pcp_cos.tolist(), "boxes": [[float(box_cos[i,j]) if valid_boxes[i] and valid_boxes[j] else None for j in range(len(ids))] for i in range(len(ids))],
                   "persons": [[float(person_cos[i,j]) if valid_persons[i] and valid_persons[j] else None for j in range(len(ids))] for i in range(len(ids))]},
        "triplets": reference["triplets"]}
    write_json(ROOT / "data.json", data)
    html = (ROOT / "viewer.html").read_text()
    assert html.count("__DATA__") == 1
    (ROOT / "report.html").write_text(html.replace("__DATA__", json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")))
    cases = []
    for weight in [0, 0.2, 0.5, 0.8, 1]:
        scores = (1-weight)*pcp_cos + weight*box_cos
        valid = np.ones(len(ids), dtype=bool) if weight == 0 else valid_boxes
        ranks = {sid: [{"id": ids[j], "score": float(scores[i,j]), "pcp": float(pcp_cos[i,j]),
                       "boxes": float(box_cos[i,j]) if valid_boxes[i] and valid_boxes[j] else None}
                      for j in sorted((j for j in range(len(ids)) if j != i and valid[j]), key=lambda j: -scores[i,j])]
                 if valid[i] else [] for i,sid in enumerate(ids)}
        tri = []
        for t in reference["triplets"]:
            qi, pi, ni = [ids.index(t[k]) for k in ["query", "positive", "negative"]]
            available = bool(valid[[qi,pi,ni]].all())
            margin = float(scores[qi,pi]-scores[qi,ni]) if available else None
            tri.append({**t, "valid": available, "margin": margin, "win": available and margin > 1e-8, "tie": available and abs(margin) <= 1e-8})
        hit = sum(bool(set(samples[i]["layouts"]) & set(reference["labels"][r["id"]]["layouts"])) for i,sid in enumerate(ids) for r in ranks[sid][:3])
        count = sum(len(r[:3]) for r in ranks.values())
        cases.append({"box_weight": weight, "rankings": ranks, "triplets": tri,
            "main_wins": sum(t["win"] for t in tri if t["split"] == "main"),
            "main_valid": sum(t["valid"] for t in tri if t["split"] == "main"),
            "top3_reference_hits": hit, "top3_count": count})
    write_json(ROOT / "weight_checks.json", {"note": "Fixed illustrative weights, no selection of best weight; same prior assistant reference.", "cases": cases})
    comparison_cases = []
    for weight in [0, 0.2, 0.5, 0.8, 1]:
        variants = {}
        for key, cosine, feature_valid in [("boxes", box_cos, valid_boxes), ("persons", person_cos, valid_persons)]:
            mixed = (1-weight)*pcp_cos + weight*cosine
            pair_valid = np.ones(len(ids), dtype=bool) if weight == 0 else feature_valid
            ranks = {sid: [{"id": ids[j], "score": float(mixed[i,j]), "pcp": float(pcp_cos[i,j]),
                           "layout": float(cosine[i,j]) if feature_valid[i] and feature_valid[j] else None}
                          for j in sorted((j for j in range(len(ids)) if j != i and valid_persons[j] and pair_valid[j]), key=lambda j: -mixed[i,j])]
                     if pair_valid[i] else [] for i,sid in enumerate(ids)}
            triplets = []
            for t in reference["triplets"]:
                qi, pi, ni = [ids.index(t[k]) for k in ["query", "positive", "negative"]]
                # Compare both variants on the same queries and candidates with person detections.
                eligible = bool(valid_persons[[qi, pi, ni]].all())
                available = eligible and bool(pair_valid[[qi, pi, ni]].all())
                margin = float(mixed[qi,pi]-mixed[qi,ni]) if available else None
                triplets.append({**t, "valid": available, "margin": margin,
                                 "win": available and margin > 1e-8, "tie": available and abs(margin) <= 1e-8})
            hit = sum(bool(set(samples[i]["layouts"]) & set(reference["labels"][r["id"]]["layouts"]))
                      for i,sid in enumerate(ids) if valid_persons[i] for r in ranks[sid][:3])
            count = sum(len(ranks[sid][:3]) for i,sid in enumerate(ids) if valid_persons[i])
            variants[key] = {"rankings": ranks, "triplets": triplets,
                "common_main_wins": sum(t["win"] for t in triplets if t["split"] == "main"),
                "common_main_valid": sum(t["valid"] for t in triplets if t["split"] == "main"),
                "common_query_top3_reference_hits": hit, "common_query_top3_count": count}
        comparison_cases.append({"box_weight": weight, "variants": variants})
    write_json(ROOT / "person_weight_checks.json", {"pool": "detected_person", "pool_count": int(valid_persons.sum()),
        "note": "Identical person-detected gallery for both methods; summary metrics also restrict queries and triplets to person-detected images. Old labels judge broad composition, not validated person-layout relevance. No weight optimization.", "cases": comparison_cases})
    print(json.dumps({"samples":len(samples), "default_box_weight":0.2,
                      "weights":[{k:c[k] for k in ["box_weight","main_wins","main_valid","top3_reference_hits","top3_count"]} for c in cases]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
