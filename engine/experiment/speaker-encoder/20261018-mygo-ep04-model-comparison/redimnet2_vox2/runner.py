"""Run pinned official ReDimNet2 checkpoints on the frozen shared manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import platform
import resource
import statistics
import sys
import time
import traceback
import wave
from datetime import datetime, timezone


EXPERIMENT = pathlib.Path(__file__).resolve().parent.parent
PROJECT = EXPERIMENT.parents[3]
MODELS = PROJECT / "outputs/local-models/redimnet2"


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", choices=["vox2", "mixed"], required=True)
    args = parser.parse_args()
    output = EXPERIMENT / f"redimnet2_{args.variant}"
    output.mkdir(parents=True, exist_ok=True)
    if (output / "embeddings.json").exists():
        raise RuntimeError("Existing embeddings are preserved; choose a fresh experiment for rerun")
    started = time.perf_counter()
    status = {"state": "running", "variant": args.variant, "completed": 0,
              "started_at": datetime.now(timezone.utc).isoformat()}
    write_json(output / "status.json", status)
    provenance = {}
    try:
        download = json.loads((MODELS / "download-provenance.json").read_text())
        filename = "b6-vox2-lm.pt" if args.variant == "vox2" else "b6-vb2+vox2+cnc2_v0-lm.pt"
        record = next(row for row in download["checkpoints"] if row["name"] == filename)
        checkpoint = MODELS / filename
        assert checkpoint.stat().st_size == record["size"]
        assert hashlib.sha256(checkpoint.read_bytes()).hexdigest() == record["sha256"]
        manifest_path = EXPERIMENT / "manifest.json"
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
        samples = manifest["samples"]
        os.environ["TORCH_HOME"] = str(MODELS / "torch-cache")
        os.environ["HF_HOME"] = str(MODELS / "hf-cache")
        os.environ["XDG_CACHE_HOME"] = str(MODELS / "xdg-cache")
        os.environ["OMP_NUM_THREADS"] = "4"
        os.environ["MKL_NUM_THREADS"] = "4"
        import_started = time.perf_counter()
        import numpy as np
        import scipy
        import torch
        import torchaudio

        torch.set_num_threads(4)
        torch.set_num_interop_threads(1)
        torch.manual_seed(0)
        sys.path.insert(0, download["source_dir"])
        from redimnet2.redimnet2 import ReDimNet2Wrap

        import_ms = (time.perf_counter() - import_started) * 1000
        provenance = {
            "model": "ReDimNet2-B6", "variant": args.variant, "dataset": "vox2" if args.variant == "vox2" else "vb2+vox2+cnc2_v0",
            "train_type": "lm", "official_repository": "https://github.com/PalabraAI/redimnet2",
            "source_commit": download["source_commit"], "source_url": download["source_url"],
            "source_archive_sha256": download["source_archive_sha256"],
            "checkpoint": record, "device": "cpu", "dtype": "float32", "batch_size": 1,
            "threads": torch.get_num_threads(), "interop_threads": torch.get_num_interop_threads(),
            "inference_mode": True, "seed": 0, "external_preprocessing": "none; PCM16 mono 16k scaled by 32768, official model includes features/normalization",
            "loading": "official ReDimNet2Wrap(**checkpoint.model_config), local torch.load(map_location=cpu, weights_only=True), load_state_dict(strict=True)",
            "versions": {"python": sys.version, "torch": torch.__version__, "torchaudio": torchaudio.__version__, "numpy": np.__version__, "scipy": scipy.__version__},
            "platform": platform.platform(), "machine": platform.machine(), "dependency_and_source_import_ms": import_ms,
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(), "samples": len(samples),
            "timing": "embedding_ms includes official waveform frontend, network and L2 normalization; excludes WAV decoding and input tensor construction; no warmup, first sample is cold",
        }
        load_started = time.perf_counter()
        payload = torch.load(checkpoint, map_location="cpu", weights_only=True)
        model = ReDimNet2Wrap(**payload["model_config"])
        loaded = model.load_state_dict(payload["state_dict"], strict=True)
        assert not loaded.missing_keys and not loaded.unexpected_keys
        model = model.to(device="cpu", dtype=torch.float32).eval()
        provenance.update({"model_config": payload["model_config"],
                           "parameter_count": sum(param.numel() for param in model.parameters()),
                           "checkpoint_and_model_load_ms": (time.perf_counter() - load_started) * 1000,
                           "strict_state_dict": True})
        del payload
        write_json(output / "provenance.json", provenance)
        embeddings = []
        with torch.inference_mode():
            for index, sample in enumerate(samples):
                audio_path = (EXPERIMENT / sample["audio"]).resolve()
                if not audio_path.is_relative_to(EXPERIMENT.resolve()):
                    raise ValueError("Audio path escapes frozen experiment")
                with wave.open(str(audio_path), "rb") as wav:
                    assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16000, 1, 2)
                    frames = wav.getnframes()
                    pcm = wav.readframes(frames)
                pcm_sha = hashlib.sha256(pcm).hexdigest()
                assert pcm_sha == sample["pcm_sha256"], sample["id"]
                waveform = torch.from_numpy(np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0).unsqueeze(0)
                embed_started = time.perf_counter()
                vector = model(waveform)
                if vector.shape != (1, 192) or not torch.isfinite(vector).all():
                    raise ValueError(f"Invalid embedding {sample['id']} shape={vector.shape}")
                norm = vector.norm(dim=1, keepdim=True)
                if norm.item() <= 0:
                    raise ValueError("Zero embedding")
                vector = vector / norm
                embedding_ms = (time.perf_counter() - embed_started) * 1000
                embeddings.append({"id": sample["id"], "vector": vector[0].tolist(),
                                   "pcm_sha256": pcm_sha, "duration_ms": frames / 16,
                                   "embedding_ms": embedding_ms, "dimension": 192})
                write_json(output / "embeddings.json", embeddings)
                status.update({"completed": index + 1, "total": len(samples), "last_id": sample["id"]})
                write_json(output / "status.json", status)
                print(json.dumps({"index": index + 1, "id": sample["id"], "duration_ms": frames / 16,
                                  "embedding_ms": round(embedding_ms, 3)}, ensure_ascii=False), flush=True)
        times = [row["embedding_ms"] for row in embeddings]
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak_rss_bytes = int(rss if sys.platform == "darwin" else rss * 1024)
        provenance.update({"peak_rss_bytes": peak_rss_bytes, "process_elapsed_ms": (time.perf_counter() - started) * 1000,
                           "embedding_total_ms": sum(times), "embedding_first_ms": times[0],
                           "embedding_warm_median_ms": statistics.median(times[1:]),
                           "embedding_warm_max_ms": max(times[1:]), "dimension": 192})
        write_json(output / "provenance.json", provenance)
        status.update({"state": "complete", "ended_at": datetime.now(timezone.utc).isoformat(), "peak_rss_bytes": peak_rss_bytes})
        write_json(output / "status.json", status)
        print(json.dumps({"state": "complete", "samples": len(embeddings), "embedding_total_ms": sum(times),
                          "warm_median_ms": statistics.median(times[1:]), "peak_rss_bytes": peak_rss_bytes}), flush=True)
    except Exception as exc:
        status.update({"state": "failed", "error": repr(exc), "ended_at": datetime.now(timezone.utc).isoformat()})
        write_json(output / "status.json", status)
        if provenance:
            write_json(output / "provenance.json", provenance)
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
