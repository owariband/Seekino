"""Run the original nine-class photographic composition Caffe checkpoint via OpenCV."""

import argparse
import hashlib
import json
from pathlib import Path
import platform
import re
import statistics
import time

import cv2
import numpy as np

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--manifest", required=True, type=Path)
parser.add_argument("--cache", required=True, type=Path)
parser.add_argument("--output", type=Path, default=Path(__file__).parent / "results.json")
parser.add_argument("--repeat", type=int, default=3)
parser.add_argument("--threads", type=int, default=6)
args = parser.parse_args()
cv2.setNumThreads(args.threads)

# Preserve trained convolution/linear/nonlinearity/softmax layers. Replace only
# training/evaluation data and label-dependent layers with one explicit input.
original = (args.cache / "finetune_iter_10000.prototxt").read_text()
layers = []
position = 0
while match := re.search(r"\blayer\s*\{", original[position:]):
    begin = position + match.start()
    end = position + match.end()
    depth = 1
    while depth:
        depth += (original[end] == "{") - (original[end] == "}")
        end += 1
    block = original[begin:end]
    name = re.search(r'name:\s*"([^"]+)"', block)[1]
    if name not in {"data", "ftacc", "ftloss"}:
        layers.append(block)
    position = end
deploy_text = ('name: "PCP_CaffeNet_inference"\ninput: "data"\n'
               'input_shape { dim: 1 dim: 3 dim: 227 dim: 227 }\n' + "\n".join(layers) + "\n")
deploy_path = args.cache / "deploy_opencv.prototxt"
deploy_path.write_text(deploy_text)
weights_path = args.cache / "caffenet_finetune_iter_10000.caffemodel"
load_begin = time.perf_counter()
net = cv2.dnn.readNetFromCaffe(str(deploy_path), str(weights_path))
net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
load_seconds = time.perf_counter() - load_begin
class_names = ["rule_of_thirds", "vertical", "horizontal", "diagonal", "curved",
               "triangle", "center", "symmetric", "pattern"]
manifest_path = args.manifest.resolve()
manifest = json.loads(manifest_path.read_text())
records = []
for sample in manifest["samples"]:
    image_path = Path(sample["image_path"])
    if not image_path.is_absolute():
        image_path = manifest_path.parent / image_path
    image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"Unreadable image {image_path}")
    measurements = []
    for repeat in range(args.repeat+1):
        begin = time.perf_counter()
        # Official Main.cpp -> ConvNet.cpp -> TEST MemoryData transform:
        # BGR uint8 values, resized 227x227, default scale=1, no mean subtraction.
        blob = cv2.dnn.blobFromImage(image, scalefactor=1.0, size=(227, 227),
                                    mean=(0, 0, 0), swapRB=False, crop=False)
        net.setInput(blob)
        forward_begin = time.perf_counter()
        distribution, feature = net.forward(["ftsoftmax", "relu7"])
        forward_end = time.perf_counter()
        distribution = distribution.reshape(-1).copy()
        feature = feature.reshape(-1).copy()
        if repeat:
            measurements.append({"forward_ms": (forward_end-forward_begin)*1000,
                                 "pipeline_ms": (time.perf_counter()-begin)*1000})
    assert distribution.shape == (9,) and feature.shape == (4096,)
    assert abs(float(distribution.sum())-1) < 1e-5
    assert np.isfinite(distribution).all() and np.isfinite(feature).all()
    record = {
        "id": sample["id"], "image_path": str(image_path),
        "class_probabilities": dict(zip(class_names, map(float, distribution))),
        "top_class": class_names[int(distribution.argmax())],
        "feature": feature.tolist(), "measurements": measurements,
        "median_forward_ms": statistics.median(m["forward_ms"] for m in measurements),
        "median_pipeline_ms": statistics.median(m["pipeline_ms"] for m in measurements),
    }
    records.append(record)
    print(json.dumps({"id": record["id"], "top_class": record["top_class"],
                      "top_probability": float(distribution.max()),
                      "pipeline_ms": record["median_pipeline_ms"]}), flush=True)
result = {
    "model": "PCP photographic composition FT_CNN CaffeNet", "status": "completed",
    "source_page": "https://mcl.korea.ac.kr/research/Submitted/jtlee_JVCIR2018/index.html",
    "source_zip": "https://mcl.korea.ac.kr/research/Submitted/jtlee_JVCIR2018/code.zip",
    "source_version": "Official unversioned ZIP; per-file SHA256 in source.json",
    "weights_path": str(weights_path.resolve()), "weights_bytes": weights_path.stat().st_size,
    "weights_sha256": hashlib.sha256(weights_path.read_bytes()).hexdigest(),
    "deploy_sha256": hashlib.sha256(deploy_path.read_bytes()).hexdigest(),
    "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    "runtime": "OpenCV DNN CPU", "opencv_version": cv2.__version__,
    "numpy_version": np.__version__, "python": platform.python_version(),
    "dtype": "float32", "threads": args.threads, "model_load_seconds": load_seconds,
    "class_order": class_names,
    "preprocessing": "Official C++: BGR, resize227x227 INTER_LINEAR, float32 scale1, zero mean, no RGB swap, no crop.",
    "feature_definition": "4096-D fc7 after in-place relu7 (drop7 is inference identity); cosine retrieval after L2 normalization.",
    "compatibility": "Only Data/MemoryData/Accuracy/SoftmaxWithLoss layers removed from prototxt; explicit data input added. Learned layers and softmax unchanged.",
    "probability_warning": "Nine-way softmax from the released checkpoint; no calibrated multilabel or anime-domain confidence claim.",
    "timing_scope": "Per-image one warmup discarded + repeated runs; preloaded BGR -> blob/forward/output copies; excludes JPEG IO and model loading.",
    "median_pipeline_ms": statistics.median(r["median_pipeline_ms"] for r in records),
    "records": records,
}
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n")
