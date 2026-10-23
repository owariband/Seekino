"""Grounding DINO Tiny: MyGO episode 1 detection and local latency experiment."""

import argparse
import hashlib
import json
import os
import platform
import resource
import statistics
import threading
import time
from pathlib import Path

import psutil
import torch
import transformers
from PIL import Image, ImageDraw, ImageFont
from transformers import AutoModelForZeroShotObjectDetection, AutoProcessor

parser = argparse.ArgumentParser()
parser.add_argument("--model-dir", required=True)
parser.add_argument("--manifest", required=True)
parser.add_argument("--output", required=True)
parser.add_argument("--device", choices=["cpu", "mps"], required=True)
parser.add_argument("--repeat", type=int, default=3)
parser.add_argument("--warmup", type=int, default=2)
parser.add_argument("--threads", type=int, default=6)
parser.add_argument("--box-threshold", type=float, default=0.35)
parser.add_argument("--text-threshold", type=float, default=0.25)
parser.add_argument("--prompt", default="person . window . door . chair . table . guitar . telescope .")
args = parser.parse_args()
out = Path(args.output)
out.mkdir(parents=True, exist_ok=True)
manifest_path = Path(args.manifest)
manifest = json.loads(manifest_path.read_text())
torch.set_num_threads(args.threads)
if args.device == "mps" and not torch.backends.mps.is_available():
    raise RuntimeError("MPS unavailable; run CPU explicitly instead of silently falling back")

process = psutil.Process()
peak_rss = [process.memory_info().rss]
stop = threading.Event()


def monitor():
    while not stop.wait(0.02):
        peak_rss[0] = max(peak_rss[0], process.memory_info().rss)


threading.Thread(target=monitor, daemon=True).start()
start = time.perf_counter()
processor = AutoProcessor.from_pretrained(args.model_dir, local_files_only=True, use_fast=False)
model = AutoModelForZeroShotObjectDetection.from_pretrained(
    args.model_dir, local_files_only=True, disable_custom_kernels=True,
).eval().to(args.device)
if args.device == "mps":
    torch.mps.synchronize()
load_seconds = time.perf_counter() - start
print(json.dumps({"event": "loaded", "device": args.device, "seconds": load_seconds}), flush=True)
samples = []
for sample in manifest["samples"]:
    path = Path(sample["frame_path"])
    if not path.is_absolute():
        path = manifest_path.parent / path
    samples.append((sample, Image.open(path).convert("RGB")))
driver_memory = []


def detect(image):
    begin = time.perf_counter()
    inputs = processor(images=image, text=args.prompt, return_tensors="pt").to(args.device)
    if args.device == "mps":
        torch.mps.synchronize()
    forward_start = time.perf_counter()
    with torch.inference_mode():
        outputs = model(**inputs)
    if args.device == "mps":
        torch.mps.synchronize()
        driver_memory.append(torch.mps.driver_allocated_memory())
    forward_end = time.perf_counter()
    result = processor.post_process_grounded_object_detection(
        outputs, inputs.input_ids, threshold=args.box_threshold,
        text_threshold=args.text_threshold, target_sizes=[image.size[::-1]],
    )[0]
    boxes = result["boxes"].detach().cpu().tolist()
    scores = result["scores"].detach().cpu().tolist()
    labels = result.get("text_labels", result.get("labels"))
    finish = time.perf_counter()
    width, height = image.size
    detections = []
    for box, score, label in zip(boxes, scores, labels):
        x0, y0, x1, y1 = box
        detections.append({
            "label": label, "score": round(score, 6),
            "box_xyxy": [round(v, 2) for v in box],
            "center_xy_normalized": [round((x0+x1)/2/width, 6), round((y0+y1)/2/height, 6)],
            "box_area_ratio": round((x1-x0)*(y1-y0)/width/height, 6),
        })
    return {"forward_ms": (forward_end-forward_start)*1000,
            "pipeline_ms": (finish-begin)*1000,
            "input_tensor_shape": list(inputs["pixel_values"].shape), "detections": detections}


warmup = [detect(samples[0][1]) for _ in range(args.warmup)]
records = {s["id"]: {"sample": s, "measurements": []} for s, _ in samples}
font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 23)
for repeat in range(args.repeat):
    for sample, image in samples:
        result = detect(image)
        record = records[sample["id"]]
        record["measurements"].append({k: v for k, v in result.items() if k != "detections"})
        if repeat == 0:
            record["detections"] = result["detections"]
            canvas = image.copy()
            draw = ImageDraw.Draw(canvas)
            for index, detection in enumerate(result["detections"]):
                color = "#ef4444" if detection["label"] == "person" else "#00bfff"
                box = detection["box_xyxy"]
                draw.rectangle(box, outline=color, width=4)
                label = f"{index+1}: {detection['label']} {detection['score']:.2f}"
                x, y = max(0, box[0]), max(0, box[1]-28)
                draw.rectangle(draw.textbbox((x, y), label, font=font), fill="black")
                draw.text((x, y), label, font=font, fill=color)
            canvas.save(out / f"{sample['id']}.jpg", quality=94)
        print(json.dumps({"event": "inference", "device": args.device, "repeat": repeat,
                          "id": sample["id"], "pipeline_ms": result["pipeline_ms"],
                          "labels": [d["label"] for d in result["detections"]]}), flush=True)
stop.set()
timings = [m for r in records.values() for m in r["measurements"]]
report = {
    "model": "IDEA-Research/grounding-dino-tiny",
    "revision": "a2bb814dd30d776dcf7e30523b00659f4f141c71",
    "arguments": vars(args), "torch": torch.__version__, "transformers": transformers.__version__,
    "platform": platform.platform(), "parameter_count": sum(p.numel() for p in model.parameters()),
    "dtype": str(next(model.parameters()).dtype), "processor_size": processor.image_processor.size,
    "batch_size": 1, "custom_kernels_disabled": True,
    "mps_fallback_env": os.environ.get("PYTORCH_ENABLE_MPS_FALLBACK", "unset"),
    "timing_scope": "Preloaded PIL image -> preprocessing, transfer, forward, synchronization, postprocess, CPU outputs. Excludes imports/model loading/JPEG IO/annotation. No NMS or manual filtering added.",
    "model_load_seconds": load_seconds,
    "warmup_pipeline_ms": [r["pipeline_ms"] for r in warmup],
    "median_forward_ms": statistics.median(m["forward_ms"] for m in timings),
    "median_pipeline_ms": statistics.median(m["pipeline_ms"] for m in timings),
    "aggregate_images_per_second": len(timings)/sum(m["pipeline_ms"]/1000 for m in timings),
    "rss_peak_sampled_bytes": peak_rss[0],
    "process_ru_maxrss_bytes_macos": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    "mps_driver_max_observed_bytes": max(driver_memory, default=None),
    "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    "records": list(records.values()),
}
(out / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n")
print(json.dumps({k: v for k, v in report.items() if k not in ["records", "arguments"]}), flush=True)
