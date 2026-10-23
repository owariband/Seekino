"""Encode only existing person boxes with the frozen local DINOv2 Small."""
import hashlib
import json
import os
import platform
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                  HF_HUB_DISABLE_TELEMETRY="1", TOKENIZERS_PARALLELISM="false",
                  PYTORCH_ENABLE_MPS_FALLBACK="0")
import numpy as np
import torch
import transformers
from PIL import Image, ImageDraw, ImageOps, __version__ as pillow_version
from transformers import AutoImageProcessor, AutoModel

ROOT = Path(__file__).resolve().parent
EXPERIMENTS = ROOT.parent.parent
FIRST = EXPERIMENTS / "20261017-first-pass"
RASTER = EXPERIMENTS / "20261017-layout-raster"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def raster(size, boxes):
    """Preserve the previous all-box raster's coordinate and drawing convention."""
    canvas = Image.new("RGB", (448, 448), "white")
    w, h = ImageOps.contain(Image.new("RGB", size), canvas.size).size
    ox, oy = (448-w)//2, (448-h)//2
    draw = ImageDraw.Draw(canvas)

    def point(p):
        return ox + np.clip(p[0], 0, 1)*(w-1), oy + np.clip(p[1], 0, 1)*(h-1)

    for box in boxes:
        draw.rectangle([point(box[:2]), point(box[2:])], outline="black", width=3)
    return canvas


