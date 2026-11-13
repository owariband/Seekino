"""Loopback OpenAI audio endpoint with aligned, anonymous speaker turns."""
from copy import deepcopy
import logging
import os
from pathlib import Path
import tempfile
from threading import Lock
from time import monotonic

from fastapi import FastAPI, File, Form, UploadFile
import uvicorn

from gateway.config import ModelBinding, PyannoteSourceConfig, WhisperXSourceConfig
from gateway.local.pyannote import PyannoteDiarizationModel
from gateway.local.whisperx import WhisperXRuntime, split_speaker_segments


logger = logging.getLogger(__name__)
MODELS = Path(os.environ.get("SEEKINO_GPU_MODELS", "/root/seekino-gpu/models"))
runtime = WhisperXRuntime(str(MODELS / "faster-whisper-large-v3"), WhisperXSourceConfig(
    device="cuda", compute_type="float16", batch_size=16, cpu_threads=8,
    alignment_models={lang: str(MODELS / f"align-{lang}") for lang in ("en", "zh", "ja")},
))
diarizer = PyannoteDiarizationModel(ModelBinding(
    source="gpu_pyannote", model=str(MODELS / "community-1"),
    kind="diarization", request_timeout_seconds=1200,
), PyannoteSourceConfig(device="cuda", cpu_threads=8))
request_lock = Lock()
app = FastAPI()


class _Progress:
    def phase(self, name, **fields):
        logger.info("event=gpu_asr_phase phase=%s", name)

    def update(self, percent):
        pass


@app.get("/health")
def health():
    return {"status": "ok", "diarization_enabled": True}


@app.post("/v1/audio/transcriptions")
def transcriptions(file: UploadFile = File(...), model: str = Form(""),
                   response_format: str = Form("verbose_json"), diarize: bool = Form(True)):
    with request_lock, tempfile.NamedTemporaryFile(suffix=".wav") as audio:
        audio.write(file.file.read())
        audio.flush()
        started = monotonic()
        result = runtime.transcribe(Path(audio.name), lambda: None, _Progress())
        asr_seconds = monotonic() - started
        transcript_text = " ".join(segment.get("text", "").strip() for segment in result["segments"]).strip()
        grouping, diarization_seconds = None, 0.0
        if diarize and result["segments"]:
            started = monotonic()
            grouping = diarizer.diarize(audio.name)
            # Use exclusive turns to avoid majority labels from simultaneous voices.
            import pandas as pd
            from whisperx.diarize import assign_word_speakers
            assigned = assign_word_speakers(pd.DataFrame(
                grouping["exclusive_segments"], columns=["start", "end", "speaker"],
            ), deepcopy(result), fill_nearest=False)
            result = {**result, "segments": split_speaker_segments(assigned["segments"])}
            diarization_seconds = monotonic() - started
    segments = result["segments"]
    return {
        "task": "transcribe", "language": result.get("language"),
        "duration": segments[-1]["end"] if segments else 0.0,
        "text": transcript_text,
        "segments": segments, "speaker_scope": "input_clip", "diarization": grouping,
        "timings": {"asr_seconds": asr_seconds, "diarization_seconds": diarization_seconds},
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="127.0.0.1", port=18844, log_level="info")
