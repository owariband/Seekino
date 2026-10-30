"""Lazy, local WhisperX transcription with mandatory forced alignment.

Weights and NLTK data are installed by the operator. No model downloads occur
here. One Gateway-owned runtime serializes calls and reuses loaded models.
"""
from __future__ import annotations

import importlib
import json
import logging
import math
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic
import wave

from gateway.config import ModelBinding, WhisperXSourceConfig
from gateway.usage import track_model_call


_logger = logging.getLogger(__name__)
_PROGRESS_INTERVAL_SECONDS = 30.0


class _WhisperXProgress:
    """Per-call diagnostics; percentages describe upstream segments in one phase."""

    def __init__(self, call, source):
        self._fields = {
            "call_id": call.call_id, "context": dict(call.context),
            "model": Path(call.model).name, "device": source.device,
            "compute_type": source.compute_type, "batch_size": source.batch_size,
            "cpu_threads": source.cpu_threads,
        }
        self._lock, self._stopped = Lock(), Event()
        self._started = self._phase_started = monotonic()
        self._phase = "waiting_for_runtime"
        self._percent = self._progress_at = None
        self._logged_percent = None
        self._thread = Thread(target=self._heartbeat, daemon=True,
                              name=f"whisperx-progress-{call.call_id[:8]}")

    def __enter__(self):
        with self._lock:
            self._emit("started")
        self._thread.start()
        return self

    def __exit__(self, error_type, error, traceback):
        self._stopped.set()
        self._thread.join()
        with self._lock:
            self._emit("failed" if error_type else "completed",
                       error_type=error_type.__name__ if error_type else None)

    def phase(self, name, **fields):
        with self._lock:
            self._emit("phase_completed")
            self._phase, self._phase_started = name, monotonic()
            self._percent = self._progress_at = self._logged_percent = None
            self._fields.update(fields)
            self._emit("phase_started")

    def update(self, percent):
        if not isinstance(percent, (int, float)) or not math.isfinite(percent):
            return
        with self._lock:
            value = max(0.0, min(100.0, float(percent)))
            if self._percent is not None and value <= self._percent:
                return
            self._percent, self._progress_at = value, monotonic()
            if (self._logged_percent is None or value >= 100
                    or value - self._logged_percent >= 5):
                self._emit("progress")

    def _heartbeat(self):
        while not self._stopped.wait(_PROGRESS_INTERVAL_SECONDS):
            with self._lock:
                if not self._stopped.is_set():
                    self._emit("heartbeat")

    def _emit(self, status, **fields):
        now = monotonic()
        summary = {
            **self._fields, "phase": self._phase, "status": status,
            "elapsed_seconds": round(now - self._started, 3),
            "phase_elapsed_seconds": round(now - self._phase_started, 3),
            "phase_percent": round(self._percent, 2) if self._percent is not None else None,
            "progress_basis": "upstream_segments" if self._percent is not None else None,
            "progress_age_seconds": (round(now - self._progress_at, 3)
                                     if self._progress_at is not None else None),
            **fields,
        }
        self._logged_percent = self._percent
        _logger.info("event=local_asr_progress summary=%s",
                     json.dumps(summary, ensure_ascii=False, default=str))


