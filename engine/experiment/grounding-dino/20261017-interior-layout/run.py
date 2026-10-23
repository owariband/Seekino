"""Run three fixed Grounding DINO prompts on the user-provided interior image."""

import argparse
import hashlib
import json
import os
import statistics
import time
from pathlib import Path

os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                  PYTORCH_ENABLE_MPS_FALLBACK="0", TOKENIZERS_PARALLELISM="false")

import torch
import transformers
from PIL import Image, ImageDraw, ImageFont
from transformers import AutoModelForZeroShotObjectDetection, AutoProcessor

parser = argparse.ArgumentParser()
parser.add_argument("--model-dir", required=True)
args = parser.parse_args()
root = Path(__file__).resolve().parent
manifest_bytes = (root / "manifest.json").read_bytes()
manifest = json.loads(manifest_bytes)
assert hashlib.sha256((root / manifest["image_path"]).read_bytes()).hexdigest() == manifest["sha256"]
torch.set_num_threads(6)
if not torch.backends.mps.is_available():
    raise RuntimeError("MPS unavailable; no silent CPU fallback")
image = Image.open(root / manifest["image_path"]).convert("RGB")
begin = time.perf_counter()
processor = AutoProcessor.from_pretrained(args.model_dir, local_files_only=True, use_fast=False)
model = AutoModelForZeroShotObjectDetection.from_pretrained(
    args.model_dir, local_files_only=True, disable_custom_kernels=True,
).eval().to("mps")
torch.mps.synchronize()
load_seconds = time.perf_counter() - begin
print(json.dumps({"event": "loaded", "seconds": load_seconds}), flush=True)


def detect(prompt):
    start = time.perf_counter()
    inputs = processor(images=image, text=prompt, return_tensors="pt").to("mps")
    torch.mps.synchronize()
    forward_start = time.perf_counter()
    with torch.inference_mode():
        outputs = model(**inputs)
    torch.mps.synchronize()
    forward_ms = (time.perf_counter() - forward_start) * 1000
    result = processor.post_process_grounded_object_detection(
        outputs, inputs.input_ids, threshold=manifest["box_threshold"],
        text_threshold=manifest["text_threshold"], target_sizes=[image.size[::-1]],
    )[0]
    boxes = result["boxes"].detach().cpu().tolist()
    scores = result["scores"].detach().cpu().tolist()
    labels = result.get("text_labels", result.get("labels"))
    pipeline_ms = (time.perf_counter() - start) * 1000
    detections = []
    for box, score, label in zip(boxes, scores, labels):
        x0, y0, x1, y1 = box
        detections.append({"label": label, "score": round(score, 6),
                           "box_xyxy": [round(v, 2) for v in box],
                           "center_xy_normalized": [round((x0+x1)/2/image.width, 6), round((y0+y1)/2/image.height, 6)],
                           "box_area_ratio": round((x1-x0)*(y1-y0)/image.width/image.height, 6)})
    return {"forward_ms": forward_ms, "pipeline_ms": pipeline_ms,
            "input_tensor_shape": list(inputs["pixel_values"].shape), "detections": detections}


warmup = [detect(manifest["cases"][0]["prompt"])["pipeline_ms"] for _ in range(2)]
records = [{**case, "measurements": []} for case in manifest["cases"]]
font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 26)
for repetition in range(manifest["repeat"]):
    for record in records:
        result = detect(record["prompt"])
        record["measurements"].append({k: v for k, v in result.items() if k != "detections"})
        if repetition == 0:
            record["detections"] = result["detections"]
            canvas = image.copy()
            draw = ImageDraw.Draw(canvas)
            for i, detection in enumerate(result["detections"]):
                color = "#ef4444" if detection["label"] == "person" else "#00bfff"
                box = detection["box_xyxy"]
                draw.rectangle(box, outline=color, width=5)
                label = f"{i+1}: {detection['label']} {detection['score']:.3f}"
                x, y = max(0, box[0]), max(0, box[1]-32)
                draw.rectangle(draw.textbbox((x, y), label, font=font), fill="black")
                draw.text((x, y), label, font=font, fill=color)
            canvas.save(root / f"{record['id']}.jpg", quality=95)
        print(json.dumps({"event": "inference", "id": record["id"], "repetition": repetition,
                          "pipeline_ms": result["pipeline_ms"],
                          "labels": [d["label"] for d in result["detections"]]}), flush=True)
for record in records:
    record["median_pipeline_ms"] = statistics.median(m["pipeline_ms"] for m in record["measurements"])
report = {"model": manifest["model"], "revision": manifest["revision"],
          "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
          "image_sha256": manifest["sha256"], "image_size": list(image.size),
          "device": "mps", "dtype": str(next(model.parameters()).dtype), "batch_size": 1,
          "parameter_count": sum(p.numel() for p in model.parameters()),
          "torch": torch.__version__, "transformers": transformers.__version__,
          "processor_size": processor.image_processor.size, "cpu_threads": 6,
          "custom_kernels_disabled": True, "mps_fallback_env": os.environ["PYTORCH_ENABLE_MPS_FALLBACK"],
          "box_threshold": manifest["box_threshold"], "text_threshold": manifest["text_threshold"],
          "model_load_seconds": load_seconds, "warmup_pipeline_ms": warmup,
          "timing_scope": "Preloaded PIL image through preprocessing, device transfer, forward, MPS synchronization, official postprocess and CPU outputs. Excludes loading/imports/drawing. No extra NMS or manual filtering.",
          "records": records}
(root / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
print(json.dumps({"event": "finished", "cases": len(records)}), flush=True)
