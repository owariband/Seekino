"""CPU CAM++ using the deployed official 3D-Speaker implementation."""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import math
from pathlib import Path
import sys
from threading import Lock
import time
import wave

from gateway.speaker_embedding import SpeakerEmbeddingResult
from gateway.usage import ModelCallTracker, track_model_call

CAMPPLUS_MODEL = "iic/speech_campplus_sv_zh-cn_16k-common"
CAMPPLUS_CHECKPOINT_REVISION = "v1.0.0"
CAMPPLUS_CHECKPOINT_SHA256 = "3388cf5fd3493c9ac9c69851d8e7a8badcfb4f3dc631020c4961371646d5ada8"
CAMPPLUS_IMPLEMENTATION_SHA256 = (
    ("speakerlab.models.campplus.DTDNN", "52272359361ef9f03a8b4198f50139301ce712833e777396940a5b437177cd34"),
    ("speakerlab.models.campplus.layers", "b3d28ad94d14b30fcd20c96d1d83516d4e44de733959460b358e90d97476adb8"),
    ("speakerlab.process.processor", "b97da9ffb97572ed1e0d112d9ed3a4013299a72fcd582c224e2db9279ff5a537"),
)
CAMPPLUS_IMPLEMENTATION_REVISION = hashlib.sha256(
    "".join(digest for _, digest in CAMPPLUS_IMPLEMENTATION_SHA256).encode("ascii")
).hexdigest()
CAMPPLUS_ENCODER_REVISION = (
    f"campplus-{CAMPPLUS_CHECKPOINT_REVISION}-sha256-{CAMPPLUS_CHECKPOINT_SHA256}"
    f"-source-{CAMPPLUS_IMPLEMENTATION_REVISION}"
    "-fbank80-16k-mean-dither0-l2-v1"
)
MIN_AUDIO_MS = 400


