"""Fixed-audio CPU experiment using the deployed official ERes2NetV2 implementation."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import platform
import resource
import subprocess
import sys
import time
import traceback
import urllib.request
import wave

HERE = Path(__file__).resolve().parent
EXPERIMENT = HERE.parent
ROOT = HERE.parents[4]
MODEL = "iic/speech_eres2netv2_sv_zh-cn_16k-common"
REVISION = "v1.0.1"
MODEL_DIR = ROOT / "outputs/local-models/eres2netv2"
CHECKPOINT = MODEL_DIR / "pretrained_eres2netv2.ckpt"
UPSTREAM = ROOT / "outputs/local-models/campplus/3D-Speaker"


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def download():
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    api = f"https://modelscope.cn/api/v1/models/{MODEL}/repo/files?Revision={REVISION}&Recursive=true"
    with urllib.request.urlopen(api, timeout=60) as response:
        metadata = json.load(response)
    record = next(row for row in metadata["Data"]["Files"] if row["Path"] == CHECKPOINT.name)
    url = f"https://modelscope.cn/models/{MODEL}/resolve/{REVISION}/{CHECKPOINT.name}"
    if not CHECKPOINT.exists():
        partial = CHECKPOINT.with_suffix(".ckpt.part")
        print(json.dumps({"event": "download_started", "url": url, "bytes": record["Size"]}), flush=True)
        with urllib.request.urlopen(url, timeout=120) as response, partial.open("wb") as target:
            while block := response.read(1024 * 1024):
                target.write(block)
        if partial.stat().st_size != record["Size"] or sha(partial) != record["Sha256"]:
            raise ValueError("Downloaded checkpoint differs from official revision metadata")
        partial.rename(CHECKPOINT)
    assert CHECKPOINT.stat().st_size == record["Size"] and sha(CHECKPOINT) == record["Sha256"]
    provenance = {"model": MODEL, "revision": REVISION, "metadata_url": api,
                  "checkpoint_url": url, "checkpoint": str(CHECKPOINT.relative_to(ROOT)),
                  "checkpoint_bytes": CHECKPOINT.stat().st_size, "checkpoint_sha256": sha(CHECKPOINT),
                  "official_file_record": record, "verified_at": datetime.now(UTC).isoformat()}
    write(MODEL_DIR / "download.json", provenance)
    print(json.dumps({"event": "checkpoint_verified", "bytes": provenance["checkpoint_bytes"],
                      "sha256": provenance["checkpoint_sha256"]}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download-only", action="store_true")
    args = parser.parse_args()
    if args.download_only:
        download()
        return
    provenance = json.loads((MODEL_DIR / "download.json").read_text())
    assert sha(CHECKPOINT) == provenance["checkpoint_sha256"]
    manifest_path = EXPERIMENT / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    samples = manifest["samples"]
    assert len(samples) == 47
    sys.path.insert(0, str(UPSTREAM))
    started = time.perf_counter()
    import numpy as np
    import torch
    from speakerlab.models.eres2net.ERes2NetV2 import ERes2NetV2
    from speakerlab.process.processor import FBank
    import_seconds = time.perf_counter() - started
    torch.set_num_threads(4)
    torch.set_num_interop_threads(1)
    torch.manual_seed(0)
    started = time.perf_counter()
    model = ERes2NetV2(feat_dim=80, embedding_size=192, baseWidth=26, scale=2, expansion=2).cpu().eval()
    model.load_state_dict(torch.load(CHECKPOINT, map_location="cpu", weights_only=True), strict=True)
    load_seconds = time.perf_counter() - started
    fbank = FBank(80, sample_rate=16000, mean_nor=True)
    records, warmup_ms = [], None
    with torch.inference_mode():
        for sample in samples:
            with wave.open(str(EXPERIMENT / sample["audio"]), "rb") as audio:
                assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (16000, 1, 2)
                frames = audio.getnframes()
                pcm = audio.readframes(frames)
            assert hashlib.sha256(pcm).hexdigest() == sample["pcm_sha256"]
            waveform = torch.from_numpy(np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768).unsqueeze(0)
            if warmup_ms is None:
                started = time.perf_counter()
                model(fbank(waveform).unsqueeze(0))
                warmup_ms = (time.perf_counter() - started) * 1000
            started = time.perf_counter()
            features = fbank(waveform).unsqueeze(0)
            vector = model(features).squeeze(0).numpy().astype(float).tolist()
            elapsed_ms = (time.perf_counter() - started) * 1000
            norm = math.hypot(*vector)
            assert len(vector) == 192 and all(math.isfinite(x) for x in vector) and norm > 0
            records.append({"id": sample["id"], "vector": [x / norm for x in vector],
                            "raw_norm": norm, "dimension": len(vector), "duration_ms": frames / 16,
                            "pcm_sha256": sample["pcm_sha256"], "embedding_ms": elapsed_ms,
                            "feature_shape": list(features.shape)})
            write(HERE / "embeddings.json", records)
            print(json.dumps({"id": sample["id"], "embedding_ms": elapsed_ms}), flush=True)
    files = ["speakerlab/models/eres2net/ERes2NetV2.py", "speakerlab/models/eres2net/fusion.py",
             "speakerlab/models/eres2net/pooling_layers.py", "speakerlab/process/processor.py"]
    provenance.update({"source_repository": "https://github.com/modelscope/3D-Speaker",
                       "source_commit": subprocess.check_output(["git", "-C", str(UPSTREAM), "rev-parse", "HEAD"], text=True).strip(),
                       "source_sha256": {name: sha(UPSTREAM / name) for name in files},
                       "manifest_sha256": sha(manifest_path), "runner_sha256": sha(Path(__file__)),
                       "python": sys.version, "platform": platform.platform(),
                       "versions": {name: importlib.metadata.version(name) for name in ("torch", "torchaudio", "numpy", "scipy")},
                       "device": "cpu", "dtype": "float32", "threads": 4, "interop_threads": 1,
                       "parameter_count": sum(p.numel() for p in model.parameters()),
                       "feature_extractor": "Official FBank80, 16kHz, mean_nor=True, dither=0",
                       "import_seconds": import_seconds, "model_load_seconds": load_seconds,
                       "warmup_ms": warmup_ms, "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                       "completed_at": datetime.now(UTC).isoformat(), "sample_count": len(records)})
    write(HERE / "provenance.json", provenance)
    write(HERE / "status.json", {"status": "complete", "sample_count": len(records)})


if __name__ == "__main__":
    try:
        main()
    except BaseException as error:
        write(HERE / "status.json", {"status": "failed", "error_type": type(error).__name__,
                                    "message": str(error), "traceback": traceback.format_exc()})
        raise