def main():
    torch.set_num_threads(6)
    torch.manual_seed(0)
    paths = [FIRST / "manifest.json", FIRST / "backbones/grounding_dino.json",
             FIRST / "models.json", RASTER / "results.json", RASTER / "run.py"]
    manifest, grounding, models, previous = [json.loads(p.read_text()) for p in paths[:4]]
    ids = [s["id"] for s in manifest["samples"]]
    assert ids == [r["id"] for r in grounding["records"]]
    assert grounding["manifest_sha256"] == digest(paths[0])
    checkpoint = models["dinov2"]
    assert checkpoint == previous["metadata"]["checkpoint"]
    for f in checkpoint["files"]:
        assert digest(Path(checkpoint["path"]) / f["name"]) == f["sha256"]
    for sample in manifest["samples"]:
        assert digest(FIRST / sample["image_path"]) == sample["sha256"]
    metadata = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "manifest_sha256": digest(paths[0]), "checkpoint": checkpoint,
        "sources": {str(p.relative_to(EXPERIMENTS)): digest(p) for p in paths},
        "runner_sha256": digest(Path(__file__)), "device": "cpu", "dtype": "float32",
        "batch_size": 1, "threads": 6, "training": False, "seed": 0,
        "torch": torch.__version__, "transformers": transformers.__version__,
        "numpy": np.__version__, "pillow": pillow_version, "platform": platform.platform(),
        "offline": {"local_files_only": True, "HF_HUB_OFFLINE": "1",
                    "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1"},
        "selection": "Exact label == 'person' from saved thresholded detections; original order, no new threshold, NMS or confidence weighting.",
        "grounding_prompt": grounding["prompt"], "box_threshold": grounding["box_threshold"],
        "text_threshold": grounding["text_threshold"], "extra_nms": False,
        "render_size": [448, 448], "stroke_width_at_render": 3,
        "raster": "White RGB canvas, aspect-preserving contain of input frame; centered integer padding; normalized xyxy clipped to [0,1] times fitted dimension minus 1; black rectangle outlines; no labels or scores.",
        "input_size": [224, 224],
        "preprocessing": "448 to 224 LANCZOS; AutoImageProcessor use_fast=False, do_resize=False, do_center_crop=False; official rescale and normalize unchanged.",
        "features": "last_hidden_state[:,0], 384D CLS; float32 L2 normalization; no learned projection.",
        "empty_policy": "Save blank raster but valid=false, embedding=null and pipeline_ms=null when no person detection; never encode or rank these samples.",
        "timing": "One valid-image warmup; per valid image processor, CPU forward and feature transfer; excludes model loading, file I/O, box extraction and rendering; one measured pass, not benchmark.",
    }
    start = time.perf_counter()
    model = AutoModel.from_pretrained(checkpoint["path"], local_files_only=True).eval().to("cpu")
    model.requires_grad_(False)
    processor = AutoImageProcessor.from_pretrained(checkpoint["path"], local_files_only=True, use_fast=False)
    metadata["model_load_seconds"] = time.perf_counter() - start
    metadata["parameter_count"] = sum(p.numel() for p in model.parameters())
    metadata["processor_effective"] = {k: getattr(processor, k, None) for k in
                                       ["do_rescale", "rescale_factor", "do_normalize", "image_mean", "image_std"]}

    def encode(image):
        start = time.perf_counter()
        tensor = processor(images=image.resize((224, 224), Image.Resampling.LANCZOS),
                           do_resize=False, do_center_crop=False, return_tensors="pt").to("cpu")
        with torch.inference_mode():
            output = model(**tensor).last_hidden_state[:, 0]
            vector = torch.nn.functional.normalize(output.float(), dim=-1)[0].cpu().tolist()
        assert len(vector) == 384 and np.isfinite(vector).all()
        return vector, (time.perf_counter()-start)*1000

    (ROOT / "images").mkdir(exist_ok=True)
    old_rows = {r["id"]: r for r in previous["variants"]["boxes"]["records"]}
    records, controls, warmed_up = [], [], False
    for sample, g in zip(manifest["samples"], grounding["records"]):
        sid = sample["id"]
        detections = [d for d in g["detections"] if d["label"] == "person"]
        with Image.open(FIRST / sample["image_path"]) as original:
            assert list(original.size) == sample["input_size"]
            image = raster(original.size, [d["box_xyxy_normalized"] for d in detections])
        path = f"images/{sid}__persons.png"
        image.save(ROOT / path)
        valid = bool(detections)
        if valid and not warmed_up:
            encode(image)
            warmed_up = True
        vector, ms = encode(image) if valid else (None, None)
        records.append({"id": sid, "image_path": path, "image_sha256": digest(ROOT / path),
                        "valid": valid, "embedding": vector, "person_box_count": len(detections),
                        "detections": detections, "pipeline_ms": ms})
        if valid and len(detections) == len(g["detections"]):
            old = old_rows[sid]
            with Image.open(RASTER / old["image_path"]) as previous_image:
                equal = bool(np.array_equal(np.asarray(image), np.asarray(previous_image)))
            a, b = np.asarray(vector), np.asarray(old["embedding"])
            max_delta = float(np.max(np.abs(a-b)))
            cosine = float(np.dot(a, b) / (np.linalg.norm(a)*np.linalg.norm(b)))
            assert equal and max_delta < 1e-5 and cosine > 0.999999
            controls.append({"id": sid, "pixel_equal": equal,
                             "png_sha256_equal": digest(ROOT / path) == old["image_sha256"],
                             "embedding_max_abs_delta": max_delta, "embedding_cosine": cosine})
        print(json.dumps({"id": sid, "person_box_count": len(detections), "valid": valid}), flush=True)
    assert len(records) == 17 and sum(r["valid"] for r in records) == 13
    assert {r["id"] for r in controls} == {"grey_left", "stage_wide", "stage_left"}
    metadata["valid_count"] = 13
    metadata["invalid_count"] = 4
    metadata["median_pipeline_ms"] = statistics.median(r["pipeline_ms"] for r in records if r["valid"])
    metadata["all_box_identical_controls"] = controls
    (ROOT / "results.json").write_text(json.dumps({"metadata": metadata, "records": records},
                                                 ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"valid": 13, "invalid": 4, "median_pipeline_ms": metadata["median_pipeline_ms"],
                      "controls": controls}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
