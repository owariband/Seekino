"""Run fixed local checkpoints without reading composition reference labels."""
import argparse
import gc
import hashlib
import json
import os
import statistics
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CACHE = ROOT.parents[3] / ".cache/composition-models"
os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                  TORCH_HOME=str(CACHE / "torch"), PYTORCH_ENABLE_MPS_FALLBACK="0")
import clip
import psutil
import torch
from PIL import Image, ImageOps
from torchvision.transforms.functional import to_tensor
from transformers import AutoImageProcessor, AutoModel, AutoModelForZeroShotObjectDetection, AutoProcessor

parser = argparse.ArgumentParser()
parser.add_argument("--device", choices=["cpu", "mps"], default="cpu")
parser.add_argument("--only", nargs="*", default=["clip", "dinov2", "musiq_ava", "grounding_dino"])
args = parser.parse_args()
if args.device == "mps" and not torch.backends.mps.is_available():
    raise RuntimeError("MPS unavailable; choose CPU explicitly")
torch.set_num_threads(6)
torch.manual_seed(0)
manifest = json.loads((ROOT / "manifest.json").read_text())
models = json.loads((ROOT / "models.json").read_text())
samples = manifest["samples"]
for s in samples:
    assert hashlib.sha256((ROOT / s["image_path"]).read_bytes()).hexdigest() == s["sha256"]
images = {s["id"]: Image.open(ROOT / s["image_path"]).convert("RGB") for s in samples}
out = ROOT / "backbones"
out.mkdir(exist_ok=True)


def sync():
    if args.device == "mps":
        torch.mps.synchronize()


def feature(tensor):
    return torch.nn.functional.normalize(tensor.float().reshape(1, -1), dim=-1)[0].cpu().tolist()


def save(name, model, records, preprocessing, **extra):
    result = {"name": name, "device": args.device, "dtype": "float32", "cpu_threads": 6,
              "parameter_count": sum(p.numel() for p in model.parameters()),
              "checkpoint": models[name.split("__")[0]],
              "preprocessing": preprocessing, "batch_size": 1, "repeat": 1,
              "timing_scope": "PIL preprocessing, model forward, device sync, CPU feature/output; no image disk IO, model loading or drawing. One warmup per variant; indicative single-pass timing, not hardware benchmark.",
              "manifest_sha256": hashlib.sha256((ROOT / "manifest.json").read_bytes()).hexdigest(),
              "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "torch": torch.__version__, "rss_after_bytes": psutil.Process().memory_info().rss,
              "median_pipeline_ms": statistics.median(r["pipeline_ms"] for r in records),
              "records": records, **extra}
    if args.device == "mps":
        result["mps_driver_after_bytes"] = torch.mps.driver_allocated_memory()
    (out / f"{name}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"event": "finished", "variant": name,
                      "samples": len(records), "median_ms": result["median_pipeline_ms"]}), flush=True)


for kind in args.only:
    begin_load = time.perf_counter()
    if kind == "clip":
        model, processor = clip.load(models[kind]["path"], device="cpu", jit=False)
        model = model.float().eval().to(args.device)
    elif kind == "dinov2":
        processor = AutoImageProcessor.from_pretrained(models[kind]["path"], local_files_only=True, use_fast=False)
        model = AutoModel.from_pretrained(models[kind]["path"], local_files_only=True).eval().to(args.device)
    elif kind == "musiq_ava":
        from pyiqa.archs.musiq_arch import MUSIQ
        model = MUSIQ(pretrained=False, num_class=10,
                      pretrained_model_path=models[kind]["path"]).eval().to(args.device)
    elif kind == "grounding_dino":
        processor = AutoProcessor.from_pretrained(models[kind]["path"], local_files_only=True, use_fast=False)
        model = AutoModelForZeroShotObjectDetection.from_pretrained(
            models[kind]["path"], local_files_only=True, disable_custom_kernels=True).eval().to(args.device)
    sync()
    load_seconds = time.perf_counter() - begin_load
    captured = []
    if kind == "musiq_ava":
        hook = model.head.register_forward_pre_hook(lambda module, inputs: captured.append(inputs[0].detach()))
    variants = ["native", "fullframe"] if kind in ["clip", "dinov2"] else ["native"]
    for variant in variants:
        records = []
        for index in range(-1, len(samples)):
            s = samples[max(index, 0)]
            image = images[s["id"]]
            start = time.perf_counter()
            with torch.inference_mode():
                if kind == "clip":
                    source = ImageOps.pad(image, (224, 224), color=(123, 117, 104)) if variant == "fullframe" else image
                    tensor = processor(source).unsqueeze(0).to(args.device)
                    vector = model.encode_image(tensor)
                    raw = {"embedding": feature(vector), "input_tensor_shape": list(tensor.shape)}
                elif kind == "dinov2":
                    source = ImageOps.pad(image, (224, 224), color=(124, 116, 104)) if variant == "fullframe" else image
                    opts = {"do_resize": False, "do_center_crop": False} if variant == "fullframe" else {}
                    inputs = processor(images=source, return_tensors="pt", **opts).to(args.device)
                    prediction = model(**inputs)
                    raw = {"embedding": feature(prediction.last_hidden_state[:, 0]),
                           "input_tensor_shape": list(inputs["pixel_values"].shape)}
                elif kind == "musiq_ava":
                    captured.clear()
                    tensor = to_tensor(image).unsqueeze(0).to(args.device)
                    score, distribution = model(tensor, return_mos=True, return_dist=True)
                    raw = {"embedding": feature(captured[-1]), "aesthetic_score_1_to_10": score.cpu().item(),
                           "score_distribution": distribution[0].cpu().tolist(),
                           "input_tensor_shape": list(tensor.shape)}
                else:
                    inputs = processor(images=image, text="person . window . door . chair . table . beam . railing . phone . building .", return_tensors="pt").to(args.device)
                    prediction = model(**inputs)
                    detection = processor.post_process_grounded_object_detection(
                        prediction, inputs.input_ids, threshold=0.35, text_threshold=0.25,
                        target_sizes=[image.size[::-1]])[0]
                    labels = detection.get("text_labels", detection.get("labels"))
                    boxes = detection["boxes"].cpu().tolist()
                    raw = {"detections": [{"label": label, "score": float(score), "box_xyxy_normalized": [v / image.size[j % 2] for j, v in enumerate(box)]}
                                           for label, score, box in zip(labels, detection["scores"].cpu().tolist(), boxes)],
                           "input_tensor_shape": list(inputs["pixel_values"].shape)}
            sync()
            if index >= 0:
                records.append({"id": s["id"], "pipeline_ms": (time.perf_counter()-start)*1000, **raw})
                print(json.dumps({"event": "image", "model": kind, "variant": variant, "id": s["id"]}), flush=True)
        name = f"{kind}__{variant}" if kind in ["clip", "dinov2"] else kind
        prep = "Official processor defaults" if variant == "native" else "Full frame fitted to 224x224 with fixed mean-color padding, no content crop"
        extra = {"model_load_seconds": load_seconds}
        if kind == "musiq_ava":
            prep = "Whole RGB tensor [0,1], model normalizes [-1,1], official multi-scale patches at 224/384/original"
            extra["feature_location"] = "384D transformer CLS immediately before aesthetic head"
        if kind == "grounding_dino":
            extra.update(box_threshold=0.35, text_threshold=0.25, extra_nms=False,
                         prompt="person . window . door . chair . table . beam . railing . phone . building .")
        save(name, model, records, prep, **extra)
    if kind == "musiq_ava":
        hook.remove()
    del model
    gc.collect()
    if args.device == "mps":
        torch.mps.empty_cache()
