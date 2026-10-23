"""Download only the fixed checkpoints used in this experiment."""
import hashlib
import json
import os
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
CACHE = ROOT.parents[3] / ".cache/composition-models"
os.environ["HF_HOME"] = str(CACHE / "huggingface")
os.environ["HF_HUB_DISABLE_XET"] = "1"
os.environ["TORCH_HOME"] = str(CACHE / "torch")
from clip.clip import _MODELS
from huggingface_hub import snapshot_download
from pyiqa.archs.musiq_arch import default_model_urls

records = {}
for name, url, target in [
    ("clip", _MODELS["ViT-B/32"], CACHE / "clip/ViT-B-32.pt"),
    ("musiq_ava", default_model_urls["ava"], CACHE / "musiq/musiq_ava.pth"),
]:
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        temporary = target.with_suffix(".partial")
        with urlopen(url, timeout=120) as response, temporary.open("wb") as output:
            while block := response.read(1024 * 1024):
                output.write(block)
        temporary.rename(target)
    records[name] = {"path": str(target), "url": url,
                     "bytes": target.stat().st_size,
                     "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}
    print(json.dumps({"event": "downloaded", "model": name, **records[name]}), flush=True)

for name, repo, revision, patterns in [
    ("dinov2", "facebook/dinov2-small", "ed25f3a31f01632728cabb09d1542f84ab7b0056",
     ["config.json", "preprocessor_config.json", "model.safetensors"]),
    ("grounding_dino", "IDEA-Research/grounding-dino-tiny", "a2bb814dd30d776dcf7e30523b00659f4f141c71",
     ["*.json", "tokenizer*", "vocab.txt", "model.safetensors"]),
]:
    path = Path(snapshot_download(repo, revision=revision, allow_patterns=patterns,
                                 cache_dir=str(CACHE / "huggingface/hub")))
    files = [{"name": p.name, "bytes": p.stat().st_size,
              "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
             for p in sorted(path.iterdir()) if p.is_file()]
    records[name] = {"repo": repo, "path": str(path), "revision": path.name,
                     "files": files, "bytes": sum(p["bytes"] for p in files)}
    print(json.dumps({"event": "downloaded", "model": name,
                      "bytes": records[name]["bytes"], "revision": path.name}), flush=True)

(ROOT / "models.json").write_text(json.dumps(records, indent=2) + "\n")
