"""Offline CPU pyannote 3.x diarization, with explicitly local model references."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import hashlib
from importlib.metadata import version
import json
import math
from pathlib import Path
from threading import Lock
import time
from typing import Any
import wave

import yaml

from gateway.speaker_diarization import SpeakerDiarizationResult, SpeakerDiarizationTurn
from gateway.usage import ModelCallTracker, track_model_call


class PyannoteSpeakerDiarizer:
    provider_name = "local"

    def __init__(
        self, model: str, *, pipeline_path: str | Path,
        request_timeout_seconds: float, cpu_threads: int = 6,
        max_audio_seconds: float = 300,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("speaker diarization model must not be empty")
        if request_timeout_seconds is None or not math.isfinite(request_timeout_seconds) or request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be finite and greater than zero")
        if not isinstance(cpu_threads, int) or isinstance(cpu_threads, bool) or not 1 <= cpu_threads <= 64:
            raise ValueError("cpu_threads must be an integer between 1 and 64")
        if not math.isfinite(max_audio_seconds) or not 0 < max_audio_seconds <= 3600:
            raise ValueError("max_audio_seconds must be finite and between zero and 3600")
        self.model_name = model.strip()
        self.model_revision = "unloaded"
        self._pipeline_path = Path(pipeline_path).expanduser()
        self._timeout = request_timeout_seconds
        self._cpu_threads = cpu_threads
        self._max_audio_seconds = max_audio_seconds
        self._pipeline: Callable[..., Any] | None = None
        self._lock = Lock()
        self._closed = False

    def diarize_audio(self, audio_path: str | Path) -> SpeakerDiarizationResult:
        if self._closed:
            raise RuntimeError("Speaker diarization model is closed")
        try:
            with wave.open(str(audio_path), "rb") as audio:
                if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, 16000, "NONE"):
                    raise ValueError("pyannote requires a 16 kHz mono PCM16 WAV")
                frames = audio.getnframes()
                if frames < 16 or frames > self._max_audio_seconds * 16000:
                    raise ValueError("audio duration must be at least 1 ms and not exceed max_audio_seconds")
                pcm = audio.readframes(frames)
        except (wave.Error, EOFError) as error:
            raise ValueError("invalid or truncated PCM WAV") from error
        if len(pcm) != frames * 2:
            raise ValueError("truncated PCM WAV")
        duration_ms = round(frames / 16)
        deadline = time.monotonic() + self._timeout
        with track_model_call(
            provider=self.provider_name, model=self.model_name,
            operation="speaker_diarization", usage_source=None,
        ) as call:
            acquired = False
            try:
                while not acquired:
                    self._check_call(call, deadline)
                    acquired = self._lock.acquire(timeout=min(0.05, max(0, deadline - time.monotonic())))
                self._check_call(call, deadline)
                if self._pipeline is None:
                    self._pipeline = self._load_pipeline()
                self._check_call(call, deadline)

                def hook(*args: Any, **kwargs: Any) -> None:
                    self._check_call(call, deadline)

                annotation = self._pipeline(pcm, hook=hook)
                # Hooks stop between batches; an active native torch forward cannot be interrupted.
                self._check_call(call, deadline)
                turns = []
                for segment, _track, speaker in annotation.itertracks(yield_label=True):
                    start, end = float(segment.start), float(segment.end)
                    if (not math.isfinite(start) or not math.isfinite(end)
                            or end <= start or end <= 0 or start >= frames / 16000):
                        raise ValueError("pyannote returned an invalid or out-of-range speaker turn")
                    if not isinstance(speaker, str) or not speaker.strip():
                        raise ValueError("pyannote returned an invalid anonymous speaker identifier")
                    start_ms = max(0, round(start * 1000))
                    end_ms = min(duration_ms, round(end * 1000))
                    if end_ms > start_ms:
                        turns.append(SpeakerDiarizationTurn(
                            start_ms=start_ms, end_ms=end_ms, speaker=speaker,
                        ))
                turns.sort(key=lambda turn: (turn.start_ms, turn.end_ms, turn.speaker))
                self._check_call(call, deadline)
                return SpeakerDiarizationResult(
                    model=self.model_name, model_revision=self.model_revision,
                    duration_ms=duration_ms, turns=turns,
                )
            finally:
                if acquired:
                    if self._closed:
                        self._pipeline = None
                    self._lock.release()

    def _check_call(self, call: ModelCallTracker, deadline: float) -> None:
        call.raise_if_cancelled()
        if self._closed:
            raise RuntimeError("Speaker diarization model is closed")
        if time.monotonic() >= deadline:
            raise TimeoutError("pyannote speaker diarization timed out")

    def _load_pipeline(self) -> Callable[..., Any]:
        path = self._pipeline_path.resolve()
        if path.is_dir():
            path = path / "config.yaml"
        if not path.is_file():
            raise FileNotFoundError("pyannote pipeline_path must name a local YAML file or directory containing config.yaml")
        config_bytes = path.read_bytes()
        config = yaml.safe_load(config_bytes)
        if not isinstance(config, dict) or set(config) - {"version", "pipeline", "params", "device"}:
            raise ValueError("pyannote requires a local 3.x pipeline config without preprocessors or external loaders")
        if not str(config.get("version", "")).startswith("3."):
            raise ValueError("pyannote adapter requires an explicit 3.x pipeline version")
        if config.get("device", "cpu") != "cpu":
            raise ValueError("pyannote adapter only supports CPU pipelines")
        definition = config.get("pipeline")
        if (not isinstance(definition, dict) or set(definition) - {"name", "params"}
                or definition.get("name") != "pyannote.audio.pipelines.SpeakerDiarization"):
            raise ValueError("pyannote pipeline must use pyannote.audio.pipelines.SpeakerDiarization")
        parameters = definition.get("params")
        allowed = {
            "segmentation", "embedding", "segmentation_step", "embedding_exclude_overlap",
            "clustering", "embedding_batch_size", "segmentation_batch_size",
        }
        if not isinstance(parameters, dict) or set(parameters) - allowed:
            raise ValueError("pyannote pipeline has unsupported constructor parameters")
        hyperparameters = config.get("params")
        if not isinstance(hyperparameters, dict) or not hyperparameters:
            raise ValueError("pyannote pipeline requires explicit frozen params")
        checkpoints = {}
        fingerprints = {}
        for name in ("segmentation", "embedding"):
            reference = parameters.get(name)
            if isinstance(reference, Mapping) and set(reference) == {"checkpoint"}:
                reference = reference["checkpoint"]
            if not isinstance(reference, str) or not reference.strip():
                raise ValueError(f"pyannote {name} must reference an explicit local checkpoint")
            checkpoint = Path(reference).expanduser()
            if not checkpoint.is_absolute():
                checkpoint = path.parent / checkpoint
            checkpoint = checkpoint.resolve()
            if not checkpoint.is_file():
                raise ValueError(f"pyannote {name} checkpoint is not a local file; automatic downloads are disabled")
            with checkpoint.open("rb") as stream:
                fingerprints[name] = hashlib.file_digest(stream, "sha256").hexdigest()
            checkpoints[name] = checkpoint
        try:
            import numpy as np
            import torch
            from pyannote.audio import Model
            from pyannote.audio.pipelines import SpeakerDiarization
        except ImportError as error:
            raise RuntimeError("pyannote requires the diarization optional dependencies") from error
        # Lightning 2.5.5 explicitly passes weights_only=False for trusted local checkpoints.
        # Newer Lightning defaults differ; do not alter torch.load or process-wide environment.
        runtime = {name: version(name) for name in (
            "pyannote.audio", "lightning", "pytorch-lightning", "torch", "torchaudio", "numpy",
        )}
        if runtime["pyannote.audio"] != "3.4.0" or any(
            runtime[name] != "2.5.5" for name in ("lightning", "pytorch-lightning")
        ):
            raise RuntimeError("pyannote adapter requires pyannote.audio 3.4.0 and lightning/pytorch-lightning 2.5.5")
        torch.set_num_threads(self._cpu_threads)
        local_models = {
            name: Model.from_pretrained(str(checkpoint), map_location="cpu", strict=True)
            for name, checkpoint in checkpoints.items()
        }
        if any(model is None for model in local_models.values()):
            raise RuntimeError("pyannote failed to load a local checkpoint")
        # Model objects avoid the SDK's name-based embedding-provider dispatch and downloads.
        pipeline = SpeakerDiarization(**{**parameters, **local_models}, use_auth_token=False)
        pipeline.instantiate(hyperparameters)
        pipeline.to(torch.device("cpu"))
        manifest = {
            "pipeline_sha256": hashlib.sha256(config_bytes).hexdigest(),
            "checkpoint_sha256": fingerprints, "runtime": runtime, "pcm": "s16le-16k-mono",
        }
        self.model_revision = "pyannote-diarization-sha256-" + hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

        def infer(pcm: bytes, *, hook: Callable[..., None]) -> Any:
            waveform = torch.from_numpy(np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768)[None]
            with torch.inference_mode():
                return pipeline({"waveform": waveform, "sample_rate": 16000}, hook=hook)

        return infer

    def close(self) -> None:
        self._closed = True
        if not self._lock.acquire(timeout=self._timeout):
            raise TimeoutError("pyannote inference has not stopped; model is closed to new calls")
        try:
            self._pipeline = None
        finally:
            self._lock.release()
