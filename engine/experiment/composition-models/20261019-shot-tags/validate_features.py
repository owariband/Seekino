"""Audit saved PCP/DINO outputs and cache provenance without model inference."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import statistics

import numpy as np

ROOT = Path(__file__).resolve().parent
CLASS_ORDER = ["rule_of_thirds", "vertical", "horizontal", "diagonal", "curved", "triangle", "center", "symmetric", "pattern"]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_jsonl(path, partial=False):
    data = path.read_bytes()
    lines = data.splitlines()
    if partial and data and not data.endswith(b"\n"):
        lines = lines[:-1]
    return [json.loads(line) for line in lines if line.strip()]


def iou(a, b):
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    intersection = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    return intersection / (area_a + area_b - intersection)


def union_area(boxes):
    xs = sorted({float(box[i]) for box in boxes for i in (0, 2)})
    total = 0
    for lo, hi in zip(xs, xs[1:]):
        intervals = sorted((b[1], b[3]) for b in boxes if b[0] < hi and b[2] > lo)
        height, end = 0, 0
        for bottom, top in intervals:
            height += max(0, top - max(bottom, end))
            end = max(end, top)
        total += (hi - lo) * height
    return total


def audit_row(row):
    pcp, detector = row["pcp"], row["detector"]
    probabilities, logits = np.array(pcp["probs"]), np.array(pcp["logits"])
    assert pcp["class_order"] == CLASS_ORDER
    assert probabilities.shape == logits.shape == (9,)
    assert np.isfinite(probabilities).all() and np.isfinite(logits).all()
    expected = np.exp(logits - logits.max())
    expected /= expected.sum()
    assert np.allclose(expected, probabilities, atol=1e-6)
    assert np.isclose(probabilities.sum(), 1, atol=1e-5)
    assert pcp["top_class"] == CLASS_ORDER[int(probabilities.argmax())]
    assert np.isclose(pcp["top_probability"], probabilities.max())
    assert detector["prompt"] == "person ." and detector["box_threshold"] == .35
    assert detector["text_threshold"] == .25 and detector["nms_iou_threshold"] == .5
    assert detector["model_revision"] == "a2bb814dd30d776dcf7e30523b00659f4f141c71"
    boxes = np.asarray(detector["person_boxes"], dtype=np.float64).reshape((-1, 5))
    assert detector["person_count"] == len(boxes)
    assert np.isfinite(boxes).all() and (boxes >= 0).all() and (boxes <= 1).all()
    assert (boxes[:, 2:4] > boxes[:, :2]).all()
    assert all(boxes[i, 4] >= boxes[i + 1, 4] for i in range(len(boxes) - 1))
    for i, box in enumerate(boxes):
        assert box[4] > .35
        for other in boxes[i + 1:]:
            assert iou(box, other) <= .50001
    raw = detector["raw_detections"]
    assert detector["raw_thresholded_count"] == len(raw)
    assert [r["raw_index"] for r in raw] == list(range(len(raw)))
    scale = np.array(row["image_size"] * 2)
    eligible = []
    for r in raw:
        normalized = np.array(r["xyxy_normalized"])
        unclipped = np.array(r["xyxy_normalized_unclipped"])
        assert np.allclose(np.array(r["xyxy_pixels"]) / scale, unclipped, atol=1e-6)
        assert np.allclose(np.clip(unclipped, 0, 1), normalized, atol=1e-6)
        assert .35 < r["score"] <= 1
        is_eligible = r["label"].strip() == "person" and bool((normalized[2:] > normalized[:2]).all())
        assert r["eligible_for_person_nms"] == is_eligible
        if is_eligible:
            eligible.append(r["raw_index"])
    assert detector["person_count_before_nms"] == len(eligible)
    kept = detector["nms_kept_raw_indices"]
    assert len(kept) == len(set(kept)) == len(boxes)
    assert set(kept) <= set(eligible)
    for index, box in zip(kept, boxes):
        assert np.allclose(box, [*raw[index]["xyxy_normalized"], raw[index]["score"]], atol=1e-7)
    for r in raw:
        assert r["kept_after_nms"] == (r["raw_index"] in kept)
        if r["eligible_for_person_nms"] and not r["kept_after_nms"]:
            assert any(k[4] >= r["score"] - 1e-7 and iou(k, r["xyxy_normalized"]) >= .49999 for k in boxes)
    if len(boxes):
        areas = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
        centers = (boxes[:, :2] + boxes[:, 2:4]) / 2
        largest = int(areas.argmax())
        assert np.isclose(detector["largest_person_area"], areas[largest], atol=1e-6)
        assert np.allclose(detector["largest_person_box"], boxes[largest, :4], atol=1e-6)
        assert np.allclose(detector["largest_person_center"], centers[largest], atol=1e-6)
        assert np.allclose(detector["people_centroid"], np.average(centers, weights=areas, axis=0), atol=1e-6)
        assert np.allclose(detector["people_extent"], [boxes[:, 0].min(), boxes[:, 1].min(), boxes[:, 2].max(), boxes[:, 3].max()], atol=1e-6)
    else:
        assert detector["largest_person_area"] == 0
        assert all(detector[key] is None for key in ["largest_person_box", "largest_person_center", "people_centroid", "people_extent"])
    assert np.isclose(detector["person_box_union_area"], union_area(boxes), atol=1e-6)
    assert datetime.fromisoformat(row["computed_at"]).tzinfo is not None
    assert all(np.isfinite(value) and value >= 0 for value in row["timing_ms"].values())


def run(input_path, features_path, output_path, partial=False):
    if not output_path.resolve().is_relative_to(ROOT) or output_path.resolve() in {input_path.resolve(), features_path.resolve()}:
        raise ValueError("Validation output must be a separate file inside this experiment")
    expected = load_jsonl(input_path)
    rows = load_jsonl(features_path, partial)
    receipt_path = ROOT / "features/runs" / (str(features_path.resolve().relative_to(ROOT)).replace("/", "_") + ".json")
    receipt = json.loads(receipt_path.read_text())
    assert receipt["input_sha256"] == sha(input_path)
    if not partial:
        assert receipt["status"] == "complete"
        assert receipt["output_sha256"] == sha(features_path)
        assert receipt["input_rows"] == receipt["completed_rows"] == len(expected) == len(rows)
    assert len(rows) <= len(expected)
    assert len({r["frame_id"] for r in rows}) == len(rows)
    gallery = Path(json.loads((ROOT / "input_manifest.json").read_text())["gallery"])
    contracts = {}
    cached = set()
    for row, source in zip(rows, expected):
        fid = row["frame_id"]
        try:
            assert all(row[key] == value for key, value in source.items())
            assert row["sha256"] == row["image_sha256"]
            image_path = Path(row["image_path"]) if row.get("image_path") else gallery / row["frame_path"]
            assert sha(image_path) == row["sha256"]
            contract_id = row["feature_contract_id"]
            if contract_id not in contracts:
                contract = json.loads((ROOT / row["contract_path"]).read_text())
                assert hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()).hexdigest() == contract_id
                contracts[contract_id] = contract
            contract = contracts[contract_id]
            assert row["detector"]["device"] == contract["runtime"]["device"] == contract["detector"]["device"]
            cache_path = ROOT / row["cache_path"]
            assert sha(cache_path) == row["cache_sha256"]
            cache = json.loads(cache_path.read_text())
            assert all(row[key] == value for key, value in cache.items())
            audit_row(row)
            cached.add(row["image_sha256"])
        except Exception as error:
            raise AssertionError(f"Feature audit failed for {fid}: {error}") from error
    summary = {"status": "passed_partial" if partial else "passed", "created_at": datetime.now(timezone.utc).isoformat(),
               "input_rows": len(expected), "audited_rows": len(rows), "unique_images": len(cached),
               "input_sha256": sha(input_path), "features_sha256": None if partial else sha(features_path),
               "contracts": list(contracts), "devices": dict(Counter(r["detector"]["device"] for r in rows)),
               "person_count_distribution": dict(sorted(Counter(r["detector"]["person_count"] for r in rows).items())),
               "raw_detections": sum(r["detector"]["raw_thresholded_count"] for r in rows),
               "kept_boxes": sum(r["detector"]["person_count"] for r in rows),
               "median_pcp_ms": statistics.median(r["timing_ms"]["pcp_pipeline"] for r in rows) if rows else None,
               "median_detector_ms": statistics.median(r["timing_ms"]["detector_pipeline"] for r in rows) if rows else None,
               "checks": ["input order/identity", "image/cache/contract SHA256", "cache contents equal output", "PCP class order/logits/softmax", "raw detections and clipping", "NMS kept/suppressed evidence", "normalized geometry and aggregates", "device provenance", "finite timings and timestamp"]}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "full/frames.jsonl")
    parser.add_argument("--features", type=Path, default=ROOT / "full/features.jsonl")
    parser.add_argument("--output", type=Path, default=ROOT / "full/features-validation.json")
    parser.add_argument("--partial", action="store_true", help="Audit a currently flushed prefix without declaring full completion")
    args = parser.parse_args()
    run(args.input, args.features, args.output, args.partial)
