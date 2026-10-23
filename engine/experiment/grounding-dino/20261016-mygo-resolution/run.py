"""Compare source resolutions using the existing Grounding DINO inference harness."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from PIL import Image

parser = argparse.ArgumentParser()
parser.add_argument("--model-dir", required=True)
parser.add_argument("--device", choices=["mps", "cpu"], default="mps")
parser.add_argument("--prepare-only", action="store_true")
args = parser.parse_args()
root = Path(__file__).resolve().parent
previous = root.parent / "20261016-mygo-local"
source_manifest = json.loads((previous / "manifest.json").read_text())
manifest = {
    "source_manifest": "../20261016-mygo-local/manifest.json",
    "source_manifest_sha256": hashlib.sha256((previous / "manifest.json").read_bytes()).hexdigest(),
    "purpose": "Resolution stability; 1080p predictions are references, not ground-truth boxes.",
    "resize": "Pillow LANCZOS, width=round(source_width*height/source_height); no cropping or extra lossy encoding.",
    "format": "RGB PNG; 1080p pixels equal the decoded original JPEG pixels.",
    "resolutions_p": [360, 480, 720, 1080],
    "annotation_method": "Prior visible-person expectations were recorded by the assistant, not human experts.",
    "order": "Resolution order rotated across frames; existing harness repeats this interleaved order three times.",
    "samples": [],
}
(root / "images").mkdir(exist_ok=True)
for index, source in enumerate(source_manifest["samples"]):
    path = previous / source["frame_path"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == source["sha256"]
    image = Image.open(path).convert("RGB")
    heights = [1080, 360, 480, 720]
    heights = heights[index % 4:] + heights[:index % 4]
    for height in heights:
        width = round(image.width * height / image.height)
        variant = image if image.size == (width, height) else image.resize((width, height), Image.Resampling.LANCZOS)
        identifier = f"{source['id']}_{height}p"
        target = root / "images" / f"{identifier}.png"
        variant.save(target)
        manifest["samples"].append({
            "id": identifier, "source_id": source["id"], "resolution_p": height,
            "timestamp_hms": source["timestamp_hms"], "timestamp_seconds": source["timestamp_seconds"],
            "scenario": source["scenario"], "expected_visible_person_count": source["manual_visible_person_count"],
            "frame_path": str(target.relative_to(root)), "width": width, "height": height,
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "source_sha256": source["sha256"],
        })
manifest_path = root / "manifest.json"
manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
print(json.dumps({"event": "prepared", "samples": len(manifest["samples"])}), flush=True)
if not args.prepare_only:
    env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
               PYTORCH_ENABLE_MPS_FALLBACK="0", TOKENIZERS_PARALLELISM="false")
    command = [sys.executable, str(previous / "run.py"), "--model-dir", args.model_dir,
               "--manifest", str(manifest_path), "--output", str(root / args.device),
               "--device", args.device, "--repeat", "3", "--warmup", "2"]
    with (root / f"{args.device}-run.log").open("w") as log:
        completed = subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT)
    if completed.returncode:
        raise RuntimeError(f"Inference failed with {completed.returncode}; see {root / (args.device + '-run.log')}")
    print(json.dumps({"event": "finished", "results": str(root / args.device / "results.json")}), flush=True)
