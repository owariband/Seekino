"""Frozen DINOv2 on original frames and rasterized layouts; strictly offline."""
import argparse
import hashlib
import json
import os
import platform
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageOps

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT.parent / "20261017-first-pass"
CACHE = ROOT.parents[3] / ".cache/composition-models"
os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                  PYTORCH_ENABLE_MPS_FALLBACK="0", TOKENIZERS_PARALLELISM="false")
import torch
from transformers import AutoImageProcessor, AutoModel


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def raster(size, boxes, lines, width=3):
    """Same aspect fit as original; monochrome strokes; retain all supplied primitives."""
    canvas = Image.new("RGB", (448, 448), "white")
    w, h = ImageOps.contain(Image.new("RGB", size), canvas.size).size
    ox, oy = (448-w)//2, (448-h)//2
    draw = ImageDraw.Draw(canvas)

    def point(p):
        return ox + np.clip(p[0], 0, 1)*(w-1), oy + np.clip(p[1], 0, 1)*(h-1)

    for box in boxes:
        draw.rectangle([point(box[:2]), point(box[2:])], outline="black", width=width)
    for line in lines:
        draw.line([point(p) for p in line], fill="black", width=width)
    return canvas


def rank(vector, records, query_id, labels):
    if vector is None:
        return []
    rows = [{"id": r["id"], "similarity": float(np.dot(vector, r["embedding"])),
             "shared_reference_layout": bool(set(labels[query_id]["layouts"]) & set(labels[r["id"]]["layouts"]))}
            for r in records if r["valid"] and r["id"] != query_id]
    rows.sort(key=lambda r: -r["similarity"])
    for r in rows:
        r["tie_group_size"] = sum(abs(r["similarity"]-s["similarity"]) <= 1e-8 for s in rows)
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", choices=["cpu", "mps"], default="cpu")
    args = parser.parse_args()
    if args.device == "mps" and not torch.backends.mps.is_available():
        raise RuntimeError("MPS unavailable; select CPU explicitly")
    torch.set_num_threads(6)
    torch.manual_seed(0)
    manifest = json.loads((SOURCE / "manifest.json").read_text())
    reference = json.loads((SOURCE / "reference.json").read_text())
    grounding = json.loads((SOURCE / "backbones/grounding_dino.json").read_text())
    lsd = json.loads((SOURCE / "lines/results.json").read_text())
    checkpoint = json.loads((SOURCE / "models.json").read_text())["dinov2"]
    manual_path = ROOT.parents[1] / "grounding-dino/20261017-aquarium-vlm-vs-dino/assistant_annotation.json"
    manual = json.loads(manual_path.read_text())
    ids = [s["id"] for s in manifest["samples"]]
    assert all([r["id"] for r in d["records"]] == ids for d in [grounding, lsd])
    assert all(d["manifest_sha256"] == digest(SOURCE / "manifest.json") for d in [grounding, lsd, reference])
    for s in manifest["samples"]:
        assert digest(SOURCE / s["image_path"]) == s["sha256"]
    for f in checkpoint["files"]:
        assert digest(Path(checkpoint["path"]) / f["name"]) == f["sha256"]
    metadata = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(), "device": args.device,
        "checkpoint": checkpoint, "torch": torch.__version__, "platform": platform.platform(),
        "dtype": "float32", "batch_size": 1, "threads": 6, "training": False,
        "input_size": [224, 224], "render_size": [448, 448], "stroke_width_at_render": 3,
        "preprocessing": "All variants: aspect-preserving fit/white padding to 448 square, LANCZOS to 224, official normalization; no center crop. Original control rerun with identical padding.",
        "features": "last_hidden_state[:,0], 384D CLS; L2 normalization; cosine; no projection or trained metric",
        "boxes": "Reuse all GD detections with existing fixed prompt/thresholds; monochrome outlines, no labels/scores, no NMS/importance weighting; clip coordinates to [0,1].",
        "lines": lsd["parameters"],
        "lines_note": "Reuse automatic selected LSD lines, NOT semantic main-line selection; borders and duplicate edges retained.",
        "empty_policy": "Blank structures have no embedding and no ranking; exclude invalid candidates/triplets. Combined is valid if either component exists; counts expose missing components.",
        "reference_note": "Reuse previous assistant visual reference unchanged; 17 selected anime images, 10 main +3 boundary triplets; exploratory, not benchmark accuracy. No tuning or mixture weights.",
        "timing": "One warmup; per image preprocessing, forward, synchronization and feature transfer; excludes disk I/O, loading, extraction, rendering. One pass, not benchmark.",
        "sources": {str(p.relative_to(ROOT.parent)): digest(p) for p in [SOURCE / "manifest.json", SOURCE / "reference.json", SOURCE / "backbones/grounding_dino.json", SOURCE / "lines/results.json"]},
        "manual_source": {"path": str(manual_path), "sha256": digest(manual_path), "note": "Existing frozen assistant annotation; extra query only, not part of automatic gallery metrics."},
        "runner_sha256": digest(Path(__file__)),
    }
    save_json(ROOT / "protocol.json", metadata)
    (ROOT / "images").mkdir(exist_ok=True)
    model = AutoModel.from_pretrained(checkpoint["path"], local_files_only=True).eval().to(args.device)
    processor = AutoImageProcessor.from_pretrained(checkpoint["path"], local_files_only=True, use_fast=False)
    metadata["parameter_count"] = sum(p.numel() for p in model.parameters())

    def encode(image):
        start = time.perf_counter()
        tensor = processor(images=image.resize((224, 224), Image.Resampling.LANCZOS),
                           do_resize=False, do_center_crop=False, return_tensors="pt").to(args.device)
        with torch.inference_mode():
            output = model(**tensor).last_hidden_state[:, 0]
            vector = torch.nn.functional.normalize(output.float(), dim=-1)[0].cpu().tolist()
        if args.device == "mps":
            torch.mps.synchronize()
        assert len(vector) == 384 and np.isfinite(vector).all()
        return vector, (time.perf_counter()-start)*1000

    encode(Image.new("RGB", (448, 448), "white"))
    labels = reference["labels"]
    variants = {k: {"label": label, "records": []} for k, label in [
        ("original", "原图对照"), ("boxes", "全部检测框"), ("lines", "自动长线段"), ("combined", "框＋自动线段")]}
    samples, geometry, canvases = [], {}, {}
    for s, g, l in zip(manifest["samples"], grounding["records"], lsd["records"]):
        sid = s["id"]
        original = Image.open(SOURCE / s["image_path"]).convert("RGB")
        boxes = [d["box_xyxy_normalized"] for d in g["detections"]]
        lines = [d["endpoints_xy_normalized"] for d in l["segments"]]
        geometry[sid] = (original.size, boxes, lines)
        samples.append({"id": sid, "original_path": "../20261017-first-pass/"+s["image_path"],
                        "layouts": labels[sid]["layouts"], "evidence": labels[sid]["evidence"],
                        "box_count": len(boxes), "line_count": len(lines)})
        for kind, variant in variants.items():
            image = ImageOps.pad(original, (448, 448), method=Image.Resampling.LANCZOS, color="white") if kind == "original" else raster(original.size, boxes if kind != "lines" else [], lines if kind != "boxes" else [])
            valid = kind == "original" or bool((boxes if kind != "lines" else []) + (lines if kind != "boxes" else []))
            path = f"images/{sid}__{kind}.png"
            image.save(ROOT / path)
            vector, ms = encode(image) if valid else (None, None)
            variant["records"].append({"id": sid, "image_path": path, "image_sha256": digest(ROOT / path),
                                       "valid": valid, "embedding": vector, "pipeline_ms": ms,
                                       "ink_fraction": float(np.mean(np.asarray(image.convert("L")) < 128)) if kind != "original" else None})
            canvases[sid, kind] = image
        print(json.dumps({"sample": sid, "boxes": len(boxes), "lines": len(lines)}), flush=True)
    for kind, variant in variants.items():
        rows = {r["id"]: r for r in variant["records"]}
        variant["rankings"] = {sid: rank(rows[sid]["embedding"], variant["records"], sid, labels) for sid in ids}
        variant["triplets"] = []
        for t in reference["triplets"]:
            q, p, n = [rows[t[k]] for k in ["query", "positive", "negative"]]
            valid = all(r["valid"] for r in [q, p, n])
            pos = float(np.dot(q["embedding"], p["embedding"])) if valid else None
            neg = float(np.dot(q["embedding"], n["embedding"])) if valid else None
            margin = pos-neg if valid else None
            variant["triplets"].append({**t, "valid": valid, "positive_similarity": pos,
                "negative_similarity": neg, "margin": margin, "win": valid and margin > 1e-8, "tie": valid and abs(margin) <= 1e-8})
        main_rows = [r for r in variant["triplets"] if r["split"] == "main"]
        valid_ranks = [r for r in variant["rankings"].values() if r]
        variant["summary"] = {"main_wins": sum(r["win"] for r in main_rows), "main_ties": sum(r["tie"] for r in main_rows),
            "main_losses": sum(r["valid"] and not r["win"] and not r["tie"] for r in main_rows),
            "main_valid": sum(r["valid"] for r in main_rows), "main_unavailable": sum(not r["valid"] for r in main_rows),
            "valid_queries": len(valid_ranks), "reference_overlap_at_3": statistics.mean(sum(r["shared_reference_layout"] for r in ranks[:3])/3 for ranks in valid_ranks),
            "median_pipeline_ms": statistics.median(r["pipeline_ms"] for r in variant["records"] if r["valid"])}
    common = [t["id"] for t in reference["triplets"] if t["split"] == "main" and all(next(r for r in v["triplets"] if r["id"] == t["id"])["valid"] for v in variants.values())]
    metadata["common_main_triplets"] = common
    for variant in variants.values():
        variant["summary"].update(common_main_wins=sum(r["win"] for r in variant["triplets"] if r["id"] in common), common_main_count=len(common))
    extra_queries = []
    for kind in ["lines", "combined"]:
        size, boxes, _ = geometry["aquarium"]
        image = raster(size, boxes if kind == "combined" else [], [l["points_normalized"] for l in manual["lines"]])
        path = f"images/aquarium__manual_{kind}.png"
        image.save(ROOT / path)
        vector, ms = encode(image)
        extra_queries.append({"id": "aquarium_manual_"+kind, "label": "水族馆已有人工6条主线" + ("＋全部自动框" if kind == "combined" else ""),
            "variant": kind, "image_path": path, "embedding": vector, "pipeline_ms": ms,
            "rankings": rank(vector, variants[kind]["records"], "aquarium", labels),
            "note": "Query-only diagnostic against automatic gallery; extractor differs. Not an end-to-end manual-line benchmark; excluded from aggregate metrics."})
    sensitivity = []
    for sid in ["aquarium", "interior", "stage_left"]:
        size, boxes, lines = geometry[sid]
        for kind in ["boxes", "lines", "combined"]:
            source_row = next(r for r in variants[kind]["records"] if r["id"] == sid)
            if not source_row["valid"]:
                continue
            b, l = boxes if kind != "lines" else [], lines if kind != "boxes" else []
            original = canvases[sid, kind]
            shifted = Image.new("RGB", original.size, "white")
            shifted.paste(original, (4, 4))
            for change, image in [("stroke_2", raster(size, b, l, 2)), ("stroke_4", raster(size, b, l, 4)),
                                  ("translate_4px_at_448", shifted), ("rotate_90", original.transpose(Image.Transpose.ROTATE_90))]:
                path = f"images/{sid}__{kind}__{change}.png"
                image.save(ROOT / path)
                vector, _ = encode(image)
                sensitivity.append({"id": sid, "variant": kind, "change": change, "image_path": path,
                                    "embedding": vector, "similarity": float(np.dot(source_row["embedding"], vector))})
    result = {"metadata": metadata, "samples": samples, "variants": variants,
              "extra_queries": extra_queries, "sensitivity": sensitivity}
    save_json(ROOT / "results.json", result)
    print(json.dumps({k: v["summary"] for k, v in variants.items()}, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
