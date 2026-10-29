"""Lazy, local WhisperX transcription with mandatory forced alignment.

Weights and NLTK data are installed by the operator. No model downloads occur
here. One Gateway-owned runtime serializes calls and reuses loaded models.
"""
from __future__ import annotations

import importlib
from pathlib import Path
from threading import Event, Lock
from time import monotonic
import wave

from gateway.config import ModelBinding, WhisperXSourceConfig
from gateway.usage import track_model_call


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

    def transcribe(self, audio_path: Path, check) -> dict:
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
            if self._model is None:
                self._load()
            check()
            np = importlib.import_module("numpy")
            audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
            result = self._model.transcribe(audio, batch_size=self.source.batch_size)
            check()
            if not result["segments"]:
                return {"segments": [], "language": result.get("language")}
            language = result["language"]
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
            aligned = self._module.align(
                result["segments"], aligner, metadata, audio, self.source.device,
                interpolate_method="nearest", return_char_alignments=False,
            )
            check()
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
            return self._runtime.transcribe(Path(audio_path), check)

    def close(self) -> None:
        self._closed.set()
        if self._owns_runtime:
            self._runtime.close()