class WhisperXRuntime:
    def __init__(self, model: str, source: WhisperXSourceConfig) -> None:
        self.model_name = model
        self.source = source
        self._lock = Lock()
        self._closed = Event()
        self._module = None
        self._model = None
        self._aligners: dict[str, tuple] = {}

    def _load(self) -> None:
        model_path = Path(self.model_name).expanduser().resolve()
        if not model_path.is_dir():
            raise FileNotFoundError(
                f"WhisperX requires a local CTranslate2 model directory: {model_path}; "
                "whisper.cpp GGML weights are not compatible"
            )
        # faster-whisper otherwise fetches a fallback tokenizer even with local_files_only.
        if not (model_path / "tokenizer.json").is_file():
            raise FileNotFoundError(f"WhisperX local model requires tokenizer.json: {model_path}")
        try:
            module = importlib.import_module("whisperx")
            nltk = importlib.import_module("nltk")
        except ImportError as error:
            raise RuntimeError("Local WhisperX requires: uv sync --extra whisperx") from error
        try:
            nltk.data.find("tokenizers/punkt_tab")
        except LookupError as error:
            raise RuntimeError("Install WhisperX sentence data first: python -m nltk.downloader punkt_tab") from error
        # pyannote VAD uses the checkpoint bundled in the WhisperX package.
        self._model = module.load_model(
            str(model_path), self.source.device,
            compute_type=self.source.compute_type, language=self.source.language,
            local_files_only=True, threads=self.source.cpu_threads, vad_method="pyannote",
        )
        self._module = module

    def transcribe(self, audio_path: Path, check, progress) -> dict:
        acquired = False
        try:
            while not acquired:
                check()
                if self._closed.is_set():
                    raise RuntimeError("WhisperX runtime is closed")
                acquired = self._lock.acquire(timeout=0.05)
            check()
            if self._closed.is_set():
                raise RuntimeError("WhisperX runtime is closed")
            progress.phase("read_audio")
            # Media preparation belongs to Server; do not decode video again here.
            with wave.open(str(audio_path), "rb") as audio_file:
                if (audio_file.getnchannels(), audio_file.getsampwidth(), audio_file.getframerate(), audio_file.getcomptype()) != (1, 2, 16000, "NONE"):
                    raise ValueError("WhisperX requires a 16 kHz mono PCM16 WAV")
                frames = audio_file.getnframes()
                if frames <= 0:
                    raise ValueError("WhisperX audio must not be empty")
                pcm = audio_file.readframes(frames)
                if len(pcm) != frames * 2:
                    raise ValueError("WhisperX audio is truncated")
            progress.phase("load_model", audio_duration_seconds=frames / 16000,
                           model_cached=self._model is not None)
            if self._model is None:
                self._load()
            check()
            np = importlib.import_module("numpy")
            audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
            # The upstream call includes VAD before its first segment callback.
            progress.phase("transcribe")
            result = self._model.transcribe(
                audio, batch_size=self.source.batch_size, progress_callback=progress.update,
            )
            check()
            if not result["segments"]:
                return {"segments": [], "language": result.get("language")}
            language = result["language"]
            progress.phase("load_aligner", language=language,
                           segment_count=len(result["segments"]),
                           aligner_cached=language in self._aligners)
            align_path = self.source.alignment_models.get(language)
            if align_path is None:
                raise ValueError(f"WhisperX has no configured local alignment model for {language!r}")
            # The pinned aligner downloads missing Punkt data. Preflight the exact
            # resource first, including partially installed language data.
            nltk = importlib.import_module("nltk")
            punkt_languages = importlib.import_module("whisperx.utils").PUNKT_LANGUAGES
            punkt_language = punkt_languages.get(language, "english")
            try:
                nltk.data.load(f"tokenizers/punkt_tab/{punkt_language}.pickle")
            except (LookupError, FileNotFoundError) as error:
                raise RuntimeError("Install WhisperX sentence data first: python -m nltk.downloader punkt_tab") from error
            if language not in self._aligners:
                path = Path(align_path).expanduser().resolve()
                if not path.is_dir():
                    raise FileNotFoundError(f"WhisperX alignment model directory is missing: {path}")
                self._aligners[language] = self._module.load_align_model(
                    language_code=language, device=self.source.device,
                    model_name=str(path), model_cache_only=True,
                )
            check()
            aligner, metadata = self._aligners[language]
            # Keep upstream sentence fallback so unalignable text is not dropped.
            # Only scored words are acoustically aligned; remove interpolated times.
            progress.phase("align")
            aligned = self._module.align(
                result["segments"], aligner, metadata, audio, self.source.device,
                interpolate_method="nearest", return_char_alignments=False,
                progress_callback=progress.update,
            )
            check()
            progress.phase("normalize")
            segments = []
            for segment in aligned["segments"]:
                words = []
                for original in segment.get("words", []):
                    word = dict(original)
                    if word.get("score") is None:
                        word.pop("start", None)
                        word.pop("end", None)
                    words.append(word)
                segments.append({**segment, "words": words})
            return {"segments": segments, "language": language}
        finally:
            if acquired:
                if self._closed.is_set():
                    self._clear()
                self._lock.release()

    def _clear(self) -> None:
        self._model = self._module = None
        self._aligners.clear()

    def close(self) -> None:
        self._closed.set()
        if self._lock.acquire(blocking=False):
            try:
                self._clear()
            finally:
                self._lock.release()


class WhisperXTranscriptionModel:
    provider_name = "local"
    input_mode = "audio"

    def __init__(self, binding: ModelBinding, source: WhisperXSourceConfig, *, runtime=None) -> None:
        if binding.request_timeout_seconds is None:
            raise ValueError("WhisperX requires a configured request timeout")
        self.model_name = binding.model
        self._timeout = binding.request_timeout_seconds
        self._runtime = runtime or WhisperXRuntime(binding.model, source)
        self._owns_runtime = runtime is None
        self._closed = Event()

    def transcribe(self, audio_path: str | Path) -> dict:
        deadline = monotonic() + self._timeout
        with track_model_call(provider=self.provider_name, model=self.model_name,
                              operation="candidate_transcript") as call:
            def check():
                call.raise_if_cancelled()
                if self._closed.is_set() or self._runtime._closed.is_set():
                    raise RuntimeError("WhisperX transcription model is closed")
                if monotonic() >= deadline:
                    raise TimeoutError("WhisperX transcription/alignment timed out")
            check()
            # Native inference cannot be preempted; late/cancelled output is rejected.
            with _WhisperXProgress(call, self._runtime.source) as progress:
                return self._runtime.transcribe(Path(audio_path), check, progress)

    def close(self) -> None:
        self._closed.set()
        if self._owns_runtime:
            self._runtime.close()
