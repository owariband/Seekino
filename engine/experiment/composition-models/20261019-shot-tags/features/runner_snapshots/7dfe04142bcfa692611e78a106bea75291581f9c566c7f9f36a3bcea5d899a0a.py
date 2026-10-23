"""Resumable offline PCP and person-box features for pilot or full gallery manifests."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import statistics
import sys
import time

sys.dont_write_bytecode = True
os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1",
                  TOKENIZERS_PARALLELISM="false", PYTORCH_ENABLE_MPS_FALLBACK="0")

import cv2
import numpy as np
from PIL import Image, __version__ as pillow_version
import torch
import torchvision
from torchvision.ops import nms
import transformers
from transformers import AutoModelForZeroShotObjectDetection, AutoProcessor


OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[3]
CACHE = ROOT / ".cache/composition-models"
CLASS_ORDER = ["rule_of_thirds", "vertical", "horizontal", "diagonal", "curved",
               "triangle", "center", "symmetric", "pattern"]
PARAMETERS = {"prompt": "person .", "box_threshold": 0.35, "text_threshold": 0.25,
              "nms_iou_threshold": 0.5, "device": "cpu", "dtype": "float32", "batch_size": 1}


def digest(path):
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def union_area(boxes):
    """Exact union of axis-aligned boxes, in normalized image coordinates."""
    area = 0.0
    xs = sorted({float(box[i]) for box in boxes for i in (0, 2)})
    for left, right in zip(xs, xs[1:]):
        intervals = sorted((box[1], box[3]) for box in boxes if box[0] < right and box[2] > left)
        height, end = 0.0, 0.0
        for bottom, top in intervals:
            height += max(0.0, top - max(bottom, end))
            end = max(end, top)
        area += (right - left) * height
    return float(area)


def infer(image, bgr, net, processor, model):
    start = time.perf_counter()
    net.setInput(cv2.dnn.blobFromImage(bgr, scalefactor=1.0, size=(227, 227), mean=(0, 0, 0),
                                      swapRB=False, crop=False))
    logits, probabilities = [x.reshape(-1).copy() for x in net.forward(["ftfc8_9ly", "ftsoftmax"])]
    expected = np.exp(logits - logits.max())
    expected /= expected.sum()
    if logits.shape != (9,) or probabilities.shape != (9,) or not np.isfinite(logits).all():
        raise ValueError("Invalid PCP output dimensions or values")
    if not np.allclose(expected, probabilities, atol=1e-6) or not np.isclose(probabilities.sum(), 1, atol=1e-5):
        raise ValueError("PCP logits/softmax verification failed")
    pcp_ms = (time.perf_counter() - start) * 1000
    pcp = {"class_order": CLASS_ORDER, "logits": logits.tolist(), "probs": probabilities.tolist(),
           "top_class": CLASS_ORDER[int(probabilities.argmax())], "top_probability": float(probabilities.max()),
           "model": "PCP photographic composition FT_CNN CaffeNet", "logits_layer": "ftfc8_9ly",
           "probabilities_layer": "ftsoftmax"}

    start = time.perf_counter()
    with torch.inference_mode():
        inputs = processor(images=image, text=PARAMETERS["prompt"], return_tensors="pt").to("cpu")
        output = model(**inputs)
        detected = processor.post_process_grounded_object_detection(
            output, inputs.input_ids, threshold=PARAMETERS["box_threshold"],
            text_threshold=PARAMETERS["text_threshold"], target_sizes=[image.size[::-1]])[0]
    raw_boxes, scores = detected["boxes"].cpu().float(), detected["scores"].cpu().float()
    labels = detected["text_labels"]
    if not torch.isfinite(raw_boxes).all() or not torch.isfinite(scores).all():
        raise ValueError("Non-finite GroundingDINO detections")
    clipped = raw_boxes.clone()
    clipped[:, 0::2].clamp_(0, image.width)
    clipped[:, 1::2].clamp_(0, image.height)
    scale = torch.tensor([image.width, image.height, image.width, image.height], dtype=torch.float32)
    valid = (clipped[:, 2] > clipped[:, 0]) & (clipped[:, 3] > clipped[:, 1])
    eligible = [i for i, label in enumerate(labels) if label.strip() == "person" and bool(valid[i])]
    kept_local = nms(clipped[eligible], scores[eligible], PARAMETERS["nms_iou_threshold"]).tolist()
    kept_raw = [eligible[i] for i in kept_local]
    raw_detections = []
    for i, (box, score, label) in enumerate(zip(raw_boxes, scores, labels)):
        raw_detections.append({"raw_index": i, "label": label, "score": float(score),
                               "xyxy_pixels": box.tolist(), "xyxy_normalized_unclipped": (box / scale).tolist(),
                               "xyxy_normalized": (clipped[i] / scale).tolist(),
                               "eligible_for_person_nms": i in eligible, "kept_after_nms": i in kept_raw})
    boxes = (clipped[kept_raw] / scale).numpy()
    person_boxes = [[*map(float, box), float(scores[index])] for box, index in zip(boxes, kept_raw)]
    if len(boxes):
        areas = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
        centers = (boxes[:, :2] + boxes[:, 2:]) / 2
        largest = int(areas.argmax())
        centroid = np.average(centers, weights=areas, axis=0).astype(float).tolist()
        largest_area, largest_box = float(areas[largest]), boxes[largest].astype(float).tolist()
        largest_center = centers[largest].astype(float).tolist()
        extent = [float(boxes[:, 0].min()), float(boxes[:, 1].min()), float(boxes[:, 2].max()), float(boxes[:, 3].max())]
    else:
        centroid = largest_box = largest_center = extent = None
        largest_area = 0.0
    detector = {"model": "IDEA-Research/grounding-dino-tiny", "model_revision": "a2bb814dd30d776dcf7e30523b00659f4f141c71",
                **PARAMETERS, "input_tensor_shape": list(inputs["pixel_values"].shape),
                "raw_logits_shape": list(output.logits.shape), "raw_detections": raw_detections,
                "raw_thresholded_count": len(raw_detections), "person_count_before_nms": len(eligible),
                "nms_kept_raw_indices": kept_raw, "person_boxes": person_boxes, "person_count": len(person_boxes),
                "largest_person_area": largest_area, "largest_person_box": largest_box,
                "largest_person_center": largest_center, "people_centroid": centroid,
                "people_centroid_method": "box-area-weighted mean of retained normalized box centers",
                "person_box_union_area": union_area(boxes), "people_extent": extent,
                "coordinate_convention": "person_boxes=[x0,y0,x1,y1,score]; xyxy clipped to [0,1]",
                "count_semantics": "retained detector boxes; zero detections does not prove no person"}
    detector_ms = (time.perf_counter() - start) * 1000
    return pcp, detector, {"pcp_pipeline": pcp_ms, "detector_pipeline": detector_ms}


def validate_cached(value, image_sha256, contract_id):
    if value["image_sha256"] != image_sha256 or value["feature_contract_id"] != contract_id:
        raise ValueError("Feature cache identity/contract mismatch")
    pcp, detector = value["pcp"], value["detector"]
    probs, logits = np.asarray(pcp["probs"]), np.asarray(pcp["logits"])
    if pcp["class_order"] != CLASS_ORDER or probs.shape != (9,) or logits.shape != (9,):
        raise ValueError("Feature cache PCP schema mismatch")
    if not np.isfinite(probs).all() or not np.isfinite(logits).all() or not np.isclose(probs.sum(), 1, atol=1e-5):
        raise ValueError("Feature cache PCP values invalid")
    if detector["person_count"] != len(detector["person_boxes"]):
        raise ValueError("Feature cache detector count mismatch")
    for box in detector["person_boxes"]:
        if len(box) != 5 or not all(np.isfinite(box)) or not 0 <= box[0] < box[2] <= 1 or not 0 <= box[1] < box[3] <= 1:
            raise ValueError("Feature cache normalized person box invalid")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=OUT / "pilot/frames.jsonl")
    parser.add_argument("--output", type=Path, default=OUT / "pilot/features.jsonl")
    parser.add_argument("--threads", type=int, default=6)
    parser.add_argument("--limit", type=int, help="Optional first-N smoke run; cached results are reusable")
    args = parser.parse_args()
    if args.threads < 1 or (args.limit is not None and args.limit < 1):
        parser.error("threads and limit must be positive")
    output = args.output.resolve()
    if not output.is_relative_to(OUT):
        parser.error("All output files must remain inside the shot-tags experiment")
    input_path = args.input.resolve()
    raw_input = input_path.read_bytes()
    rows = [json.loads(line) for line in raw_input.splitlines() if line.strip()]
    if args.limit is not None:
        rows = rows[:args.limit]
    if not rows or len({r["frame_id"] for r in rows}) != len(rows):
        raise ValueError("Input must contain unique, nonempty frame rows")
    input_sha256 = hashlib.sha256(raw_input).hexdigest()
    source_manifest = json.loads((OUT / "input_manifest.json").read_text())
    gallery = Path(source_manifest["gallery"])
    cv2.setNumThreads(args.threads)
    torch.set_num_threads(args.threads)
    torch.manual_seed(0)

    model_info = json.loads((OUT.parent / "20261017-first-pass/models.json").read_text())["grounding_dino"]
    model_path = Path(model_info["path"])
    for artifact in model_info["files"]:
        file = model_path / artifact["name"]
        if not file.is_file() or digest(file) != artifact["sha256"]:
            raise ValueError(f"Missing/changed cached GroundingDINO artifact: {artifact['name']}")
    pcp_weights = CACHE / "pcp/caffenet_finetune_iter_10000.caffemodel"
    pcp_deploy = CACHE / "pcp/deploy_opencv.prototxt"
    pcp_weight_sha = digest(pcp_weights)
    if pcp_weight_sha != "1c56d9d0526aaf0b3ef6f62b20283bd27cc0c48a1c369ddb16d356608d980700":
        raise ValueError("Cached PCP weights differ from original experiment")
    contract = {"feature_schema_version": "pcp-person-v1", "pcp": {"weights_sha256": pcp_weight_sha,
                "deploy_sha256": digest(pcp_deploy), "class_order": CLASS_ORDER,
                "preprocessing": "BGR uint8 to float32, direct227x227 INTER_LINEAR, scale1, mean0, no RGB swap/crop",
                "backend": "OpenCV DNN CPU", "logits_layer": "ftfc8_9ly", "probabilities_layer": "ftsoftmax",
                "revision": "official unversioned release identified by checkpoint and deployment SHA256"},
                "detector": {"checkpoint": model_info, **PARAMETERS, "processor": "official defaults, use_fast=False",
                             "disable_custom_kernels": True, "extra_nms": "torchvision.ops.nms CPU, IoU>0.5 suppressed",
                             "clip_before_nms": True, "person_label_filter": "exact stripped label == person"},
                "runtime": {"python": platform.python_version(), "torch": torch.__version__,
                            "torchvision": torchvision.__version__, "transformers": transformers.__version__,
                            "opencv": cv2.__version__, "numpy": np.__version__, "pillow": pillow_version,
                            "threads": args.threads, "seed": 0, "device": "cpu", "local_files_only": True}}
    contract_id = hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    feature_root = OUT / "features"
    feature_root.mkdir(exist_ok=True)
    cache = feature_root / contract_id
    cache.mkdir(exist_ok=True)
    contract_path = cache / "contract.json"
    save_json(contract_path, contract)
    receipt_path = feature_root / "runs" / (str(output.relative_to(OUT)).replace("/", "_") + ".json")
    receipt = {"status": "running", "started_at": datetime.now(timezone.utc).isoformat(),
               "input_path": str(input_path), "input_sha256": input_sha256, "input_rows": len(rows),
               "output_path": str(output), "feature_contract_id": contract_id,
               "contract_path": str(contract_path.relative_to(OUT)), "runner_sha256": digest(Path(__file__)),
               "cache_hits": 0, "new_inferences": 0, "completed_rows": 0, "limit": args.limit,
               "timing_scope": "per-image decoded inputs through preprocessing, inference and postprocess/NMS; image decode separately; no model load",
               "mps_available": torch.backends.mps.is_available(), "platform": platform.platform()}
    net = processor = model = None
    warmed_up = False
    timings = []
    started = time.perf_counter()
    with (feature_root / "inference.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another shot-tags feature process is active") from None
        save_json(receipt_path, receipt)
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            with output.open("w") as handle:
                for index, row in enumerate(rows):
                    image_path = Path(row["image_path"]) if row.get("image_path") else gallery / row["frame_path"]
                    image_bytes = image_path.read_bytes()
                    image_sha = hashlib.sha256(image_bytes).hexdigest()
                    if image_sha != row["sha256"]:
                        raise ValueError(f"Image SHA256 mismatch: {row['frame_id']}")
                    cached_path = cache / f"{image_sha}.json"
                    cache_hit = cached_path.exists()
                    if cache_hit:
                        feature = json.loads(cached_path.read_text())
                        validate_cached(feature, image_sha, contract_id)
                        receipt["cache_hits"] += 1
                    else:
                        if model is None:
                            load_start = time.perf_counter()
                            net = cv2.dnn.readNetFromCaffe(str(pcp_deploy), str(pcp_weights))
                            net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
                            net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
                            processor = AutoProcessor.from_pretrained(str(model_path), local_files_only=True, use_fast=False)
                            model = AutoModelForZeroShotObjectDetection.from_pretrained(
                                str(model_path), local_files_only=True, disable_custom_kernels=True).eval().to("cpu")
                            model.requires_grad_(False)
                            receipt["model_load_seconds"] = time.perf_counter() - load_start
                        decode_start = time.perf_counter()
                        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
                        bgr = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
                        if bgr is None or tuple(bgr.shape[1::-1]) != image.size:
                            raise ValueError(f"Image decoding failed: {row['frame_id']}")
                        decode_ms = (time.perf_counter() - decode_start) * 1000
                        if not warmed_up:
                            _, _, receipt["warmup_timing_ms"] = infer(image, bgr, net, processor, model)
                            warmed_up = True
                        pcp, detector, measured = infer(image, bgr, net, processor, model)
                        measured["image_decode"] = decode_ms
                        feature = {"feature_schema_version": "pcp-person-v1", "feature_contract_id": contract_id,
                                   "image_sha256": image_sha, "image_size": list(image.size),
                                   "computed_at": datetime.now(timezone.utc).isoformat(), "pcp": pcp,
                                   "detector": detector, "timing_ms": measured}
                        validate_cached(feature, image_sha, contract_id)
                        save_json(cached_path, feature)
                        receipt["new_inferences"] += 1
                    result = {**row, **feature, "cache_path": str(cached_path.relative_to(OUT)),
                              "cache_sha256": digest(cached_path), "cache_hit": cache_hit,
                              "contract_path": str(contract_path.relative_to(OUT))}
                    handle.write(json.dumps(result, ensure_ascii=False, allow_nan=False) + "\n")
                    handle.flush()
                    timings.append(feature["timing_ms"])
                    receipt["completed_rows"] = index + 1
                    if (index + 1) % 10 == 0 or index + 1 == len(rows):
                        receipt["elapsed_seconds"] = time.perf_counter() - started
                        save_json(receipt_path, receipt)
                        print(json.dumps({"stage": "features", "completed": index + 1, "total": len(rows),
                                          "new_inferences": receipt["new_inferences"], "cache_hits": receipt["cache_hits"],
                                          "elapsed_seconds": round(receipt["elapsed_seconds"], 1)}), flush=True)
            receipt.update(status="complete", finished_at=datetime.now(timezone.utc).isoformat(),
                           elapsed_seconds=time.perf_counter() - started, output_sha256=digest(output),
                           median_pcp_ms=statistics.median(t["pcp_pipeline"] for t in timings),
                           median_detector_ms=statistics.median(t["detector_pipeline"] for t in timings))
            save_json(receipt_path, receipt)
            print(json.dumps(receipt, indent=2))
        except Exception as error:
            receipt.update(status="failed", error_type=type(error).__name__, error=str(error),
                           elapsed_seconds=time.perf_counter() - started)
            save_json(receipt_path, receipt)
            raise


if __name__ == "__main__":
    main()
