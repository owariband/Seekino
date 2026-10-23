"""Fixed EP04 CAM++ CPU experiment; writes only into a fresh output directory."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import resource
import statistics
import subprocess
import sys
import tempfile
import time
import wave


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads((root / "manifest.json").read_text())
    repo = root.parents[3]
    video = repo / manifest["video_path"]
    assert digest(video) == manifest["video_sha256"]
    assert digest(args.checkpoint) == manifest["checkpoint_sha256"]
    commit = subprocess.check_output(["git", "-C", str(args.upstream), "rev-parse", "HEAD"], text=True).strip()
    assert commit == manifest["upstream_commit"]
    assert not subprocess.check_output(["git", "-C", str(args.upstream), "status", "--porcelain"])
    samples = manifest["samples"]
    assert len({s["id"] for s in samples}) == len(samples)
    for s in samples:
        assert 0 <= s["start_ms"] < s["end_ms"]
        source = repo / s["source_snapshot"]
        assert digest(source) == s["source_sha256"]
        original = json.loads(source.read_text())
        assert original["task_id"] == manifest["task_id"]
        assert original["chunk_id"] == s["chunk_id"] and original["version"] == s["content_version"]
        assert original["content"]["timeline"][s["timeline_index"]] == s["timeline_event"]
    args.output.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(args.upstream))
    started = time.perf_counter()
    import numpy as np
    import torch
    from speakerlab.models.campplus.DTDNN import CAMPPlus
    from speakerlab.process.processor import FBank
    import_seconds = time.perf_counter() - started
    torch.set_num_threads(manifest["protocol"]["threads"])
    torch.set_num_interop_threads(1)
    torch.manual_seed(0)
    started = time.perf_counter()
    model = CAMPPlus(feat_dim=80, embedding_size=192).cpu().eval()
    model.load_state_dict(torch.load(args.checkpoint, map_location="cpu", weights_only=True), strict=True)
    load_seconds = time.perf_counter() - started
    fbank = FBank(80, sample_rate=16000, mean_nor=True)
    records, embeddings = [], []
    with tempfile.TemporaryDirectory(prefix="anivag-campplus-audio-") as temp:
        temporary = Path(temp)
        for s in samples:
            start = time.perf_counter()
            wav_path = temporary / f"{s['id']}.wav"
            subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", str(s["start_ms"] / 1000),
                            "-i", str(video), "-t", str((s["end_ms"] - s["start_ms"]) / 1000),
                            "-map", "0:a:0", "-af", "pan=mono|c0=c0", "-ar", "16000", "-c:a", "pcm_s16le", str(wav_path)],
                           check=True, timeout=60, capture_output=True)
            decode_ms = (time.perf_counter() - start) * 1000
            with wave.open(str(wav_path), "rb") as audio:
                assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (1, 2, 16000)
                pcm = audio.readframes(audio.getnframes())
            x = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
            assert len(x) == (s["end_ms"] - s["start_ms"]) * 16
            waveform = torch.from_numpy(x).unsqueeze(0)
            timings, vectors, warmup = [], [], None
            for repeat in range(manifest["protocol"]["repeats"] + (s == samples[0])):
                start = time.perf_counter()
                with torch.inference_mode():
                    feat = fbank(waveform).unsqueeze(0)
                    vector = model(feat).squeeze(0).numpy().copy()
                elapsed = (time.perf_counter() - start) * 1000
                if s == samples[0] and repeat == 0:
                    warmup = elapsed
                    continue
                timings.append(elapsed)
                vectors.append(vector)
            assert vector.shape == (192,) and np.isfinite(vector).all() and np.linalg.norm(vector) > 0
            embeddings.append(vectors[0] / np.linalg.norm(vectors[0]))
            records.append({"id": s["id"], "duration_ms": len(x) / 16, "pcm_sha256": hashlib.sha256(pcm).hexdigest(),
                            "wav_sha256": digest(wav_path), "decode_ms": decode_ms, "embedding_ms": timings,
                            "warmup_ms": warmup, "rms_dbfs": float(20 * np.log10(max(np.sqrt(np.mean(x*x)), 1e-12))),
                            "peak": float(np.max(np.abs(x))), "feature_shape": list(feat.shape),
                            "repeat_max_abs_delta": float(max(np.max(np.abs(v - vectors[0])) for v in vectors))})
            wav_path.unlink()
            print(json.dumps({"id": s["id"], "embedding_median_ms": statistics.median(timings)}), flush=True)
    assert not temporary.exists()
    e = np.stack(embeddings)
    sim = e @ e.T
    refs = [i for i, s in enumerate(samples) if s["kind"] == "reference"]
    speakers = sorted({samples[i]["speaker"] for i in refs})

    def compare(index, allowed):
        target = samples[index]
        valid = [j for j in allowed if j != index and not
                 (samples[j]["start_ms"] < target["end_ms"] and target["start_ms"] < samples[j]["end_ms"])]
        candidates = []
        for speaker in speakers:
            selected = [j for j in valid if samples[j]["speaker"] == speaker]
            if not selected:
                continue
            centroid = e[selected].mean(axis=0)
            centroid /= np.linalg.norm(centroid)
            scores = sorted([{"reference": samples[j]["id"], "cosine": float(sim[index, j])} for j in selected], key=lambda x: -x["cosine"])
            candidates.append({"speaker": speaker, "centroid_cosine": float(e[index] @ centroid), "references": scores})
        candidates.sort(key=lambda x: -x["centroid_cosine"])
        return {"id": target["id"], "expected": target["speaker"], "prediction": candidates[0]["speaker"],
                "margin": candidates[0]["centroid_cosine"] - candidates[1]["centroid_cosine"], "candidates": candidates}

    evaluations = {}
    for count in (1, 2, 0):
        enrolled = refs if count == 0 else [j for speaker in speakers for j in [i for i in refs if samples[i]["speaker"] == speaker][:count]]
        probes = refs if count == 0 else [i for i in refs if i not in enrolled]
        rows = [compare(i, enrolled) for i in probes]
        evaluations[f"{count}_shot" if count else "leave_one_out"] = {"correct": sum(r["prediction"] == r["expected"] for r in rows), "total": len(rows), "probes": rows}
    pairs = [{"a": samples[i]["id"], "b": samples[j]["id"], "same_speaker": samples[i]["speaker"] == samples[j]["speaker"], "cosine": float(sim[i, j])}
             for i in refs for j in refs if i < j]
    assert len({r["pcm_sha256"] for r in records[:len(refs)]}) == len(refs)
    assert all(samples[i]["end_ms"] <= samples[j]["start_ms"] or samples[j]["end_ms"] <= samples[i]["start_ms"] for i in refs for j in refs if i < j)
    same = np.array([p["cosine"] for p in pairs if p["same_speaker"]])
    different = np.array([p["cosine"] for p in pairs if not p["same_speaker"]])
    auc = float(((same[:, None] > different).sum() + (same[:, None] == different).sum() / 2) / (same.size * different.size))
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "manifest_sha256": digest(root / "manifest.json"),
              "runner_sha256": digest(Path(__file__)), "upstream_commit": commit, "checkpoint_bytes": args.checkpoint.stat().st_size,
              "python": sys.version, "platform": platform.platform(), "versions": {p: importlib.metadata.version(p) for p in ("torch", "torchaudio", "numpy", "scipy", "pyyaml")},
              "device": "cpu", "dtype": "float32", "threads": torch.get_num_threads(), "interop_threads": torch.get_num_interop_threads(),
              "parameter_count": sum(p.numel() for p in model.parameters()), "import_seconds": import_seconds, "model_load_seconds": load_seconds,
              "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, "temporary_audio_cleaned": True,
              "records": records, "evaluations": evaluations, "pairwise_auc_exploratory": auc, "pairs": pairs,
              "targets": {policy: [compare(i, allowed) for i, s in enumerate(samples) if s["kind"] != "reference"]
                          for policy, allowed in (("all_references", refs), ("after_5_minutes", [i for i in refs if samples[i]["start_ms"] >= 300000]))}}
    np.savez_compressed(args.output / "embeddings.npz", ids=np.array([s["id"] for s in samples]), embeddings=e)
    (args.output / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    size, cell = 650, 21
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 {size} {size}"><rect width="100%" height="100%" fill="white"/><g font-family="monospace" font-size="9">']
    for i in refs:
        svg.extend([f'<text x="65" y="{104+i*cell}" text-anchor="end">{samples[i]["id"]}</text>', f'<text x="{83+i*cell}" y="85" transform="rotate(-60 {83+i*cell} 85)">{samples[i]["id"]}</text>'])
        for j in refs:
            intensity = int(255 * (1 - max(0, min(1, float(sim[i, j])))))
            svg.append(f'<rect x="{75+j*cell}" y="{90+i*cell}" width="20" height="20" fill="rgb({intensity},{intensity},255)"><title>{samples[i]["id"]}/{samples[j]["id"]}: {sim[i,j]:.4f}</title></rect>')
    svg.append('<text x="75" y="625">Cosine: white=0, blue=1. Diagonal excluded from evaluation.</text></g></svg>')
    (args.output / "similarity.svg").write_text("".join(svg))
    print(json.dumps({"completed": True, "auc": auc, "temporary_audio_cleaned": True}))


if __name__ == "__main__":
    main()
