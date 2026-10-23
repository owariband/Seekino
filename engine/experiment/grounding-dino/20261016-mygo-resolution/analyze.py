"""Compare detector consistency across resolutions; 1080p predictions are not ground truth."""

import argparse
import csv
import hashlib
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def normalized_box(detection, sample):
    return [v / sample["width" if i % 2 == 0 else "height"]
            for i, v in enumerate(detection["box_xyxy"])]


def intersection_over_union(a, b):
    intersection = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - intersection
    return intersection / union if union > 0 else 0


def compare(reference, candidate, person_only=False):
    ref = [(i, d) for i, d in enumerate(reference["detections"]) if not person_only or d["label"] == "person"]
    cur = [(i, d) for i, d in enumerate(candidate["detections"]) if not person_only or d["label"] == "person"]
    possible = []
    for ri, rd in ref:
        for ci, cd in cur:
            if rd["label"] == cd["label"]:
                overlap = intersection_over_union(normalized_box(rd, reference["sample"]), normalized_box(cd, candidate["sample"]))
                if overlap >= 0.5:
                    possible.append((overlap, ri, ci))
    used_ref, used_cur, matches = set(), set(), []
    for overlap, ri, ci in sorted(possible, reverse=True):
        if ri in used_ref or ci in used_cur:
            continue
        used_ref.add(ri)
        used_cur.add(ci)
        rd, cd = reference["detections"][ri], candidate["detections"][ci]
        matches.append({"reference_index": ri, "candidate_index": ci, "label": rd["label"], "iou": overlap,
                        "center_max_axis_shift_pp": 100 * max(abs(a - b) for a, b in zip(rd["center_xy_normalized"], cd["center_xy_normalized"])),
                        "box_area_relative_change_percent": 100 * (cd["box_area_ratio"] / rd["box_area_ratio"] - 1)})
    return {"reference_count": len(ref), "candidate_count": len(cur), "matched_count": len(matches),
            "unmatched_reference_indices": [i for i, _ in ref if i not in used_ref],
            "unmatched_candidate_indices": [i for i, _ in cur if i not in used_cur], "matches": matches}


