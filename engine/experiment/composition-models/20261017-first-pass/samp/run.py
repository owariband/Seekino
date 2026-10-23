"""Run the official SAMP-Net checkpoint on the shared composition image manifest."""

import argparse
import ast
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import statistics
import sys
import time
import types

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

import cv2
import numpy as np
from PIL import Image
import torch
import torchvision.models as models
import torchvision.transforms as transforms


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--manifest", required=True, type=Path)
parser.add_argument("--repo", required=True, type=Path)
parser.add_argument("--weights", required=True, type=Path)
parser.add_argument("--output", type=Path, default=Path(__file__).parent / "results.json")
parser.add_argument("--device", choices=["cpu", "mps"], default="cpu")
parser.add_argument("--threads", type=int, default=6)
parser.add_argument("--repeat", type=int, default=3)
args = parser.parse_args()
torch.set_num_threads(args.threads)
torch.manual_seed(1)
cv2.setNumThreads(0)
if args.device == "mps" and not torch.backends.mps.is_available():
    raise RuntimeError("MPS requested but unavailable")

# The official Config asserts that its training dataset exists at import time.
# Remove just that assertion in memory; retain the exact official inference config.
source_root = args.repo.resolve() / "SAMPNet"
config_ast = ast.parse((source_root / "config.py").read_text())
for node in config_ast.body:
    if isinstance(node, ast.ClassDef) and node.name == "Config":
        node.body = [item for item in node.body if not isinstance(item, ast.Assert)]
config_module = types.ModuleType("config")
exec(compile(config_ast, str(source_root / "config.py"), "exec"), config_module.__dict__)
sys.modules["config"] = config_module
sys.path.insert(0, str(source_root))
import samp_net


def build_resnet_compatible(layers, pretrained=False):
    """Translate torchvision's removed positional pretrained arg to weights=None."""
    assert layers == 18 and not pretrained
    return torch.nn.Sequential(*list(models.resnet18(weights=None).children())[:-2])


samp_net.build_resnet = build_resnet_compatible
saliency_ast = ast.parse((source_root / "cadb_dataset.py").read_text())
saliency_node = next(item for item in saliency_ast.body
                     if isinstance(item, ast.FunctionDef) and item.name == "detect_saliency")
saliency_module = ast.Module(body=[saliency_node], type_ignores=[])
exec(compile(saliency_module, str(source_root / "cadb_dataset.py"), "exec"), globals())

load_begin = time.perf_counter()
cfg = config_module.Config()
model = samp_net.SAMPNet(cfg, pretrained=False)
state = torch.load(args.weights, map_location="cpu", weights_only=True)
model.load_state_dict(state, strict=True)
model.eval().to(args.device)
if args.device == "mps":
    torch.mps.synchronize()
load_seconds = time.perf_counter() - load_begin
captured = {}


def capture_fused(module, inputs):
    captured["feature"] = inputs[0]


def capture_penultimate(module, inputs, output):
    captured["penultimate_feature"] = output


model.com_pred_layer.register_forward_pre_hook(capture_fused)
model.com_pred_layer[4].register_forward_hook(capture_penultimate)
transform = transforms.Compose([
    transforms.Resize((cfg.image_size, cfg.image_size)),
    transforms.ToTensor(),
    transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
])
manifest_path = args.manifest.resolve()
manifest = json.loads(manifest_path.read_text())
samples = manifest["samples"]
records = []
for sample in samples:
    image_name = next(sample[key] for key in ("image_path", "path", "frame_path", "image")
                      if key in sample)
    image_path = Path(image_name)
    if not image_path.is_absolute():
        image_path = manifest_path.parent / image_path
    image = Image.open(image_path).convert("RGB")
    # Preloaded PIL image -> official preprocessing + saliency + forward + CPU output.
    measurements = []
    for repeat in range(args.repeat + 1):
        begin = time.perf_counter()
        image_tensor = transform(image).unsqueeze(0).to(args.device)
        saliency = detect_saliency(np.asarray(image).copy(), target_size=(224, 224))
        saliency_tensor = torch.from_numpy(saliency.astype(np.float32))[None, None].to(args.device)
        if args.device == "mps":
            torch.mps.synchronize()
        forward_begin = time.perf_counter()
        with torch.inference_mode():
            weight, attribute, distribution = model(image_tensor, saliency_tensor)
        if args.device == "mps":
            torch.mps.synchronize()
        forward_end = time.perf_counter()
        feature = captured["feature"][0].detach().cpu().numpy().astype(np.float32)
        penultimate = captured["penultimate_feature"][0].detach().cpu().numpy().astype(np.float32)
        distribution_values = distribution[0].detach().cpu().tolist()
        if repeat:
            measurements.append({"forward_ms": (forward_end-forward_begin)*1000,
                                 "pipeline_ms": (time.perf_counter()-begin)*1000})
    record = {
        "id": sample["id"], "image_path": str(image_path),
        "score_distribution_1_to_5": distribution_values,
        "mean_composition_quality": sum((i+1)*p for i, p in enumerate(distribution_values)),
        "feature": feature.tolist(),
        "penultimate_feature": penultimate.tolist(),
        "pattern_importance_weights": weight[0].softmax(0).detach().cpu().tolist(),
        "attribute_predictions": dict(zip(cfg.attribute_types, attribute[0].detach().cpu().tolist())),
        "measurements": measurements,
        "median_forward_ms": statistics.median(m["forward_ms"] for m in measurements),
        "median_pipeline_ms": statistics.median(m["pipeline_ms"] for m in measurements),
    }
    assert abs(sum(distribution_values)-1) < 1e-5
    assert np.isfinite(feature).all() and np.isfinite(penultimate).all()
    records.append(record)
    print(json.dumps({k: record[k] for k in ["id", "mean_composition_quality", "median_pipeline_ms"]}), flush=True)

result = {
    "model": "SAMP-Net official CADB checkpoint", "status": "completed",
    "source_repo": "https://github.com/bcmi/Image-Composition-Assessment-Dataset-CADB",
    "source_commit": "b8f80be379d4a4caf9d045db3187cb60bcaca583",
    "weights_path": str(args.weights.resolve()),
    "weights_bytes": args.weights.stat().st_size,
    "weights_sha256": hashlib.sha256(args.weights.read_bytes()).hexdigest(),
    "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    "parameter_count": sum(p.numel() for p in model.parameters()),
    "device": args.device, "dtype": "float32", "threads": args.threads,
    "python": platform.python_version(),
    "versions": {name: importlib.metadata.version(name) for name in ["torch", "torchvision", "numpy", "Pillow", "einops"]},
    "opencv_version": cv2.__version__, "model_load_seconds": load_seconds,
    "preprocessing": "Official 224x224 Resize + ImageNet normalization; official spectral-residual saliency computed from original RGB image.",
    "compatibility": ["Ignore only Config training dataset assertion in memory.",
                      "Construct the identical ResNet-18 backbone with torchvision weights=None; checkpoint strict load."],
    "feature_definition": "1024-D input of com_pred_layer, after attentional attribute/composition feature fusion; L2 normalize for cosine retrieval.",
    "penultimate_feature_definition": "512-D output of com_pred_layer[4] ReLU, immediately before the final 5-score linear layer.",
    "pattern_warning": "Eight pattern weights describe importance for composition quality; they are not composition class probabilities.",
    "timing_scope": "Per-image one warmup discarded + repeated runs; preloaded PIL RGB -> preprocessing/saliency/transfers/forward/feature CPU copy; excludes file IO/model loading.",
    "median_pipeline_ms": statistics.median(r["median_pipeline_ms"] for r in records),
    "records": records,
}
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n")
