"""Offline pyannote diarization; source/task identity remains with the caller."""
from __future__ import annotations

import gc
import importlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from threading import Event, Lock
from time import monotonic, time
import wave

from gateway.config import ModelBinding, PyannoteSourceConfig
from gateway.usage import track_model_call


logger = logging.getLogger(__name__)


class PyannoteDiarizationModel:
    provider_name = "local"

    def __init__(self, binding: ModelBinding, source: PyannoteSourceConfig, *, device_lock=None):
        if binding.request_timeout_seconds is None:
            raise ValueError("pyannote requires a configured request timeout")
        self.model_name = binding.model
        self._source = source
        self._timeout = binding.request_timeout_seconds
        self._closed = Event()
        self._lock = device_lock or Lock()

    def diarize(self, audio_path: str | Path) -> dict:
        started, wall_started = monotonic(), time()
        acquired = False
        pipeline = output = torch = sleep_guard = None
        phase, phase_started, last_log = "waiting_for_runtime", started, -float("inf")
        peak_tensor = peak_driver = 0
        with track_model_call(provider="local", model=self.model_name, operation="audio_diarization") as call:
            def check():
                call.raise_if_cancelled()
                if self._closed.is_set():
                    raise RuntimeError("pyannote model is closed")
                if monotonic() - started >= self._timeout:
                    raise TimeoutError("pyannote diarization timed out")

            def progress(name, *, completed=None, total=None, force=False, **fields):
                nonlocal phase, phase_started, last_log, peak_tensor, peak_driver
                now = monotonic()
                if name != phase:
                    phase, phase_started = name, now
                    force = True
                if torch is not None and self._source.device == "mps" and torch.backends.mps.is_available():
                    peak_tensor = max(peak_tensor, torch.mps.current_allocated_memory())
                    peak_driver = max(peak_driver, torch.mps.driver_allocated_memory())
                complete = completed is not None and total is not None and completed == total
                if not force and now - last_log < 15 and not complete:
                    return
                # Monotonic excludes macOS sleep; retain calendar elapsed separately.
                logger.info("event=local_diarization_progress summary=%s", json.dumps({
                    "call_id": call.call_id, "context": dict(call.context),
                    "model": Path(self.model_name).name, "device": self._source.device,
                    "phase": phase, "elapsed_seconds": round(now-started, 3),
                    "wall_elapsed_seconds": round(time()-wall_started, 3),
                    "phase_elapsed_seconds": round(now-phase_started, 3),
                    "completed": int(completed) if completed is not None else None,
                    "total": int(total) if total is not None else None,
                    "peak_mps_tensor_bytes": peak_tensor, "peak_mps_driver_bytes": peak_driver,
                    **fields,
                }, ensure_ascii=False))
                last_log = now

            try:
                while not acquired:
                    check()
                    progress("waiting_for_runtime", force=monotonic()-last_log >= 15)
                    acquired = self._lock.acquire(timeout=0.05)
                check()
                if sys.platform == "darwin":
                    # Scoped to this inference; no persistent system power setting changes.
                    sleep_guard = subprocess.Popen(
                        ["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                root = Path(self.model_name).expanduser().resolve(strict=True)
                for relative in ("config.yaml", "segmentation/pytorch_model.bin",
                                 "embedding/pytorch_model.bin", "plda/plda.npz", "plda/xvec_transform.npz"):
                    if not (root / relative).is_file():
                        raise FileNotFoundError(f"pyannote community-1 local weight is missing: {relative}")
                config = importlib.import_module("yaml").safe_load((root / "config.yaml").read_text())
                params = config["pipeline"]["params"]
                if any(params[key] != "$model/" + key for key in ("segmentation", "embedding", "plda")):
                    raise ValueError("pyannote weights must resolve inside the local model directory")
                if self._source.device == "mps" and os.environ.get("PYTORCH_ENABLE_MPS_FALLBACK", "0") != "0":
                    raise ValueError("unset PYTORCH_ENABLE_MPS_FALLBACK: silent CPU fallback is not allowed")
                progress("read_audio", force=True)
                with wave.open(str(audio_path), "rb") as handle:
                    if (handle.getnchannels(), handle.getsampwidth(), handle.getframerate(), handle.getcomptype()) != (1, 2, 16000, "NONE"):
                        raise ValueError("pyannote requires a 16 kHz mono PCM16 WAV")
                    frames = handle.getnframes()
                    pcm = handle.readframes(frames)
                    if frames <= 0 or len(pcm) != frames * 2:
                        raise ValueError("pyannote audio is empty or truncated")
                progress("load_model", force=True, audio_duration_seconds=frames/16000)
                torch = importlib.import_module("torch")
                np = importlib.import_module("numpy")
                if self._source.device == "mps" and not torch.backends.mps.is_available():
                    raise RuntimeError("MPS is unavailable; refusing CPU fallback")
                torch.set_num_threads(self._source.cpu_threads)
                module = importlib.import_module("pyannote.audio")
                importlib.import_module("pyannote.audio.telemetry").set_telemetry_metrics(False)
                pipeline = module.Pipeline.from_pretrained(str(root))
                pipeline.to(torch.device(self._source.device))
                pipeline.segmentation_batch_size = self._source.segmentation_batch_size
                pipeline.embedding_batch_size = self._source.embedding_batch_size
                device = str(next(pipeline._segmentation.model.parameters()).device)
                embedding_device = str(pipeline._embedding.device)
                if not device.startswith(self._source.device) or not embedding_device.startswith(self._source.device):
                    raise RuntimeError("pyannote did not load both neural models on the requested device")
                progress("model_loaded", force=True, segmentation_device=device,
                         embedding_device=embedding_device, cpu_threads=self._source.cpu_threads,
                         segmentation_batch_size=pipeline.segmentation_batch_size,
                         embedding_batch_size=pipeline.embedding_batch_size)
                waveform = torch.from_numpy(np.frombuffer(pcm, dtype="<i2").astype(np.float32)/32768).unsqueeze(0)
                del pcm

                def hook(step_name, step_artifact, file=None, total=None, completed=None):
                    check()
                    progress(step_name, completed=completed, total=total)

                check()
                output = pipeline({"waveform": waveform, "sample_rate": 16000}, hook=hook)
                check()
                if output.speaker_embeddings is not None and not np.isfinite(output.speaker_embeddings).all():
                    raise ValueError("pyannote returned non-finite speaker embeddings")

                def segments(annotation):
                    return [{"start": float(span.start), "end": float(span.end), "speaker": str(label)}
                            for span, _, label in annotation.itertracks(yield_label=True)]

                result = {"segments": segments(output.speaker_diarization),
                          "exclusive_segments": segments(output.exclusive_speaker_diarization)}
                progress("completed", force=True, speaker_count=len(output.speaker_diarization.labels()))
                return result
            except BaseException as error:
                progress("failed", force=True, error_type=type(error).__name__)
                raise
            finally:
                # Re-loading costs <1 s in the measured run; release GPU memory for later stages.
                pipeline = output = None
                try:
                    if acquired:
                        try:
                            gc.collect()
                            if torch is not None and self._source.device == "mps" and torch.backends.mps.is_available():
                                torch.mps.empty_cache()
                        finally:
                            self._lock.release()
                finally:
                    if sleep_guard is not None:
                        sleep_guard.terminate()
                        try:
                            sleep_guard.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            sleep_guard.kill()
                            sleep_guard.wait()

    def close(self):
        self._closed.set()