def metric_summary(matches):
    return {"median_iou": statistics.median(m["iou"] for m in matches) if matches else None,
            "min_iou": min((m["iou"] for m in matches), default=None),
            "median_center_shift_pp": statistics.median(m["center_max_axis_shift_pp"] for m in matches) if matches else None,
            "max_center_shift_pp": max((m["center_max_axis_shift_pp"] for m in matches), default=None),
            "median_absolute_area_change_percent": statistics.median(abs(m["box_area_relative_change_percent"]) for m in matches) if matches else None,
            "max_absolute_area_change_percent": max((abs(m["box_area_relative_change_percent"]) for m in matches), default=None)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    root = args.experiment
    manifest_bytes = (root / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    results = json.loads((root / "mps/results.json").read_text())
    assert results["manifest_sha256"] == hashlib.sha256(manifest_bytes).hexdigest(), "Results/manifest mismatch"
    assert {r["sample"]["id"] for r in results["records"]} == {s["id"] for s in manifest["samples"]}
    groups = defaultdict(dict)
    for record in results["records"]:
        sample = record["sample"]
        assert sample["resolution_p"] not in groups[sample["source_id"]], "Duplicate source/resolution"
        groups[sample["source_id"]][sample["resolution_p"]] = record
    comparisons, csv_rows, strips = [], [], []
    font_path = Path("/System/Library/Fonts/Supplemental/Arial.ttf")
    font = ImageFont.truetype(str(font_path), 22) if font_path.exists() else ImageFont.load_default(size=22)
    (root / "comparisons").mkdir(exist_ok=True)
    for source_id, variants in sorted(groups.items()):
        assert set(variants) == {360, 480, 720, 1080}, f"Incomplete resolutions: {source_id}"
        reference = variants[1080]
        strip = Image.new("RGB", (2560, 406), "#161a23")
        draw = ImageDraw.Draw(strip)
        for column, resolution in enumerate(sorted(variants)):
            record = variants[resolution]
            sample = record["sample"]
            counts = dict(sorted(Counter(d["label"] for d in record["detections"]).items()))
            row = {"source_id": source_id, "timestamp_hms": sample["timestamp_hms"], "resolution_p": resolution,
                   "width": sample["width"], "height": sample["height"], "label_counts": counts,
                   "detection_count": len(record["detections"]), "person_count": counts.get("person", 0),
                   "median_pipeline_ms": statistics.median(m["pipeline_ms"] for m in record["measurements"]),
                   "median_forward_ms": statistics.median(m["forward_ms"] for m in record["measurements"]),
                   "input_tensor_shapes": sorted({tuple(m["input_tensor_shape"]) for m in record["measurements"]})}
            flat = {**row, "label_counts": json.dumps(counts), "input_tensor_shapes": json.dumps(row["input_tensor_shapes"])}
            for scope in ("person", "all_labels"):
                comparison = compare(reference, record, person_only=scope == "person")
                comparison.update(metric_summary(comparison["matches"]))
                row[scope] = comparison
                for name in ("reference_count", "candidate_count", "matched_count", *metric_summary([])):
                    flat[f"{scope}_{name}"] = comparison[name]
                for side in ("reference", "candidate"):
                    flat[f"{scope}_unmatched_{side}_count"] = len(comparison[f"unmatched_{side}_indices"])
            comparisons.append(row)
            csv_rows.append(flat)
            with Image.open(root / "mps" / f"{sample['id']}.jpg") as annotated:
                strip.paste(annotated.convert("RGB").resize((640, 360), Image.Resampling.LANCZOS), (column * 640, 46))
            suffix = " (reference, not GT)" if resolution == 1080 else ""
            draw.text((column * 640 + 10, 5), f"{sample['timestamp_hms']} | {resolution}p{suffix}", font=font, fill="white")
            draw.text((column * 640 + 10, 26), f"person={counts.get('person', 0)}  all={len(record['detections'])}", font=font, fill="#a8bdcf")
        strip.save(root / "comparisons" / f"{source_id}.jpg", quality=94)
        strips.append(strip.resize((1280, 203), Image.Resampling.LANCZOS))
    aggregate = []
    for resolution in (360, 480, 720, 1080):
        subset = [r for r in comparisons if r["resolution_p"] == resolution]
        row = {"resolution_p": resolution, "frame_count": len(subset)}
        timings = [m["pipeline_ms"] for r in results["records"] if r["sample"]["resolution_p"] == resolution for m in r["measurements"]]
        row["median_pipeline_ms"] = statistics.median(timings)
        for scope in ("person", "all_labels"):
            matches = [m for r in subset for m in r[scope]["matches"]]
            row[scope] = {**metric_summary(matches), "matched_count": len(matches),
                          "reference_count": sum(r[scope]["reference_count"] for r in subset),
                          "candidate_count": sum(r[scope]["candidate_count"] for r in subset),
                          "unmatched_reference_count": sum(len(r[scope]["unmatched_reference_indices"]) for r in subset),
                          "unmatched_candidate_count": sum(len(r[scope]["unmatched_candidate_indices"]) for r in subset)}
        aggregate.append(row)
    output = {"method": "Within-source same-label greedy maximum-IoU one-to-one matching; normalized boxes, IoU >= 0.5. 1080p detector output is the reference, NOT ground truth. All shifts summarize matched boxes only; unmatched outputs are reported separately. Center shift is maximum absolute x/y difference in percentage points of image dimensions; area change is relative to reference box area. This measures prediction consistency, not accuracy.",
              "manifest_sha256": results["manifest_sha256"], "aggregate_by_resolution": aggregate, "comparisons": comparisons}
    (root / "comparison.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    with (root / "summary.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(csv_rows[0]))
        writer.writeheader()
        writer.writerows(csv_rows)
    contact = Image.new("RGB", (1280, 203 * len(strips)), "#161a23")
    for i, strip in enumerate(strips):
        contact.paste(strip, (0, 203 * i))
    contact.save(root / "contact_sheet.jpg", quality=94)
    print(json.dumps(aggregate, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