class CampPlusSpeakerEmbedder:
    provider_name = "local"
    encoder_revision = CAMPPLUS_ENCODER_REVISION
    dimensions = 192

    def __init__(
        self, model: str, *, checkpoint_path: str | Path,
        request_timeout_seconds: float, cpu_threads: int = 6,
        max_audio_seconds: float = 30,
    ) -> None:
        if model != CAMPPLUS_MODEL:
            raise ValueError(f"CAM++ only supports {CAMPPLUS_MODEL!r}")
        if request_timeout_seconds is None or not math.isfinite(request_timeout_seconds) or request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be finite and greater than zero")
        if not isinstance(cpu_threads, int) or isinstance(cpu_threads, bool) or cpu_threads < 1:
            raise ValueError("cpu_threads must be a positive integer")
        if not math.isfinite(max_audio_seconds) or max_audio_seconds < MIN_AUDIO_MS / 1000:
            raise ValueError(f"max_audio_seconds must be finite and at least {MIN_AUDIO_MS / 1000}")
        self.model_name = model
        self._checkpoint_path = Path(checkpoint_path)
        self._timeout = request_timeout_seconds
        self._cpu_threads = cpu_threads
        self._max_audio_seconds = max_audio_seconds
        self._lock = Lock()
        self._closed = False
        self._encode: Callable[[bytes], list[float]] | None = None

    def embed_audio(self, audio_path: str | Path) -> SpeakerEmbeddingResult:
        if self._closed:
            raise RuntimeError("Speaker embedding model is closed")
        # Bound allocation from the WAV header before reading the payload.
        try:
            with wave.open(str(audio_path), "rb") as audio:
                if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, 16000, "NONE"):
                    raise ValueError("CAM++ requires a 16 kHz mono PCM16 WAV")
                frames = audio.getnframes()
                if not MIN_AUDIO_MS * 16 <= frames <= self._max_audio_seconds * 16000:
                    raise ValueError(f"audio duration must be between {MIN_AUDIO_MS} ms and {self._max_audio_seconds} seconds")
                pcm = audio.readframes(frames)
        except (wave.Error, EOFError) as error:
            raise ValueError("invalid or truncated PCM WAV") from error
        if len(pcm) != frames * 2:
            raise ValueError("truncated PCM WAV")
        if pcm == pcm[:2] * frames:
            raise ValueError("audio must not be silent or constant")
        deadline = time.monotonic() + self._timeout
        with track_model_call(
            provider=self.provider_name, model=self.model_name,
            operation="speaker_embedding", usage_source=None,
        ) as call:
            acquired = False
            try:
                while not acquired:
                    self._check_call(call, deadline)
                    acquired = self._lock.acquire(timeout=min(0.05, max(0, deadline - time.monotonic())))
                self._check_call(call, deadline)
                if self._encode is None:
                    self._encode = self._load_encoder()
                self._check_call(call, deadline)
                vector = self._encode(pcm)
                # Native torch work cannot be interrupted; reject cancelled/late output.
                self._check_call(call, deadline)
                if len(vector) != self.dimensions or not all(math.isfinite(value) for value in vector):
                    raise RuntimeError("CAM++ returned an invalid embedding")
                norm = math.hypot(*vector)
                if not math.isfinite(norm) or norm <= 0:
                    raise RuntimeError("CAM++ returned a zero or invalid embedding norm")
                return SpeakerEmbeddingResult(
                    model=self.model_name, encoder_revision=self.encoder_revision,
                    vector=[value / norm for value in vector],
                    duration_ms=round(frames / 16),
                )
            finally:
                if acquired:
                    if self._closed:
                        self._encode = None
                    self._lock.release()

    def _check_call(self, call: ModelCallTracker, deadline: float) -> None:
        call.raise_if_cancelled()
        if self._closed:
            raise RuntimeError("Speaker embedding model is closed")
        if time.monotonic() >= deadline:
            raise TimeoutError("CAM++ speaker embedding timed out")

    def _load_encoder(self) -> Callable[[bytes], list[float]]:
        with self._checkpoint_path.open("rb") as checkpoint:
            if hashlib.file_digest(checkpoint, "sha256").hexdigest() != CAMPPLUS_CHECKPOINT_SHA256:
                raise ValueError("CAM++ checkpoint SHA-256 does not match v1.0.0")
            try:
                import numpy as np
                import torch
                from speakerlab.models.campplus.DTDNN import CAMPPlus
                from speakerlab.process.processor import FBank
            except ImportError as error:
                raise RuntimeError("CAM++ requires the speaker optional dependencies and deployed official 3D-Speaker on PYTHONPATH") from error
            for module_name, expected_sha256 in CAMPPLUS_IMPLEMENTATION_SHA256:
                with Path(sys.modules[module_name].__file__).open("rb") as source:
                    if hashlib.file_digest(source, "sha256").hexdigest() != expected_sha256:
                        raise ValueError(f"CAM++ implementation SHA-256 mismatch: {module_name}")
            # PyTorch's intra-op thread count is process-wide; deployments must use one value.
            torch.set_num_threads(self._cpu_threads)
            checkpoint.seek(0)
            model = CAMPPlus(feat_dim=80, embedding_size=self.dimensions).cpu().float().eval()
            model.load_state_dict(torch.load(checkpoint, map_location="cpu", weights_only=True), strict=True)
        fbank = FBank(80, sample_rate=16000, mean_nor=True)

        def encode(pcm: bytes) -> list[float]:
            samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
            with torch.inference_mode():
                features = fbank(torch.from_numpy(samples).unsqueeze(0), dither=0).unsqueeze(0)
                return model(features).squeeze(0).numpy().tolist()

        return encode

    def close(self) -> None:
        self._closed = True
        if not self._lock.acquire(timeout=self._timeout):
            raise TimeoutError("CAM++ inference has not stopped; model is closed to new calls")
        try:
            self._encode = None
        finally:
            self._lock.release()
