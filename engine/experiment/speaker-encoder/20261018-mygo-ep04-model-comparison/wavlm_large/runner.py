"""CPU-only WavLM-Large SV experiment using official WavLM and ECAPA modules."""
from __future__ import annotations

from datetime import UTC, datetime
import gc
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
import types
import wave

HERE = Path(__file__).resolve().parent
EXPERIMENT = HERE.parent
ROOT = HERE.parents[4]
MODEL_DIR = ROOT / "outputs/local-models/wavlm-speaker-sv"
UPSTREAM = MODEL_DIR / "UniSpeech"
S3PRL = MODEL_DIR / "s3prl"
CHECKPOINT = MODEL_DIR / "checkpoints/wavlm_large_finetune.pth"
BACKBONE = MODEL_DIR / "checkpoints/wavlm_large_backbone.pt"
EXPECTED_BYTES = 1301926579
EXPECTED_SHA = "51f07e3b94d9e0262a6a675ef5a087be3dd09e8c62e9d886827f44f82fe7f94b"


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write(name, value):
    (HERE / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def main():
    write("status.json", {"status": "loading", "started_at": datetime.now(UTC).isoformat()})
    assert CHECKPOINT.stat().st_size == EXPECTED_BYTES
    assert sha(CHECKPOINT) == EXPECTED_SHA
    manifest_path = EXPERIMENT / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    assert len(manifest["samples"]) == 47
    started = time.perf_counter()
    import numpy as np
    import torch
    import torch.nn.functional as functional
    import torch.nn as nn
    import_seconds = time.perf_counter() - started
    torch.set_num_threads(4)
    torch.set_num_interop_threads(1)
    torch.manual_seed(0)
    started = time.perf_counter()
    sys.path.insert(0, str(UPSTREAM / "WavLM"))
    from WavLM import WavLM, WavLMConfig

    # Read the exact upstream initialization config; final embedding weights all
    # come from the finetuned SV checkpoint, including its backbone and ECAPA head.
    initialized = torch.load(BACKBONE, map_location="cpu", weights_only=False)
    cfg = initialized["cfg"]
    del initialized
    gc.collect()
    assert cfg["encoder_layers"] == 24 and cfg["encoder_embed_dim"] == 1024
    backbone = WavLM(WavLMConfig(cfg))
    backbone.feature_grad_mult = 0.0
    backbone.encoder.layerdrop = 0.0

    class OfficialHookFeatures(nn.Module):
        """Same hooks and waveform normalization as s3prl upstream/wavlm/expert.py."""
        def __init__(self, model):
            super().__init__()
            self.model = model
            self.states = []
            for layer in self.model.encoder.layers:
                layer.register_forward_hook(lambda module, inputs, output: self.states.append(inputs[0].transpose(0, 1)))
            self.model.encoder.register_forward_hook(lambda module, inputs, output: self.states.append(output[0]))

        def forward(self, wavs):
            assert len(wavs) == 1
            self.states.clear()
            waveform = wavs[0]
            if cfg["normalize"]:
                waveform = functional.layer_norm(waveform, waveform.shape)
            padding_mask = torch.zeros((1, waveform.numel()), dtype=torch.bool)
            self.model.extract_features(waveform.unsqueeze(0), padding_mask=padding_mask, mask=False)
            states = tuple(self.states)
            self.states.clear()
            assert len(states) == 25
            return {"hidden_states": states}

    # models.utils imports old fairseq eagerly, but only serves the unrelated
    # UniSpeech-SAT config path. Bypass that unused import, not any neural layer.
    shim = types.ModuleType("models.utils")
    def unused_fairseq_path(*args, **kwargs):
        raise RuntimeError("Unexpected fairseq config path in standalone WavLM experiment")
    shim.UpstreamExpert = unused_fairseq_path
    sys.modules["models.utils"] = shim
    sys.path.insert(0, str(UPSTREAM / "downstreams/speaker_verification"))
    from models.ecapa_tdnn import ECAPA_TDNN_SMALL

    # Instantiate the unchanged official ECAPA structure without its torch.hub
    # downloader, then attach the official WavLM model and identical hook wrapper.
    model = ECAPA_TDNN_SMALL(feat_dim=1024, emb_dim=256, feat_type="fbank")
    model.feature_extract = OfficialHookFeatures(backbone)
    model.feat_type = "wavlm_large"
    model.feat_num = 25
    model.feature_weight = nn.Parameter(torch.zeros(25))
    state = torch.load(CHECKPOINT, map_location="cpu", weights_only=True)
    state = state["model"]
    expected_keys = set(model.state_dict())
    missing = sorted(expected_keys - set(state))
    unexpected = sorted(set(state) - expected_keys)
    assert not missing, missing
    # The 5994-speaker training classifier is not part of embedding inference.
    # Its exact key was observed in run1.log; all embedding keys must still match.
    assert unexpected == ["loss_calculator.projection.weight"], unexpected
    training_projection_shape = list(state.pop(unexpected[0]).shape)
    model.load_state_dict(state, strict=True)
    del state
    gc.collect()
    model.cpu().float().eval()
    load_seconds = time.perf_counter() - started
    files = ["WavLM/WavLM.py", "WavLM/modules.py", "downstreams/speaker_verification/models/ecapa_tdnn.py"]
    provenance = {
        "model": "WavLM-Large finetuned speaker verification + official ECAPA-TDNN_SMALL head",
        "official_model_table": "https://github.com/microsoft/UniSpeech/tree/main/downstreams/speaker_verification",
        "official_checkpoint_url": "https://drive.google.com/file/d/1-aE1NfzpRCLxA4GUxX9ITI3F9LlbtEGP/view",
        "official_download_result": "Google Drive quota exceeded; frozen-Large OneDrive fallback returned HTTP 403",
        "checkpoint_url": "https://huggingface.co/Dongchao/UniAudio/resolve/ec5555f9bd5038a606e09bf3399dc3bc6798d62d/wavlm_large_finetune.pth",
        "checkpoint_source": "UniAudio author third-party mirror; not a Microsoft-hosted checkpoint",
        "identity_limitation": "Mirror bytes and SHA match independently published mirrors; bit-identical equivalence to the inaccessible Microsoft Drive asset cannot be proven.",
        "mirror_cross_checks": [
            "https://huggingface.co/lmzjms/wavlm-large/blob/f1c40859a86883dac660f314165a2ea45a8e4dab/wavlm_large_finetune.pth",
            "https://huggingface.co/bezzam/wavlm_large_finetune_seed_tts_eval",
        ],
        "checkpoint": str(CHECKPOINT.relative_to(ROOT)), "checkpoint_bytes": CHECKPOINT.stat().st_size,
        "checkpoint_sha256": sha(CHECKPOINT), "backbone_config_checkpoint": str(BACKBONE.relative_to(ROOT)),
        "backbone_config_url": "https://huggingface.co/s3prl/converted_ckpts/resolve/main/wavlm_large.pt",
        "backbone_config_checkpoint_bytes": BACKBONE.stat().st_size, "backbone_config_checkpoint_sha256": sha(BACKBONE),
        "backbone_configuration": cfg,
        "source_repository": "https://github.com/microsoft/UniSpeech",
        "source_commit": subprocess.check_output(["git", "-C", str(UPSTREAM), "rev-parse", "HEAD"], text=True).strip(),
        "source_sha256": {name: sha(UPSTREAM / name) for name in files},
        "feature_wrapper_reference": "https://github.com/s3prl/s3prl/blob/main/s3prl/upstream/wavlm/expert.py",
        "feature_wrapper_source_commit": subprocess.check_output(["git", "-C", str(S3PRL), "rev-parse", "HEAD"], text=True).strip(),
        "feature_wrapper_reference_sha256": sha(S3PRL / "s3prl/upstream/wavlm/expert.py"),
        "compatibility_adapter": "Official standalone WavLM + unchanged official ECAPA head. Same 25 s3prl hook features, learned feature_weight, waveform normalization, and instance normalization. Bypasses only unused eager fairseq import and torch.hub downloader.",
        "state_dict": {"strict": True, "keys": len(expected_keys), "missing": missing, "unexpected_after_training_key_exclusion": [],
                       "excluded_training_only": {unexpected[0]: training_projection_shape}},
        "prior_attempt": {"log": "run1.log", "failure": "Strict extra-key inspection identified the training-only loss_calculator.projection.weight; no embedding weights were missing."},
        "python": sys.version, "platform": platform.platform(), "device": "cpu", "dtype": "float32",
        "threads": torch.get_num_threads(), "interop_threads": torch.get_num_interop_threads(), "batch_size": 1,
        "versions": {name: importlib.metadata.version(name) for name in ("torch", "torchaudio", "numpy")},
        "parameter_count": sum(p.numel() for p in model.parameters()),
        "manifest_sha256": sha(manifest_path), "runner_sha256": sha(Path(__file__)),
        "import_seconds": import_seconds, "model_load_seconds": load_seconds,
    }
    write("provenance.json", provenance)
    records, warmup_ms = [], None
    with torch.inference_mode():
        for sample in manifest["samples"]:
            with wave.open(str(EXPERIMENT / sample["audio"]), "rb") as audio:
                assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (16000, 1, 2)
                frames = audio.getnframes()
                pcm = audio.readframes(frames)
            assert hashlib.sha256(pcm).hexdigest() == sample["pcm_sha256"]
            waveform = torch.from_numpy(np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768).unsqueeze(0)
            if warmup_ms is None:
                started = time.perf_counter()
                model(waveform)
                warmup_ms = (time.perf_counter() - started) * 1000
            started = time.perf_counter()
            vector = model(waveform).squeeze(0).numpy().astype(float).tolist()
            elapsed_ms = (time.perf_counter() - started) * 1000
            norm = math.hypot(*vector)
            assert len(vector) == 256 and all(math.isfinite(x) for x in vector) and norm > 0
            records.append({"id": sample["id"], "vector": [x / norm for x in vector], "raw_norm": norm,
                            "dimension": len(vector), "pcm_sha256": sample["pcm_sha256"],
                            "duration_ms": frames / 16, "embedding_ms": elapsed_ms})
            write("embeddings.json", records)
            print(json.dumps({"id": sample["id"], "embedding_ms": elapsed_ms}), flush=True)
    provenance.update({"sample_count": len(records), "warmup_ms": warmup_ms,
                       "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                       "completed_at": datetime.now(UTC).isoformat()})
    write("provenance.json", provenance)
    write("status.json", {"status": "complete", "sample_count": len(records)})


if __name__ == "__main__":
    try:
        main()
    except BaseException as error:
        write("status.json", {"status": "failed", "error_type": type(error).__name__, "message": str(error),
                              "traceback": traceback.format_exc()})
        raise
