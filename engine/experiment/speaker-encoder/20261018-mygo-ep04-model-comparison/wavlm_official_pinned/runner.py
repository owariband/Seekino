"""Run the official pinned s3prl WavLM + UniSpeech SV chain on frozen PCM."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gc
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import resource
import subprocess
import sys
import time
import traceback
import wave

HERE = Path(__file__).resolve().parent
EXPERIMENT = HERE.parent
ROOT = HERE.parents[4]
CACHE = ROOT / "outputs/local-models/wavlm-speaker-sv"
PINNED = CACHE / "official-pinned/s3prl"
UPSTREAM = CACHE / "UniSpeech"
SV = CACHE / "checkpoints/wavlm_large_finetune.pth"
INITIAL = CACHE / "checkpoints/wavlm_large_backbone.pt"
COMMIT = "7ab62aaf2606d83da6c71ee74e7d16e0979edbc3"
WEIGHTS = {
    SV: "51f07e3b94d9e0262a6a675ef5a087be3dd09e8c62e9d886827f44f82fe7f94b",
    INITIAL: "6fb4b3c3e6aa567f0a997b30855859cb81528ee8078802af439f7b2da0bf100f",
}


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else _sha_stream(stream)


def _sha_stream(stream):
    digest = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(block)
    return digest.hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def main(output):
    assert not (output / "embeddings.json").exists(), "Keep prior attempts; select a fresh output directory"
    write(output / "status.json", {"status": "loading", "started_at": datetime.now(timezone.utc).isoformat()})
    commit = subprocess.check_output(["git", "-C", str(PINNED), "rev-parse", "HEAD"], text=True).strip()
    assert commit == COMMIT
    for path, expected in WEIGHTS.items():
        assert sha(path) == expected, path
    manifest_path = EXPERIMENT / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    assert len(manifest["samples"]) == 47
    os.environ["TORCH_HOME"] = str(CACHE / "official-pinned/cache/torch")
    started = time.perf_counter()
    import numpy as np
    import soundfile as sf
    import torch
    from torchaudio.transforms import Resample
    import_seconds = time.perf_counter() - started
    torch.set_num_threads(4)
    torch.set_num_interop_threads(1)
    torch.manual_seed(0)
    sys.path.insert(0, str(PINNED))
    sys.path.insert(0, str(UPSTREAM / "downstreams/speaker_verification"))
    real_hub_load = torch.hub.load
    hub_calls = []

    def pinned_hub_load(repo, entry, *args, **kwargs):
        # Only bind source/cache paths. Official wavlm_local, UpstreamExpert,
        # UpstreamBase hooks and ECAPA execute unchanged, including their forwards.
        assert repo == "s3prl/s3prl" and entry == "wavlm_large" and not args and not kwargs
        hub_calls.append({"requested_repo": repo, "requested_entry": entry,
                          "actual_entry": "wavlm_local", "checkpoint": str(INITIAL.relative_to(ROOT))})
        return real_hub_load(str(HERE), "wavlm_local", ckpt=str(INITIAL), source="local")

    torch.hub.load = pinned_hub_load
    started = time.perf_counter()
    try:
        from verification import init_model
        model = init_model("wavlm_large", checkpoint=str(SV))
    finally:
        torch.hub.load = real_hub_load
    assert len(hub_calls) == 1
    import s3prl.upstream.wavlm.expert as expert
    import s3prl.upstream.interfaces as interfaces
    import s3prl.upstream.wavlm.WavLM as wavlm_module
    for module in (expert, interfaces, wavlm_module):
        assert Path(module.__file__).resolve().is_relative_to(PINNED.resolve())
    assert isinstance(model.feature_extract, expert.UpstreamExpert)
    assert isinstance(model.feature_extract, interfaces.UpstreamBase)
    assert isinstance(model.feature_extract.model, wavlm_module.WavLM)
    assert model.feat_num == 25 and len(model.feature_extract.model.encoder.layers) == 24
    # The official init_model uses strict=False. Audit every embedding parameter,
    # allowing only the observed training classifier, then load strict=True.
    payload = torch.load(str(SV), map_location="cpu")
    state = payload["model"]
    missing = sorted(set(model.state_dict()) - set(state))
    extra = sorted(set(state) - set(model.state_dict()))
    assert not missing and extra == ["loss_calculator.projection.weight"], (missing, extra)
    classifier_shape = list(state.pop(extra[0]).shape)
    model.load_state_dict(state, strict=True)
    del state, payload
    gc.collect()
    model.cpu().float().eval()
    assert all(parameter.device.type == "cpu" and parameter.dtype == torch.float32 for parameter in model.parameters())
    assert all(not module.training for module in model.modules())
    load_seconds = time.perf_counter() - started
    source_paths = [UPSTREAM / "downstreams/speaker_verification" / filename for filename in
                    ("verification.py", "models/ecapa_tdnn.py", "models/utils.py")]
    source_paths += [PINNED / "s3prl/upstream" / filename for filename in
                     ("interfaces.py", "wavlm/expert.py", "wavlm/WavLM.py", "wavlm/modules.py", "wavlm/hubconf.py")]
    provenance = {
        "model": "WavLM-Large finetuned SV with official pinned s3prl and official ECAPA",
        "official_instructions": "https://github.com/microsoft/UniSpeech/tree/main/downstreams/speaker_verification",
        "s3prl_commit": commit,
        "unispeech_commit": subprocess.check_output(["git", "-C", str(UPSTREAM), "rev-parse", "HEAD"], text=True).strip(),
        "source_sha256": {str(path.relative_to(ROOT)): sha(path) for path in source_paths},
        "runner_sha256": sha(Path(__file__)), "hub_router_sha256": sha(HERE / "hubconf.py"),
        "hub_calls": hub_calls, "model_entry": "unchanged official verification.init_model(wavlm_large)",
        "entry_routing": "Select official wavlm_local with existing local checkpoint; avoid unpinned torch.hub repository and expired remote initialization URL. No neural, frontend or hook code is replaced.",
        "manifest_sha256": sha(manifest_path),
        "checkpoint_sha256": {str(path.relative_to(ROOT)): expected for path, expected in WEIGHTS.items()},
        "checkpoint_identity_limitation": "Same third-party-mirrored SV bytes as prior experiment; cannot directly verify against inaccessible Microsoft Drive original.",
        "state_dict": {"strict": True, "keys": len(model.state_dict()), "missing": missing,
                       "unexpected_after_exclusion": [], "excluded_training_only": {extra[0]: classifier_shape}},
        "python": sys.version, "python_executable": sys.executable,
        "platform": platform.platform(), "machine": platform.machine(), "device": "cpu", "dtype": "float32",
        "actual_feature_modules": {module.__name__: str(Path(module.__file__).resolve().relative_to(ROOT))
                                   for module in (expert, interfaces, wavlm_module)},
        "threads": 4, "interop_threads": 1, "batch_size": 1, "parameter_count": sum(p.numel() for p in model.parameters()),
        "versions": {name: importlib.metadata.version(name) for name in
                     ("torch", "torchaudio", "numpy", "scipy", "s3prl", "fairseq", "soundfile", "fire", "sentencepiece", "sklearn", "scikit-learn", "omegaconf")},
        "import_seconds": import_seconds, "model_load_seconds": load_seconds,
    }
    write(output / "provenance.json", provenance)
    records, warmup_ms = [], None
    with torch.no_grad():
        for sample in manifest["samples"]:
            audio_path = EXPERIMENT / sample["audio"]
            with wave.open(str(audio_path), "rb") as audio:
                pcm = audio.readframes(audio.getnframes())
            assert hashlib.sha256(pcm).hexdigest() == sample["pcm_sha256"]
            # Same soundfile -> float tensor -> Resample flow as verification.py.
            values, sample_rate = sf.read(str(audio_path))
            assert sample_rate == 16000 and values.ndim == 1
            waveform = Resample(orig_freq=sample_rate, new_freq=16000)(torch.from_numpy(values).unsqueeze(0).float())
            assert np.array_equal(waveform.numpy().ravel(), np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768)
            if warmup_ms is None:
                started = time.perf_counter()
                model(waveform)
                warmup_ms = (time.perf_counter() - started) * 1000
            started = time.perf_counter()
            vector = model(waveform).squeeze(0).numpy().astype(float).tolist()
            elapsed_ms = (time.perf_counter() - started) * 1000
            norm = math.sqrt(math.fsum(value * value for value in vector))
            assert len(vector) == 256 and all(math.isfinite(value) for value in vector) and norm > 0
            records.append({"id": sample["id"], "vector": [value / norm for value in vector], "raw_norm": norm,
                            "dimension": 256, "pcm_sha256": sample["pcm_sha256"],
                            "duration_ms": sample["duration_ms"], "embedding_ms": elapsed_ms})
            write(output / "embeddings.json", records)
            write(output / "status.json", {"status": "running", "completed": len(records), "total": 47})
            print(json.dumps({"id": sample["id"], "embedding_ms": elapsed_ms}), flush=True)
    provenance.update({"sample_count": len(records), "warmup_ms": warmup_ms,
                       "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                       "completed_at": datetime.now(timezone.utc).isoformat()})
    write(output / "provenance.json", provenance)
    write(output / "status.json", {"status": "complete", "sample_count": len(records)})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=HERE / "run1")
    output = parser.parse_args().output.resolve()
    assert output.is_relative_to(HERE.resolve())
    output.mkdir(parents=True, exist_ok=True)
    try:
        main(output)
    except BaseException as error:
        write(output / "status.json", {"status": "failed", "error_type": type(error).__name__,
                                      "message": str(error), "traceback": traceback.format_exc()})
        raise
